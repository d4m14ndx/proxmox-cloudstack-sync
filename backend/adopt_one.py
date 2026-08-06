"""One-command, resumable operator runner for one existing Proxmox QEMU guest.

Run inside the sync container while the deployed scheduled executor remains disabled.
The runner reuses the durable claim/execution state machine and never automatically
replays ambiguous deploy or start submissions.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

import main as app_main
from adoption_authority import (
    acquire_write_authority,
    assert_write_authority,
    release_write_authority,
    renew_write_authority,
)
from adoption_executor import _vm_matches_plan, load_exact_external_vm
from adoption_registry import bind_claim
from cloudstack_client import CloudStackClient
from database import AdoptionClaim, AdoptionExecution, get_session, init_db

_LIVE_CATALOG_URL = "http://127.0.0.1:8088/api/adoption/candidates"

_ACTIVE_RESUMABLE_STATES = {
    "planned",
    "deploy_submitting",
    "deploy_submitted",
    "deploy_succeeded",
    "submission_unknown",
    "start_submitting",
    "start_submitted",
    "start_unknown",
    "verifying",
}
_AMBIGUOUS_STATES = {
    "deploy_submitting",
    "submission_unknown",
    "start_submitting",
    "start_unknown",
}


class OperatorStop(Exception):
    """Sanitized fail-closed operator outcome."""


@dataclass(frozen=True)
class Target:
    proxmox_id: str
    cluster: str
    vmid: int
    manifest_sha256: str
    network_ip_overrides: tuple[tuple[int, str], ...] = ()


class BoundedCloudStackClient:
    """Allow at most the side effects reachable from the persisted starting state."""

    def __init__(
        self,
        delegate,
        *,
        allow_deploy: bool,
        allow_start: bool,
        authority_guard: Callable[[], None],
    ):
        self._delegate = delegate
        self._authority_guard = authority_guard
        self._deploy_limit = int(allow_deploy)
        self._start_limit = int(allow_start)
        self.deploy_calls = 0
        self.start_calls = 0
        self.destroy_calls = 0
        self.job_queries = 0

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def deploy_virtual_machine(self, **params):
        if self.deploy_calls >= self._deploy_limit:
            raise OperatorStop("deploy_call_not_authorized_by_starting_state")
        if params.get("startvm") != "false":
            raise OperatorStop("deploy_startvm_must_be_false")
        self._authority_guard()
        self.deploy_calls += 1
        return self._delegate.deploy_virtual_machine(**params)

    def start_virtual_machine(self, vm_id: str):
        if self.start_calls >= self._start_limit:
            raise OperatorStop("start_call_not_authorized_by_starting_state")
        self._authority_guard()
        self.start_calls += 1
        return self._delegate.start_virtual_machine(vm_id)

    def destroy_virtual_machine(self, vm_id: str, *, expunge: bool = True):
        self.destroy_calls += 1
        raise OperatorStop("destroy_call_is_never_authorized")

    def query_async_job(self, job_id: str):
        self.job_queries += 1
        return self._delegate.query_async_job(job_id)

    def list_virtual_machines(self, **params):
        requested_max_pages = params.pop("_max_pages", 20)
        requested_deadline = params.pop(
            "_deadline_monotonic", time.monotonic() + 120
        )
        if (
            isinstance(requested_max_pages, bool)
            or not isinstance(requested_max_pages, int)
            or requested_max_pages < 1
        ):
            raise OperatorStop("vm_inventory_page_bound_invalid")
        if (
            isinstance(requested_deadline, bool)
            or not isinstance(requested_deadline, (int, float))
        ):
            raise OperatorStop("vm_inventory_deadline_invalid")
        return self._delegate.list_virtual_machines(
            _max_pages=min(requested_max_pages, 20),
            _deadline_monotonic=min(
                float(requested_deadline), time.monotonic() + 120
            ),
            **params,
        )

    def list_vlan_ip_ranges(self, *, networkid: str):
        if not isinstance(networkid, str) or not networkid:
            raise OperatorStop("cloudstack_network_id_is_required")
        return self._delegate.list_vlan_ip_ranges(networkid=networkid)

    def public_counts(self) -> dict:
        return {
            "deploy": self.deploy_calls,
            "start": self.start_calls,
            "destroy": self.destroy_calls,
            "job_queries": self.job_queries,
        }


def _parse_network_ip_overrides(values: list[str] | None) -> tuple[tuple[int, str], ...]:
    parsed: dict[int, str] = {}
    seen_ips: set[str] = set()
    for value in values or []:
        match = re.fullmatch(r"net([0-9]+)=([^\s=]+)", value or "")
        if match is None:
            raise OperatorStop("network_ip_must_be_net_device_equals_ipv4")
        device_id = int(match.group(1))
        if str(device_id) != match.group(1):
            raise OperatorStop("network_ip_device_must_be_canonical")
        try:
            ip = ipaddress.ip_address(match.group(2))
        except ValueError as exc:
            raise OperatorStop("network_ip_must_be_canonical_ipv4") from exc
        canonical_ip = str(ip)
        if ip.version != 4 or canonical_ip != match.group(2):
            raise OperatorStop("network_ip_must_be_canonical_ipv4")
        if device_id in parsed:
            raise OperatorStop("network_ip_device_is_duplicate")
        if canonical_ip in seen_ips:
            raise OperatorStop("network_ip_is_duplicate")
        parsed[device_id] = canonical_ip
        seen_ips.add(canonical_ip)
    return tuple(sorted(parsed.items()))


def parse_target(
    proxmox_id: str,
    manifest_sha256: str,
    network_ip_values: list[str] | None = None,
) -> Target:
    if not isinstance(proxmox_id, str) or not re.fullmatch(
        r"[^\s:]+:[1-9][0-9]*", proxmox_id
    ):
        raise OperatorStop("proxmox_id_must_be_canonical_cluster_colon_vmid")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256 or ""):
        raise OperatorStop("manifest_sha256_must_be_lowercase_hex")
    cluster, raw_vmid = proxmox_id.split(":", 1)
    return Target(
        proxmox_id=proxmox_id,
        cluster=cluster,
        vmid=int(raw_vmid),
        manifest_sha256=manifest_sha256,
        network_ip_overrides=_parse_network_ip_overrides(network_ip_values),
    )


def _network_ip_override_requests(target: Target) -> list[app_main.NetworkIPOverride]:
    return [
        app_main.NetworkIPOverride(device_id=device_id, ip=ip)
        for device_id, ip in target.network_ip_overrides
    ]


def strict_job_status(result: object) -> int:
    if not isinstance(result, dict):
        raise OperatorStop("cloudstack_job_result_not_an_object")
    status = result.get("jobstatus")
    if isinstance(status, bool) or not isinstance(status, int) or status not in {0, 1, 2}:
        raise OperatorStop("cloudstack_job_status_invalid")
    return status


def wait_for_job(
    query: Callable[[str], dict],
    job_id: object,
    *,
    deadline: float,
    poll_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    if not isinstance(job_id, str) or not job_id or job_id != job_id.strip():
        raise OperatorStop("persisted_job_id_invalid")
    while True:
        status = strict_job_status(query(job_id))
        if status != 0:
            return status
        if monotonic() >= deadline:
            raise OperatorStop("async_job_wait_timeout_safe_to_resume")
        sleep(poll_seconds)


def _load_target_state(target: Target) -> dict:
    session = get_session()
    try:
        claims = session.query(AdoptionClaim).filter_by(
            proxmox_cluster=target.cluster,
            proxmox_vmid=target.vmid,
        ).all()
        if len(claims) > 1:
            raise OperatorStop("target_claim_is_ambiguous")
        claim = claims[0] if claims else None
        executions = []
        if claim is not None:
            executions = session.query(AdoptionExecution).filter_by(
                claim_id=claim.id,
                generation=claim.generation,
            ).all()
            if len(executions) > 1:
                raise OperatorStop("target_execution_is_ambiguous")
        execution = executions[0] if executions else None
        return {
            "claim": None if claim is None else {
                "id": claim.id,
                "cluster": claim.proxmox_cluster,
                "node": claim.proxmox_node,
                "vmid": claim.proxmox_vmid,
                "generation": claim.generation,
                "manifest_sha256": claim.manifest_sha256,
                "state": claim.state,
                "cloudstack_vm_ref": claim.cloudstack_vm_ref,
                "cloudstack_instance_name": claim.cloudstack_instance_name,
                "operation_lease_present": claim.operation_lease_id is not None,
            },
            "execution": None if execution is None else {
                "id": execution.id,
                "claim_id": execution.claim_id,
                "generation": execution.generation,
                "state": execution.state,
                "attempt_count": execution.attempt_count,
                "deploy_job_id": execution.deploy_job_id,
                "start_job_id": execution.start_job_id,
                "cleanup_job_id": execution.cleanup_job_id,
                "cloudstack_vm_ref": execution.cloudstack_vm_ref,
                "cloudstack_instance_name": execution.cloudstack_instance_name,
                "error_code": execution.error_code,
                "worker_lease_present": execution.worker_lease_id is not None,
            },
        }
    finally:
        session.close()


def _exact_candidate(catalog: dict, target: Target) -> dict:
    matches = [
        row for row in catalog.get("candidates", [])
        if isinstance(row, dict) and row.get("proxmox_id") == target.proxmox_id
    ]
    if len(matches) != 1:
        raise OperatorStop("current_candidate_is_not_unique")
    return matches[0]


def _load_live_catalog(token: str) -> dict:
    if not token:
        raise OperatorStop("operator_auth_token_not_configured")
    request = urllib.request.Request(
        _LIVE_CATALOG_URL,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
    except Exception as exc:
        raise OperatorStop(f"live_candidate_request_failed_{type(exc).__name__}") from exc
    if not isinstance(result, dict):
        raise OperatorStop("live_candidate_response_not_an_object")
    return result


def _validate_live_runtime(catalog: dict) -> None:
    runtime = catalog.get("runtime_safety")
    if not isinstance(runtime, dict) or runtime != {
        "adoption_executor_enabled": False,
        "auto_reconcile": False,
        "auto_reconcile_nics": False,
    }:
        raise OperatorStop("live_runtime_safety_gate_failed")


def _validate_new_candidate(catalog: dict, target: Target, *, executor_enabled: bool) -> dict:
    freshness = catalog.get("freshness") or {}
    if (
        freshness.get("inventory_collection_current") is not True
        or freshness.get("nic_collection_current") is not True
    ):
        raise OperatorStop("candidate_inventory_not_current")
    candidate = _exact_candidate(catalog, target)
    blockers = set(candidate.get("blockers") or [])
    unresolved_devices = {
        int(match.group(1))
        for blocker in blockers
        if (match := re.fullmatch(r"nic([0-9]+)_ip_unresolved", blocker))
    }
    expected_blockers = {
        f"nic{device_id}_ip_unresolved" for device_id in unresolved_devices
    }
    if not executor_enabled:
        expected_blockers.add("adoption_executor_not_enabled")
    plan = candidate.get("adoption_plan") or {}
    if (
        blockers != expected_blockers
        or {device_id for device_id, _ip in target.network_ip_overrides}
        != unresolved_devices
        or plan.get("manifest_sha256") != target.manifest_sha256
        or not isinstance(plan.get("manifest"), dict)
        or not isinstance(plan.get("host"), dict)
        or not isinstance(plan.get("service_offering"), dict)
        or not isinstance(plan.get("networks"), list)
        or not plan.get("networks")
    ):
        raise OperatorStop("candidate_plan_not_ready_or_manifest_changed")
    if executor_enabled and not isinstance(plan.get("template"), dict):
        raise OperatorStop("candidate_template_plan_not_ready")
    return candidate


def _starting_permissions(state: dict) -> tuple[bool, bool]:
    execution = state.get("execution") or {}
    current = execution.get("state")
    if current is None:
        return True, True
    return (
        current == "planned",
        current in {"planned", "deploy_submitted", "deploy_succeeded"},
    )


def _validate_existing_state(state: dict, target: Target) -> None:
    claim = state.get("claim") or {}
    execution = state.get("execution")
    if (
        claim.get("cluster") != target.cluster
        or claim.get("vmid") != target.vmid
        or claim.get("manifest_sha256") != target.manifest_sha256
        or claim.get("operation_lease_present")
        or claim.get("state") not in {"reserved", "bound", "managed"}
    ):
        raise OperatorStop("existing_claim_does_not_match_authorized_target")
    if execution is not None and (
        execution.get("claim_id") != claim.get("id")
        or execution.get("generation") != claim.get("generation")
        or execution.get("cleanup_job_id") is not None
        or execution.get("worker_lease_present")
        or execution.get("state") not in _ACTIVE_RESUMABLE_STATES | {"succeeded"}
    ):
        raise OperatorStop("existing_execution_is_not_safely_resumable")
    _validate_phase_pair(claim, execution)


def _validate_phase_pair(claim: dict, execution: dict | None) -> None:
    claim_state = claim.get("state")
    claim_ref = claim.get("cloudstack_vm_ref")
    claim_name = claim.get("cloudstack_instance_name")
    reserved_pristine = (
        claim_state == "reserved" and claim_ref is None and claim_name is None
    )
    if execution is None:
        if not reserved_pristine:
            raise OperatorStop("claim_without_execution_is_not_pristine_reserved")
        return

    execution_id = execution.get("id")
    execution_state = execution.get("state")
    execution_ref = execution.get("cloudstack_vm_ref")
    execution_name = execution.get("cloudstack_instance_name")
    bound_correlated = (
        claim_state == "bound"
        and isinstance(execution_id, str)
        and claim_ref == execution_id
        and isinstance(claim_name, str)
        and bool(claim_name)
        and execution_ref in {None, execution_id}
        and execution_name in {None, claim_name}
    )

    if execution_state == "planned":
        valid = (
            reserved_pristine
            and execution_ref is None
            and execution_name is None
            and execution.get("deploy_job_id") is None
            and execution.get("start_job_id") is None
        )
    elif execution_state in {
        "deploy_submitting",
        "deploy_submitted",
        "submission_unknown",
    }:
        valid = reserved_pristine or bound_correlated
    elif execution_state in {
        "deploy_succeeded",
        "start_submitting",
        "start_submitted",
        "start_unknown",
        "verifying",
    }:
        valid = (
            (reserved_pristine or bound_correlated)
            and execution_ref == execution_id
            and isinstance(execution_name, str)
            and bool(execution_name)
        )
    elif execution_state == "succeeded":
        valid = (
            claim_state == "managed"
            and claim_ref == execution_id
            and execution_ref == execution_id
            and isinstance(claim_name, str)
            and bool(claim_name)
            and execution_name == claim_name
        )
    else:
        valid = False
    if not valid:
        raise OperatorStop("claim_execution_phase_pair_invalid")


def _validate_completed_state(state: dict, target: Target) -> None:
    _validate_existing_state(state, target)
    claim = state.get("claim") or {}
    execution = state.get("execution") or {}
    if (
        claim.get("state") != "managed"
        or execution.get("state") != "succeeded"
        or claim.get("generation") != execution.get("generation")
        or claim.get("cloudstack_vm_ref") != execution.get("id")
        or execution.get("cloudstack_vm_ref") != execution.get("id")
        or not isinstance(claim.get("cloudstack_instance_name"), str)
        or not claim.get("cloudstack_instance_name")
        or claim.get("cloudstack_instance_name")
        != execution.get("cloudstack_instance_name")
        or not isinstance(execution.get("deploy_job_id"), str)
        or not execution.get("deploy_job_id")
        or not isinstance(execution.get("start_job_id"), str)
        or not execution.get("start_job_id")
        or execution.get("cleanup_job_id") is not None
        or execution.get("error_code") is not None
        or execution.get("worker_lease_present")
        or claim.get("operation_lease_present")
    ):
        raise OperatorStop("completed_identity_or_execution_attestation_failed")


def _recover_missing_bind(
    target: Target,
    state: dict,
    client: BoundedCloudStackClient,
    write_guard: Callable[[], None],
) -> bool:
    """Bind an exact already-running VM when its callback was lost."""

    claim_state = state.get("claim") or {}
    execution_state = state.get("execution") or {}
    if not (
        claim_state.get("state") == "reserved"
        and claim_state.get("cloudstack_vm_ref") is None
        and claim_state.get("cloudstack_instance_name") is None
        and execution_state.get("state") == "verifying"
    ):
        return False

    session = get_session()
    try:
        claim = session.query(AdoptionClaim).filter_by(
            id=claim_state.get("id"),
            generation=claim_state.get("generation"),
        ).first()
        execution = session.query(AdoptionExecution).filter_by(
            id=execution_state.get("id"),
            claim_id=claim_state.get("id"),
            generation=claim_state.get("generation"),
        ).first()
        if claim is None or execution is None:
            raise OperatorStop("missing_bind_state_changed")
        if (
            claim.state != "reserved"
            or claim.cloudstack_vm_ref is not None
            or claim.cloudstack_instance_name is not None
            or execution.state != "verifying"
            or execution.cloudstack_vm_ref != execution.id
            or not execution.cloudstack_instance_name
        ):
            raise OperatorStop("missing_bind_state_changed")
        try:
            plan = json.loads(execution.plan_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise OperatorStop("missing_bind_plan_invalid") from exc

        matches = load_exact_external_vm(client, execution.id)
        if len(matches) != 1:
            raise OperatorStop("missing_bind_cloudstack_vm_not_unique")
        vm = matches[0]
        if (
            vm.get("state") != "Running"
            or vm.get("instancename") != execution.cloudstack_instance_name
            or not _vm_matches_plan(vm, execution, plan)
        ):
            raise OperatorStop("missing_bind_cloudstack_vm_mismatch")

        bind_claim(
            session,
            claim_id=claim.id,
            generation=claim.generation,
            proxmox_cluster=target.cluster,
            proxmox_node=claim.proxmox_node,
            proxmox_vmid=target.vmid,
            manifest_sha256=target.manifest_sha256,
            cloudstack_vm_ref=execution.id,
            cloudstack_instance_name=execution.cloudstack_instance_name,
            write_guard=write_guard,
        )
        return True
    finally:
        session.close()


def drive_execution(
    *,
    load_state: Callable[[], dict],
    reconcile: Callable[[str], object],
    query_job: Callable[[str], dict],
    deadline: float,
    poll_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Drive only durable safe states; ambiguous states get one observation."""

    observed_ambiguous = set()
    while True:
        if monotonic() >= deadline:
            raise OperatorStop("operator_timeout_safe_to_resume")
        state = load_state()
        claim = state.get("claim") or {}
        execution = state.get("execution") or {}
        execution_id = execution.get("id")
        current = execution.get("state")
        if current == "succeeded":
            if claim.get("state") != "managed":
                raise OperatorStop("execution_succeeded_without_managed_claim")
            return state
        if current not in _ACTIVE_RESUMABLE_STATES or not isinstance(execution_id, str):
            raise OperatorStop("execution_state_is_not_safely_resumable")
        if current == "deploy_submitted":
            wait_for_job(
                query_job,
                execution.get("deploy_job_id"),
                deadline=deadline,
                poll_seconds=poll_seconds,
                monotonic=monotonic,
                sleep=sleep,
            )
        elif current == "start_submitted":
            wait_for_job(
                query_job,
                execution.get("start_job_id"),
                deadline=deadline,
                poll_seconds=poll_seconds,
                monotonic=monotonic,
                sleep=sleep,
            )
        elif current in _AMBIGUOUS_STATES:
            if current in observed_ambiguous:
                raise OperatorStop(f"ambiguous_{current}_requires_operator_recovery")
            observed_ambiguous.add(current)
        reconcile(execution_id)
        updated = load_state().get("execution") or {}
        if current in _AMBIGUOUS_STATES and updated.get("state") == current:
            raise OperatorStop(f"ambiguous_{current}_requires_operator_recovery")
        if updated.get("state") == current:
            raise OperatorStop(f"reconciliation_made_no_progress_from_{current}")


