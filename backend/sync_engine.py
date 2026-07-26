import json
import logging
import threading
import time
from datetime import datetime, timezone
from database import get_session, ProxmoxVM, CloudStackVM, HostMapping, NetworkMapping, SyncLog
from proxmox_client import ProxmoxClient, parse_disks, parse_nics
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
        self.cs_db_last_error: dict | None = None
        self._cs_db_connect_lock = threading.Lock()
        self._cs_db_next_retry_at = 0.0
        # Persisted freshness flags are necessary but not sufficient: a local
        # marker-reset transaction can fail and roll back to old True values.
        # Reconciliation also requires a successful collection in this process.
        self._inventory_collection_ready = False
        self._nic_collection_ready = False

        for cluster in settings.proxmox_clusters:
            try:
                self.proxmox_clients.append(ProxmoxClient(cluster))
                log.info(f"Connected to Proxmox cluster: {cluster.name}")
            except Exception as e:
                log.error(
                    "Failed to initialize Proxmox cluster %s (%s)",
                    cluster.name,
                    type(e).__name__,
                )

        if settings.cloudstack.api_key:
            self.cs_client = CloudStackClient(settings.cloudstack)
            log.info("Connected to CloudStack API")

        if settings.cloudstack_db.password:
            self.connect_cloudstack_db()

    def connect_cloudstack_db(self, force: bool = False) -> bool:
        """Probe and (re)attach the CloudStack DB connection provider.

        A failed startup probe must not permanently disable DB functionality:
        routing/firewall maintenance can make the database temporarily
        unavailable while the sync service remains healthy.
        """
        with self._cs_db_connect_lock:
            if self.cs_db is not None:
                return True
            now = time.monotonic()
            if not force and now < self._cs_db_next_retry_at:
                return False
            if not self.settings.cloudstack_db.password:
                self.cs_db = None
                self.cs_db_last_error = {
                    "type": "ConfigurationError",
                    "code": None,
                }
                return False

            candidate = CloudStackDB(self.settings.cloudstack_db)
            if candidate.test_connection():
                self.cs_db = candidate
                self.cs_db_last_error = None
                self._cs_db_next_retry_at = 0.0
                log.info("Connected to CloudStack database")
                return True

            self.cs_db = None
            self.cs_db_last_error = candidate.last_connection_error
            self._cs_db_next_retry_at = (
                now + self.settings.cloudstack_db.reconnect_backoff_seconds
            )
            log.error("CloudStack DB connection failed")
            return False

    def sync_proxmox(self) -> dict:
        stats = {"clusters": 0, "vms_found": 0, "vms_updated": 0, "vms_new": 0, "errors": []}
        session = get_session()

        try:
            now = datetime.now(timezone.utc)
            # A full cycle starts with no current Proxmox truth. Configured
            # clusters are promoted back to current only after a complete poll;
            # removed/unconfigured clusters therefore cannot stay actionable.
            session.query(ProxmoxVM).update(
                {ProxmoxVM.current: False}, synchronize_session=False
            )
            for client in self.proxmox_clients:
                stats["clusters"] += 1
                # Each completed cluster poll is an authoritative snapshot. Old
                # rows are marked stale before applying the newly observed set;
                # a failed poll therefore cannot drive matching or writes.
                try:
                    raw_vms = client.get_all_vms()
                    for raw in raw_vms:
                        vm_data = client.normalize_vm(raw)
                        stats["vms_found"] += 1

                        existing = session.query(ProxmoxVM).filter_by(id=vm_data["id"]).first()
                        if existing:
                            changed = any(
                                getattr(existing, key) != val
                                for key, val in vm_data.items()
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
                                stats["vms_updated"] += 1
                            existing.current = True
                            existing.last_seen = now
                        else:
                            vm = ProxmoxVM(
                                **vm_data, current=True,
                                last_seen=now, first_seen=now,
                            )
                            session.add(vm)
                            stats["vms_new"] += 1
                            self._log(session, "new_vm",
                                      f"Discovered {vm_data['name']} ({vm_data['id']}) on {vm_data['node']}")

                except Exception as e:
                    log.error(
                        "Error syncing cluster %s (%s)",
                        client.cluster_name,
                        type(e).__name__,
                    )
                    stats["errors"].append(
                        f"Error syncing cluster {client.cluster_name}: {type(e).__name__}"
                    )

            session.commit()
        except Exception as e:
            session.rollback()
            log.error(
                "Proxmox inventory transaction failed (%s)",
                type(e).__name__,
            )
            stats["errors"].append(
                f"Proxmox inventory transaction failed: {type(e).__name__}"
            )
        finally:
            session.close()

        return stats

    def sync_cloudstack(self) -> dict:
        stats = {"vms_found": 0, "vms_updated": 0, "vms_new": 0, "errors": []}
        if not self.cs_client:
            stats["errors"].append("CloudStack not configured")
            session = get_session()
            try:
                session.query(CloudStackVM).update(
                    {CloudStackVM.current: False}, synchronize_session=False
                )
                session.commit()
            finally:
                session.close()
            return stats

        session = get_session()
        try:
            now = datetime.now(timezone.utc)
            session.query(CloudStackVM).update(
                {CloudStackVM.current: False}, synchronize_session=False
            )
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
                    "proxmox_vmid": self._cloudstack_proxmox_vmid(cs_vm),
                    "current": True,
                    "last_seen": now,
                }

                existing = session.query(CloudStackVM).filter_by(uuid=uuid).first()
                if existing:
                    changed = any(
                        getattr(existing, key) != val
                        for key, val in data.items()
                        if key != "last_seen"
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
            session.query(CloudStackVM).update(
                {CloudStackVM.current: False}, synchronize_session=False
            )
            session.commit()
            log.error(
                "CloudStack inventory sync failed (%s)",
                type(e).__name__,
            )
            stats["errors"].append(
                f"CloudStack inventory sync failed: {type(e).__name__}"
            )
        finally:
            session.close()

        return stats

    @staticmethod
    def _cloudstack_proxmox_vmid(cs_vm: dict) -> int | None:
        """Extract the External VMID without retaining arbitrary details."""
        details = cs_vm.get("details") or {}
        value = None
        if isinstance(details, dict):
            value = details.get("proxmox_vmid")
        elif isinstance(details, list):
            for item in details:
                if isinstance(item, dict) and item.get("name") == "proxmox_vmid":
                    value = item.get("value")
                    break
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _canonical_mapping_value(value: str | None) -> str | None:
        """Return a normalized identity only when the stored value is already clean."""
        if not isinstance(value, str) or not value or value != value.strip():
            return None
        return value.lower()

    @classmethod
    def _globally_unique_host_mappings(cls, session) -> list[HostMapping]:
        """Return complete mappings that are one-to-one on PX placement, CS name and ID."""
        rows = session.query(HostMapping).all()
        identities = []
        for row in rows:
            identities.append((
                row,
                cls._canonical_mapping_value(row.proxmox_cluster),
                cls._canonical_mapping_value(row.proxmox_node),
                cls._canonical_mapping_value(row.cloudstack_host_name),
                cls._canonical_mapping_value(row.cloudstack_host_id),
            ))

        unique = []
        for row, cluster, node, host_name, host_id in identities:
            if not all((cluster, node, host_name, host_id)):
                continue
            px_rows = [
                item for item in identities
                if item[1] == cluster and item[2] == node
            ]
            host_rows = [item for item in identities if item[3] == host_name]
            host_id_rows = [item for item in identities if item[4] == host_id]
            if len(px_rows) == len(host_rows) == len(host_id_rows) == 1:
                unique.append(row)
        return unique

    @classmethod
    def _unique_host_mapping(
        cls, session, px_cluster: str, px_node: str, cs_host_name: str
    ) -> HostMapping | None:
        cluster = cls._canonical_mapping_value(px_cluster)
        node = cls._canonical_mapping_value(px_node)
        host_name = cls._canonical_mapping_value(cs_host_name)
        if not all((cluster, node, host_name)):
            return None
        candidates = [
            row for row in cls._globally_unique_host_mappings(session)
            if (
                cls._canonical_mapping_value(row.proxmox_cluster) == cluster
                and cls._canonical_mapping_value(row.proxmox_node) == node
                and cls._canonical_mapping_value(row.cloudstack_host_name)
                == host_name
            )
        ]
        return candidates[0] if len(candidates) == 1 else None

    @classmethod
    def _unique_mapping_for_px(
        cls, session, px_cluster: str, px_node: str
    ) -> HostMapping | None:
        cluster = cls._canonical_mapping_value(px_cluster)
        node = cls._canonical_mapping_value(px_node)
        if not all((cluster, node)):
            return None
        candidates = [
            row for row in cls._globally_unique_host_mappings(session)
            if (
                cls._canonical_mapping_value(row.proxmox_cluster) == cluster
                and cls._canonical_mapping_value(row.proxmox_node) == node
            )
        ]
        return candidates[0] if len(candidates) == 1 else None

    @classmethod
    def _unique_mapping_for_cs_host(
        cls, session, cs_host_name: str
    ) -> HostMapping | None:
        host_name = cls._canonical_mapping_value(cs_host_name)
        if not host_name:
            return None
        candidates = [
            row for row in cls._globally_unique_host_mappings(session)
            if cls._canonical_mapping_value(row.cloudstack_host_name) == host_name
        ]
        return candidates[0] if len(candidates) == 1 else None

    @classmethod
    def _relationship_mappings(cls, session, px, cs):
        """Return authoritative (source, target) mappings for a durable pair."""
        if (
            not px
            or not cs
            or not px.current
            or not cs.current
            or not px.matched
            or not cs.matched
            or px.vm_type != "qemu"
            or px.template
            or cs.hypervisor != "External"
            or px.cloudstack_uuid != cs.uuid
            or cs.proxmox_id != px.id
            or cs.proxmox_vmid != px.vmid
            or px.match_source != cs.match_source
            or px.match_source not in {"manual", "auto_external_vmid_host"}
        ):
            return None
        source = cls._unique_mapping_for_cs_host(session, cs.host_name)
        target = cls._unique_mapping_for_px(session, px.cluster, px.node)
        return (source, target) if source and target else None

    def match_vms(self) -> dict:
        stats = {
            "matched": 0,
            "manual_matches": 0,
            "automatic_matches": 0,
            "unmatched_proxmox": 0,
            "unmatched_cloudstack": 0,
            "ambiguous": 0,
        }
        session = get_session()
        try:
            current_px = session.query(ProxmoxVM).filter_by(current=True).all()
            current_cs = session.query(CloudStackVM).filter_by(current=True).all()

            all_mapping_hosts = {
                self._canonical_mapping_value(mapping.cloudstack_host_name)
                for mapping in session.query(HostMapping).all()
                if self._canonical_mapping_value(mapping.cloudstack_host_name)
            }
            mappings = self._globally_unique_host_mappings(session)
            placements_by_cs_host = {}
            for mapping in mappings:
                host = self._canonical_mapping_value(mapping.cloudstack_host_name)
                cluster = self._canonical_mapping_value(mapping.proxmox_cluster)
                node = self._canonical_mapping_value(mapping.proxmox_node)
                placements_by_cs_host.setdefault(host, []).append((cluster, node))

            # Preserve only durable mutual links that still have complete,
            # globally unique source and destination placement. This keeps an
            # established relationship across a host migration while legacy,
            # hostless-fallback and malformed links are discarded.
            px_by_id = {px.id: px for px in current_px}
            cs_by_uuid = {cs.uuid: cs for cs in current_cs}
            persisted_pairs = []
            for px in current_px:
                if not px.cloudstack_uuid:
                    continue
                cs = cs_by_uuid.get(px.cloudstack_uuid)
                if self._relationship_mappings(session, px, cs):
                    persisted_pairs.append((px.id, cs.uuid, px.match_source))

            for px in session.query(ProxmoxVM).all():
                px.matched = False
                px.cloudstack_uuid = None
                px.match_source = ""
            for cs in session.query(CloudStackVM).all():
                cs.matched = False
                cs.proxmox_id = None
                cs.match_source = ""
            session.flush()

            claimed_px = set()
            claimed_cs = set()

            def apply_match(px, cs, source):
                px.matched = True
                px.cloudstack_uuid = cs.uuid
                px.match_source = source
                cs.matched = True
                cs.proxmox_id = px.id
                cs.match_source = source
                claimed_px.add(px.id)
                claimed_cs.add(cs.uuid)
                stats["matched"] += 1

            persisted_pairs_by_cs = {}
            for px_id, cs_uuid, source in persisted_pairs:
                persisted_pairs_by_cs.setdefault(cs_uuid, []).append((px_id, source))
            for cs_uuid, pairs in persisted_pairs_by_cs.items():
                if len(pairs) != 1:
                    stats["ambiguous"] += len(pairs)
                    continue
                px_id, source = pairs[0]
                px = px_by_id[px_id]
                cs = cs_by_uuid[cs_uuid]
                apply_match(px, cs, source)
                if source == "manual":
                    stats["manual_matches"] += 1
                else:
                    stats["automatic_matches"] += 1

            px_by_cluster_vmid = {}
            px_by_vmid = {}
            for px in current_px:
                if px.vm_type != "qemu" or px.template:
                    continue
                cluster = self._canonical_mapping_value(px.cluster)
                if not cluster or not self._canonical_mapping_value(px.node):
                    continue
                px_by_cluster_vmid.setdefault((cluster, px.vmid), []).append(px)
                px_by_vmid.setdefault(px.vmid, []).append(px)

            automatic_candidates = []
            for cs in current_cs:
                if cs.uuid in claimed_cs:
                    continue
                if cs.hypervisor != "External" or cs.proxmox_vmid is None:
                    continue

                candidates = []
                source = ""
                raw_host_name = cs.host_name or ""
                host_identity = self._canonical_mapping_value(raw_host_name)
                host_placements = placements_by_cs_host.get(
                    host_identity, []
                )
                if len(host_placements) == 1:
                    cluster, node = host_placements[0]
                    candidates = px_by_cluster_vmid.get(
                        (cluster, cs.proxmox_vmid), []
                    )
                    candidates = [
                        px for px in candidates
                        if self._canonical_mapping_value(px.node) == node
                    ]
                    source = "auto_external_vmid_host"
                elif len(host_placements) > 1:
                    stats["ambiguous"] += 1
                    continue
                elif host_identity in all_mapping_hosts:
                    # A persisted mapping exists for this host but is malformed
                    # or globally non-bijective, so it is ambiguous, not hostless.
                    stats["ambiguous"] += 1
                    continue
                elif (
                    raw_host_name == ""
                    and (cs.state or "").lower() in {"stopped", "error"}
                ):
                    # Only genuinely hostless stopped/error rows may use the
                    # fallback. A populated but unmapped host is incomplete
                    # placement, not hostless identity, and must fail closed.
                    names = {
                        value.lower() for value in (
                            cs.name, cs.display_name, cs.instance_name
                        ) if value
                    }
                    candidates = [
                        px for px in px_by_vmid.get(cs.proxmox_vmid, [])
                        if px.name and px.name.lower() in names
                    ]
                    source = "auto_external_vmid_name"
                else:
                    continue

                candidates = [px for px in candidates if px.id not in claimed_px]
                if len(candidates) == 1:
                    automatic_candidates.append((candidates[0], cs, source))
                elif len(candidates) > 1:
                    stats["ambiguous"] += 1

            # Enforce uniqueness in both directions before applying anything:
            # one CS row must identify one PX row, and that PX row must be
            # identified by exactly one current External CS row.
            candidates_by_px = {}
            for px, cs, source in automatic_candidates:
                candidates_by_px.setdefault(px.id, []).append((px, cs, source))
            for pairs in candidates_by_px.values():
                if len(pairs) != 1:
                    stats["ambiguous"] += len(pairs)
                    continue
                px, cs, source = pairs[0]
                apply_match(px, cs, source)
                stats["automatic_matches"] += 1

            stats["unmatched_proxmox"] = sum(
                px.vm_type == "qemu" and not px.template and not px.matched
                for px in current_px
            )
            stats["unmatched_cloudstack"] = sum(
                cs.hypervisor == "External" and not cs.matched
                for cs in current_cs
            )

            session.commit()
        except Exception as e:
            session.rollback()
            log.error("Match error (%s)", type(e).__name__)
        finally:
            session.close()

        return stats

    def sync_nics(self) -> dict:
        """Capture current Proxmox NIC inventory and matched CS snapshots.

        Config-derived MAC/bridge/VLAN data is collected for every current
        guest so unmatched QEMU adoption candidates can be reviewed. Guest
        agent IP enrichment and CloudStack DB reads remain limited to matched
        records.
        """
        stats = {
            "px_vms": 0,
            "cs_vms": 0,
            "errors": [],
            "collection_current": False,
        }
        self._nic_collection_ready = False
        session = get_session()
        try:
            session.query(ProxmoxVM).filter_by(current=True).update(
                {ProxmoxVM.config_current: False},
                synchronize_session=False,
            )
            session.query(CloudStackVM).filter_by(
                current=True, hypervisor="External"
            ).update(
                {CloudStackVM.nics_current: False},
                synchronize_session=False,
            )
            # Make invalidation durable before collecting/promoting snapshots.
            # A later rollback must not restore stale True markers.
            session.commit()
        except Exception as e:
            try:
                session.rollback()
            finally:
                session.close()
            log.error(
                "NIC/config freshness invalidation failed (%s)",
                type(e).__name__,
            )
            stats["errors"].append(
                f"NIC/config freshness invalidation failed: {type(e).__name__}"
            )
            return stats

        try:
            if not self.settings.nic_sync_enabled:
                return stats

            inventory_px = session.query(ProxmoxVM).filter_by(current=True).all()
            clients = {c.cluster_name: c for c in self.proxmox_clients}

            for px in inventory_px:
                client = clients.get(px.cluster)
                if not client:
                    continue
                try:
                    config = client.get_vm_config(px.node, px.vmid, px.vm_type)
                    nics = parse_nics(config)
                    disks = parse_disks(config, px.vm_type)
                    # Enrich missing IPs from the QEMU guest agent (best-effort)
                    if px.matched and px.vm_type == "qemu" and px.status == "running" and \
                            any(not n["ip"] for n in nics):
                        ifaces = client.get_guest_ifaces(px.node, px.vmid)
                        for n in nics:
                            if not n["ip"] and n["mac"] in ifaces:
                                n["ip"] = ifaces[n["mac"]]["ip"]
                                n["netmask"] = n["netmask"] or ifaces[n["mac"]]["netmask"]
                    px.networks = json.dumps(nics)
                    px.storage = json.dumps(disks)
                    px.config_current = True
                    stats["px_vms"] += 1
                except Exception as e:
                    log.error(
                        "PX config sync failed for %s (%s)",
                        px.id,
                        type(e).__name__,
                    )
                    stats["errors"].append(
                        f"PX config {px.id}: {type(e).__name__}"
                    )

            if self.cs_db:
                matched_cs = session.query(CloudStackVM).filter_by(
                    matched=True, current=True, hypervisor="External"
                ).all()
                for cs in matched_cs:
                    try:
                        vm = self.cs_db.get_vm_by_uuid(cs.uuid)
                        if not vm:
                            continue
                        cs_nics = self.cs_db.get_vm_nics(vm["id"])
                        if not isinstance(cs_nics, list):
                            raise ValueError("CloudStack NIC query returned invalid data")
                        cs.nics = json.dumps(cs_nics, default=str)
                        cs.nics_current = True
                        stats["cs_vms"] += 1
                    except Exception as e:
                        log.error(
                            "CS NIC sync failed for %s (%s)",
                            cs.uuid,
                            type(e).__name__,
                        )
                        stats["errors"].append(
                            f"CS NIC {cs.uuid}: {type(e).__name__}"
                        )

            session.commit()
            if not stats["errors"]:
                stats["collection_current"] = True
                self._nic_collection_ready = True
        except Exception as e:
            session.rollback()
            log.error(
                "NIC/config inventory transaction failed (%s)",
                type(e).__name__,
            )
            stats["errors"].append(
                f"NIC/config inventory transaction failed: {type(e).__name__}"
            )
        finally:
            session.close()
        return stats

    def full_sync(self) -> dict:
        # Retry a database that was unavailable at startup.  The configured
        # sync interval bounds retries and avoids a tight connection loop.
        if self.cs_db is None and self.settings.cloudstack_db.password:
            self.connect_cloudstack_db()
        log.info("Starting full sync...")
        self._inventory_collection_ready = False
        px_stats = self.sync_proxmox()
        cs_stats = self.sync_cloudstack()
        if not px_stats.get("errors") and not cs_stats.get("errors"):
            self._inventory_collection_ready = True
        match_stats = self.match_vms()
        nic_stats = self.sync_nics()

        reconcile_stats = None
        if self.settings.auto_reconcile and self.cs_db:
            reconcile_stats = self.reconcile_all()
            if reconcile_stats["updated"] > 0:
                # Re-sync CS to pick up our DB changes
                cs_stats = self.sync_cloudstack()

        nic_reconcile_stats = None
        if self.settings.auto_reconcile_nics and self.cs_db:
            if (
                nic_stats.get("collection_current")
                and self._nic_collection_ready
            ):
                nic_reconcile_stats = self.reconcile_nics_all()
                if nic_reconcile_stats.get("updated", 0) > 0:
                    self.sync_nics()
            else:
                nic_reconcile_stats = {
                    "drift_items": 0,
                    "updated": 0,
                    "failed": 0,
                    "dry_run": False,
                    "results": [],
                    "skipped": "NIC inventory collection is not current",
                }

        session = get_session()
        msg = (f"PX: {px_stats['vms_found']} found, {px_stats['vms_new']} new | "
               f"CS: {cs_stats['vms_found']} found | "
               f"Matched: {match_stats['matched']}, "
               f"Unmatched PX: {match_stats['unmatched_proxmox']}, "
               f"Unmatched CS: {match_stats['unmatched_cloudstack']}")
        if reconcile_stats:
            msg += f" | Reconciled: {reconcile_stats['updated']}"
        if nic_stats.get("px_vms") or nic_stats.get("cs_vms"):
            msg += f" | NICs: {nic_stats['px_vms']} PX, {nic_stats['cs_vms']} CS"
        if nic_reconcile_stats:
            msg += f" | NIC reconciled: {nic_reconcile_stats.get('updated', 0)}"
        try:
            self._log(session, "full_sync", msg)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        log.info(f"Sync complete. Matched: {match_stats['matched']}, "
                 f"Unmatched PX: {match_stats['unmatched_proxmox']}")

        result = {
            "proxmox": px_stats,
            "cloudstack": cs_stats,
            "matching": match_stats,
            "nics": nic_stats,
        }
        if reconcile_stats:
            result["reconcile"] = reconcile_stats
        if nic_reconcile_stats:
            result["nic_reconcile"] = nic_reconcile_stats
        return result

    def _resolve_host_db_id(self, host_ref: str) -> int | None:
        """Resolve a host identifier (integer ID string or UUID) to the CS DB integer ID."""
        if not host_ref or not self.cs_db:
            return None
        try:
            return int(host_ref)
        except ValueError:
            host = self.cs_db.get_host_by_uuid(host_ref)
            return host["id"] if host else None

    @classmethod
    def _reconciliation_mappings(cls, session, px, cs):
        """Return authoritative source and target placements eligible for writes."""
        return cls._relationship_mappings(session, px, cs)

    def detect_drift(self) -> list[dict]:
        if not getattr(self, "_inventory_collection_ready", False):
            return []
        drift = []
        session = get_session()
        try:
            matched = session.query(ProxmoxVM).filter_by(
                matched=True, current=True
            ).all()
            for px in matched:
                if not px.cloudstack_uuid:
                    continue
                cs = session.query(CloudStackVM).filter_by(uuid=px.cloudstack_uuid).first()
                if not cs or not cs.current or cs.hypervisor != "External":
                    continue

                mappings = self._reconciliation_mappings(session, px, cs)
                if not mappings:
                    continue
                source_mapping, target_mapping = mappings

                expected_cs_host = target_mapping.cloudstack_host_name
                source_cs_host = source_mapping.cloudstack_host_name
                host_mismatch = (
                    self._canonical_mapping_value(expected_cs_host)
                    != self._canonical_mapping_value(source_cs_host)
                )
                if host_mismatch:
                    drift.append({
                        "type": "host_mismatch",
                        "vm_name": px.name,
                        "proxmox_id": px.id,
                        "cloudstack_uuid": cs.uuid,
                        "cloudstack_host_id": source_mapping.cloudstack_host_id,
                        "source_cs_host_id": source_mapping.cloudstack_host_id,
                        "proxmox_host": px.node,
                        "expected_cs_host": expected_cs_host,
                        "actual_cs_host": cs.host_name,
                        "target_cs_host_id": target_mapping.cloudstack_host_id,
                    })

                state_map = {"running": "Running", "stopped": "Stopped"}
                expected_cs_state = state_map.get(px.status)
                if (
                    not host_mismatch
                    and expected_cs_state
                    and cs.state != expected_cs_state
                ):
                    drift.append({
                        "type": "state_mismatch",
                        "vm_name": px.name,
                        "proxmox_id": px.id,
                        "cloudstack_uuid": cs.uuid,
                        "cloudstack_host_id": source_mapping.cloudstack_host_id,
                        "source_cs_host_id": source_mapping.cloudstack_host_id,
                        "target_cs_host_id": target_mapping.cloudstack_host_id,
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

        identity = (
            drift_item.get("type"),
            drift_item.get("proxmox_id"),
            drift_item.get("cloudstack_uuid"),
            drift_item.get("source_cs_host_id"),
            drift_item.get("target_cs_host_id"),
        )
        authoritative = next((
            item for item in self.detect_drift()
            if (
                item.get("type"),
                item.get("proxmox_id"),
                item.get("cloudstack_uuid"),
                item.get("source_cs_host_id"),
                item.get("target_cs_host_id"),
            ) == identity
        ), None)
        if authoritative is None:
            return {"error": "Drift item is stale or not authoritative"}
        drift_item = authoritative

        vm_uuid = drift_item.get("cloudstack_uuid")
        drift_type = drift_item.get("type")
        session = get_session()

        try:
            if drift_type == "host_mismatch":
                target_host_ref = drift_item.get("target_cs_host_id")
                old_host_ref = drift_item.get("source_cs_host_id")
                if not target_host_ref:
                    return {"error": "No target host ID in drift item"}

                target_db_id = self._resolve_host_db_id(str(target_host_ref))
                old_db_id = self._resolve_host_db_id(str(old_host_ref)) if old_host_ref else None
                if not target_db_id:
                    return {"error": f"Could not resolve target host: {target_host_ref}"}
                if not old_db_id:
                    return {"error": f"Could not resolve source host: {old_host_ref}"}

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
                host_ref = drift_item.get("target_cs_host_id")
                host_db_id = self._resolve_host_db_id(str(host_ref)) if host_ref else None
                if not host_db_id:
                    return {"error": "Could not resolve current mapped CloudStack host"}

                if px_state == "running":
                    new_host = host_db_id
                    old_host = host_db_id
                else:
                    new_host = None
                    old_host = host_db_id

                ok = self.cs_db.update_vm_placement_and_state(
                    vm_uuid, new_host, power_state, vm_state, old_host
                )
                if ok:
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
            log.error(
                "Reconcile failed for %s (%s)", vm_uuid, type(e).__name__
            )
            return {
                "error": "Reconciliation failed",
                "error_type": type(e).__name__,
            }
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

    # --- NIC drift & reconciliation ---

    def _build_network_map(self, session) -> dict:
        """Lookup: (cluster, bridge, vlan) -> NetworkMapping. vlan None = untagged."""
        mappings = session.query(NetworkMapping).all()
        result = {}
        for m in mappings:
            result[(m.proxmox_cluster, m.proxmox_bridge.lower(), m.proxmox_vlan)] = m
        return result

    def _resolve_bridge_to_network(self, net_map: dict, cluster: str,
                                   bridge: str, vlan):
        """Find the NetworkMapping for a NIC: exact (bridge, vlan), then
        fall back to a bridge-only (untagged) mapping."""
        key = (cluster, (bridge or "").lower(), vlan)
        if key in net_map:
            return net_map[key]
        return net_map.get((cluster, (bridge or "").lower(), None))

    def detect_nic_drift(self) -> list[dict]:
        """Compare each matched VM's Proxmox NICs against its CloudStack NICs."""
        if not getattr(self, "_nic_collection_ready", False):
            return []
        drift = []
        session = get_session()
        try:
            net_map = self._build_network_map(session)
            # Resolve mapped network refs to CS DB integer ids once (cached)
            net_id_cache = {}

            def resolve_net_id(ref):
                if ref not in net_id_cache:
                    net = self.cs_db.get_network(str(ref)) if self.cs_db else None
                    net_id_cache[ref] = net
                return net_id_cache[ref]

            matched = session.query(ProxmoxVM).filter_by(
                matched=True, current=True, config_current=True
            ).all()
            for px in matched:
                if not px.cloudstack_uuid:
                    continue
                cs = session.query(CloudStackVM).filter_by(uuid=px.cloudstack_uuid).first()
                if (
                    not cs
                    or not cs.current
                    or not cs.nics_current
                    or cs.hypervisor != "External"
                ):
                    continue
                if not self._reconciliation_mappings(session, px, cs):
                    continue

                px_nics = json.loads(px.networks or "[]")
                cs_nics = json.loads(cs.nics or "[]")
                cs_by_mac = {(n.get("mac_address") or "").upper(): n
                             for n in cs_nics if n.get("mac_address")}
                matched_cs_ids = set()

                running = px.status == "running"
                for idx, pn in enumerate(px_nics):
                    mac = (pn.get("mac") or "").upper()
                    mapping = self._resolve_bridge_to_network(
                        net_map, px.cluster, pn.get("bridge"), pn.get("vlan"))
                    base = {
                        "vm_name": px.name,
                        "proxmox_id": px.id,
                        "cloudstack_uuid": cs.uuid,
                        "device_id": pn.get("device_id", idx),
                        "mac": mac,
                        "bridge": pn.get("bridge"),
                        "vlan": pn.get("vlan"),
                        "ip": pn.get("ip"),
                        "default_nic": pn.get("device_id", idx) == 0,
                        "running": running,
                    }

                    if not mapping:
                        drift.append({**base, "type": "unmapped_network"})
                        continue

                    net = resolve_net_id(mapping.cloudstack_network_id)
                    base["target_network_id"] = net["id"] if net else None
                    base["target_network_name"] = mapping.cloudstack_network_name
                    base["netmask"] = pn.get("netmask") or (net.get("netmask") if net else None)
                    base["gateway"] = pn.get("gateway") or (net.get("gateway") if net else None)

                    cn = cs_by_mac.get(mac)
                    if not cn:
                        drift.append({**base, "type": "nic_missing_in_cs"})
                        continue

                    matched_cs_ids.add(cn.get("id"))
                    base["cs_nic_id"] = cn.get("id")
                    if net and cn.get("network_id") != net["id"]:
                        drift.append({**base, "type": "nic_network_mismatch",
                                      "actual_network_id": cn.get("network_id")})
                    if pn.get("ip") and cn.get("ip4_address") != pn.get("ip"):
                        drift.append({**base, "type": "nic_ip_mismatch",
                                      "actual_ip": cn.get("ip4_address")})

                # CS NICs with no matching Proxmox MAC
                for cn in cs_nics:
                    if cn.get("id") in matched_cs_ids:
                        continue
                    drift.append({
                        "type": "nic_extra_in_cs",
                        "vm_name": px.name,
                        "proxmox_id": px.id,
                        "cloudstack_uuid": cs.uuid,
                        "cs_nic_id": cn.get("id"),
                        "mac": cn.get("mac_address"),
                        "ip": cn.get("ip4_address"),
                        "actual_network_id": cn.get("network_id"),
                    })
        finally:
            session.close()
        return drift

    def reconcile_nic(self, item: dict, dry_run: bool = False) -> dict:
        """Fix a single NIC drift item by writing to the CloudStack nics table."""
        if not self.cs_db:
            return {"error": "CloudStack DB not configured"}
        if not getattr(self, "_nic_collection_ready", False):
            return {"error": "NIC inventory collection is not current"}

        identity_keys = (
            "type", "proxmox_id", "cloudstack_uuid", "device_id",
            "cs_nic_id", "mac",
        )
        identity = tuple(item.get(key) for key in identity_keys)
        authoritative = next((
            drift for drift in self.detect_nic_drift()
            if tuple(drift.get(key) for key in identity_keys) == identity
        ), None)
        if authoritative is None:
            return {"error": "NIC drift item is stale or not authoritative"}
        item = authoritative

        drift_type = item.get("type")
        session = get_session()
        try:
            if drift_type == "nic_missing_in_cs":
                vm = self.cs_db.get_vm_by_uuid(item["cloudstack_uuid"])
                if not vm:
                    return {"error": f"VM {item['cloudstack_uuid']} not found in CS DB"}
                net_id = item.get("target_network_id")
                if not net_id:
                    return {"error": "Target network not resolved"}
                params = {
                    "instance_id": vm["id"],
                    "network_id": net_id,
                    "mac": item.get("mac"),
                    "device_id": item.get("device_id", 0),
                    "default_nic": item.get("default_nic", False),
                    "running": item.get("running", True),
                    "ip": item.get("ip"),
                    "netmask": item.get("netmask"),
                    "gateway": item.get("gateway"),
                }
                result = self.cs_db.insert_nic(params, dry_run=dry_run)
                if not dry_run and result.get("status") == "inserted":
                    self._log(session, "reconcile_nic",
                              f"Added NIC {item.get('mac')} -> {item.get('target_network_name')} "
                              f"for {item['vm_name']}")
                    session.commit()
                return result

            elif drift_type == "nic_mac_mismatch":
                return self.cs_db.update_nic(item["cs_nic_id"],
                                             {"mac_address": item.get("mac")}, dry_run=dry_run)

            elif drift_type == "nic_network_mismatch":
                if not item.get("target_network_id"):
                    return {"error": "Target network not resolved"}
                result = self.cs_db.update_nic(item["cs_nic_id"],
                                               {"network_id": item["target_network_id"]},
                                               dry_run=dry_run)
                if not dry_run and result.get("status") == "updated":
                    self._log(session, "reconcile_nic",
                              f"Moved NIC {item.get('mac')} to {item.get('target_network_name')} "
                              f"for {item['vm_name']}")
                    session.commit()
                return result

            elif drift_type == "nic_ip_mismatch":
                fields = {"ip4_address": item.get("ip")}
                if item.get("netmask"):
                    fields["netmask"] = item["netmask"]
                if item.get("gateway"):
                    fields["gateway"] = item["gateway"]
                return self.cs_db.update_nic(item["cs_nic_id"], fields, dry_run=dry_run)

            elif drift_type == "nic_extra_in_cs":
                result = self.cs_db.remove_nic(item["cs_nic_id"], dry_run=dry_run)
                if not dry_run and result.get("status") == "removed":
                    self._log(session, "reconcile_nic",
                              f"Removed stale NIC {item.get('mac')} from {item.get('vm_name', '')}")
                    session.commit()
                return result

            else:
                return {"error": f"Cannot reconcile NIC drift type: {drift_type}"}

        except Exception as e:
            log.error(
                "NIC reconcile failed (%s, %s)",
                drift_type,
                type(e).__name__,
            )
            return {
                "error": "NIC reconciliation failed",
                "error_type": type(e).__name__,
            }
        finally:
            session.close()

    def reconcile_nics_all(self, dry_run: bool = False) -> dict:
        """Reconcile all actionable NIC drift (everything except unmapped_network)."""
        if not self.cs_db:
            return {"error": "CloudStack DB not configured", "updated": 0, "failed": 0}
        if not getattr(self, "_nic_collection_ready", False):
            return {
                "error": "NIC inventory collection is not current",
                "drift_items": 0,
                "updated": 0,
                "failed": 0,
                "dry_run": dry_run,
                "results": [],
            }

        drift = self.detect_nic_drift()
        actionable = {"nic_missing_in_cs", "nic_mac_mismatch",
                      "nic_network_mismatch", "nic_ip_mismatch", "nic_extra_in_cs"}
        results, updated, failed = [], 0, 0
        for d in drift:
            if d["type"] not in actionable:
                continue
            r = self.reconcile_nic(d, dry_run=dry_run)
            results.append(r)
            if r.get("status") in ("inserted", "updated", "removed") or r.get("dry_run"):
                updated += 1
            else:
                failed += 1
        return {"drift_items": len(drift), "updated": updated,
                "failed": failed, "dry_run": dry_run, "results": results}

    def nic_comparison(self) -> list[dict]:
        """Per-matched-VM side-by-side NIC view for the UI."""
        rows = []
        session = get_session()
        try:
            drift = self.detect_nic_drift()
            drift_by_vm = {}
            for d in drift:
                drift_by_vm.setdefault(d["proxmox_id"], []).append(d["type"])

            matched = session.query(ProxmoxVM).filter_by(
                matched=True, current=True
            ).all()
            for px in matched:
                cs = session.query(CloudStackVM).filter_by(
                    uuid=px.cloudstack_uuid,
                    current=True,
                    hypervisor="External",
                ).first() if px.cloudstack_uuid else None
                rows.append({
                    "proxmox_id": px.id,
                    "vm_name": px.name,
                    "cloudstack_uuid": px.cloudstack_uuid,
                    "proxmox_nics": json.loads(px.networks or "[]"),
                    "cloudstack_nics": json.loads(cs.nics or "[]") if cs else [],
                    "proxmox_config_current": bool(
                        self._nic_collection_ready and px.config_current
                    ),
                    "cloudstack_nics_current": bool(
                        self._nic_collection_ready and cs and cs.nics_current
                    ),
                    "drift_types": drift_by_vm.get(px.id, []),
                })
        finally:
            session.close()
        return rows

    def _log(self, session, action: str, details: str, success: bool = True):
        session.add(SyncLog(action=action, details=details, success=success))
