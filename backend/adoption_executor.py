import hashlib
import ipaddress
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from adoption import custom_root_disk_size_gib
from adoption_authority import AuthorityConflict
from adoption_registry import ClaimConflict, ClaimInvalid
from database import AdoptionClaim, AdoptionExecution, get_session
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError, OperationalError

log = logging.getLogger(__name__)

# Plans persisted before customized CPU speed became explicit use the estate's
# established CloudStack accounting value. This does not cap Proxmox CPU.
LEGACY_CUSTOMIZED_CPU_SPEED_MHZ = 1200

ACTIVE_STATES = {
    "planned",
    "deploy_submitting",
    "deploy_submitted",
    "submission_unknown",
    "deploy_succeeded",
    "start_submitting",
    "start_submitted",
    "start_unknown",
    "verifying",
    "cleanup_submitting",
    "cleanup_authorized",
    "cleanup_submitted",
}
TERMINAL_STATES = {"succeeded", "failed", "cleanup_required", "rolled_back"}


class ExecutionConflict(Exception):
    pass


class ExecutionInvalid(Exception):
    pass


def _customized_cpu_speed_mhz(
    deployment: dict,
    *,
    allow_legacy_missing: bool,
) -> int:
    if "cpu_speed_mhz" not in deployment:
        if allow_legacy_missing:
            return LEGACY_CUSTOMIZED_CPU_SPEED_MHZ
        raise ExecutionInvalid("invalid customized CPU speed")
    cpu_speed_mhz = deployment["cpu_speed_mhz"]
    if (
        isinstance(cpu_speed_mhz, bool)
        or not isinstance(cpu_speed_mhz, int)
        or not 1 <= cpu_speed_mhz <= 2147483647
    ):
        raise ExecutionInvalid("invalid customized CPU speed")
    return cpu_speed_mhz


def _cloudstack_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    return None


_RETRYABLE_MYSQL_OPERATIONAL_CODES = {1020, 1205, 1213}


def _is_retryable_operational_error(exc: OperationalError) -> bool:
    args = getattr(getattr(exc, "orig", None), "args", None)
    if not args:
        return False
    try:
        return int(args[0]) in _RETRYABLE_MYSQL_OPERATIONAL_CODES
    except (IndexError, TypeError, ValueError):
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ExecutionInvalid("execution identity must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ExecutionInvalid("execution identity must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ExecutionInvalid("execution identity must be a canonical UUID")
    return canonical


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ExecutionInvalid(f"invalid {field}")
    return value


def _manifest_network_device_id(network: object) -> int | None:
    if not isinstance(network, dict):
        return None
    device = network.get("device")
    if isinstance(device, str) and (match := re.fullmatch(r"net(0|[1-9][0-9]*)", device)):
        return int(match.group(1))
    device_id = network.get("device_id")
    if isinstance(device_id, int) and not isinstance(device_id, bool) and device_id >= 0:
        return device_id
    return None


def _validated_execution_time_ip_overrides(
    plan: dict,
    manifest_networks: list,
) -> dict[int, str]:
    """Return only canonical bindings for explicitly unresolved manifest NICs."""

    required: set[int] = set()
    seen_manifest_devices: set[int] = set()
    for network in manifest_networks:
        device_id = _manifest_network_device_id(network)
        if device_id is None or device_id in seen_manifest_devices:
            raise ExecutionInvalid("claim manifest network device is invalid")
        seen_manifest_devices.add(device_id)
        ip_allocation = network.get("ip_allocation", "cloudstack")
        if ip_allocation not in {"cloudstack", "external"}:
            raise ExecutionInvalid("claim manifest network IP allocation is invalid")
        if network.get("ip") is None:
            if ip_allocation == "external":
                if network.get("ip_override_required") is not True:
                    raise ExecutionInvalid("unresolved manifest network lacks override flag")
                required.add(device_id)
            elif network.get("ip_override_required") is not None:
                raise ExecutionInvalid("CloudStack DHCP network cannot require an override")
        elif network.get("ip_override_required") is not None:
            raise ExecutionInvalid("known manifest network cannot require an override")

    raw = plan.get("execution_time_ip_overrides")
    if raw is None and "execution_time_ip_overrides" not in plan:
        if required:
            raise ExecutionInvalid("execution-time IP overrides are required")
        return {}
    if not isinstance(raw, list):
        raise ExecutionInvalid("execution-time IP overrides are invalid")
    result: dict[int, str] = {}
    canonical: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"device_id", "ip"}:
            raise ExecutionInvalid("execution-time IP override schema is invalid")
        device_id = item.get("device_id")
        ip = item.get("ip")
        if isinstance(device_id, bool) or not isinstance(device_id, int) or device_id < 0:
            raise ExecutionInvalid("execution-time IP override device is invalid")
        if not isinstance(ip, str):
            raise ExecutionInvalid("execution-time IP override is invalid")
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise ExecutionInvalid("execution-time IP override is invalid") from exc
        if parsed.version != 4 or str(parsed) != ip or device_id in result:
            raise ExecutionInvalid("execution-time IP override is invalid")
        result[device_id] = ip
        canonical.append({"device_id": device_id, "ip": ip})
    if raw != sorted(canonical, key=lambda item: item["device_id"]):
        raise ExecutionInvalid("execution-time IP overrides are not canonically sorted")
    if set(result) != required:
        raise ExecutionInvalid("execution-time IP overrides do not exactly cover manifest NICs")
    return result


