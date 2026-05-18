import logging
from datetime import datetime, timezone
from database import get_session, ProxmoxVM, CloudStackVM, SyncLog
from proxmox_client import ProxmoxClient
from cloudstack_client import CloudStackClient
from config import Settings

log = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.proxmox_clients: list[ProxmoxClient] = []
        self.cs_client: CloudStackClient | None = None

        for cluster in settings.proxmox_clusters:
            try:
                self.proxmox_clients.append(ProxmoxClient(cluster))
                log.info(f"Connected to Proxmox cluster: {cluster.name}")
            except Exception as e:
                log.error(f"Failed to connect to Proxmox cluster {cluster.name}: {e}")

        if settings.cloudstack.api_key:
            self.cs_client = CloudStackClient(settings.cloudstack)
            log.info("Connected to CloudStack API")

    def sync_proxmox(self) -> dict:
        stats = {"clusters": 0, "vms_found": 0, "vms_updated": 0, "vms_new": 0, "errors": []}
        session = get_session()

        try:
            now = datetime.now(timezone.utc)
            for client in self.proxmox_clients:
                stats["clusters"] += 1
                try:
                    raw_vms = client.get_all_vms()
                    for raw in raw_vms:
                        vm_data = client.normalize_vm(raw)
                        stats["vms_found"] += 1

                        existing = session.query(ProxmoxVM).filter_by(id=vm_data["id"]).first()
                        if existing:
                            changed = (
                                existing.node != vm_data["node"]
                                or existing.status != vm_data["status"]
                                or existing.name != vm_data["name"]
                                or existing.cpus != vm_data["cpus"]
                                or existing.memory_mb != vm_data["memory_mb"]
                            )
                            if changed:
                                if existing.node != vm_data["node"]:
                                    self._log(session, "host_change",
                                              f"{vm_data['name']} ({vm_data['id']}) moved: "
                                              f"{existing.node} -> {vm_data['node']}")
                                if existing.status != vm_data["status"]:
                                    self._log(session, "state_change",
                                              f"{vm_data['name']} ({vm_data['id']}): "
                                              f"{existing.status} -> {vm_data['status']}")

                                for key, val in vm_data.items():
                                    setattr(existing, key, val)
                                existing.last_seen = now
                                stats["vms_updated"] += 1
                            else:
                                existing.last_seen = now
                        else:
                            vm = ProxmoxVM(**vm_data, last_seen=now, first_seen=now)
                            session.add(vm)
                            stats["vms_new"] += 1
                            self._log(session, "new_vm",
                                      f"Discovered {vm_data['name']} ({vm_data['id']}) on {vm_data['node']}")

                except Exception as e:
                    msg = f"Error syncing cluster {client.cluster_name}: {e}"
                    log.error(msg)
                    stats["errors"].append(msg)

            session.commit()
        except Exception as e:
            session.rollback()
            stats["errors"].append(str(e))
        finally:
            session.close()

        return stats

    def sync_cloudstack(self) -> dict:
        stats = {"vms_found": 0, "vms_updated": 0, "vms_new": 0, "errors": []}
        if not self.cs_client:
            stats["errors"].append("CloudStack not configured")
            return stats

        session = get_session()
        try:
            now = datetime.now(timezone.utc)
            cs_vms = self.cs_client.list_virtual_machines()

            for cs_vm in cs_vms:
                stats["vms_found"] += 1
                uuid = cs_vm["id"]

                data = {
                    "uuid": uuid,
                    "name": cs_vm.get("name", ""),
                    "display_name": cs_vm.get("displayname", ""),
                    "instance_name": cs_vm.get("instancename", ""),
                    "state": cs_vm.get("state", ""),
                    "host_name": cs_vm.get("hostname", ""),
                    "host_id": cs_vm.get("hostid", ""),
                    "cluster_name": cs_vm.get("clustername", ""),
                    "zone_name": cs_vm.get("zonename", ""),
                    "cpus": cs_vm.get("cpunumber", 0),
                    "memory_mb": cs_vm.get("memory", 0),
                    "hypervisor": cs_vm.get("hypervisor", ""),
                    "last_seen": now,
                }

                existing = session.query(CloudStackVM).filter_by(uuid=uuid).first()
                if existing:
                    changed = (
                        existing.host_name != data["host_name"]
                        or existing.state != data["state"]
                    )
                    for key, val in data.items():
                        setattr(existing, key, val)
                    if changed:
                        stats["vms_updated"] += 1
                else:
                    session.add(CloudStackVM(**data))
                    stats["vms_new"] += 1

            session.commit()
        except Exception as e:
            session.rollback()
            stats["errors"].append(str(e))
        finally:
            session.close()

        return stats

    def match_vms(self) -> dict:
        stats = {"matched": 0, "unmatched_proxmox": 0, "unmatched_cloudstack": 0}
        session = get_session()
        try:
            px_vms = session.query(ProxmoxVM).all()
            cs_vms = session.query(CloudStackVM).all()

            cs_by_instance = {}
            cs_by_name = {}
            for cs in cs_vms:
                if cs.instance_name:
                    cs_by_instance[cs.instance_name.lower()] = cs
                if cs.name:
                    cs_by_name[cs.name.lower()] = cs

            for px in px_vms:
                if px.matched and px.cloudstack_uuid:
                    stats["matched"] += 1
                    continue

                match = None

                instance_key = f"i-{px.vmid}".lower()
                if instance_key in cs_by_instance:
                    match = cs_by_instance[instance_key]

                if not match and px.name:
                    if px.name.lower() in cs_by_name:
                        match = cs_by_name[px.name.lower()]

                if not match and px.name:
                    for cs in cs_vms:
                        if cs.display_name and cs.display_name.lower() == px.name.lower():
                            match = cs
                            break

                if match:
                    px.matched = True
                    px.cloudstack_uuid = match.uuid
                    match.matched = True
                    match.proxmox_id = px.id
                    stats["matched"] += 1
                else:
                    stats["unmatched_proxmox"] += 1

            for cs in cs_vms:
                if not cs.matched:
                    stats["unmatched_cloudstack"] += 1

            session.commit()
        except Exception as e:
            session.rollback()
            log.error(f"Match error: {e}")
        finally:
            session.close()

        return stats

    def full_sync(self) -> dict:
        log.info("Starting full sync...")
        px_stats = self.sync_proxmox()
        cs_stats = self.sync_cloudstack()
        match_stats = self.match_vms()

        session = get_session()
        self._log(session, "full_sync",
                  f"PX: {px_stats['vms_found']} found, {px_stats['vms_new']} new | "
                  f"CS: {cs_stats['vms_found']} found | "
                  f"Matched: {match_stats['matched']}, "
                  f"Unmatched PX: {match_stats['unmatched_proxmox']}, "
                  f"Unmatched CS: {match_stats['unmatched_cloudstack']}")
        session.commit()
        session.close()

        log.info(f"Sync complete. Matched: {match_stats['matched']}, "
                 f"Unmatched PX: {match_stats['unmatched_proxmox']}")

        return {
            "proxmox": px_stats,
            "cloudstack": cs_stats,
            "matching": match_stats,
        }

    def detect_drift(self) -> list[dict]:
        drift = []
        session = get_session()
        try:
            matched = session.query(ProxmoxVM).filter_by(matched=True).all()
            for px in matched:
                if not px.cloudstack_uuid:
                    continue
                cs = session.query(CloudStackVM).filter_by(uuid=px.cloudstack_uuid).first()
                if not cs:
                    continue

                px_host = px.node
                cs_host = cs.host_name
                if px_host and cs_host and px_host.lower() != cs_host.lower():
                    drift.append({
                        "type": "host_mismatch",
                        "vm_name": px.name,
                        "proxmox_id": px.id,
                        "cloudstack_uuid": cs.uuid,
                        "proxmox_host": px_host,
                        "cloudstack_host": cs_host,
                    })

                state_map = {"running": "Running", "stopped": "Stopped"}
                expected_cs_state = state_map.get(px.status)
                if expected_cs_state and cs.state != expected_cs_state:
                    drift.append({
                        "type": "state_mismatch",
                        "vm_name": px.name,
                        "proxmox_id": px.id,
                        "cloudstack_uuid": cs.uuid,
                        "proxmox_state": px.status,
                        "cloudstack_state": cs.state,
                    })
        finally:
            session.close()
        return drift

    def _log(self, session, action: str, details: str, success: bool = True):
        session.add(SyncLog(action=action, details=details, success=success))
