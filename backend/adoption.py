import hashlib
import json
import re
import uuid


def canonical_adoption_manifest_json(manifest: dict) -> str:
    return json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def hash_adoption_manifest(manifest: dict) -> str:
    return hashlib.sha256(
        canonical_adoption_manifest_json(manifest).encode("utf-8")
    ).hexdigest()


def build_adoption_manifest(
    *,
    cluster: str,
    node: str,
    vmid: int,
    name: str,
    status: str,
    cpus: int,
    memory_mb: int,
    networks: list[dict],
    storage: list[dict],
) -> dict:
    return {
        "placement": {"cluster": cluster, "node": node},
        "vmid": vmid,
        "name": name,
        "status": status,
        "cpus": cpus,
        "memory_mib": memory_mb,
        "networks": sorted(
            [
                {
                    "device": f"net{item['device_id']}",
                    "mac": item["mac"],
                    "bridge": item["proxmox_bridge"],
                    "tag": item.get("proxmox_vlan"),
                    "ip": item["ip"],
                    "cloudstack_network_id": item["cloudstack_network_id"],
                    "cloudstack_network_name": item["cloudstack_network_name"],
                    "ip_allocation": item.get("ip_allocation", "cloudstack"),
                }
                for item in networks
            ],
            key=lambda item: item["device"],
        ),
        "storage": sorted(
            [
                {
                    "device": item["device"],
                    "volume": item["volume"],
                    "storage": item["storage"],
                    "size": item["size"],
                }
                for item in storage
            ],
            key=lambda item: item["device"],
        ),
    }


def _is_customized(offering: dict) -> bool:
    return offering.get("iscustomized") is True or str(
        offering.get("iscustomized", "")
    ).lower() == "true"


def _is_active_offering(offering: dict) -> bool:
    """Accept active-state labels returned by supported CloudStack APIs."""
    return offering.get("state") in {"Active", "Enabled"}


_QEMU_ROOT_DISK_RE = re.compile(r"^(?:ide|sata|scsi|virtio)[0-9]+$")
_PROXMOX_SIZE_RE = re.compile(r"^([1-9][0-9]*)([KMGT])$")
_SIZE_MULTIPLIERS = {
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
}


def custom_root_disk_size_gib(storage: list[dict]) -> int | None:
    """Return one exact inherited QEMU root-disk size in integral GiB."""

    eligible = [
        item
        for item in storage
        if isinstance(item, dict)
        and isinstance(item.get("device"), str)
        and _QEMU_ROOT_DISK_RE.fullmatch(item["device"])
        and isinstance(item.get("volume"), str)
        and "cloudinit" not in item["volume"].lower()
        and item.get("media") != "cdrom"
    ]
    if len(eligible) != 1:
        return None
    size = eligible[0].get("size")
    if not isinstance(size, str):
        return None
    match = _PROXMOX_SIZE_RE.fullmatch(size)
    if match is None:
        return None
    size_bytes = int(match.group(1)) * _SIZE_MULTIPLIERS[match.group(2)]
    gib = 1024**3
    if size_bytes % gib != 0:
        return None
    size_gib = size_bytes // gib
    return size_gib if 1 <= size_gib <= 2147483647 else None


def _custom_root_disk_required(offering: dict) -> bool | None:
    disk_offering_present = "diskofferingid" in offering
    root_disk_size_present = "rootdisksize" in offering
    if not disk_offering_present and not root_disk_size_present:
        return False
    if not disk_offering_present or not root_disk_size_present:
        return None
    root_disk_size = offering.get("rootdisksize")
    disk_offering_id = offering.get("diskofferingid")
    if not isinstance(disk_offering_id, str):
        return None
    try:
        canonical_disk_offering_id = str(uuid.UUID(disk_offering_id))
    except (ValueError, AttributeError, TypeError):
        return None
    if disk_offering_id != canonical_disk_offering_id:
        return None
    if (
        isinstance(root_disk_size, bool)
        or not isinstance(root_disk_size, int)
        or root_disk_size < 0
    ):
        return None
    return root_disk_size == 0


def select_exact_service_offering(
    cpus: int,
    memory_mb: int,
    offerings: list[dict],
    customized_offering_id: str,
    customized_cpu_speed_mhz: int,
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
        and _is_active_offering(offering)
        and offering.get("cpunumber") == cpus
        and offering.get("memory") == memory_mb
    ]
    if len(exact_static) == 1:
        offering = exact_static[0]
        root_disk_size_customized = _custom_root_disk_required(offering)
        if root_disk_size_customized is None:
            return None, ["service_offering_root_disk_contract_invalid"]
        return {
            "id": offering.get("id"),
            "name": offering.get("name"),
            "customized": False,
            "root_disk_size_customized": root_disk_size_customized,
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
        and _is_active_offering(offering)
        and _is_customized(offering)
    ]
    if len(customized) != 1:
        return None, ["service_offering_exact_match_unavailable"]
    if (
        isinstance(customized_cpu_speed_mhz, bool)
        or not isinstance(customized_cpu_speed_mhz, int)
        or not 1 <= customized_cpu_speed_mhz <= 2147483647
    ):
        return None, ["customized_service_offering_cpu_speed_invalid"]
    offering = customized[0]
    root_disk_size_customized = _custom_root_disk_required(offering)
    if root_disk_size_customized is None:
        return None, ["service_offering_root_disk_contract_invalid"]
    return {
        "id": offering.get("id"),
        "name": offering.get("name"),
        "customized": True,
        "root_disk_size_customized": root_disk_size_customized,
        "details": {
            "cpuNumber": cpus,
            "cpuSpeed": customized_cpu_speed_mhz,
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
    return hash_adoption_manifest(payload)