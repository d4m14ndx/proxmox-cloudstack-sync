from proxmoxer import ProxmoxAPI
from config import ProxmoxCluster
import logging

log = logging.getLogger(__name__)


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
