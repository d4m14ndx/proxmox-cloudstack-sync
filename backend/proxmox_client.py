from proxmoxer import ProxmoxAPI
from config import ProxmoxCluster
import ipaddress
import re
import logging

log = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")

# QEMU NIC model keys whose value is the MAC address (net0: virtio=AA:BB:...)
_QEMU_NIC_MODELS = {
    "virtio", "e1000", "e1000e", "e1000-82540em", "e1000-82544gc",
    "e1000-82545em", "vmxnet3", "rtl8139", "ne2k_pci", "ne2k_isa",
    "i82551", "i82557b", "i82559er", "pcnet",
}


def _cidr_to_netmask(prefix: int) -> str:
    try:
        return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
    except Exception:
        return ""


def parse_nics(config: dict) -> list[dict]:
    """Parse Proxmox VM config netN entries into structured NIC dicts.

    Handles both QEMU (`net0: virtio=AA:BB:..,bridge=vmbr0,tag=100,firewall=1`)
    and LXC (`net0: name=eth0,hwaddr=AA:BB:..,bridge=vmbr0,ip=10.0.0.5/24,gw=10.0.0.1,tag=100`).

    Returns a list ordered by device id, each:
      {device_id, model, mac, bridge, vlan, ip, netmask, gateway, firewall, link_down}
    """
    nics = []
    for key in sorted(config.keys()):
        m = re.fullmatch(r"net(\d+)", key)
        if not m or not isinstance(config[key], str):
            continue
        device_id = int(m.group(1))
        fields = {}
        for part in config[key].split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                fields[k.strip().lower()] = v.strip()

        # MAC + model: LXC uses hwaddr; QEMU uses <model>=<MAC>
        mac = ""
        model = ""
        if "hwaddr" in fields:
            mac = fields["hwaddr"]
            model = fields.get("type", "veth")
        else:
            for k, v in fields.items():
                if k in _QEMU_NIC_MODELS or _MAC_RE.match(v):
                    mac, model = v, k
                    break

        ip = netmask = gateway = None
        ip_val = fields.get("ip", "")
        if ip_val and ip_val.lower() not in ("dhcp", "manual", "auto"):
            if "/" in ip_val:
                addr, _, prefix = ip_val.partition("/")
                ip = addr
                if prefix.isdigit():
                    netmask = _cidr_to_netmask(int(prefix))
            else:
                ip = ip_val
        gateway = fields.get("gw") or None

        vlan = fields.get("tag")
        nics.append({
            "device_id": device_id,
            "model": model,
            "mac": mac.upper() if mac else "",
            "bridge": fields.get("bridge", ""),
            "vlan": int(vlan) if vlan and vlan.isdigit() else None,
            "ip": ip,
            "netmask": netmask,
            "gateway": gateway,
            "firewall": fields.get("firewall") == "1",
            "link_down": fields.get("link_down") == "1",
        })
    return nics


