import hashlib
import json


def _is_customized(offering: dict) -> bool:
    return offering.get("iscustomized") is True or str(
        offering.get("iscustomized", "")
    ).lower() == "true"


def select_exact_service_offering(
    cpus: int,
    memory_mb: int,
    offerings: list[dict],
    customized_offering_id: str,
) -> tuple[dict | None, list[str]]:
    """Select one exact static offering or the configured customized offering."""
    if cpus <= 0 or memory_mb <= 0:
        return None, ["proxmox_cpu_memory_invalid"]

    exact_static = [
        offering
        for offering in offerings
        if not _is_customized(offering)
        and isinstance(offering.get("id"), str)
        and offering.get("id")
        and isinstance(offering.get("name"), str)
        and offering.get("name")
        and offering.get("state", "Enabled") == "Enabled"
        and offering.get("cpunumber") == cpus
        and offering.get("memory") == memory_mb
    ]
    if len(exact_static) == 1:
        offering = exact_static[0]
        return {
            "id": offering.get("id"),
            "name": offering.get("name"),
            "customized": False,
            "details": None,
            "cpus": cpus,
            "memory_mb": memory_mb,
        }, []
    if len(exact_static) > 1:
        return None, ["service_offering_exact_static_ambiguous"]

    customized = [
        offering
        for offering in offerings
        if offering.get("id") == customized_offering_id
        and isinstance(offering.get("name"), str)
        and offering.get("name")
        and offering.get("state", "Enabled") == "Enabled"
        and _is_customized(offering)
    ]
    if len(customized) != 1:
        return None, ["service_offering_exact_match_unavailable"]
    offering = customized[0]
    return {
        "id": offering.get("id"),
        "name": offering.get("name"),
        "customized": True,
        "details": {
            "cpuNumber": cpus,
            "memory": memory_mb,
        },
        "cpus": cpus,
        "memory_mb": memory_mb,
    }, []


def adoption_manifest_hash(
    *,
    cluster: str,
    node: str,
    vmid: int,
    name: str,
    cpus: int,
    memory_mb: int,
    networks: list[dict],
    storage: list[dict],
) -> str:
    """Hash only non-secret authoritative identity/configuration fields."""
    payload = {
        "cluster": cluster,
        "node": node,
        "vmid": vmid,
        "name": name,
        "cpus": cpus,
        "memory_mb": memory_mb,
        "networks": sorted(networks, key=lambda item: item.get("device_id", -1)),
        "storage": sorted(storage, key=lambda item: item.get("device", "")),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()