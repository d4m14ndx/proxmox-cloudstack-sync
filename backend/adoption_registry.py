"""Durable source-free claims for existing Proxmox QEMU guests.

The registry is deliberately independent of CloudStack's schema.  A unique
(cluster, VMID) row and an atomic compare-and-set bind provide the identity
primitive missing from CloudStack 4.22 External deployments.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from database import AdoptionClaim, AdoptionExecution, AdoptionOperationLease
from sqlalchemy import exists, func, literal_column, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

MANAGED_OPERATION_ACTIONS = frozenset(
    {
        "console",
        "start",
        "stop",
        "reboot",
        "create_snapshot",
        "restore_snapshot",
        "delete_snapshot",
    }
)
MANAGED_OPERATION_LEASE_DURATION = timedelta(hours=2)


class ClaimConflict(Exception):
    """The requested identity is already claimed or bound differently."""


class ClaimInvalid(Exception):
    """The claim request is malformed or does not match its frozen manifest."""


class ClaimNotFound(Exception):
    """The requested claim does not exist."""


@dataclass(frozen=True)
class Reservation:
    claim: AdoptionClaim


@dataclass(frozen=True)
class ManagedOperationLease:
    id: str
    action: str
    expires_at: datetime


def _normalized_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ClaimInvalid(f"{field} must be a nonempty normalized string")
    return value


def _database_wall_clock(session):
    if session.get_bind().dialect.name in {"mysql", "mariadb"}:
        # NOW() is transaction-start time on MariaDB/MySQL. SYSDATE(6)
        # is evaluated when the statement executes, including after lock waits.
        return literal_column("SYSDATE(6)")
    return func.current_timestamp()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validated_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ClaimInvalid("claim generation must be a positive integer")
    return value


def _canonical_manifest(manifest_json: str) -> tuple[str, dict]:
    try:
        parsed = json.loads(manifest_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ClaimInvalid("manifest_json is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ClaimInvalid("manifest_json must contain an object")
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return canonical, parsed


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_RETRYABLE_MYSQL_OPERATIONAL_CODES = {1020, 1205, 1213}


def _is_retryable_operational_error(exc: OperationalError) -> bool:
    """Recognize MariaDB/MySQL current-read, lock-timeout and deadlock races."""

    args = getattr(getattr(exc, "orig", None), "args", None)
    if not args:
        return False
    try:
        return int(args[0]) in _RETRYABLE_MYSQL_OPERATIONAL_CODES
    except (IndexError, TypeError, ValueError):
        return False


def reserve_claim(
    session,
    *,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_json: str,
    manifest_sha256: str,
    write_guard: Callable[[], None],
) -> Reservation:
    """Reserve one globally unique cluster-local Proxmox VMID.

    Claim identity and generation are non-secret. Known MariaDB current-read
    and deadlock outcomes are retried or normalized to a controlled conflict;
    unrelated database failures are never hidden.
    """

    cluster = _normalized_text(proxmox_cluster, "proxmox_cluster")
    node = _normalized_text(proxmox_node, "proxmox_node")
    if isinstance(proxmox_vmid, bool) or not isinstance(proxmox_vmid, int):
        raise ClaimInvalid("proxmox_vmid must be a positive integer")
    if proxmox_vmid <= 0:
        raise ClaimInvalid("proxmox_vmid must be a positive integer")
    if (
        not isinstance(manifest_sha256, str)
        or len(manifest_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in manifest_sha256)
    ):
        raise ClaimInvalid("manifest_sha256 must be lowercase hexadecimal")

    canonical, manifest = _canonical_manifest(manifest_json)
    if _sha256(canonical) != manifest_sha256:
        raise ClaimInvalid("manifest hash does not match canonical manifest")
    placement = manifest.get("placement") or {}
    if (
        placement.get("cluster") != cluster
        or placement.get("node") != node
        or manifest.get("vmid") != proxmox_vmid
    ):
        raise ClaimInvalid("manifest identity does not match reservation identity")

    write_guard()
    claim = AdoptionClaim(
        id=str(uuid.uuid4()),
        proxmox_cluster=cluster,
        proxmox_node=node,
        proxmox_vmid=proxmox_vmid,
        manifest_sha256=manifest_sha256,
        manifest_json=canonical,
        state="reserved",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(claim)
    try:
        session.commit()
        session.refresh(claim)
        return Reservation(claim=claim)
    except (IntegrityError, OperationalError) as exc:
        if isinstance(exc, OperationalError) and not _is_retryable_operational_error(
            exc
        ):
            session.rollback()
            raise
        session.rollback()

    def load_existing() -> AdoptionClaim | None:
        session.expire_all()
        return (
            session.query(AdoptionClaim)
            .filter_by(proxmox_cluster=cluster, proxmox_vmid=proxmox_vmid)
            .execution_options(populate_existing=True)
            .first()
        )

    def is_same_reservation(candidate: AdoptionClaim | None) -> bool:
        return bool(
            candidate is not None
            and candidate.state == "reserved"
            and candidate.proxmox_node == node
            and candidate.manifest_sha256 == manifest_sha256
            and candidate.manifest_json == canonical
        )

    existing = load_existing()
    if is_same_reservation(existing):
        return Reservation(claim=existing)
    if existing is None or existing.state != "released":
        raise ClaimConflict("Proxmox cluster/VMID is already claimed")

    for _attempt in range(3):
        released_generation = existing.generation
        try:
            write_guard()
            result = session.execute(
                update(AdoptionClaim)
                .where(
                    AdoptionClaim.id == existing.id,
                    AdoptionClaim.state == "released",
                    AdoptionClaim.generation == released_generation,
                )
                .values(
                    proxmox_node=node,
                    manifest_sha256=manifest_sha256,
                    manifest_json=canonical,
                    generation=released_generation + 1,
                    state="reserved",
                    cloudstack_vm_ref=None,
                    cloudstack_instance_name=None,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if result.rowcount == 1:
                session.commit()
                current = load_existing()
                if is_same_reservation(current):
                    return Reservation(claim=current)
                raise ClaimConflict(
                    "Released Proxmox claim changed during reservation"
                )
            session.rollback()
        except OperationalError as exc:
            session.rollback()
            if not _is_retryable_operational_error(exc):
                raise

        existing = load_existing()
        if is_same_reservation(existing):
            return Reservation(claim=existing)
        if existing is None or existing.state != "released":
            raise ClaimConflict("Released Proxmox claim was reserved concurrently")

    raise ClaimConflict("Released Proxmox claim reservation retry limit reached")


def bind_claim(
    session,
    *,
    claim_id: str,
    generation: int,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_sha256: str,
    cloudstack_vm_ref: str,
    cloudstack_instance_name: str,
    write_guard: Callable[[], None],
) -> AdoptionClaim:
    """Atomically bind a reservation to one CloudStack VM.

    A retry for the exact same VM is idempotent.  A second VM loses the CAS and
    receives a conflict, even when calls race across management servers.
    """

    try:
        claim_uuid = str(uuid.UUID(claim_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("claim_id must be a UUID") from exc
    requested_generation = _validated_generation(generation)
    cluster = _normalized_text(proxmox_cluster, "proxmox_cluster")
    node = _normalized_text(proxmox_node, "proxmox_node")
    vm_ref = _normalized_text(cloudstack_vm_ref, "cloudstack_vm_ref")
    instance_name = _normalized_text(
        cloudstack_instance_name, "cloudstack_instance_name"
    )

    claim = session.query(AdoptionClaim).filter_by(id=claim_uuid).first()
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    if claim.generation != requested_generation:
        raise ClaimInvalid("claim generation is stale")
    if (
        claim.proxmox_cluster != cluster
        or claim.proxmox_node != node
        or claim.proxmox_vmid != proxmox_vmid
        or claim.manifest_sha256 != manifest_sha256
    ):
        raise ClaimInvalid("claim identity or manifest does not match")

    if claim.state == "bound":
        if (
            claim.cloudstack_vm_ref == vm_ref
            and claim.cloudstack_instance_name == instance_name
        ):
            return claim
        raise ClaimConflict("claim is already bound to another CloudStack VM")
    if claim.state != "reserved":
        raise ClaimConflict("claim is not bindable")

    bind_generation = requested_generation
    for _attempt in range(3):
        try:
            write_guard()
            result = session.execute(
                update(AdoptionClaim)
                .where(
                    AdoptionClaim.id == claim_uuid,
                    AdoptionClaim.state == "reserved",
                    AdoptionClaim.generation == bind_generation,
                    AdoptionClaim.manifest_sha256 == manifest_sha256,
                    AdoptionClaim.cloudstack_vm_ref.is_(None),
                )
                .values(
                    state="bound",
                    cloudstack_vm_ref=vm_ref,
                    cloudstack_instance_name=instance_name,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if result.rowcount == 1:
                session.commit()
            else:
                session.rollback()
        except IntegrityError as exc:
            session.rollback()
            raise ClaimConflict("CloudStack VM reference is already bound") from exc
        except OperationalError as exc:
            session.rollback()
            if not _is_retryable_operational_error(exc):
                raise

        session.expire_all()
        current = (
            session.query(AdoptionClaim)
            .filter_by(id=claim_uuid)
            .execution_options(populate_existing=True)
            .first()
        )
        if current and (
            current.state == "bound"
            and current.generation == bind_generation
            and current.manifest_sha256 == manifest_sha256
            and current.cloudstack_vm_ref == vm_ref
            and current.cloudstack_instance_name == instance_name
        ):
            return current
        if current and (
            current.state == "reserved"
            and current.generation == bind_generation
            and current.manifest_sha256 == manifest_sha256
        ):
            continue
        raise ClaimConflict("claim was bound concurrently to another CloudStack VM")

    raise ClaimConflict("claim bind retry limit reached")


def activate_bound_claim(
    session,
    *,
    claim_id: str,
    generation: int,
    cloudstack_vm_ref: str,
    execution_id: str,
    worker_lease_id: str,
    write_guard: Callable[[], None],
) -> AdoptionClaim:
    """Promote an exact bound claim after CloudStack deployment is verified.

    ``managed`` is the lifecycle authority boundary. The adoption extension
    remains non-mutating while a claim is only reserved or bound.
    """

    try:
        claim_uuid = str(uuid.UUID(claim_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("claim_id must be a UUID") from exc
    requested_generation = _validated_generation(generation)
    vm_ref = _normalized_text(cloudstack_vm_ref, "cloudstack_vm_ref")
    execution_uuid = _normalized_text(execution_id, "execution_id")
    try:
        lease_uuid = str(uuid.UUID(worker_lease_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("worker_lease_id must be a UUID") from exc
    if vm_ref != execution_uuid:
        raise ClaimInvalid("activation execution does not match CloudStack VM reference")

    claim = session.query(AdoptionClaim).filter_by(id=claim_uuid).first()
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    if claim.generation != requested_generation:
        raise ClaimInvalid("claim generation is stale")
    if claim.cloudstack_vm_ref != vm_ref:
        raise ClaimInvalid("CloudStack VM reference does not match bound claim")
    if claim.state == "managed":
        return claim
    if claim.state != "bound":
        raise ClaimConflict("only a bound claim may become managed")

    for _attempt in range(3):
        try:
            write_guard()
            leased_execution = (
                session.query(AdoptionExecution)
                .filter(
                    AdoptionExecution.id == execution_uuid,
                    AdoptionExecution.claim_id == claim_uuid,
                    AdoptionExecution.generation == requested_generation,
                    AdoptionExecution.state == "verifying",
                    AdoptionExecution.worker_lease_id == lease_uuid,
                )
                .with_for_update()
                .one_or_none()
            )
            if leased_execution is None:
                session.rollback()
                raise ClaimConflict("activation execution lease is not current")
            database_now = session.execute(
                select(_database_wall_clock(session))
            ).scalar_one()
            if (
                leased_execution.worker_lease_expires_at is None
                or _as_utc(leased_execution.worker_lease_expires_at)
                <= _as_utc(database_now)
            ):
                session.rollback()
                raise ClaimConflict("activation execution lease has expired")
            write_guard()
            result = session.execute(
                update(AdoptionClaim)
                .where(
                    AdoptionClaim.id == claim_uuid,
                    AdoptionClaim.state == "bound",
                    AdoptionClaim.generation == requested_generation,
                    AdoptionClaim.cloudstack_vm_ref == vm_ref,
                    exists().where(
                        AdoptionExecution.id == execution_uuid,
                        AdoptionExecution.claim_id == claim_uuid,
                        AdoptionExecution.generation == requested_generation,
                        AdoptionExecution.state == "verifying",
                        AdoptionExecution.worker_lease_id == lease_uuid,
                        AdoptionExecution.worker_lease_expires_at
                        > _database_wall_clock(session),
                    ),
                )
                .values(
                    state="managed", updated_at=datetime.now(timezone.utc)
                )
            )
            if result.rowcount == 1:
                session.commit()
            else:
                session.rollback()
        except OperationalError as exc:
            session.rollback()
            if not _is_retryable_operational_error(exc):
                raise

        session.expire_all()
        current = (
            session.query(AdoptionClaim)
            .filter_by(id=claim_uuid)
            .execution_options(populate_existing=True)
            .first()
        )
        if current is None:
            raise ClaimConflict("claim disappeared during activation")
        exact_identity = (
            current.generation == requested_generation
            and current.cloudstack_vm_ref == vm_ref
        )
        if exact_identity and current.state == "managed":
            return current
        if exact_identity and current.state == "bound":
            continue
        raise ClaimConflict("claim activation lost a concurrent state transition")

    raise ClaimConflict("claim activation retry limit reached")


def validated_claim_state(
    session,
    *,
    claim_id: str,
    generation: int,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_sha256: str,
    cloudstack_vm_ref: str,
    cloudstack_instance_name: str,
) -> str:
    """Return lifecycle state only after validating the complete claim identity."""

    try:
        claim_uuid = str(uuid.UUID(claim_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("claim_id must be a UUID") from exc
    requested_generation = _validated_generation(generation)
    cluster = _normalized_text(proxmox_cluster, "proxmox_cluster")
    node = _normalized_text(proxmox_node, "proxmox_node")
    vm_ref = _normalized_text(cloudstack_vm_ref, "cloudstack_vm_ref")
    instance_name = _normalized_text(
        cloudstack_instance_name, "cloudstack_instance_name"
    )

    claim = session.query(AdoptionClaim).filter_by(id=claim_uuid).first()
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    if claim.generation != requested_generation:
        raise ClaimInvalid("claim generation is stale")
    if (
        claim.proxmox_cluster != cluster
        or claim.proxmox_node != node
        or claim.proxmox_vmid != proxmox_vmid
        or claim.manifest_sha256 != manifest_sha256
        or claim.cloudstack_vm_ref != vm_ref
        or claim.cloudstack_instance_name != instance_name
    ):
        raise ClaimInvalid("claim lifecycle identity does not match")
    if claim.state not in {"bound", "managed", "operating", "retiring"}:
        raise ClaimConflict("claim has no active CloudStack lifecycle identity")
    return claim.state


def acquire_managed_operation_lease(
    session,
    *,
    claim_id: str,
    generation: int,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_sha256: str,
    cloudstack_vm_ref: str,
    cloudstack_instance_name: str,
    action: str,
    write_guard: Callable[[], None],
) -> ManagedOperationLease:
    """Atomically fence one managed mutation against retirement."""

    try:
        claim_uuid = str(uuid.UUID(claim_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("claim_id must be a UUID") from exc
    requested_generation = _validated_generation(generation)
    operation = _normalized_text(action, "action")
    if operation not in MANAGED_OPERATION_ACTIONS:
        raise ClaimInvalid("managed operation action is unsupported")

    identity = {
        "claim_id": claim_uuid,
        "generation": requested_generation,
        "proxmox_cluster": proxmox_cluster,
        "proxmox_node": proxmox_node,
        "proxmox_vmid": proxmox_vmid,
        "manifest_sha256": manifest_sha256,
        "cloudstack_vm_ref": cloudstack_vm_ref,
        "cloudstack_instance_name": cloudstack_instance_name,
    }
    for _attempt in range(3):
        state = validated_claim_state(session, **identity)
        claim = (
            session.query(AdoptionClaim)
            .filter_by(id=claim_uuid)
            .execution_options(populate_existing=True)
            .one()
        )
        now = datetime.now(timezone.utc)
        if state == "operating":
            if not claim.operation_lease_id:
                raise ClaimConflict("operating claim has no lease fence")
            active = (
                session.query(AdoptionOperationLease)
                .filter(
                    AdoptionOperationLease.claim_id == claim_uuid,
                    AdoptionOperationLease.id == claim.operation_lease_id,
                    AdoptionOperationLease.expires_at > now,
                )
                .first()
            )
            if active is not None:
                raise ClaimConflict("managed operation already in progress")
            expired = (
                session.query(AdoptionOperationLease)
                .filter(
                    AdoptionOperationLease.claim_id == claim_uuid,
                    AdoptionOperationLease.id == claim.operation_lease_id,
                    AdoptionOperationLease.expires_at <= now,
                )
                .first()
            )
            if expired is None:
                raise ClaimConflict("operating claim has no recoverable lease")
            write_guard()
            recovered = session.execute(
                update(AdoptionClaim)
                .where(
                    AdoptionClaim.id == claim_uuid,
                    AdoptionClaim.generation == requested_generation,
                    AdoptionClaim.state == "operating",
                    AdoptionClaim.operation_lease_id == expired.id,
                )
                .values(
                    state="managed", operation_lease_id=None, updated_at=now
                )
            )
            if recovered.rowcount == 1:
                session.query(AdoptionOperationLease).filter_by(
                    id=expired.id, claim_id=claim_uuid
                ).delete(synchronize_session=False)
                session.commit()
            else:
                session.rollback()
            session.expire_all()
            continue
        if state != "managed":
            raise ClaimConflict("claim lifecycle is not managed")

        lease_id = str(uuid.uuid4())
        expires_at = now + MANAGED_OPERATION_LEASE_DURATION
        try:
            write_guard()
            fenced = session.execute(
                update(AdoptionClaim)
                .where(
                    AdoptionClaim.id == claim_uuid,
                    AdoptionClaim.generation == requested_generation,
                    AdoptionClaim.state == "managed",
                    AdoptionClaim.operation_lease_id.is_(None),
                )
                .values(
                    state="operating",
                    operation_lease_id=lease_id,
                    updated_at=now,
                )
            )
            if fenced.rowcount != 1:
                session.rollback()
                session.expire_all()
                continue
            session.add(
                AdoptionOperationLease(
                    id=lease_id,
                    claim_id=claim_uuid,
                    generation=requested_generation,
                    action=operation,
                    expires_at=expires_at,
                )
            )
            session.commit()
            return ManagedOperationLease(
                id=lease_id,
                action=operation,
                expires_at=expires_at,
            )
        except IntegrityError as exc:
            session.rollback()
            raise ClaimConflict("managed operation already in progress") from exc
        except OperationalError as exc:
            session.rollback()
            if not _is_retryable_operational_error(exc):
                raise
        session.expire_all()

    raise ClaimConflict("managed operation lease retry limit reached")


def complete_managed_operation_lease(
    session,
    *,
    claim_id: str,
    generation: int,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_sha256: str,
    cloudstack_vm_ref: str,
    cloudstack_instance_name: str,
    action: str,
    lease_id: str,
    write_guard: Callable[[], None],
) -> str:
    """Complete exactly one lease without clearing a newer operation fence."""

    try:
        claim_uuid = str(uuid.UUID(claim_id))
        normalized_lease_id = str(uuid.UUID(lease_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("claim_id and lease_id must be UUIDs") from exc
    requested_generation = _validated_generation(generation)
    operation = _normalized_text(action, "action")
    if operation not in MANAGED_OPERATION_ACTIONS:
        raise ClaimInvalid("managed operation action is unsupported")
    state = validated_claim_state(
        session,
        claim_id=claim_uuid,
        generation=requested_generation,
        proxmox_cluster=proxmox_cluster,
        proxmox_node=proxmox_node,
        proxmox_vmid=proxmox_vmid,
        manifest_sha256=manifest_sha256,
        cloudstack_vm_ref=cloudstack_vm_ref,
        cloudstack_instance_name=cloudstack_instance_name,
    )
    lease = (
        session.query(AdoptionOperationLease)
        .filter_by(claim_id=claim_uuid)
        .first()
    )
    if lease is None:
        if state in {"managed", "retiring"}:
            return state
        raise ClaimConflict("operating claim has no completion lease")
    if lease.id != normalized_lease_id or lease.action != operation:
        raise ClaimInvalid("managed operation lease does not match")
    if lease.generation != requested_generation:
        raise ClaimInvalid("managed operation lease generation is stale")

    write_guard()
    completed = session.execute(
        update(AdoptionClaim)
        .where(
            AdoptionClaim.id == claim_uuid,
            AdoptionClaim.generation == requested_generation,
            AdoptionClaim.state == "operating",
            AdoptionClaim.operation_lease_id == normalized_lease_id,
        )
        .values(
            state="managed",
            operation_lease_id=None,
            updated_at=datetime.now(timezone.utc),
        )
    )
    if completed.rowcount != 1:
        session.rollback()
        raise ClaimConflict("managed operation completion lost its state fence")
    session.delete(lease)
    session.commit()
    return "managed"


def retire_claim(
    session,
    *,
    claim_id: str,
    generation: int,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_sha256: str,
    cloudstack_vm_ref: str,
    cloudstack_instance_name: str,
    write_guard: Callable[[], None],
) -> AdoptionClaim:
    """Tombstone a claim only when no managed operation lease is live.

    Exact duplicate retirements are idempotent.  ``retiring`` remains present
    in status mappings and cannot be reserved again.  A separate server-side
    CloudStack-absence check must finalize the transition to ``released``.
    """

    try:
        claim_uuid = str(uuid.UUID(claim_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("claim_id must be a UUID") from exc
    requested_generation = _validated_generation(generation)
    cluster = _normalized_text(proxmox_cluster, "proxmox_cluster")
    node = _normalized_text(proxmox_node, "proxmox_node")
    vm_ref = _normalized_text(cloudstack_vm_ref, "cloudstack_vm_ref")
    instance_name = _normalized_text(
        cloudstack_instance_name, "cloudstack_instance_name"
    )

    retiring_generation = requested_generation

    for _attempt in range(3):
        claim = (
            session.query(AdoptionClaim)
            .filter_by(id=claim_uuid)
            .execution_options(populate_existing=True)
            .first()
        )
        if claim is None:
            raise ClaimNotFound("claim does not exist")
        if claim.generation != requested_generation:
            raise ClaimInvalid("claim generation is stale")
        if (
            claim.proxmox_cluster != cluster
            or claim.proxmox_node != node
            or claim.proxmox_vmid != proxmox_vmid
            or claim.manifest_sha256 != manifest_sha256
            or claim.cloudstack_vm_ref != vm_ref
            or claim.cloudstack_instance_name != instance_name
        ):
            raise ClaimInvalid("claim retirement identity does not match")
        if claim.state == "retiring":
            return claim
        source_state = claim.state
        if source_state == "operating":
            if not claim.operation_lease_id:
                raise ClaimConflict("operating claim has no lease fence")
            now = datetime.now(timezone.utc)
            active_lease = (
                session.query(AdoptionOperationLease)
                .filter(
                    AdoptionOperationLease.claim_id == claim_uuid,
                    AdoptionOperationLease.id == claim.operation_lease_id,
                    AdoptionOperationLease.expires_at > now,
                )
                .first()
            )
            if active_lease is not None:
                raise ClaimConflict("managed operation is still in progress")
            expired_lease = (
                session.query(AdoptionOperationLease)
                .filter(
                    AdoptionOperationLease.claim_id == claim_uuid,
                    AdoptionOperationLease.id == claim.operation_lease_id,
                    AdoptionOperationLease.expires_at <= now,
                )
                .first()
            )
            if expired_lease is None:
                raise ClaimConflict("operating claim has no recoverable lease")
        elif source_state not in {"bound", "managed"}:
            raise ClaimConflict("claim lifecycle cannot be retired")
        elif claim.operation_lease_id is not None:
            raise ClaimConflict("non-operating claim has an unexpected lease fence")
        expected_operation_lease_id = claim.operation_lease_id

        try:
            write_guard()
            result = session.execute(
                update(AdoptionClaim)
                .where(
                    AdoptionClaim.id == claim_uuid,
                    AdoptionClaim.state == source_state,
                    AdoptionClaim.generation == retiring_generation,
                    AdoptionClaim.operation_lease_id
                    == expected_operation_lease_id,
                    AdoptionClaim.proxmox_cluster == cluster,
                    AdoptionClaim.proxmox_node == node,
                    AdoptionClaim.proxmox_vmid == proxmox_vmid,
                    AdoptionClaim.manifest_sha256 == manifest_sha256,
                    AdoptionClaim.cloudstack_vm_ref == vm_ref,
                    AdoptionClaim.cloudstack_instance_name == instance_name,
                )
                .values(
                    state="retiring",
                    operation_lease_id=None,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if result.rowcount == 1:
                if source_state == "operating":
                    session.query(AdoptionOperationLease).filter_by(
                        id=expected_operation_lease_id,
                        claim_id=claim_uuid,
                    ).delete(synchronize_session=False)
                session.commit()
                session.expire_all()
                return (
                    session.query(AdoptionClaim)
                    .filter_by(id=claim_uuid)
                    .execution_options(populate_existing=True)
                    .one()
                )
            else:
                session.rollback()
        except OperationalError as exc:
            session.rollback()
            if not _is_retryable_operational_error(exc):
                raise

        session.expire_all()

    raise ClaimConflict("claim retirement retry limit reached")


def finalize_retiring_claim(
    session,
    *,
    claim_id: str,
    cloudstack_vm_ref: str,
    write_guard: Callable[[], None],
) -> AdoptionClaim:
    """Release a tombstone after the caller proved the CloudStack VM is absent."""

    try:
        claim_uuid = str(uuid.UUID(claim_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("claim_id must be a UUID") from exc
    vm_ref = _normalized_text(cloudstack_vm_ref, "cloudstack_vm_ref")

    claim = session.query(AdoptionClaim).filter_by(id=claim_uuid).first()
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    if claim.cloudstack_vm_ref != vm_ref:
        raise ClaimInvalid("CloudStack VM reference does not match tombstone")
    if claim.state == "released":
        return claim
    if claim.state != "retiring":
        raise ClaimConflict("only a retiring claim may be finalized")
    retiring_generation = claim.generation

    for _attempt in range(3):
        try:
            write_guard()
            result = session.execute(
                update(AdoptionClaim)
                .where(
                    AdoptionClaim.id == claim_uuid,
                    AdoptionClaim.state == "retiring",
                    AdoptionClaim.generation == retiring_generation,
                    AdoptionClaim.cloudstack_vm_ref == vm_ref,
                )
                .values(
                    state="released", updated_at=datetime.now(timezone.utc)
                )
            )
            if result.rowcount == 1:
                session.commit()
            else:
                session.rollback()
        except OperationalError as exc:
            session.rollback()
            if not _is_retryable_operational_error(exc):
                raise

        session.expire_all()
        current = (
            session.query(AdoptionClaim)
            .filter_by(id=claim_uuid)
            .execution_options(populate_existing=True)
            .first()
        )
        if current is None:
            raise ClaimConflict("claim disappeared during finalization")
        if (
            current.state == "released"
            and current.generation == retiring_generation
            and current.cloudstack_vm_ref == vm_ref
        ):
            return current
        if (
            current.state == "retiring"
            and current.generation == retiring_generation
            and current.cloudstack_vm_ref == vm_ref
        ):
            continue
        raise ClaimConflict("claim finalization lost a concurrent state transition")

    raise ClaimConflict("claim finalization retry limit reached")


def bound_status_map(session, *, proxmox_cluster: str) -> dict[str, str]:
    """Return VMID -> CloudStack instance name for active identity claims."""

    cluster = _normalized_text(proxmox_cluster, "proxmox_cluster")
    rows = (
        session.query(AdoptionClaim)
        .filter(
            AdoptionClaim.proxmox_cluster == cluster,
            AdoptionClaim.state.in_(("bound", "managed", "operating", "retiring")),
        )
        .order_by(AdoptionClaim.proxmox_vmid)
        .all()
    )
    result: dict[str, str] = {}
    for row in rows:
        if row.cloudstack_instance_name:
            result[str(row.proxmox_vmid)] = row.cloudstack_instance_name
    return result


def bound_status_bindings(session, *, proxmox_cluster: str) -> dict[str, dict]:
    """Return status mappings with immutable Proxmox-name corroboration."""

    cluster = _normalized_text(proxmox_cluster, "proxmox_cluster")
    rows = (
        session.query(AdoptionClaim)
        .filter(
            AdoptionClaim.proxmox_cluster == cluster,
            AdoptionClaim.state.in_(("bound", "managed", "operating", "retiring")),
        )
        .order_by(AdoptionClaim.proxmox_vmid)
        .all()
    )
    result: dict[str, dict] = {}
    for row in rows:
        try:
            manifest = json.loads(row.manifest_json)
            expected_name = _normalized_text(
                manifest.get("name"), "manifest.name"
            )
        except (json.JSONDecodeError, ClaimInvalid, AttributeError) as exc:
            raise ClaimInvalid("bound claim manifest is invalid") from exc
        result[str(row.proxmox_vmid)] = {
            "cloudstack_instance_name": row.cloudstack_instance_name,
            "expected_proxmox_name": expected_name,
            "manifest_sha256": row.manifest_sha256,
            "claim_state": row.state,
        }
    return result


def public_claim(claim: AdoptionClaim) -> dict:
    """Secret-free operator projection."""

    return {
        "id": claim.id,
        "state": claim.state,
        "generation": claim.generation,
        "proxmox_cluster": claim.proxmox_cluster,
        "proxmox_node": claim.proxmox_node,
        "proxmox_vmid": claim.proxmox_vmid,
        "manifest_sha256": claim.manifest_sha256,
        "cloudstack_vm_ref": claim.cloudstack_vm_ref,
        "cloudstack_instance_name": claim.cloudstack_instance_name,
        "created_at": claim.created_at.isoformat() if claim.created_at else None,
        "updated_at": claim.updated_at.isoformat() if claim.updated_at else None,
    }