def run_one(
    target: Target,
    *,
    timeout_seconds: int,
    poll_seconds: float,
    client_factory: Callable[[], object] = lambda: CloudStackClient(app_main.settings.cloudstack),
    live_catalog_loader: Callable[[str], dict] = _load_live_catalog,
) -> dict:
    settings = app_main.settings
    runtime = {
        "executor_disabled": settings.adoption_executor_enabled is False,
        "auto_reconcile_disabled": settings.auto_reconcile is False,
        "auto_reconcile_nics_disabled": settings.auto_reconcile_nics is False,
        "registry_enabled": settings.adoption_registry_enabled is True,
        "policy_enabled": settings.adoption_policy.enabled is True,
    }
    if not all(runtime.values()):
        raise OperatorStop("deployed_runtime_safety_gate_failed")

    init_db(settings.database_url)
    live_catalog = live_catalog_loader(settings.api_auth_token)
    _validate_live_runtime(live_catalog)
    initial = _load_target_state(target)
    if initial["claim"] is not None:
        _validate_existing_state(initial, target)
        if initial["execution"] is None:
            _validate_new_candidate(live_catalog, target, executor_enabled=False)
    else:
        _validate_new_candidate(live_catalog, target, executor_enabled=False)

    authority_lease_seconds = timeout_seconds + 300
    authority_owner = acquire_write_authority(
        mode="operator",
        target=target.proxmox_id,
        lease_seconds=authority_lease_seconds,
    )
    if authority_owner is None:
        raise OperatorStop("write_authority_is_held_by_another_process")

    completed = False
    authority_context_token = None
    try:
        def renew_operator_authority() -> None:
            renew_write_authority(
                owner_id=authority_owner,
                mode="operator",
                target=target.proxmox_id,
                lease_seconds=authority_lease_seconds,
            )

        authority_context_token = app_main.push_adoption_write_guard(
            renew_operator_authority
        )
        allow_deploy, allow_start = _starting_permissions(initial)
        client = BoundedCloudStackClient(
            client_factory(),
            allow_deploy=allow_deploy,
            allow_start=allow_start,
            authority_guard=renew_operator_authority,
        )
        original_engine = app_main.engine
        if original_engine is not None:
            raise OperatorStop("operator_process_engine_must_start_uninitialized")
        app_main.engine = SimpleNamespace(
            cs_client=client,
            _inventory_collection_ready=True,
            _nic_collection_ready=True,
        )
        settings.adoption_executor_enabled = True
        try:
            state = _load_target_state(target)
            if state["claim"] is None:
                full_catalog = app_main.list_adoption_candidates()
                _validate_new_candidate(full_catalog, target, executor_enabled=True)
                prewrite_catalog = live_catalog_loader(settings.api_auth_token)
                _validate_live_runtime(prewrite_catalog)
                _validate_new_candidate(prewrite_catalog, target, executor_enabled=False)
                renew_write_authority(
                    owner_id=authority_owner,
                    mode="operator",
                    target=target.proxmox_id,
                    lease_seconds=authority_lease_seconds,
                )
                app_main.create_adoption_claim(
                    app_main.ReserveAdoptionClaimRequest(
                        proxmox_id=target.proxmox_id,
                        manifest_sha256=target.manifest_sha256,
                        network_ip_overrides=_network_ip_override_requests(target),
                    ),
                    None,
                )
                state = _load_target_state(target)
            _validate_existing_state(state, target)
            if state["execution"] is None:
                prewrite_catalog = live_catalog_loader(settings.api_auth_token)
                _validate_live_runtime(prewrite_catalog)
                _validate_new_candidate(prewrite_catalog, target, executor_enabled=False)
                claim = state["claim"]
                renew_write_authority(
                    owner_id=authority_owner,
                    mode="operator",
                    target=target.proxmox_id,
                    lease_seconds=authority_lease_seconds,
                )
                app_main._execute_adoption_claim_under_authority(
                    claim["id"],
                    app_main.ExecuteAdoptionClaimRequest(
                        generation=claim["generation"],
                        network_ip_overrides=_network_ip_override_requests(target),
                    ),
                    renew_operator_authority,
                )
            else:
                _validate_live_runtime(live_catalog_loader(settings.api_auth_token))
            deadline = time.monotonic() + timeout_seconds

            def load_validated_state() -> dict:
                assert_write_authority(owner_id=authority_owner, mode="operator")
                current = _load_target_state(target)
                if _recover_missing_bind(
                    target,
                    current,
                    client,
                    renew_operator_authority,
                ):
                    current = _load_target_state(target)
                _validate_existing_state(current, target)
                return current

            def reconcile_with_authority(execution_id: str):
                renew_write_authority(
                    owner_id=authority_owner,
                    mode="operator",
                    target=target.proxmox_id,
                    lease_seconds=authority_lease_seconds,
                )
                return app_main._reconcile_adoption_execution_under_authority(
                    execution_id,
                    renew_operator_authority,
                )

            final = drive_execution(
                load_state=load_validated_state,
                reconcile=reconcile_with_authority,
                query_job=client.query_async_job,
                deadline=deadline,
                poll_seconds=poll_seconds,
            )
            _validate_completed_state(final, target)
        finally:
            settings.adoption_executor_enabled = False
            app_main.engine = original_engine

        assert_write_authority(owner_id=authority_owner, mode="operator")
        _validate_live_runtime(live_catalog_loader(settings.api_auth_token))
        result = {
            "target": {
                "proxmox_id": target.proxmox_id,
                "manifest_sha256": target.manifest_sha256,
            },
            "claim": final["claim"],
            "execution": final["execution"],
            "calls_this_run": client.public_counts(),
            "deployed_runtime_restored_disabled": (
                settings.adoption_executor_enabled is False
            ),
        }
        completed = True
    finally:
        if authority_context_token is not None:
            app_main.pop_adoption_write_guard(authority_context_token)
        released = release_write_authority(
            owner_id=authority_owner,
            mode="operator",
        )

    if completed and not released:
        raise OperatorStop("write_authority_release_failed")
    result["write_authority_released"] = True
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Adopt exactly one preflighted Proxmox QEMU guest, resumably."
    )
    parser.add_argument("--proxmox-id", required=True, help="Canonical cluster:VMID")
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument(
        "--network-ip",
        action="append",
        default=[],
        metavar="netN=IPv4",
        help="Exact IPv4 for an unresolved NIC; repeat once per unresolved NIC",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Overall wait timeout in seconds (30-3600; default: 900)",
    )
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 30 <= args.timeout <= 3600:
            raise OperatorStop("timeout_out_of_range")
        if not 0.5 <= args.poll_seconds <= 30:
            raise OperatorStop("poll_seconds_out_of_range")
        target = parse_target(
            args.proxmox_id,
            args.manifest_sha256,
            args.network_ip,
        )
        result = run_one(
            target,
            timeout_seconds=args.timeout,
            poll_seconds=args.poll_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - sanitize the CLI failure boundary
        code = exc.args[0] if isinstance(exc, OperatorStop) and exc.args else type(exc).__name__
        print(json.dumps({"status": "stopped", "code": code}, sort_keys=True))
        return 1
    print(json.dumps({"status": "succeeded", **result}, indent=2, sort_keys=True))
    print("ONE_VM_ADOPTION_COMPLETE=PASS")
    print("SCHEDULED_EXECUTOR_AND_AUTO_RECONCILIATION_REMAIN_DISABLED=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