def validate_execution_plan(plan: dict, claim: AdoptionClaim) -> dict:
    """Validate and canonicalize the non-secret, frozen deployment plan."""

    if not isinstance(plan, dict):
        raise ExecutionInvalid("execution plan must be an object")
    identity = plan.get("claim")
    deployment = plan.get("deployment")
    if not isinstance(identity, dict) or not isinstance(deployment, dict):
        raise ExecutionInvalid("execution plan is incomplete")
    if identity != {
        "id": claim.id,
        "generation": claim.generation,
        "manifest_sha256": claim.manifest_sha256,
    }:
        raise ExecutionInvalid("execution plan claim identity mismatch")

    for field in (
        "zone_id",
        "cluster_id",
        "host_id",
        "template_id",
        "service_offering_id",
        "account",
        "domain_id",
        "name",
        "display_name",
    ):
        _required_string(deployment.get(field), field)
    for key in (
        "zone_id",
        "cluster_id",
        "host_id",
        "template_id",
        "service_offering_id",
        "domain_id",
    ):
        _canonical_uuid(deployment.get(key))
    if not isinstance(deployment.get("service_offering_customized"), bool):
        raise ExecutionInvalid("service offering type is not explicit")
    if deployment["service_offering_customized"]:
        _customized_cpu_speed_mhz(deployment, allow_legacy_missing=False)
    elif deployment.get("cpu_speed_mhz") is not None:
        raise ExecutionInvalid("static offering cannot override CPU speed")
    root_disk_size_gib = _custom_root_disk_size_gib(
        deployment,
        allow_legacy_missing=False,
    )
    if deployment["account"] != "admin" or deployment.get("project_id") is not None:
        raise ExecutionInvalid("executor owner must be ROOT admin without a project")
    if not isinstance(deployment.get("cpus"), int) or deployment["cpus"] <= 0:
        raise ExecutionInvalid("invalid CPU count")
    if not isinstance(deployment.get("memory_mib"), int) or deployment["memory_mib"] <= 0:
        raise ExecutionInvalid("invalid memory size")

    networks = deployment.get("networks")
    details = deployment.get("external_details")
    if not isinstance(networks, list) or not networks:
        raise ExecutionInvalid("execution plan requires networks")
    if not isinstance(details, dict) or not details:
        raise ExecutionInvalid("execution plan requires external details")
    expected_detail_keys = {
        "adopt_existing",
        "adopt_claim_id",
        "adopt_claim_generation",
        "adopt_manifest_sha256",
        "adopt_manifest_json",
        "proxmox_cluster",
        "proxmox_vmid",
    }
    if set(details) != expected_detail_keys or not all(
        isinstance(value, str) for value in details.values()
    ):
        raise ExecutionInvalid("execution plan has unexpected external details")
    if details.get("adopt_existing") != "true":
        raise ExecutionInvalid("execution plan is not an adoption")
    if details.get("adopt_claim_id") != claim.id:
        raise ExecutionInvalid("execution plan claim detail mismatch")
    if details.get("adopt_claim_generation") != str(claim.generation):
        raise ExecutionInvalid("execution plan generation detail mismatch")
    if details.get("adopt_manifest_sha256") != claim.manifest_sha256:
        raise ExecutionInvalid("execution plan manifest detail mismatch")
    if details.get("adopt_manifest_json") != claim.manifest_json:
        raise ExecutionInvalid("execution plan manifest mismatch")
    if details.get("proxmox_cluster") != claim.proxmox_cluster:
        raise ExecutionInvalid("execution plan Proxmox cluster mismatch")
    if details.get("proxmox_vmid") != str(claim.proxmox_vmid):
        raise ExecutionInvalid("execution plan Proxmox VMID mismatch")
    if any("secret" in key.lower() or "token" in key.lower() or "nonce" in key.lower() for key in details):
        raise ExecutionInvalid("execution plan contains a forbidden credential field")
    if root_disk_size_gib is not None:
        try:
            claim_manifest = json.loads(claim.manifest_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ExecutionInvalid("claim manifest is invalid") from exc
        storage = (
            claim_manifest.get("storage")
            if isinstance(claim_manifest, dict)
            else None
        )
        if not isinstance(storage, list):
            raise ExecutionInvalid("claim manifest storage is invalid")
        expected_root_disk_size_gib = custom_root_disk_size_gib(storage)
        if expected_root_disk_size_gib != root_disk_size_gib:
            raise ExecutionInvalid(
                "custom root disk size does not match claim manifest"
            )

    try:
        claim_manifest = json.loads(claim.manifest_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecutionInvalid("claim manifest is invalid") from exc
    manifest_networks = (
        claim_manifest.get("networks")
        if isinstance(claim_manifest, dict)
        else None
    )
    if not isinstance(manifest_networks, list) or len(manifest_networks) != len(networks):
        raise ExecutionInvalid("claim manifest networks do not match execution plan")

    overrides = _validated_execution_time_ip_overrides(plan, manifest_networks)
    seen_networks = set()
    seen_macs = set()
    for index, network in enumerate(networks):
        if not isinstance(network, dict):
            raise ExecutionInvalid("invalid network plan")
        if network.get("device_id") != index:
            raise ExecutionInvalid("network devices must be contiguous and ordered")
        network_id = _canonical_uuid(network.get("network_id"))
        mac = _required_string(network.get("mac"), "network MAC").upper()
        ip_allocation = network.get("ip_allocation", "cloudstack")
        if ip_allocation not in {"cloudstack", "external"}:
            raise ExecutionInvalid("invalid network IP allocation mode")
        raw_ip = network.get("ip")
        if raw_ip is None:
            if ip_allocation != "cloudstack":
                raise ExecutionInvalid("external network IP is missing")
            ip = None
        else:
            ip = _required_string(raw_ip, "network IP")
        if not re.fullmatch(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", mac):
            raise ExecutionInvalid("invalid network MAC")
        if ip is not None:
            try:
                parsed_ip = ipaddress.ip_address(ip)
            except ValueError as exc:
                raise ExecutionInvalid("invalid network IP") from exc
            if parsed_ip.version != 4:
                raise ExecutionInvalid("only IPv4 adoption networks are supported")
        manifest_network = manifest_networks[index]
        manifest_device_id = _manifest_network_device_id(manifest_network)
        manifest_ip = manifest_network.get("ip") if isinstance(manifest_network, dict) else None
        manifest_requires_override = (
            manifest_network.get("ip_override_required")
            if isinstance(manifest_network, dict)
            else None
        )
        if not isinstance(manifest_network, dict) or (
            manifest_device_id != index
            or manifest_network.get("cloudstack_network_id") != network_id
            or str(manifest_network.get("mac", "")).upper() != mac
            or (manifest_ip is not None and manifest_ip != ip)
            or (
                manifest_ip is None
                and ip_allocation == "external"
                and (manifest_requires_override is not True or overrides.get(index) != ip)
            )
            or (
                manifest_ip is None
                and ip_allocation == "cloudstack"
                and (
                    manifest_requires_override is not None
                    or manifest_network.get("ip_allocation", "cloudstack") != "cloudstack"
                    or ip is not None
                )
            )
        ):
            raise ExecutionInvalid(
                "execution network identity does not match claim manifest"
            )
        if network_id in seen_networks or mac in seen_macs:
            raise ExecutionInvalid("duplicate network identity")
        seen_networks.add(network_id)
        seen_macs.add(mac)
        network["network_id"] = network_id
        network["mac"] = mac
        network["ip"] = ip
        network["ip_allocation"] = ip_allocation

    return json.loads(_canonical_json(plan))


def create_execution(
    session,
    *,
    claim_id: str,
    generation: int,
    plan: dict,
    write_guard: Callable[[], None],
) -> AdoptionExecution:
    claim_id = _canonical_uuid(claim_id)
    claim = session.query(AdoptionClaim).filter_by(id=claim_id).first()
    if claim is None:
        raise ExecutionInvalid("adoption claim not found")
    canonical_plan = validate_execution_plan(plan, claim)
    plan_json = _canonical_json(canonical_plan)
    plan_sha256 = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
    execution_id = str(uuid.uuid4())

    for _attempt in range(3):
        session.expire_all()
        claim = (
            session.query(AdoptionClaim)
            .filter_by(id=claim_id)
            .execution_options(populate_existing=True)
            .first()
        )
        if claim is None:
            raise ExecutionInvalid("adoption claim not found")
        if claim.generation != generation:
            raise ExecutionConflict("adoption claim generation changed")
        if claim.state != "reserved":
            raise ExecutionConflict("only a reserved claim can be executed")

        existing = session.query(AdoptionExecution).filter_by(
            claim_id=claim.id,
            generation=generation,
        ).first()
        if existing is not None:
            if existing.plan_sha256 == plan_sha256:
                return existing
            raise ExecutionConflict("claim already has a different execution")

        execution = AdoptionExecution(
            id=execution_id,
            claim_id=claim.id,
            generation=claim.generation,
            plan_sha256=plan_sha256,
            plan_json=plan_json,
            state="planned",
        )
        write_guard()
        session.add(execution)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            winner = session.query(AdoptionExecution).filter_by(
                claim_id=claim.id,
                generation=generation,
            ).first()
            if winner and winner.plan_sha256 == plan_sha256:
                return winner
            raise ExecutionConflict("claim execution was created concurrently") from exc
        except OperationalError as exc:
            session.rollback()
            if not _is_retryable_operational_error(exc):
                raise
            winner = session.query(AdoptionExecution).filter_by(
                claim_id=claim.id,
                generation=generation,
            ).first()
            if winner is not None:
                if winner.plan_sha256 == plan_sha256:
                    return winner
                raise ExecutionConflict("claim already has a different execution") from exc
            continue
        session.refresh(execution)
        return execution

    raise ExecutionConflict("claim execution retry limit reached")


def public_execution(execution: AdoptionExecution) -> dict:
    return {
        "id": execution.id,
        "claim_id": execution.claim_id,
        "generation": execution.generation,
        "plan_sha256": execution.plan_sha256,
        "state": execution.state,
        "deploy_job_id": execution.deploy_job_id,
        "start_job_id": execution.start_job_id,
        "cleanup_job_id": execution.cleanup_job_id,
        "cloudstack_vm_ref": execution.cloudstack_vm_ref,
        "error_code": execution.error_code,
        "attempt_count": execution.attempt_count,
        "created_at": execution.created_at.isoformat() if execution.created_at else None,
        "updated_at": execution.updated_at.isoformat() if execution.updated_at else None,
    }


def _custom_root_disk_size_gib(
    deployment: dict,
    *,
    allow_legacy_missing: bool,
) -> int | None:
    required = deployment.get("root_disk_size_customized")
    if required is None and "root_disk_size_customized" not in deployment:
        if not allow_legacy_missing:
            raise ExecutionInvalid("root disk size contract is missing")
        if deployment.get("service_offering_customized") is False:
            return None
        if deployment.get("service_offering_customized") is not True:
            raise ExecutionInvalid("legacy root disk contract is invalid")
        external_details = deployment.get("external_details")
        if not isinstance(external_details, dict):
            raise ExecutionInvalid("legacy root disk manifest is missing")
        manifest_json = external_details.get("adopt_manifest_json")
        if not isinstance(manifest_json, str):
            raise ExecutionInvalid("legacy root disk manifest is missing")
        try:
            manifest = json.loads(manifest_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ExecutionInvalid("legacy root disk manifest is invalid") from exc
        storage = manifest.get("storage") if isinstance(manifest, dict) else None
        if not isinstance(storage, list):
            raise ExecutionInvalid("legacy root disk storage is invalid")
        size_gib = custom_root_disk_size_gib(storage)
        if size_gib is None:
            raise ExecutionInvalid("legacy root disk size is ambiguous")
        return size_gib
    if not isinstance(required, bool):
        raise ExecutionInvalid("root disk size contract is invalid")
    size_gib = deployment.get("root_disk_size_gib")
    if required is False:
        if size_gib is not None:
            raise ExecutionInvalid("fixed root disk has a custom size")
        return None
    if (
        isinstance(size_gib, bool)
        or not isinstance(size_gib, int)
        or not 1 <= size_gib <= 2147483647
    ):
        raise ExecutionInvalid("custom root disk size is invalid")
    return size_gib


def _deploy_params(execution: AdoptionExecution, plan: dict) -> dict:
    deployment = plan["deployment"]
    params = {
        "customid": execution.id,
        "zoneid": deployment["zone_id"],
        "serviceofferingid": deployment["service_offering_id"],
        "templateid": deployment["template_id"],
        "hostid": deployment["host_id"],
        "account": deployment["account"],
        "domainid": deployment["domain_id"],
        "name": deployment["name"],
        "displayname": deployment["display_name"],
        "startvm": "false",
    }
    if deployment["service_offering_customized"]:
        params["details[0].cpuNumber"] = str(deployment["cpus"])
        params["details[0].cpuSpeed"] = str(
            _customized_cpu_speed_mhz(deployment, allow_legacy_missing=True)
        )
        params["details[0].memory"] = str(deployment["memory_mib"])
    root_disk_size_gib = _custom_root_disk_size_gib(
        deployment,
        allow_legacy_missing=True,
    )
    if root_disk_size_gib is not None:
        params["rootdisksize"] = str(root_disk_size_gib)
    for index, network in enumerate(deployment["networks"]):
        prefix = f"iptonetworklist[{index}]"
        params[f"{prefix}.networkid"] = network["network_id"]
        if (
            network.get("ip_allocation", "cloudstack") == "cloudstack"
            and network.get("ip") is not None
        ):
            params[f"{prefix}.ip"] = network["ip"]
        params[f"{prefix}.mac"] = network["mac"]
    for key, value in sorted(deployment["external_details"].items()):
        params[f"externaldetails[0].{key}"] = value
    if "execution_time_ip_overrides" in plan:
        params["externaldetails[0].adopt_execution_plan_sha256"] = execution.plan_sha256
        params["externaldetails[0].adopt_ip_overrides_json"] = json.dumps(
            plan["execution_time_ip_overrides"], separators=(",", ":")
        )
    return params


def _vm_matches_plan(vm: dict, execution: AdoptionExecution, plan: dict) -> bool:
    deployment = plan["deployment"]
    if not isinstance(vm, dict):
        return False
    try:
        expected_cpu_speed = (
            _customized_cpu_speed_mhz(deployment, allow_legacy_missing=True)
            if deployment["service_offering_customized"]
            else None
        )
    except ExecutionInvalid:
        return False
    vm_cpus = _cloudstack_positive_int(vm.get("cpunumber"))
    vm_memory = _cloudstack_positive_int(vm.get("memory"))
    vm_cpu_speed = (
        _cloudstack_positive_int(vm.get("cpuspeed"))
        if deployment["service_offering_customized"]
        else None
    )
    host_id_matches = (
        "hostid" not in vm and vm.get("state") == "Stopped"
    ) or vm.get("hostid") == deployment["host_id"]
    if vm_cpus is None or vm_memory is None or (
        deployment["service_offering_customized"] and vm_cpu_speed is None
    ):
        return False
    if any(
        (
            vm.get("id") != execution.id,
            vm.get("hypervisor") != "External",
            not host_id_matches,
            vm.get("serviceofferingid") != deployment["service_offering_id"],
            vm.get("templateid") != deployment["template_id"],
            vm.get("account") != "admin",
            vm.get("domainid") != deployment["domain_id"],
            vm.get("projectid") not in (None, ""),
            vm.get("name") != deployment["name"],
            vm.get("displayname") != deployment["display_name"],
            not isinstance(vm.get("instancename"), str),
            not vm.get("instancename"),
            vm_cpus != deployment["cpus"],
            deployment["service_offering_customized"]
            and vm_cpu_speed != expected_cpu_speed,
            vm_memory != deployment["memory_mib"],
        )
    ):
        return False
    details = vm.get("details")
    if not isinstance(details, dict):
        return False
    expected_details = dict(deployment["external_details"])
    if "execution_time_ip_overrides" in plan:
        expected_details.update({
            "adopt_execution_plan_sha256": execution.plan_sha256,
            "adopt_ip_overrides_json": json.dumps(
                plan["execution_time_ip_overrides"], separators=(",", ":")
            ),
        })
    for key, value in expected_details.items():
        observed = [
            details[candidate]
            for candidate in (key, f"external.{key}", f"External:{key}")
            if candidate in details
        ]
        if not observed or any(item != value for item in observed):
            return False
    observed_nics = vm.get("nic") or []
    if not isinstance(observed_nics, list) or not all(
        isinstance(item, dict) for item in observed_nics
    ):
        return False
    expected_nics = {
        (
            item["device_id"],
            item["network_id"],
            item["mac"].upper(),
        ): item
        for item in deployment["networks"]
    }
    if len(expected_nics) != len(deployment["networks"]):
        return False
    actual_nics = {}
    for item in observed_nics:
        raw_device_id = item.get("deviceid")
        if isinstance(raw_device_id, bool):
            return False
        if isinstance(raw_device_id, int):
            device_id = raw_device_id if raw_device_id >= 0 else None
        elif isinstance(raw_device_id, str) and re.fullmatch(
            r"0|[1-9][0-9]*", raw_device_id
        ):
            device_id = int(raw_device_id)
        else:
            device_id = None
        if device_id is None:
            return False
        identity = (
            device_id,
            item.get("networkid"),
            str(item.get("macaddress") or "").upper(),
        )
        if identity in actual_nics:
            return False
        actual_nics[identity] = item.get("ipaddress")
    if set(actual_nics) != set(expected_nics):
        return False
    for identity, expected in expected_nics.items():
        actual_ip = actual_nics[identity]
        if expected.get("ip_allocation", "cloudstack") == "cloudstack":
            expected_ip = expected.get("ip")
            if expected_ip is None:
                try:
                    parsed = ipaddress.ip_address(actual_ip)
                except (TypeError, ValueError):
                    return False
                if parsed.version != 4 or str(parsed) != actual_ip:
                    return False
            elif actual_ip != expected_ip:
                return False
        elif actual_ip not in (None, "", expected["ip"]):
            return False
    return True


def _job_status(result: dict) -> int:
    value = result.get("jobstatus")
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2):
        raise ExecutionInvalid("invalid CloudStack async job status")
    return value


def load_exact_external_vm(client, vm_id: str) -> list[dict]:
    # CloudStack returns HTTP 431 when ``id`` is a valid UUID that does not yet
    # exist, so an exact-ID API query cannot represent the required pre-deploy
    # absence check. Restrict the server-side inventory to External VMs, then
    # retain only the deterministic custom UUID locally. Other API failures
    # still propagate and fail closed.
    result = client.list_virtual_machines(
        hypervisor="External",
        details="all",
        _max_pages=20,
        _deadline_monotonic=time.monotonic() + 120,
    )
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise ExecutionInvalid("invalid CloudStack VM response")
    return [item for item in result if item.get("id") == vm_id]


def _load_exact_vm(client, execution: AdoptionExecution) -> list[dict]:
    return load_exact_external_vm(client, execution.id)


def _save(execution: AdoptionExecution, *, state: str, error_code: str | None = None, **fields) -> None:
    execution.state = state
    execution.error_code = error_code
    execution.worker_lease_id = None
    execution.worker_lease_expires_at = None
    for key, value in fields.items():
        setattr(execution, key, value)


def _finalize_cleanup_release(
    execution_id: str,
    lease_id: str,
    *,
    write_guard: Callable[[], None],
) -> dict:
    """Atomically release an absent VM's claim and complete its exact leased execution."""

    for _attempt in range(3):
        session = get_session()
        try:
            session.expire_all()
            execution = (
                session.query(AdoptionExecution)
                .filter_by(id=execution_id)
                .execution_options(populate_existing=True)
                .first()
            )
            if execution is None:
                raise ExecutionInvalid("adoption execution not found")
            claim = (
                session.query(AdoptionClaim)
                .filter_by(id=execution.claim_id)
                .execution_options(populate_existing=True)
                .first()
            )
            if claim is None or claim.generation != execution.generation:
                raise ExecutionConflict("cleanup claim generation changed")
            if execution.state == "rolled_back" and claim.state == "released":
                return public_execution(execution)
            if (
                execution.state
                not in {"cleanup_submitting", "cleanup_authorized", "cleanup_submitted"}
                or execution.worker_lease_id != lease_id
                or _as_utc(execution.worker_lease_expires_at) is None
                or _as_utc(execution.worker_lease_expires_at) < _now()
                or claim.state not in {"reserved", "cleanup"}
                or claim.cloudstack_vm_ref is not None
                or claim.cloudstack_instance_name is not None
            ):
                raise ExecutionConflict("unbound claim cannot be released after cleanup")

            write_guard()
            try:
                claim_result = session.execute(
                    update(AdoptionClaim)
                    .where(
                        AdoptionClaim.id == claim.id,
                        AdoptionClaim.generation == execution.generation,
                        AdoptionClaim.state.in_({"reserved", "cleanup"}),
                        AdoptionClaim.cloudstack_vm_ref.is_(None),
                        AdoptionClaim.cloudstack_instance_name.is_(None),
                    )
                    .values(state="released", updated_at=_now())
                )
                execution_result = session.execute(
                    update(AdoptionExecution)
                    .where(
                        AdoptionExecution.id == execution.id,
                        AdoptionExecution.claim_id == claim.id,
                        AdoptionExecution.generation == execution.generation,
                        AdoptionExecution.state.in_(
                            {"cleanup_submitting", "cleanup_authorized", "cleanup_submitted"}
                        ),
                        AdoptionExecution.worker_lease_id == lease_id,
                    )
                    .values(
                        state="rolled_back",
                        error_code=None,
                        worker_lease_id=None,
                        worker_lease_expires_at=None,
                        updated_at=_now(),
                    )
                )
                if claim_result.rowcount == 1 and execution_result.rowcount == 1:
                    session.commit()
                else:
                    session.rollback()
            except OperationalError as exc:
                session.rollback()
                if not _is_retryable_operational_error(exc):
                    raise
            else:
                if claim_result.rowcount == 1 and execution_result.rowcount == 1:
                    session.expire_all()
                    completed = (
                        session.query(AdoptionExecution)
                        .filter_by(id=execution_id)
                        .execution_options(populate_existing=True)
                        .one()
                    )
                    return public_execution(completed)
                else:
                    raise ExecutionConflict(
                        "cleanup release lost a concurrent transition"
                    )
        finally:
            session.close()

    raise ExecutionConflict("cleanup release CAS limit reached")


def authorize_cleanup_delete(
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
) -> AdoptionExecution:
    """Authorize the extension's metadata-only delete during explicit rollback."""

    claim_id = _canonical_uuid(claim_id)
    cloudstack_vm_ref = _canonical_uuid(cloudstack_vm_ref)
    for _attempt in range(3):
        session.expire_all()
        execution = (
            session.query(AdoptionExecution)
            .filter_by(id=cloudstack_vm_ref)
            .execution_options(populate_existing=True)
            .first()
        )
        claim = (
            session.query(AdoptionClaim)
            .filter_by(id=claim_id)
            .execution_options(populate_existing=True)
            .first()
        )
        if execution is None or claim is None:
            raise ExecutionInvalid("cleanup identity not found")
        if (
            execution.claim_id != claim.id
            or execution.generation != generation
            or execution.state
            not in {"cleanup_submitting", "cleanup_authorized", "cleanup_submitted"}
            or execution.cloudstack_vm_ref != cloudstack_vm_ref
            or execution.cloudstack_instance_name != cloudstack_instance_name
            or claim.generation != generation
            or claim.proxmox_cluster != proxmox_cluster
            or claim.proxmox_node != proxmox_node
            or claim.proxmox_vmid != proxmox_vmid
            or claim.manifest_sha256 != manifest_sha256
            or claim.cloudstack_vm_ref is not None
            or claim.cloudstack_instance_name is not None
        ):
            raise ExecutionConflict("cleanup identity or state mismatch")
        if claim.state == "cleanup":
            return execution
        if claim.state != "reserved":
            raise ExecutionConflict("cleanup claim is no longer unbound")

        write_guard()
        try:
            execution_result = session.execute(
                update(AdoptionExecution)
                .where(
                    AdoptionExecution.id == execution.id,
                    AdoptionExecution.claim_id == claim.id,
                    AdoptionExecution.generation == generation,
                    AdoptionExecution.state.in_(
                        {"cleanup_submitting", "cleanup_submitted"}
                    ),
                    AdoptionExecution.cloudstack_vm_ref == cloudstack_vm_ref,
                    AdoptionExecution.cloudstack_instance_name
                    == cloudstack_instance_name,
                )
                .values(state="cleanup_authorized", updated_at=_now())
            )
            claim_result = session.execute(
                update(AdoptionClaim)
                .where(
                    AdoptionClaim.id == claim.id,
                    AdoptionClaim.generation == generation,
                    AdoptionClaim.state == "reserved",
                    AdoptionClaim.proxmox_cluster == proxmox_cluster,
                    AdoptionClaim.proxmox_node == proxmox_node,
                    AdoptionClaim.proxmox_vmid == proxmox_vmid,
                    AdoptionClaim.manifest_sha256 == manifest_sha256,
                    AdoptionClaim.cloudstack_vm_ref.is_(None),
                    AdoptionClaim.cloudstack_instance_name.is_(None),
                )
                .values(state="cleanup", updated_at=_now())
            )
            if execution_result.rowcount == 1 and claim_result.rowcount == 1:
                session.commit()
            else:
                session.rollback()
        except OperationalError as exc:
            session.rollback()
            if not _is_retryable_operational_error(exc):
                raise
        else:
            if execution_result.rowcount == 1 and claim_result.rowcount == 1:
                session.expire_all()
                return session.query(AdoptionExecution).filter_by(id=execution.id).one()

    raise ExecutionConflict("cleanup authorization lost a concurrent transition")


def request_execution_retry(
    execution_id: str,
    *,
    client,
    write_guard: Callable[[], None],
) -> dict:
    """Explicitly retry an ambiguous deploy/start after exact live revalidation."""

    execution_id = _canonical_uuid(execution_id)
    session = get_session()
    try:
        execution = session.query(AdoptionExecution).filter_by(id=execution_id).first()
        if execution is None:
            raise ExecutionInvalid("adoption execution not found")
        if execution.state not in {"submission_unknown", "start_unknown"}:
            raise ExecutionConflict("execution is not awaiting an explicit retry")
        plan = json.loads(execution.plan_json)
        vms = _load_exact_vm(client, execution)
        claim = session.query(AdoptionClaim).filter_by(id=execution.claim_id).first()
        if claim is None or claim.generation != execution.generation:
            raise ExecutionConflict("execution claim generation changed")

        next_state: str
        if execution.state == "submission_unknown":
            if len(vms) > 1 or (vms and not _vm_matches_plan(vms[0], execution, plan)):
                raise ExecutionConflict("ambiguous deployment identity is not exact")
            if vms:
                if str(vms[0].get("state") or "").lower() != "stopped":
                    raise ExecutionConflict("ambiguous deployment is still in progress")
                next_state = "deploy_succeeded"
            else:
                next_state = "planned"
        else:
            if len(vms) != 1 or not _vm_matches_plan(vms[0], execution, plan):
                raise ExecutionConflict("ambiguous start VM identity is not exact")
            vm_state = str(vms[0].get("state") or "").lower()
            if vm_state == "running":
                next_state = "verifying"
            elif (
                vm_state == "stopped"
                and (
                    (
                        claim.state == "reserved"
                        and claim.cloudstack_vm_ref is None
                        and claim.cloudstack_instance_name is None
                    )
                    or _claim_is_bound(session, execution)
                )
            ):
                next_state = "deploy_succeeded"
            else:
                raise ExecutionConflict("ambiguous start cannot be retried safely")

        expected_state = execution.state
        for _attempt in range(3):
            write_guard()
            try:
                updated = (
                    session.query(AdoptionExecution)
                    .filter(
                        AdoptionExecution.id == execution.id,
                        AdoptionExecution.state == expected_state,
                        AdoptionExecution.worker_lease_id.is_(None),
                    )
                    .update(
                        {
                            AdoptionExecution.state: next_state,
                            AdoptionExecution.error_code: None,
                            AdoptionExecution.updated_at: _now(),
                        },
                        synchronize_session=False,
                    )
                )
                if updated == 1:
                    session.commit()
                else:
                    session.rollback()
            except OperationalError as exc:
                session.rollback()
                if not _is_retryable_operational_error(exc):
                    raise

            session.expire_all()
            current = (
                session.query(AdoptionExecution)
                .filter_by(id=execution_id)
                .execution_options(populate_existing=True)
                .one()
            )
            if current.state == next_state and current.worker_lease_id is None:
                return public_execution(current)
            if current.state == expected_state and current.worker_lease_id is None:
                continue
            raise ExecutionConflict("execution retry raced with another worker")
        raise ExecutionConflict("execution retry CAS limit reached")
    finally:
        session.close()


def request_execution_cleanup(
    execution_id: str,
    *,
    client,
    write_guard: Callable[[], None],
) -> dict:
    """Explicitly delete only exact stopped CloudStack metadata, never the guest."""

    execution_id = _canonical_uuid(execution_id)
    session = get_session()
    try:
        execution = session.query(AdoptionExecution).filter_by(id=execution_id).first()
        if execution is None:
            raise ExecutionInvalid("adoption execution not found")
        if execution.state != "cleanup_required":
            raise ExecutionConflict("execution does not require cleanup")
        claim = session.query(AdoptionClaim).filter_by(id=execution.claim_id).first()
        if (
            claim is None
            or claim.generation != execution.generation
            or claim.state not in {"reserved", "cleanup"}
            or claim.cloudstack_vm_ref is not None
            or claim.cloudstack_instance_name is not None
        ):
            raise ExecutionConflict("cleanup is allowed only before claim binding")
        plan = json.loads(execution.plan_json)
        vms = _load_exact_vm(client, execution)
        if len(vms) != 1 or not _vm_matches_plan(vms[0], execution, plan):
            raise ExecutionConflict("cleanup VM identity is not exact")
        if str(vms[0].get("state") or "").lower() != "stopped":
            raise ExecutionConflict("cleanup VM is not stopped")
        instance_name = vms[0].get("instancename")
        if not isinstance(instance_name, str) or not instance_name:
            raise ExecutionInvalid("cleanup VM instance name is missing")
        won_cleanup_submission = False
        for _attempt in range(3):
            write_guard()
            retryable_error = False
            try:
                updated = (
                    session.query(AdoptionExecution)
                    .filter(
                        AdoptionExecution.id == execution.id,
                        AdoptionExecution.state == "cleanup_required",
                        AdoptionExecution.worker_lease_id.is_(None),
                    )
                    .update(
                        {
                            AdoptionExecution.state: "cleanup_submitting",
                            AdoptionExecution.error_code: None,
                            AdoptionExecution.cloudstack_vm_ref: execution.id,
                            AdoptionExecution.cloudstack_instance_name: instance_name,
                            AdoptionExecution.updated_at: _now(),
                        },
                        synchronize_session=False,
                    )
                )
                if updated == 1:
                    session.commit()
                    won_cleanup_submission = True
                    break
                session.rollback()
            except OperationalError as exc:
                session.rollback()
                if not _is_retryable_operational_error(exc):
                    raise
                retryable_error = True

            session.expire_all()
            current = (
                session.query(AdoptionExecution)
                .filter_by(id=execution_id)
                .execution_options(populate_existing=True)
                .one()
            )
            if (
                current.state == "cleanup_submitting"
                and current.cloudstack_vm_ref == execution_id
                and current.cloudstack_instance_name == instance_name
            ):
                if retryable_error:
                    return public_execution(current)
                raise ExecutionConflict("cleanup was started concurrently")
            if current.state == "cleanup_required" and current.worker_lease_id is None:
                continue
            raise ExecutionConflict("cleanup was started concurrently")
        if not won_cleanup_submission:
            raise ExecutionConflict("cleanup submission CAS limit reached")
        session.expire_all()
        execution = session.query(AdoptionExecution).filter_by(id=execution_id).one()
        write_guard()
        try:
            response = client.destroy_virtual_machine(execution.id, expunge=True)
        except AuthorityConflict:
            raise
        except Exception as exc:
            log.error("Adoption cleanup submission failed (%s)", type(exc).__name__)
            write_guard()
            execution.state = "cleanup_submitting"
            execution.error_code = "cleanup_submission_unknown"
            session.commit()
            return public_execution(execution)
        write_guard()
        job_id = response.get("jobid")
        if not isinstance(job_id, str) or not job_id or job_id != job_id.strip():
            execution.state = "cleanup_submitting"
            execution.error_code = "cleanup_job_id_missing"
        else:
            execution.state = "cleanup_submitted"
            execution.cleanup_job_id = job_id
        session.commit()
        return public_execution(execution)
    finally:
        session.close()


def acquire_execution(
    session,
    execution_id: str,
    lease_seconds: int,
    *,
    write_guard: Callable[[], None],
) -> tuple[AdoptionExecution, str] | None:
    execution_id = _canonical_uuid(execution_id)
    lease_id = str(uuid.uuid4())
    for _attempt in range(3):
        now = _now()
        write_guard()
        try:
            updated = (
                session.query(AdoptionExecution)
                .filter(
                    AdoptionExecution.id == execution_id,
                    AdoptionExecution.state.in_(ACTIVE_STATES),
                    or_(
                        AdoptionExecution.worker_lease_id.is_(None),
                        AdoptionExecution.worker_lease_expires_at < now,
                    ),
                )
                .update(
                    {
                        AdoptionExecution.worker_lease_id: lease_id,
                        AdoptionExecution.worker_lease_expires_at: now
                        + timedelta(seconds=lease_seconds),
                        AdoptionExecution.attempt_count: AdoptionExecution.attempt_count
                        + 1,
                    },
                    synchronize_session=False,
                )
            )
            if updated == 1:
                session.commit()
            else:
                session.rollback()
        except OperationalError as exc:
            session.rollback()
            if not _is_retryable_operational_error(exc):
                raise

        session.expire_all()
        execution = (
            session.query(AdoptionExecution)
            .filter_by(id=execution_id)
            .execution_options(populate_existing=True)
            .first()
        )
        if execution is None or execution.state not in ACTIVE_STATES:
            return None
        if execution.worker_lease_id == lease_id:
            return execution, lease_id
        expires_at = _as_utc(execution.worker_lease_expires_at)
        if execution.worker_lease_id is not None and (
            expires_at is None or expires_at >= _now()
        ):
            return None
    return None


def _claim_is_bound(session, execution: AdoptionExecution) -> bool:
    claim = session.query(AdoptionClaim).filter_by(id=execution.claim_id).first()
    return bool(
        claim
        and claim.generation == execution.generation
        and claim.state in {"bound", "managed"}
        and claim.cloudstack_vm_ref == execution.id
    )


def reconcile_execution(
    execution_id: str,
    *,
    client,
    lease_seconds: int,
    activate: Callable[[str, int, str, str], None],
    write_guard: Callable[[], None],
) -> dict | None:
    """Advance at most one external side effect or one observation step."""

    session = get_session()
    try:
        acquired = acquire_execution(
            session,
            execution_id,
            lease_seconds,
            write_guard=write_guard,
        )
        if acquired is None:
            return None
        execution, lease_id = acquired
        plan = json.loads(execution.plan_json)
        state = execution.state

        def assert_lease() -> None:
            with session.no_autoflush:
                lease = (
                    session.query(
                        AdoptionExecution.worker_lease_id,
                        AdoptionExecution.worker_lease_expires_at,
                    )
                    .filter_by(id=execution.id)
                    .one()
                )
            expires_at = _as_utc(lease.worker_lease_expires_at)
            if (
                lease.worker_lease_id != lease_id
                or expires_at is None
                or expires_at < _now()
            ):
                raise ExecutionConflict("execution worker lease is not current")

        def commit_current() -> None:
            assert_lease()
            write_guard()
            session.commit()

        if state in {"deploy_submitting", "submission_unknown"}:
            vms = _load_exact_vm(client, execution)
            assert_lease()
            if len(vms) > 1:
                _save(execution, state="cleanup_required", error_code="cloudstack_vm_ambiguous")
            elif len(vms) == 1:
                if not _vm_matches_plan(vms[0], execution, plan):
                    _save(execution, state="cleanup_required", error_code="cloudstack_vm_identity_mismatch")
                elif str(vms[0].get("state") or "").lower() != "stopped":
                    _save(
                        execution,
                        state="submission_unknown",
                        error_code="deploy_vm_not_yet_stopped",
                    )
                else:
                    _save(
                        execution,
                        state="deploy_succeeded",
                        cloudstack_vm_ref=execution.id,
                        cloudstack_instance_name=vms[0].get("instancename"),
                    )
            else:
                _save(execution, state="submission_unknown", error_code="deploy_submission_unknown")
            commit_current()
            return public_execution(execution)

        if state == "planned":
            existing = _load_exact_vm(client, execution)
            assert_lease()
            if existing:
                if len(existing) == 1 and _vm_matches_plan(existing[0], execution, plan):
                    if str(existing[0].get("state") or "").lower() == "stopped":
                        _save(
                            execution,
                            state="deploy_succeeded",
                            cloudstack_vm_ref=execution.id,
                            cloudstack_instance_name=existing[0].get("instancename"),
                        )
                    else:
                        _save(
                            execution,
                            state="submission_unknown",
                            error_code="deploy_vm_not_yet_stopped",
                        )
                else:
                    _save(execution, state="cleanup_required", error_code="cloudstack_vm_identity_mismatch")
                commit_current()
                return public_execution(execution)
            execution.state = "deploy_submitting"
            commit_current()
            write_guard()
            try:
                assert_lease()
                response = client.deploy_virtual_machine(**_deploy_params(execution, plan))
            except AuthorityConflict:
                raise
            except Exception as exc:
                log.error("Adoption deploy submission failed (%s)", type(exc).__name__)
                assert_lease()
                _save(execution, state="submission_unknown", error_code="deploy_submission_unknown")
                commit_current()
                return public_execution(execution)
            assert_lease()
            job_id = response.get("jobid")
            if not isinstance(job_id, str) or not job_id or job_id != job_id.strip():
                _save(execution, state="submission_unknown", error_code="deploy_job_id_missing")
            else:
                _save(execution, state="deploy_submitted", deploy_job_id=job_id)
            commit_current()
            return public_execution(execution)

        if state == "deploy_submitted":
            result = client.query_async_job(execution.deploy_job_id)
            assert_lease()
            status = _job_status(result)
            if status == 0:
                _save(execution, state="deploy_submitted")
            elif status == 2:
                vms = _load_exact_vm(client, execution)
                assert_lease()
                if vms:
                    _save(execution, state="cleanup_required", error_code="deploy_job_failed_with_vm")
                else:
                    _save(execution, state="failed", error_code="deploy_job_failed")
            else:
                vms = _load_exact_vm(client, execution)
                assert_lease()
                if len(vms) != 1 or not _vm_matches_plan(vms[0], execution, plan):
                    _save(execution, state="cleanup_required", error_code="deployed_vm_verification_failed")
                elif str(vms[0].get("state") or "").lower() != "stopped":
                    _save(execution, state="cleanup_required", error_code="deployed_vm_not_stopped")
                else:
                    _save(
                        execution,
                        state="deploy_succeeded",
                        cloudstack_vm_ref=execution.id,
                        cloudstack_instance_name=vms[0].get("instancename"),
                    )
            commit_current()
            return public_execution(execution)

        if state in {"start_submitting", "start_unknown"}:
            vms = _load_exact_vm(client, execution)
            assert_lease()
            if (
                len(vms) == 1
                and _vm_matches_plan(vms[0], execution, plan)
                and str(vms[0].get("state") or "").lower() == "running"
            ):
                _save(execution, state="verifying")
            else:
                _save(execution, state="start_unknown", error_code="start_submission_unknown")
            commit_current()
            return public_execution(execution)

        if state == "deploy_succeeded":
            vms = _load_exact_vm(client, execution)
            assert_lease()
            if len(vms) != 1 or not _vm_matches_plan(vms[0], execution, plan):
                _save(execution, state="cleanup_required", error_code="pre_start_vm_verification_failed")
                commit_current()
                return public_execution(execution)
            if str(vms[0].get("state") or "").lower() != "stopped":
                _save(execution, state="cleanup_required", error_code="pre_start_vm_not_stopped")
                commit_current()
                return public_execution(execution)
            execution.state = "start_submitting"
            commit_current()
            write_guard()
            try:
                assert_lease()
                response = client.start_virtual_machine(
                    execution.id,
                    host_id=plan["deployment"]["host_id"],
                )
            except AuthorityConflict:
                raise
            except Exception as exc:
                log.error("Adoption start submission failed (%s)", type(exc).__name__)
                assert_lease()
                _save(execution, state="start_unknown", error_code="start_submission_unknown")
                commit_current()
                return public_execution(execution)
            assert_lease()
            job_id = response.get("jobid")
            if not isinstance(job_id, str) or not job_id or job_id != job_id.strip():
                _save(execution, state="start_unknown", error_code="start_job_id_missing")
            else:
                _save(execution, state="start_submitted", start_job_id=job_id)
            commit_current()
            return public_execution(execution)

        if state == "start_submitted":
            result = client.query_async_job(execution.start_job_id)
            assert_lease()
            status = _job_status(result)
            if status == 0:
                _save(execution, state="start_submitted")
            elif status == 2:
                _save(execution, state="start_unknown", error_code="start_job_failed")
            else:
                _save(execution, state="verifying")
            commit_current()
            return public_execution(execution)

        if state == "verifying":
            session.expunge(execution)
            session.close()
            try:
                activate(
                    execution.claim_id,
                    execution.generation,
                    execution.id,
                    lease_id,
                )
            except (ClaimConflict, ClaimInvalid) as exc:
                log.warning("Adoption activation pending (%s)", type(exc).__name__)
                session = get_session()
                execution = session.query(AdoptionExecution).filter_by(id=execution_id).one()
                if execution.worker_lease_id == lease_id:
                    _save(execution, state="verifying", error_code="activation_pending")
                    commit_current()
                return public_execution(execution)
            session = get_session()
            execution = session.query(AdoptionExecution).filter_by(id=execution_id).one()
            expires_at = _as_utc(execution.worker_lease_expires_at)
            if (
                execution.worker_lease_id != lease_id
                or expires_at is None
                or expires_at < _now()
            ):
                raise ExecutionConflict("execution worker lease is not current")
            _save(execution, state="succeeded")
            commit_current()
            return public_execution(execution)

        if state in {"cleanup_submitting", "cleanup_authorized"}:
            vms = _load_exact_vm(client, execution)
            assert_lease()
            if not vms:
                session.close()
                return _finalize_cleanup_release(
                    execution_id,
                    lease_id,
                    write_guard=write_guard,
                )
            elif len(vms) != 1 or not _vm_matches_plan(vms[0], execution, plan):
                _save(
                    execution,
                    state="cleanup_required",
                    error_code="cleanup_identity_mismatch",
                )
            else:
                _save(
                    execution,
                    state="cleanup_submitting",
                    error_code=execution.error_code or "cleanup_pending_unknown_job",
                )
            commit_current()
            return public_execution(execution)

        if state == "cleanup_submitted":
            result = client.query_async_job(execution.cleanup_job_id)
            assert_lease()
            status = _job_status(result)
            if status == 0:
                _save(execution, state="cleanup_submitted")
            elif status == 2:
                _save(
                    execution,
                    state="cleanup_required",
                    error_code="cleanup_job_failed",
                )
            else:
                vms = _load_exact_vm(client, execution)
                assert_lease()
                if vms:
                    _save(
                        execution,
                        state="cleanup_required",
                        error_code="cleanup_job_succeeded_but_vm_present",
                    )
                else:
                    session.close()
                    return _finalize_cleanup_release(
                        execution_id,
                        lease_id,
                        write_guard=write_guard,
                    )
            commit_current()
            return public_execution(execution)

        raise ExecutionInvalid("unknown execution state")
    finally:
        session.close()


def reconcile_active_executions(
    *, client, lease_seconds: int, activate, before_each: Callable[[], None]
) -> dict:
    session = get_session()
    try:
        execution_ids = [
            row[0]
            for row in session.query(AdoptionExecution.id)
            .filter(AdoptionExecution.state.in_(ACTIVE_STATES))
            .order_by(AdoptionExecution.created_at)
            .all()
        ]
    finally:
        session.close()
    stats = {"considered": len(execution_ids), "advanced": 0, "errors": 0}
    for execution_id in execution_ids:
        try:
            before_each()
            if reconcile_execution(
                execution_id,
                client=client,
                lease_seconds=lease_seconds,
                activate=activate,
                write_guard=before_each,
            ) is not None:
                stats["advanced"] += 1
        except Exception as exc:
            log.error("Adoption execution reconciliation failed (%s)", type(exc).__name__)
            stats["errors"] += 1
    return stats