class ProxmoxClient:
    def __init__(self, cluster_config: ProxmoxCluster):
        self.cluster_name = cluster_config.name
        self._config = cluster_config
        self._hosts = cluster_config.all_hosts
        if not self._hosts:
            raise ValueError(f"Cluster {cluster_config.name}: provide at least one host")
        self.api = None
        self.active_host = None

    def _build_auth(self) -> dict:
        auth = {"verify_ssl": self._config.verify_ssl}
        if self._config.token_name and self._config.token_value:
            auth["user"] = self._config.user
            auth["token_name"] = self._config.token_name
            auth["token_value"] = self._config.token_value
        elif self._config.password:
            auth["user"] = self._config.user
            auth["password"] = self._config.password
        else:
            raise ValueError(f"Cluster {self.cluster_name}: provide token or password")
        return auth

    def _connect(self) -> ProxmoxAPI:
        """Try each host in order, return the first that responds."""
        auth = self._build_auth()
        errors = []

        # Try the last known good host first if we have one
        ordered = list(self._hosts)
        if self.active_host and self.active_host in ordered:
            ordered.remove(self.active_host)
            ordered.insert(0, self.active_host)

        for host in ordered:
            try:
                api = ProxmoxAPI(host, **auth)
                api.nodes.get()  # lightweight probe
                if host != self.active_host:
                    log.info(f"Cluster {self.cluster_name}: connected via {host}")
                self.active_host = host
                self.api = api
                return api
            except Exception as e:
                errors.append(f"{host}: {e}")
                log.warning(f"Cluster {self.cluster_name}: {host} unreachable ({e})")

        raise ConnectionError(
            f"Cluster {self.cluster_name}: all hosts unreachable: "
            + "; ".join(errors)
        )

    def get_nodes(self) -> list[dict]:
        return self._connect().nodes.get()

    def get_all_vms(self) -> list[dict]:
        api = self._connect()
        vms = []
        try:
            resources = api.cluster.resources.get(type="vm")
            for r in resources:
                r["_cluster"] = self.cluster_name
                r["_source"] = "cluster_resources"
                vms.append(r)
            return vms
        except Exception as e:
            log.warning(f"cluster/resources failed for {self.cluster_name}, falling back to per-node: {e}")

        for node_info in api.nodes.get():
            node = node_info["node"]
            if node_info.get("status") != "online":
                continue
            vms.extend(self._get_node_vms(node))
        return vms

    def _get_node_vms(self, node: str) -> list[dict]:
        vms = []
        for vm_type in ("qemu", "lxc"):
            try:
                items = getattr(self.api.nodes(node), vm_type).get()
                for item in items:
                    item["_cluster"] = self.cluster_name
                    item["_node"] = node
                    item["_type"] = vm_type
                    item["_source"] = "node_list"
                    vms.append(item)
            except Exception as e:
                log.error(f"Failed listing {vm_type} on {self.cluster_name}/{node}: {e}")
        return vms

    def get_vm_config(self, node: str, vmid: int, vm_type: str = "qemu") -> dict:
        try:
            return getattr(self.api.nodes(node), vm_type)(vmid).config.get()
        except Exception as e:
            log.error(f"Failed getting config for {self.cluster_name}/{node}/{vm_type}/{vmid}: {e}")
            return {}

    def get_guest_ifaces(self, node: str, vmid: int) -> dict:
        """Best-effort QEMU guest-agent NIC IP lookup: MAC -> {ip, netmask}.

        Returns {} if the agent is disabled/unreachable (common) so callers
        simply fall back to whatever the VM config provided.
        """
        result = {}
        try:
            data = self.api.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
            ifaces = data.get("result", data) if isinstance(data, dict) else data
            for iface in ifaces or []:
                mac = (iface.get("hardware-address") or "").upper()
                if not mac or mac == "00:00:00:00:00:00":
                    continue
                for addr in iface.get("ip-addresses", []) or []:
                    if addr.get("ip-address-type") != "ipv4":
                        continue
                    ip = addr.get("ip-address", "")
                    if ip.startswith("127.") or not ip:
                        continue
                    prefix = addr.get("prefix")
                    result[mac] = {
                        "ip": ip,
                        "netmask": _cidr_to_netmask(int(prefix)) if prefix is not None else None,
                    }
                    break
        except Exception as e:
            log.debug(f"guest-agent ifaces unavailable for {self.cluster_name}/{node}/{vmid}: {e}")
        return result

    def normalize_vm(self, raw: dict) -> dict:
        if raw.get("_source") == "cluster_resources":
            vmid = raw.get("vmid", 0)
            node = raw.get("node", "")
            vm_type = raw.get("type", "qemu")
        else:
            vmid = raw.get("vmid", 0)
            node = raw.get("_node", "")
            vm_type = raw.get("_type", "qemu")

        mem_bytes = raw.get("maxmem", 0)
        disk_bytes = raw.get("maxdisk", 0)

        return {
            "id": f"{self.cluster_name}:{vmid}",
            "cluster": self.cluster_name,
            "node": node,
            "vmid": vmid,
            "name": raw.get("name", f"vm-{vmid}"),
            "status": raw.get("status", "unknown"),
            "vm_type": vm_type,
            "cpus": raw.get("maxcpu", raw.get("cpus", 0)),
            "memory_mb": mem_bytes // (1024 * 1024) if mem_bytes else 0,
            "disk_gb": round(disk_bytes / (1024 ** 3), 1) if disk_bytes else 0.0,
            "tags": raw.get("tags", ""),
        }
