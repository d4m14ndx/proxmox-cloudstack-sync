import logging
from datetime import datetime, timezone
from database import get_session, ProxmoxVM, CloudStackVM, HostMapping, SyncLog
from proxmox_client import ProxmoxClient
from cloudstack_client import CloudStackClient
from cloudstack_db import CloudStackDB
from config import Settings

log = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.proxmox_clients: list[ProxmoxClient] = []
        self.cs_client: CloudStackClient | None = None
        self.cs_db: CloudStackDB | None = None

        for cluster in settings.proxmox_clusters:
            try:
                self.proxmox_clients.append(ProxmoxClient(cluster))
                log.info(f"Connected to Proxmox cluster: {cluster.name}")
            except Exception as e:
                log.error(f"Failed to connect to Proxmox cluster {cluster.name}: {e}")

        if settings.cloudstack.api_key:
            self.cs_client = CloudStackClient(settings.cloudstack)
            log.info("Connected to CloudStack API")

        if settings.cloudstack_db.password:
            self.cs_db = CloudStackDB(settings.cloudstack_db)
            if self.cs_db.test_connection():
                log.info("Connected to CloudStack database")
            else:
                log.error("CloudStack DB connection failed")
                self.cs_db = None

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

        reconcile_stats = None
        if self.settings.auto_reconcile and self.cs_db:
            reconcile_stats = self.reconcile_all()
            if reconcile_stats["updated"] > 0:
                # Re-sync CS to pick up our DB changes
                cs_stats = self.sync_cloudstack()

        session = get_session()
        msg = (f"PX: {px_stats['vms_found']} found, {px_stats['vms_new']} new | "
               f"CS: {cs_stats['vms_found']} found | "
               f"Matched: {match_stats['matched']}, "
               f"Unmatched PX: {match_stats['unmatched_proxmox']}, "
               f"Unmatched CS: {match_stats['unmatched_cloudstack']}")
        if reconcile_stats:
            msg += f" | Reconciled: {reconcile_stats['updated']}"
        self._log(session, "full_sync", msg)
        session.commit()
        session.close()

        log.info(f"Sync complete. Matched: {match_stats['matched']}, "
                 f"Unmatched PX: {match_stats['unmatched_proxmox']}")

        result = {
            "proxmox": px_stats,
            "cloudstack": cs_stats,
            "matching": match_stats,
        }
        if reconcile_stats:
            result["reconcile"] = reconcile_stats
        return result

    def _build_host_map(self, session) -> dict:
        """Build a lookup: (proxmox_cluster, proxmox_node) -> cloudstack_host_name."""
        mappings = session.query(HostMapping).all()
        return {
            (m.proxmox_cluster, m.proxmox_node.lower()): m
            for m in mappings
        }

    def _resolve_px_host_to_cs(self, px_cluster: str, px_node: str,
                                host_map: dict) -> str | None:
        """Translate a Proxmox node name to the CloudStack host name via mapping."""
        mapping = host_map.get((px_cluster, px_node.lower()))
        if mapping:
            return mapping.cloudstack_host_name
        return None

    def _resolve_host_db_id(self, host_ref: str) -> int | None:
        """Resolve a host identifier (integer ID string or UUID) to the CS DB integer ID."""
        if not host_ref or not self.cs_db:
            return None
        try:
            return int(host_ref)
        except ValueError:
            host = self.cs_db.get_host_by_uuid(host_ref)
            return host["id"] if host else None

    def detect_drift(self) -> list[dict]:
        drift = []
        session = get_session()
        try:
            host_map = self._build_host_map(session)
            matched = session.query(ProxmoxVM).filter_by(matched=True).all()
            for px in matched:
                if not px.cloudstack_uuid:
                    continue
                cs = session.query(CloudStackVM).filter_by(uuid=px.cloudstack_uuid).first()
                if not cs:
                    continue

                # Resolve PX node to CS host name via mapping
                expected_cs_host = self._resolve_px_host_to_cs(
                    px.cluster, px.node, host_map
                )
                if expected_cs_host and cs.host_name:
                    if expected_cs_host.lower() != cs.host_name.lower():
                        mapping = host_map.get((px.cluster, px.node.lower()))
                        drift.append({
                            "type": "host_mismatch",
                            "vm_name": px.name,
                            "proxmox_id": px.id,
                            "cloudstack_uuid": cs.uuid,
                            "cloudstack_host_id": cs.host_id,
                            "proxmox_host": px.node,
                            "expected_cs_host": expected_cs_host,
                            "actual_cs_host": cs.host_name,
                            "target_cs_host_id": mapping.cloudstack_host_id if mapping else "",
                        })
                elif not expected_cs_host and px.node and cs.host_name:
                    # No mapping exists — flag as unmapped so user knows to set one up
                    drift.append({
                        "type": "unmapped_host",
                        "vm_name": px.name,
                        "proxmox_id": px.id,
                        "cloudstack_uuid": cs.uuid,
                        "cloudstack_host_id": cs.host_id,
                        "proxmox_host": px.node,
                        "proxmox_cluster": px.cluster,
                        "actual_cs_host": cs.host_name,
                    })

                state_map = {"running": "Running", "stopped": "Stopped"}
                expected_cs_state = state_map.get(px.status)
                if expected_cs_state and cs.state != expected_cs_state:
                    drift.append({
                        "type": "state_mismatch",
                        "vm_name": px.name,
                        "proxmox_id": px.id,
                        "cloudstack_uuid": cs.uuid,
                        "cloudstack_host_id": cs.host_id,
                        "proxmox_state": px.status,
                        "cloudstack_state": cs.state,
                    })
        finally:
            session.close()
        return drift

    def reconcile_vm(self, drift_item: dict) -> dict:
        """Fix a single drifted VM by updating the CloudStack database directly."""
        if not self.cs_db:
            return {"error": "CloudStack DB not configured"}

        vm_uuid = drift_item.get("cloudstack_uuid")
        drift_type = drift_item.get("type")
        session = get_session()

        try:
            if drift_type == "host_mismatch":
                target_host_ref = drift_item.get("target_cs_host_id")
                old_host_ref = drift_item.get("cloudstack_host_id")
                if not target_host_ref:
                    return {"error": "No target host ID in drift item"}

                target_db_id = self._resolve_host_db_id(str(target_host_ref))
                old_db_id = self._resolve_host_db_id(str(old_host_ref)) if old_host_ref else None
                if not target_db_id:
                    return {"error": f"Could not resolve target host: {target_host_ref}"}

                px = session.query(ProxmoxVM).filter_by(
                    id=drift_item.get("proxmox_id")
                ).first()
                px_state = px.status if px else "running"

                power_state = "PowerOn" if px_state == "running" else "PowerOff"
                vm_state = "Running" if px_state == "running" else "Stopped"
                new_host = target_db_id if px_state == "running" else None

                ok = self.cs_db.update_vm_placement_and_state(
                    vm_uuid, new_host, power_state, vm_state, old_db_id
                )
                if ok:
                    self._log(session, "reconcile_host",
                              f"Updated {drift_item['vm_name']} host in CS DB: "
                              f"{drift_item['actual_cs_host']} -> {drift_item['expected_cs_host']}")
                    session.commit()
                    return {"status": "updated", "vm": drift_item["vm_name"],
                            "action": "host_placement"}
                return {"error": f"DB update failed for {vm_uuid}"}

            elif drift_type == "state_mismatch":
                px_state = drift_item.get("proxmox_state", "")
                power_state = "PowerOn" if px_state == "running" else "PowerOff"
                vm_state = "Running" if px_state == "running" else "Stopped"
                host_ref = drift_item.get("cloudstack_host_id")
                host_db_id = self._resolve_host_db_id(str(host_ref)) if host_ref else 0

                ok_power = self.cs_db.update_vm_power_state(
                    vm_uuid, power_state, host_db_id or 0
                )
                ok_state = self.cs_db.update_vm_state(vm_uuid, vm_state)

                if ok_power or ok_state:
                    self._log(session, "reconcile_state",
                              f"Updated {drift_item['vm_name']} state in CS DB: "
                              f"{drift_item['cloudstack_state']} -> {vm_state}")
                    session.commit()
                    return {"status": "updated", "vm": drift_item["vm_name"],
                            "action": "state_update"}
                return {"error": f"DB update failed for {vm_uuid}"}

            else:
                return {"error": f"Cannot reconcile drift type: {drift_type}"}

        except Exception as e:
            log.error(f"Reconcile failed for {vm_uuid}: {e}")
            return {"error": str(e)}
        finally:
            session.close()

    def reconcile_all(self) -> dict:
        """Fix all drifted VMs by updating the CloudStack database."""
        if not self.cs_db:
            return {"error": "CloudStack DB not configured", "updated": 0, "failed": 0}

        drift = self.detect_drift()
        results = []
        updated = 0
        failed = 0

        for d in drift:
            if d["type"] in ("host_mismatch", "state_mismatch"):
                result = self.reconcile_vm(d)
                results.append(result)
                if result.get("status") == "updated":
                    updated += 1
                else:
                    failed += 1

        return {
            "drift_items": len(drift),
            "updated": updated,
            "failed": failed,
            "results": results,
        }

    def _log(self, session, action: str, details: str, success: bool = True):
        session.add(SyncLog(action=action, details=details, success=success))
