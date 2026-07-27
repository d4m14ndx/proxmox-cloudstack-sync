"""Durable source-free claims for existing Proxmox QEMU guests.

The registry is deliberately independent of CloudStack's schema.  A unique
(cluster, VMID) row and an atomic compare-and-set bind provide the identity
primitive missing from CloudStack 4.22 External deployments.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from database import AdoptionClaim


class ClaimConflict(Exception):
    """The requested identity is already claimed or bound differently."""


class ClaimInvalid(Exception):
    """The claim request is malformed or does not match its frozen manifest."""


class ClaimNotFound(Exception):
    """The requested claim does not exist."""


@dataclass(frozen=True)
class Reservation:
    claim: AdoptionClaim
    nonce: str


def _normalized_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ClaimInvalid(f"{field} must be a nonempty normalized string")
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


def reserve_claim(
    session,
    *,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_json: str,
    manifest_sha256: str,
) -> Reservation:
    """Reserve one globally unique cluster-local Proxmox VMID.

    The nonce is returned exactly once.  Only its digest is persisted.
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

    nonce = secrets.token_urlsafe(32)
    claim = AdoptionClaim(
        id=str(uuid.uuid4()),
        proxmox_cluster=cluster,
        proxmox_node=node,
        proxmox_vmid=proxmox_vmid,
        manifest_sha256=manifest_sha256,
        manifest_json=canonical,
        nonce_sha256=_sha256(nonce),
        state="reserved",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(claim)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        existing = (
            session.query(AdoptionClaim)
            .filter_by(
                proxmox_cluster=cluster,
                proxmox_vmid=proxmox_vmid,
            )
            .first()
        )
        if existing is None or existing.state != "released":
            raise ClaimConflict("Proxmox cluster/VMID is already claimed") from exc
        result = session.execute(
            update(AdoptionClaim)
            .where(
                AdoptionClaim.id == existing.id,
                AdoptionClaim.state == "released",
            )
            .values(
                proxmox_node=node,
                manifest_sha256=manifest_sha256,
                manifest_json=canonical,
                nonce_sha256=_sha256(nonce),
                generation=AdoptionClaim.generation + 1,
                state="reserved",
                cloudstack_vm_ref=None,
                cloudstack_instance_name=None,
                updated_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount != 1:
            session.rollback()
            raise ClaimConflict(
                "Released Proxmox claim was reserved concurrently"
            ) from exc
        session.commit()
        claim = session.query(AdoptionClaim).filter_by(id=existing.id).one()
        return Reservation(claim=claim, nonce=nonce)
    session.refresh(claim)
    return Reservation(claim=claim, nonce=nonce)


def bind_claim(
    session,
    *,
    claim_id: str,
    nonce: str,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_sha256: str,
    cloudstack_vm_ref: str,
    cloudstack_instance_name: str,
) -> AdoptionClaim:
    """Atomically bind a reservation to one CloudStack VM.

    A retry for the exact same VM is idempotent.  A second VM loses the CAS and
    receives a conflict, even when calls race across management servers.
    """

    try:
        claim_uuid = str(uuid.UUID(claim_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("claim_id must be a UUID") from exc
    nonce = _normalized_text(nonce, "nonce")
    cluster = _normalized_text(proxmox_cluster, "proxmox_cluster")
    node = _normalized_text(proxmox_node, "proxmox_node")
    vm_ref = _normalized_text(cloudstack_vm_ref, "cloudstack_vm_ref")
    instance_name = _normalized_text(
        cloudstack_instance_name, "cloudstack_instance_name"
    )

    claim = session.query(AdoptionClaim).filter_by(id=claim_uuid).first()
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    if not secrets.compare_digest(claim.nonce_sha256, _sha256(nonce)):
        raise ClaimInvalid("claim nonce is invalid")
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

    now = datetime.now(timezone.utc)
    try:
        result = session.execute(
            update(AdoptionClaim)
            .where(
                AdoptionClaim.id == claim_uuid,
                AdoptionClaim.state == "reserved",
                AdoptionClaim.cloudstack_vm_ref.is_(None),
            )
            .values(
                state="bound",
                cloudstack_vm_ref=vm_ref,
                cloudstack_instance_name=instance_name,
                updated_at=now,
            )
        )
        if result.rowcount == 1:
            session.commit()
            return session.query(AdoptionClaim).filter_by(id=claim_uuid).one()
        session.rollback()
    except IntegrityError as exc:
        session.rollback()
        raise ClaimConflict("CloudStack VM reference is already bound") from exc

    current = session.query(AdoptionClaim).filter_by(id=claim_uuid).first()
    if current and (
        current.state == "bound"
        and current.cloudstack_vm_ref == vm_ref
        and current.cloudstack_instance_name == instance_name
    ):
        return current
    raise ClaimConflict("claim was bound concurrently to another CloudStack VM")


def release_claim(
    session,
    *,
    claim_id: str,
    nonce: str,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_sha256: str,
    cloudstack_vm_ref: str,
    cloudstack_instance_name: str,
) -> AdoptionClaim:
    """Release a bound claim after metadata-only CloudStack deletion."""

    try:
        claim_uuid = str(uuid.UUID(claim_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClaimInvalid("claim_id must be a UUID") from exc
    nonce = _normalized_text(nonce, "nonce")
    cluster = _normalized_text(proxmox_cluster, "proxmox_cluster")
    node = _normalized_text(proxmox_node, "proxmox_node")
    vm_ref = _normalized_text(cloudstack_vm_ref, "cloudstack_vm_ref")
    instance_name = _normalized_text(
        cloudstack_instance_name, "cloudstack_instance_name"
    )

    claim = session.query(AdoptionClaim).filter_by(id=claim_uuid).first()
    if claim is None:
        raise ClaimNotFound("claim does not exist")
    if not secrets.compare_digest(claim.nonce_sha256, _sha256(nonce)):
        raise ClaimInvalid("claim nonce is invalid")
    if (
        claim.proxmox_cluster != cluster
        or claim.proxmox_node != node
        or claim.proxmox_vmid != proxmox_vmid
        or claim.manifest_sha256 != manifest_sha256
        or claim.cloudstack_vm_ref != vm_ref
        or claim.cloudstack_instance_name != instance_name
    ):
        raise ClaimInvalid("claim release identity does not match")
    if claim.state == "released":
        return claim
    if claim.state != "bound":
        raise ClaimConflict("only a bound claim may be released")

    result = session.execute(
        update(AdoptionClaim)
        .where(
            AdoptionClaim.id == claim_uuid,
            AdoptionClaim.state == "bound",
            AdoptionClaim.cloudstack_vm_ref == vm_ref,
        )
        .values(state="released", updated_at=datetime.now(timezone.utc))
    )
    if result.rowcount != 1:
        session.rollback()
        raise ClaimConflict("claim release lost a concurrent state transition")
    session.commit()
    return session.query(AdoptionClaim).filter_by(id=claim_uuid).one()


def bound_status_map(session, *, proxmox_cluster: str) -> dict[str, str]:
    """Return VMID -> CloudStack instance name for bound claims only."""

    cluster = _normalized_text(proxmox_cluster, "proxmox_cluster")
    rows = (
        session.query(AdoptionClaim)
        .filter_by(proxmox_cluster=cluster, state="bound")
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
        .filter_by(proxmox_cluster=cluster, state="bound")
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
