import json
import uuid
from datetime import datetime, timedelta, timezone

from database import (
    AdoptionClaim,
    AdoptionExecution,
    AdoptionWriteAuthority,
    get_session,
)
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

_AUTHORITY_ROW_ID = 1
_VALID_MODES = {"operator", "automatic"}


class AuthorityConflict(Exception):
    """The mutually exclusive write authority is held by another process."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_authority_row() -> None:
    session = get_session()
    try:
        if session.get(AdoptionWriteAuthority, _AUTHORITY_ROW_ID) is None:
            session.add(AdoptionWriteAuthority(id=_AUTHORITY_ROW_ID))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
    finally:
        session.close()


def acquire_write_authority(
    *,
    mode: str,
    target: str,
    lease_seconds: int,
    owner_id: str | None = None,
) -> str | None:
    if mode not in _VALID_MODES:
        raise ValueError("invalid write authority mode")
    if not isinstance(target, str) or not target or target != target.strip():
        raise ValueError("invalid write authority target")
    if not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 7200:
        raise ValueError("invalid write authority lease")
    owner = owner_id or str(uuid.uuid4())
    try:
        uuid.UUID(owner)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid write authority owner") from exc

    _ensure_authority_row()
    now = _utcnow()
    expires = now + timedelta(seconds=lease_seconds)
    session = get_session()
    try:
        updated = (
            session.query(AdoptionWriteAuthority)
            .filter(AdoptionWriteAuthority.id == _AUTHORITY_ROW_ID)
            .filter(
                or_(
                    AdoptionWriteAuthority.owner_id.is_(None),
                    AdoptionWriteAuthority.expires_at.is_(None),
                    AdoptionWriteAuthority.expires_at <= now,
                    AdoptionWriteAuthority.owner_id == owner,
                )
            )
            .update(
                {
                    AdoptionWriteAuthority.mode: mode,
                    AdoptionWriteAuthority.owner_id: owner,
                    AdoptionWriteAuthority.target: target,
                    AdoptionWriteAuthority.expires_at: expires,
                    AdoptionWriteAuthority.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return owner if updated == 1 else None
    finally:
        session.close()


def assert_write_authority(*, owner_id: str, mode: str) -> None:
    now = _utcnow()
    session = get_session()
    try:
        held = (
            session.query(AdoptionWriteAuthority.id)
            .filter(
                AdoptionWriteAuthority.id == _AUTHORITY_ROW_ID,
                AdoptionWriteAuthority.owner_id == owner_id,
                AdoptionWriteAuthority.mode == mode,
                AdoptionWriteAuthority.expires_at > now,
            )
            .one_or_none()
        )
        if held is None:
            raise AuthorityConflict("write authority is not held")
    finally:
        session.close()


def _callback_execution_binding_matches(
    execution: AdoptionExecution | None,
    *,
    execution_plan_sha256: str | None,
    ip_overrides_json: str | None,
) -> bool:
    if execution is None:
        return False
    try:
        plan = json.loads(execution.plan_json)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(plan, dict) or "execution_time_ip_overrides" not in plan:
        return execution_plan_sha256 is None and ip_overrides_json is None
    overrides = plan.get("execution_time_ip_overrides")
    return isinstance(overrides, list) and (
        execution_plan_sha256 == execution.plan_sha256
        and ip_overrides_json == json.dumps(overrides, sort_keys=True, separators=(",", ":"))
    )


def assert_operator_bind_callback_authority(
    *,
    claim_id: str,
    generation: int,
    proxmox_cluster: str,
    proxmox_node: str,
    proxmox_vmid: int,
    manifest_sha256: str,
    cloudstack_vm_ref: str,
    cloudstack_instance_name: str,
    execution_plan_sha256: str | None = None,
    ip_overrides_json: str | None = None,
) -> None:
    """Allow only the exact extension bind belonging to the active operator run."""

    now = _utcnow()
    session = get_session()
    try:
        authority = (
            session.query(AdoptionWriteAuthority)
            .filter(
                AdoptionWriteAuthority.id == _AUTHORITY_ROW_ID,
                AdoptionWriteAuthority.mode == "operator",
                AdoptionWriteAuthority.owner_id.is_not(None),
                AdoptionWriteAuthority.target
                == f"{proxmox_cluster}:{proxmox_vmid}",
                AdoptionWriteAuthority.expires_at > now,
            )
            .one_or_none()
        )
        claim = session.query(AdoptionClaim).filter_by(id=claim_id).one_or_none()
        execution = (
            session.query(AdoptionExecution)
            .filter_by(
                id=cloudstack_vm_ref,
                claim_id=claim_id,
                generation=generation,
            )
            .one_or_none()
        )
        exact_claim = bool(
            claim is not None
            and claim.generation == generation
            and claim.proxmox_cluster == proxmox_cluster
            and claim.proxmox_node == proxmox_node
            and claim.proxmox_vmid == proxmox_vmid
            and claim.manifest_sha256 == manifest_sha256
            and claim.state in {"reserved", "bound"}
            and (
                claim.state == "reserved"
                or (
                    claim.cloudstack_vm_ref == cloudstack_vm_ref
                    and claim.cloudstack_instance_name == cloudstack_instance_name
                )
            )
        )
        exact_execution = bool(
            execution is not None
            and execution.state
            in {
                "deploy_submitting",
                "deploy_submitted",
                "submission_unknown",
                "deploy_succeeded",
            }
            and execution.cloudstack_vm_ref in {None, cloudstack_vm_ref}
            and execution.cloudstack_instance_name
            in {None, cloudstack_instance_name}
            and _callback_execution_binding_matches(
                execution,
                execution_plan_sha256=execution_plan_sha256,
                ip_overrides_json=ip_overrides_json,
            )
        )
        exact_authority = authority is not None
        if not (exact_authority and exact_claim and exact_execution):
            raise AuthorityConflict("operator callback identity is not authorized")
    finally:
        session.close()


def renew_write_authority(
    *, owner_id: str, mode: str, target: str, lease_seconds: int
) -> None:
    acquired = acquire_write_authority(
        mode=mode,
        target=target,
        lease_seconds=lease_seconds,
        owner_id=owner_id,
    )
    if acquired != owner_id:
        raise AuthorityConflict("write authority renewal failed")


def release_write_authority(*, owner_id: str, mode: str) -> bool:
    session = get_session()
    try:
        released = (
            session.query(AdoptionWriteAuthority)
            .filter(
                AdoptionWriteAuthority.id == _AUTHORITY_ROW_ID,
                AdoptionWriteAuthority.owner_id == owner_id,
                AdoptionWriteAuthority.mode == mode,
            )
            .update(
                {
                    AdoptionWriteAuthority.mode: None,
                    AdoptionWriteAuthority.owner_id: None,
                    AdoptionWriteAuthority.target: None,
                    AdoptionWriteAuthority.expires_at: None,
                    AdoptionWriteAuthority.updated_at: _utcnow(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return released == 1
    finally:
        session.close()
