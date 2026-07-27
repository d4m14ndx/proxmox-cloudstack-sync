import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from config import Settings
from database import (
    CloudStackVM,
    HostMapping,
    NetworkMapping,
    ProxmoxVM,
    get_session,
    init_db,
)
from fastapi import HTTPException
from proxmox_client import ProxmoxClient, parse_disks
from sync_engine import SyncEngine


class FakeProxmoxClient:
    def __init__(self, cluster_name, rows=None, error=None, configs=None):
        self.cluster_name = cluster_name
        self.rows = rows or []
        self.error = error
        self.configs = configs or {}
        self.config_calls = []
        self.agent_calls = []

    def get_all_vms(self):
        if self.error:
            raise self.error
        return list(self.rows)

    def normalize_vm(self, row):
        return dict(row)

    def get_vm_config(self, node, vmid, vm_type):
        self.config_calls.append((node, vmid, vm_type))
        return self.configs.get(vmid, {})

    def get_guest_ifaces(self, node, vmid):
        self.agent_calls.append((node, vmid))
        return {}


class FakeCloudStackClient:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def list_virtual_machines(self):
        if self.error:
            raise self.error
        return list(self.rows)


class InventoryTruthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'sync.db'}")
        self.engine = SyncEngine.__new__(SyncEngine)
        self.engine.settings = Settings(
            database_url=f"sqlite:///{Path(self.tmp.name) / 'sync.db'}",
            nic_sync_enabled=True,
        )
        self.engine.proxmox_clients = []
        self.engine.cs_client = None
        self.engine.cs_db = None
        self.engine.cs_db_last_error = None
        # Most tests construct an already-successful synthetic current cycle;
        # individual failure/restart tests explicitly clear these tokens.
        self.engine._inventory_collection_ready = True
        self.engine._nic_collection_ready = False

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def px_row(cluster, vmid, name, **overrides):
        row = {
            "id": f"{cluster}:{vmid}",
            "cluster": cluster,
            "node": "hv01",
            "vmid": vmid,
            "name": name,
            "status": "running",
            "vm_type": "qemu",
            "template": False,
            "cpus": 2,
            "memory_mb": 2048,
            "disk_gb": 20.0,
            "tags": "",
        }
        row.update(overrides)
        return row

    @staticmethod
    def add_px(session, cluster, vmid, name, **overrides):
        values = InventoryTruthTests.px_row(cluster, vmid, name, **overrides)
        values.setdefault("current", True)
        vm = ProxmoxVM(**values)
        session.add(vm)
        return vm

    @staticmethod
    def add_cs(session, uuid, name, **overrides):
        values = {
            "uuid": uuid,
            "name": name,
            "display_name": name,
            "instance_name": name,
            "state": "Running",
            "host_name": "",
            "host_id": "",
            "cluster_name": "",
            "zone_name": "",
            "cpus": 2,
            "memory_mb": 2048,
            "hypervisor": "External",
            "proxmox_vmid": None,
            "current": True,
        }
        values.update(overrides)
        vm = CloudStackVM(**values)
        session.add(vm)
        return vm

    def test_normalize_vm_preserves_template_flag(self):
        client = ProxmoxClient.__new__(ProxmoxClient)
        client.cluster_name = "p2"
        template = client.normalize_vm({
            "vmid": 100, "node": "hv01", "type": "qemu",
            "name": "template", "template": "1",
        })
        guest = client.normalize_vm({
            "vmid": 101, "node": "hv01", "type": "qemu",
            "name": "guest", "template": "0",
        })
        self.assertTrue(template["template"])
        self.assertFalse(guest["template"])

    def test_successful_proxmox_poll_marks_missing_rows_stale(self):
        session = get_session()
        self.add_px(session, "p2", 99, "disappeared", current=True)
        session.commit()
        session.close()

        current = self.px_row("p2", 100, "current")
        self.engine.proxmox_clients = [FakeProxmoxClient("p2", [current])]
        stats = self.engine.sync_proxmox()

        session = get_session()
        self.assertFalse(session.get(ProxmoxVM, "p2:99").current)
        self.assertTrue(session.get(ProxmoxVM, "p2:100").current)
        session.close()
        self.assertEqual(1, stats["vms_found"])
        self.assertEqual([], stats["errors"])

    def test_failed_proxmox_poll_fails_closed_without_raw_error(self):
        session = get_session()
        self.add_px(session, "p2", 99, "previous", current=True)
        session.commit()
        session.close()

        self.engine.proxmox_clients = [
            FakeProxmoxClient("p2", error=RuntimeError("sensitive-marker"))
        ]
        stats = self.engine.sync_proxmox()

        session = get_session()
        self.assertFalse(session.get(ProxmoxVM, "p2:99").current)
        session.close()
        self.assertNotIn("sensitive-marker", repr(stats))
        self.assertIn("RuntimeError", repr(stats))

    def test_unconfigured_sources_mark_persisted_inventory_stale(self):
        session = get_session()
        self.add_px(session, "removed-cluster", 99, "previous", current=True)
        self.add_cs(session, "old", "old", current=True)
        session.commit()
        session.close()

        self.engine.proxmox_clients = []
        self.engine.cs_client = None
        self.engine.sync_proxmox()
        cs_stats = self.engine.sync_cloudstack()

        session = get_session()
        self.assertFalse(session.get(ProxmoxVM, "removed-cluster:99").current)
        self.assertFalse(session.get(CloudStackVM, "old").current)
        session.close()
        self.assertEqual(["CloudStack not configured"], cs_stats["errors"])

    def test_cloudstack_sync_captures_vmid_and_marks_absent_rows_stale(self):
        session = get_session()
        self.add_cs(session, "old", "old", current=True)
        session.commit()
        session.close()

        self.engine.cs_client = FakeCloudStackClient([{
            "id": "new", "name": "new", "displayname": "new",
            "instancename": "i-1-100-VM", "state": "Running",
            "hostname": "p2-hv01.example", "hostid": "host-uuid",
            "clustername": "P2", "zonename": "P2", "cpunumber": 2,
            "memory": 2048, "hypervisor": "External",
            "details": {"proxmox_vmid": "100", "ignored": "not stored"},
        }])
        stats = self.engine.sync_cloudstack()

        session = get_session()
        self.assertFalse(session.get(CloudStackVM, "old").current)
        new = session.get(CloudStackVM, "new")
        self.assertTrue(new.current)
        self.assertEqual(100, new.proxmox_vmid)
        session.close()
        self.assertEqual(1, stats["vms_found"])

    def test_matching_uses_external_vmid_and_mapped_cluster_not_names(self):
        session = get_session()
        p2 = self.add_px(session, "p2", 100, "migrated-name")
        p2_id = p2.id
        self.add_px(session, "p3", 100, "other-cluster")
        self.add_cs(
            session, "external", "unrelated-display", proxmox_vmid=100,
            host_name="p2-hv01.example",
        )
        vmware = self.add_cs(
            session, "vmware", "migrated-name", hypervisor="VMware",
            proxmox_vmid=None, host_name="legacy-esx",
        )
        vmware_uuid = vmware.uuid
        session.add(HostMapping(
            proxmox_cluster="p2", proxmox_node="hv01",
            cloudstack_host_id="host-uuid",
            cloudstack_host_name="p2-hv01.example",
        ))
        session.commit()
        session.close()

        stats = self.engine.match_vms()
        session = get_session()
        self.assertTrue(session.get(ProxmoxVM, p2_id).matched)
        self.assertEqual("external", session.get(ProxmoxVM, p2_id).cloudstack_uuid)
        self.assertEqual("auto_external_vmid_host", session.get(ProxmoxVM, p2_id).match_source)
        self.assertFalse(session.get(CloudStackVM, vmware_uuid).matched)
        session.close()
        self.assertEqual(1, stats["automatic_matches"])
        self.assertEqual(1, stats["matched"])

    def test_duplicate_external_candidates_do_not_claim_one_proxmox_vm(self):
        session = get_session()
        self.add_px(session, "p2", 100, "candidate")
        self.add_cs(
            session, "external-a", "candidate-a", proxmox_vmid=100,
            host_name="p2-hv01.example",
        )
        self.add_cs(
            session, "external-b", "candidate-b", proxmox_vmid=100,
            host_name="p2-hv01.example",
        )
        session.add(HostMapping(
            proxmox_cluster="p2", proxmox_node="hv01",
            cloudstack_host_id="host-uuid",
            cloudstack_host_name="p2-hv01.example",
        ))
        session.commit()
        session.close()

        stats = self.engine.match_vms()
        session = get_session()
        self.assertFalse(session.get(ProxmoxVM, "p2:100").matched)
        self.assertFalse(session.get(CloudStackVM, "external-a").matched)
        self.assertFalse(session.get(CloudStackVM, "external-b").matched)
        session.close()
        self.assertEqual(0, stats["automatic_matches"])
        self.assertEqual(2, stats["ambiguous"])

    def test_manual_match_is_cleared_without_exact_vmid_and_host_placement(self):
        session = get_session()
        px = self.add_px(session, "p2", 100, "candidate")
        cs = self.add_cs(
            session, "external", "candidate", proxmox_vmid=None,
            host_name="p3-hv01.example",
        )
        px.matched = True
        px.cloudstack_uuid = cs.uuid
        px.match_source = "manual"
        cs.matched = True
        cs.proxmox_id = px.id
        cs.match_source = "manual"
        session.add(HostMapping(
            proxmox_cluster="p3", proxmox_node="hv01",
            cloudstack_host_id="p3-host",
            cloudstack_host_name="p3-hv01.example",
        ))
        session.commit()
        session.close()

        stats = self.engine.match_vms()
        session = get_session()
        self.assertFalse(session.get(ProxmoxVM, "p2:100").matched)
        self.assertFalse(session.get(CloudStackVM, "external").matched)
        session.close()
        self.assertEqual(0, stats["manual_matches"])

    def test_stopped_external_fallback_requires_vmid_and_exact_unique_name(self):
        session = get_session()
        p2 = self.add_px(session, "p2", 115, "p2-vgw02")
        p2_id = p2.id
        self.add_px(session, "p3", 115, "worker-node")
        cs = self.add_cs(
            session, "external", "P2-VGW02", proxmox_vmid=115,
            state="Stopped", host_name="",
        )
        cs_uuid = cs.uuid
        session.commit()
        session.close()

        stats = self.engine.match_vms()
        session = get_session()
        self.assertEqual(cs_uuid, session.get(ProxmoxVM, p2_id).cloudstack_uuid)
        self.assertEqual("auto_external_vmid_name", session.get(ProxmoxVM, p2_id).match_source)
        session.close()
        self.assertEqual(1, stats["automatic_matches"])

    def test_hostless_fallback_rejects_unmapped_running_and_ambiguous_placement(self):
        session = get_session()
        self.add_px(session, "p2", 120, "unmapped-host")
        self.add_cs(
            session,
            "external-unmapped",
            "unmapped-host",
            proxmox_vmid=120,
            state="Stopped",
            host_name="unknown-host.example",
        )
        self.add_px(session, "p2", 123, "running-unmapped-host")
        self.add_cs(
            session,
            "external-running-unmapped",
            "running-unmapped-host",
            proxmox_vmid=123,
            state="Running",
            host_name="unknown-running-host.example",
        )
        self.add_px(session, "p2", 124, "whitespace-host")
        self.add_cs(
            session,
            "external-whitespace",
            "whitespace-host",
            proxmox_vmid=124,
            state="Stopped",
            host_name="   ",
        )
        self.add_px(session, "p2", 121, "running-hostless")
        self.add_cs(
            session,
            "external-running-hostless",
            "running-hostless",
            proxmox_vmid=121,
            state="Running",
            host_name="",
        )
        self.add_px(session, "p2", 122, "ambiguous-host")
        self.add_cs(
            session,
            "external-ambiguous",
            "ambiguous-host",
            proxmox_vmid=122,
            state="Running",
            host_name="ambiguous.example",
        )
        session.add_all([
            HostMapping(
                proxmox_cluster="p2",
                proxmox_node="hv01",
                cloudstack_host_id="p2-host",
                cloudstack_host_name="ambiguous.example",
            ),
            HostMapping(
                proxmox_cluster="p3",
                proxmox_node="hv01",
                cloudstack_host_id="p3-host",
                cloudstack_host_name="ambiguous.example",
            ),
        ])
        session.commit()
        session.close()

        stats = self.engine.match_vms()
        session = get_session()
        self.assertEqual(0, session.query(ProxmoxVM).filter_by(matched=True).count())
        for uuid in (
            "external-unmapped",
            "external-running-unmapped",
            "external-whitespace",
            "external-running-hostless",
            "external-ambiguous",
        ):
            self.assertFalse(session.get(CloudStackVM, uuid).matched)
        session.close()
        self.assertEqual(0, stats["automatic_matches"])
        self.assertEqual(1, stats["ambiguous"])

    def test_hostless_fallback_is_inventory_only_and_never_reconciles(self):
        session = get_session()
        px = self.add_px(
            session,
            "p2",
            125,
            "fallback-only",
            status="running",
            config_current=True,
            networks="[]",
        )
        cs = self.add_cs(
            session,
            "external-fallback-only",
            "fallback-only",
            proxmox_vmid=125,
            state="Stopped",
            host_name="",
            nics_current=True,
            nics="[]",
        )
        px_id = px.id
        cs_uuid = cs.uuid
        session.commit()
        session.close()

        stats = self.engine.match_vms()
        session = get_session()
        self.assertEqual(
            "auto_external_vmid_name",
            session.get(ProxmoxVM, px_id).match_source,
        )
        self.assertTrue(session.get(CloudStackVM, cs_uuid).matched)
        session.close()
        self.assertEqual(1, stats["automatic_matches"])

        self.engine.cs_db = Mock()
        self.engine._nic_collection_ready = True
        self.assertEqual([], self.engine.detect_drift())
        self.assertEqual([], self.engine.detect_nic_drift())
        self.assertEqual(0, self.engine.reconcile_all()["updated"])
        self.assertEqual(0, self.engine.reconcile_nics_all()["updated"])
        self.engine.cs_db.update_vm_placement_and_state.assert_not_called()
        self.engine.cs_db.insert_nic.assert_not_called()
        self.engine.cs_db.update_nic.assert_not_called()
        self.engine.cs_db.remove_nic.assert_not_called()

    def test_manual_match_rejects_blank_cloudstack_host_mapping(self):
        session = get_session()
        self.add_px(session, "p2", 126, "manual-blank")
        self.add_cs(
            session,
            "external-manual-blank",
            "manual-blank",
            proxmox_vmid=126,
            state="Stopped",
            host_name="",
        )
        session.add(HostMapping(
            proxmox_cluster="p2",
            proxmox_node="hv01",
            cloudstack_host_id="host-uuid",
            cloudstack_host_name="",
        ))
        session.commit()
        session.close()

        with self.assertRaises(HTTPException) as rejected:
            app_main.manual_match(app_main.MatchRequest(
                proxmox_id="p2:126",
                cloudstack_uuid="external-manual-blank",
            ))
        self.assertEqual(409, rejected.exception.status_code)
        session = get_session()
        self.assertFalse(session.get(ProxmoxVM, "p2:126").matched)
        self.assertFalse(session.get(CloudStackVM, "external-manual-blank").matched)
        session.close()

        with self.assertRaises(HTTPException) as invalid_mapping:
            app_main.create_host_mapping(app_main.HostMappingRequest(
                proxmox_cluster="p2",
                proxmox_node="hv02",
                cloudstack_host_id="host-uuid-2",
                cloudstack_host_name="   ",
            ))
        self.assertEqual(422, invalid_mapping.exception.status_code)

    def test_conflicting_case_variant_mapping_blocks_matches_and_writes(self):
        session = get_session()
        px = self.add_px(
            session,
            "p2",
            128,
            "conflicted",
            status="running",
            config_current=True,
            networks="[]",
        )
        cs = self.add_cs(
            session,
            "external-conflicted",
            "conflicted",
            proxmox_vmid=128,
            state="Stopped",
            host_name="host-a.example",
            nics_current=True,
            nics="[]",
        )
        session.add_all([
            HostMapping(
                proxmox_cluster="p2",
                proxmox_node="hv01",
                cloudstack_host_id="host-a-id",
                cloudstack_host_name="host-a.example",
            ),
            HostMapping(
                proxmox_cluster="p2",
                proxmox_node="HV01",
                cloudstack_host_id="host-b-id",
                cloudstack_host_name="host-b.example",
            ),
        ])
        px_id = px.id
        cs_uuid = cs.uuid
        session.commit()
        session.close()

        match_stats = self.engine.match_vms()
        self.assertEqual(0, match_stats["automatic_matches"])

        with self.assertRaises(HTTPException) as manual_rejected:
            app_main.manual_match(app_main.MatchRequest(
                proxmox_id=px_id,
                cloudstack_uuid=cs_uuid,
            ))
        self.assertEqual(409, manual_rejected.exception.status_code)
        with self.assertRaises(HTTPException) as mapping_rejected:
            app_main.create_host_mapping(app_main.HostMappingRequest(
                proxmox_cluster="P2",
                proxmox_node="hv01",
                cloudstack_host_id="host-c-id",
                cloudstack_host_name="host-c.example",
            ))
        self.assertEqual(409, mapping_rejected.exception.status_code)

        session = get_session()
        px = session.get(ProxmoxVM, "p2:128")
        cs = session.get(CloudStackVM, "external-conflicted")
        px.matched = True
        px.cloudstack_uuid = cs.uuid
        px.match_source = "manual"
        cs.matched = True
        cs.proxmox_id = px.id
        cs.match_source = "manual"
        session.commit()
        session.close()

        self.engine.cs_db = Mock()
        self.engine._nic_collection_ready = True
        self.assertEqual([], self.engine.detect_drift())
        self.assertEqual([], self.engine.detect_nic_drift())
        self.assertEqual(0, self.engine.reconcile_all()["updated"])
        self.assertEqual(0, self.engine.reconcile_nics_all()["updated"])
        self.engine.cs_db.update_vm_placement_and_state.assert_not_called()
        self.engine.cs_db.insert_nic.assert_not_called()
        self.engine.cs_db.update_nic.assert_not_called()
        self.engine.cs_db.remove_nic.assert_not_called()

    def test_whitespace_mapping_id_and_cluster_block_all_writes(self):
        session = get_session()
        for vmid, cluster, node, host, host_id in (
            (129, "p2", "hv02", "whitespace-id.example", " "),
            (130, " ", "hv03", "whitespace-cluster.example", "host-130"),
        ):
            px = self.add_px(
                session,
                cluster,
                vmid,
                f"invalid-{vmid}",
                node=node,
                status="running",
                config_current=True,
                networks="[]",
            )
            cs = self.add_cs(
                session,
                f"external-invalid-{vmid}",
                f"invalid-{vmid}",
                proxmox_vmid=vmid,
                state="Stopped",
                host_name=host,
                nics_current=True,
                nics="[]",
            )
            px.matched = True
            px.cloudstack_uuid = cs.uuid
            px.match_source = "manual"
            cs.matched = True
            cs.proxmox_id = px.id
            cs.match_source = "manual"
            session.add(HostMapping(
                proxmox_cluster=cluster,
                proxmox_node=node,
                cloudstack_host_id=host_id,
                cloudstack_host_name=host,
            ))
        session.commit()
        session.close()

        self.engine.cs_db = Mock()
        self.engine.cs_db.get_host_by_uuid.return_value = {"id": 999}
        self.engine._nic_collection_ready = True
        self.assertEqual([], self.engine.detect_drift())
        self.assertEqual([], self.engine.detect_nic_drift())
        self.assertEqual(0, self.engine.reconcile_all()["updated"])
        self.assertEqual(0, self.engine.reconcile_nics_all()["updated"])
        self.engine.cs_db.update_vm_placement_and_state.assert_not_called()
        self.engine.cs_db.insert_nic.assert_not_called()
        self.engine.cs_db.update_nic.assert_not_called()
        self.engine.cs_db.remove_nic.assert_not_called()

    def test_complete_mapped_pair_can_reconcile_state_authoritatively(self):
        session = get_session()
        px = self.add_px(session, "p2", 127, "mapped", status="running")
        cs = self.add_cs(
            session,
            "external-mapped",
            "mapped",
            proxmox_vmid=127,
            state="Stopped",
            host_name="p2-hv01.example",
            host_id="101",
        )
        px.matched = True
        px.cloudstack_uuid = cs.uuid
        px.match_source = "auto_external_vmid_host"
        cs.matched = True
        cs.proxmox_id = px.id
        cs.match_source = "auto_external_vmid_host"
        session.add(HostMapping(
            proxmox_cluster="p2",
            proxmox_node="hv01",
            cloudstack_host_id="101",
            cloudstack_host_name="p2-hv01.example",
        ))
        session.commit()
        session.close()

        self.engine.cs_db = Mock()
        self.engine.cs_db.update_vm_placement_and_state.return_value = True
        self.engine._inventory_collection_ready = False
        self.assertEqual([], self.engine.detect_drift())
        self.engine._inventory_collection_ready = True
        drift = self.engine.detect_drift()
        self.assertEqual(["state_mismatch"], [item["type"] for item in drift])
        result = self.engine.reconcile_all()
        self.assertEqual(1, result["updated"])
        self.engine.cs_db.update_vm_placement_and_state.assert_called_once_with(
            "external-mapped", 101, "PowerOn", "Running", 101
        )

    def test_durable_pair_detects_and_reconciles_host_migration(self):
        session = get_session()
        px = self.add_px(
            session, "p2", 131, "migrated", node="hv02", status="running"
        )
        cs = self.add_cs(
            session,
            "external-migrated",
            "migrated",
            proxmox_vmid=131,
            state="Running",
            host_name="p2-hv01.example",
            host_id="1",
        )
        px.matched = True
        px.cloudstack_uuid = cs.uuid
        px.match_source = "auto_external_vmid_host"
        cs.matched = True
        cs.proxmox_id = px.id
        cs.match_source = "auto_external_vmid_host"
        session.add_all([
            HostMapping(
                proxmox_cluster="p2",
                proxmox_node="hv01",
                cloudstack_host_id="1",
                cloudstack_host_name="p2-hv01.example",
            ),
            HostMapping(
                proxmox_cluster="p2",
                proxmox_node="hv02",
                cloudstack_host_id="2",
                cloudstack_host_name="p2-hv02.example",
            ),
        ])
        session.commit()
        session.close()

        match_stats = self.engine.match_vms()
        self.assertEqual(1, match_stats["automatic_matches"])
        drift = self.engine.detect_drift()
        self.assertEqual(["host_mismatch"], [item["type"] for item in drift])
        self.assertEqual("1", drift[0]["source_cs_host_id"])
        self.assertEqual("2", drift[0]["target_cs_host_id"])

        # A mapping mutation between detection and reconciliation makes the
        # original item stale rather than redirecting its write.
        session = get_session()
        target = session.query(HostMapping).filter_by(proxmox_node="hv02").one()
        target.cloudstack_host_id = "3"
        session.commit()
        session.close()
        self.engine.cs_db = Mock()
        stale_result = self.engine.reconcile_vm(drift[0])
        self.assertIn("stale", stale_result["error"])
        self.engine.cs_db.update_vm_placement_and_state.assert_not_called()

        session = get_session()
        target = session.query(HostMapping).filter_by(proxmox_node="hv02").one()
        target.cloudstack_host_id = "2"
        session.commit()
        session.close()
        self.engine.cs_db.update_vm_placement_and_state.return_value = True
        result = self.engine.reconcile_all()
        self.assertEqual(1, result["updated"])
        self.engine.cs_db.update_vm_placement_and_state.assert_called_once_with(
            "external-migrated", 2, "PowerOn", "Running", 1
        )

    def test_templates_lxc_stale_and_vmware_are_never_automatic_matches(self):
        session = get_session()
        self.add_px(session, "p2", 100, "template", template=True)
        self.add_px(session, "p2", 101, "container", vm_type="lxc")
        stale = self.add_px(session, "p2", 102, "stale", current=False)
        stale.matched = True
        stale.cloudstack_uuid = "stale-cs"
        self.add_cs(session, "template-cs", "template", proxmox_vmid=100)
        self.add_cs(session, "lxc-cs", "container", proxmox_vmid=101)
        self.add_cs(session, "stale-cs", "stale", proxmox_vmid=102, current=False)
        self.add_cs(session, "vmware", "template", hypervisor="VMware")
        session.commit()
        session.close()

        stats = self.engine.match_vms()
        session = get_session()
        self.assertEqual(0, session.query(ProxmoxVM).filter_by(matched=True).count())
        self.assertFalse(session.get(ProxmoxVM, "p2:102").matched)
        session.close()
        self.assertEqual(0, stats["matched"])

    def test_nic_inventory_includes_current_unmatched_but_not_stale(self):
        session = get_session()
        self.add_px(session, "p2", 100, "candidate", current=True)
        self.add_px(session, "p2", 101, "stale", current=False)
        session.commit()
        session.close()

        client = FakeProxmoxClient("p2", configs={
            100: {
                "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=120",
                "scsi0": "p2-rbd:vm-100-disk-0,size=32G,discard=on",
            },
            101: {"net0": "virtio=00:11:22:33:44:55,bridge=vmbr0"},
        })
        self.engine.proxmox_clients = [client]
        stats = self.engine.sync_nics()

        session = get_session()
        candidate = session.get(ProxmoxVM, "p2:100")
        self.assertIn("AA:BB:CC:DD:EE:FF", candidate.networks)
        self.assertIn("p2-rbd:vm-100-disk-0", candidate.storage)
        self.assertTrue(candidate.config_current)
        self.assertEqual("", session.get(ProxmoxVM, "p2:101").networks)
        session.close()
        self.assertEqual([("hv01", 100, "qemu")], client.config_calls)
        self.assertEqual([("hv01", 100)], client.agent_calls)
        self.assertEqual(1, stats["px_vms"])

    def test_failed_or_disabled_config_collection_invalidates_snapshot(self):
        session = get_session()
        existing = self.add_px(
            session, "p2", 100, "candidate", current=True,
            config_current=True,
        )
        existing.networks = '[{"mac":"old"}]'
        session.commit()
        session.close()

        client = FakeProxmoxClient("p2")
        client.get_vm_config = Mock(side_effect=RuntimeError("sensitive-marker"))
        self.engine.proxmox_clients = [client]
        stats = self.engine.sync_nics()

        session = get_session()
        candidate = session.get(ProxmoxVM, "p2:100")
        self.assertFalse(candidate.config_current)
        self.assertEqual('[{"mac":"old"}]', candidate.networks)
        session.close()
        self.assertEqual(["PX config p2:100: RuntimeError"], stats["errors"])
        preflight = app_main.list_adoption_candidates()
        row = preflight["candidates"][0]
        self.assertIn("config_snapshot_not_current", row["blockers"])

        session = get_session()
        session.get(ProxmoxVM, "p2:100").config_current = True
        session.commit()
        session.close()
        self.engine.settings.nic_sync_enabled = False
        self.engine.sync_nics()
        session = get_session()
        self.assertFalse(session.get(ProxmoxVM, "p2:100").config_current)
        session.close()

    def test_stale_nic_snapshots_cannot_reconcile_cloudstack(self):
        session = get_session()
        px = self.add_px(
            session, "p2", 100, "candidate", current=True,
            config_current=True,
        )
        px.matched = True
        px.cloudstack_uuid = "external"
        px.networks = (
            '[{"device_id":0,"mac":"AA:BB:CC:DD:EE:FF",'
            '"bridge":"vmbr0","vlan":120}]'
        )
        cs = self.add_cs(
            session, "external", "candidate", proxmox_vmid=100,
            host_name="p2-hv01.example",
        )
        cs.matched = True
        cs.proxmox_id = px.id
        cs.nics = "[]"
        cs.nics_current = True
        session.add(NetworkMapping(
            proxmox_cluster="p2",
            proxmox_bridge="vmbr0",
            proxmox_vlan=120,
            cloudstack_network_id="network-uuid",
            cloudstack_network_name="guest120",
        ))
        session.commit()
        session.close()

        client = FakeProxmoxClient("p2")
        client.get_vm_config = Mock(side_effect=RuntimeError("sensitive-marker"))
        self.engine.proxmox_clients = [client]
        self.engine.cs_db = Mock()
        self.engine.cs_db.get_vm_by_uuid.side_effect = RuntimeError(
            "sensitive-marker"
        )
        self.engine.sync_nics()

        session = get_session()
        self.assertFalse(session.get(ProxmoxVM, "p2:100").config_current)
        self.assertFalse(session.get(CloudStackVM, "external").nics_current)
        session.close()
        result = self.engine.reconcile_nics_all()
        self.assertEqual(0, result["drift_items"])
        self.assertEqual(0, result["updated"])
        self.engine.cs_db.insert_nic.assert_not_called()
        self.engine.cs_db.update_nic.assert_not_called()
        self.engine.cs_db.remove_nic.assert_not_called()

    def test_marker_reset_commit_failure_suppresses_automatic_nic_writes(self):
        session = get_session()
        px = self.add_px(
            session, "p2", 100, "candidate", current=True,
            config_current=True,
        )
        px.matched = True
        px.cloudstack_uuid = "external"
        px.networks = (
            '[{"device_id":0,"mac":"AA:BB:CC:DD:EE:FF",'
            '"bridge":"vmbr0","vlan":120}]'
        )
        cs = self.add_cs(
            session, "external", "candidate", proxmox_vmid=100,
            host_name="p2-hv01.example", nics_current=True,
        )
        cs.matched = True
        cs.proxmox_id = px.id
        cs.nics = "[]"
        px_id = px.id
        cs_uuid = cs.uuid
        session.commit()
        session.close()

        self.engine.settings.auto_reconcile_nics = True
        self.engine.cs_db = Mock()
        self.engine._nic_collection_ready = True
        self.engine.sync_proxmox = Mock(return_value={
            "vms_found": 1, "vms_new": 0,
        })
        self.engine.sync_cloudstack = Mock(return_value={"vms_found": 1})
        self.engine.match_vms = Mock(return_value={
            "matched": 1,
            "unmatched_proxmox": 0,
            "unmatched_cloudstack": 0,
        })

        failing_session = get_session()
        failing_session.rollback = Mock(wraps=failing_session.rollback)
        failing_session.close = Mock(wraps=failing_session.close)
        failing_session.commit = Mock(side_effect=RuntimeError("commit failed"))
        final_log_session = get_session()
        with patch(
            "sync_engine.get_session",
            side_effect=[failing_session, final_log_session],
        ):
            result = self.engine.full_sync()

        self.assertFalse(self.engine._nic_collection_ready)
        failing_session.rollback.assert_called_once_with()
        failing_session.close.assert_called_once_with()
        self.assertIn("freshness invalidation failed", result["nics"]["errors"][0])
        self.assertEqual(
            "NIC inventory collection is not current",
            result["nic_reconcile"]["skipped"],
        )
        session = get_session()
        self.assertTrue(session.get(ProxmoxVM, px_id).config_current)
        self.assertTrue(session.get(CloudStackVM, cs_uuid).nics_current)
        session.close()
        self.engine.cs_db.insert_nic.assert_not_called()
        self.engine.cs_db.update_nic.assert_not_called()
        self.engine.cs_db.remove_nic.assert_not_called()

    def test_parse_disks_keeps_storage_identity_without_unrelated_config(self):
        disks = parse_disks({
            "scsi0": "p2-rbd:vm-100-disk-0,size=32G,discard=on,iothread=1",
            "ide2": "local:iso/ubuntu.iso,media=cdrom",
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
            "description": "must not be copied",
        })
        self.assertEqual(["ide2", "scsi0"], [d["device"] for d in disks])
        root = next(d for d in disks if d["device"] == "scsi0")
        self.assertEqual("p2-rbd", root["storage"])
        self.assertEqual("32G", root["size"])
        self.assertNotIn("description", repr(disks))

    def test_adoption_preflight_is_read_only_and_fail_closed(self):
        self.engine._nic_collection_ready = True
        session = get_session()
        candidate = self.add_px(session, "p2", 100, "candidate")
        candidate.networks = (
            '[{"device_id": 0, "mac": "AA:BB:CC:DD:EE:FF", '
            '"bridge": "vmbr0", "vlan": 120}]'
        )
        candidate.storage = (
            '[{"device": "scsi0", "volume": "p2-rbd:vm-100-disk-0", '
            '"storage": "p2-rbd", "media": "disk"}]'
        )
        candidate.config_current = True
        self.add_px(session, "p2", 101, "container", vm_type="lxc")
        session.add(HostMapping(
            proxmox_cluster="p2", proxmox_node="hv01",
            cloudstack_host_id="host", cloudstack_host_name="host.example",
        ))
        session.commit()
        session.close()

        with patch.object(app_main, "engine", self.engine):
            result = app_main.list_adoption_candidates()
        by_id = {row["proxmox_id"]: row for row in result["candidates"]}
        self.assertEqual("blocked", by_id["p2:100"]["disposition"])
        self.assertIn(
            "network_mapping_missing:vmbr0:120",
            by_id["p2:100"]["blockers"],
        )
        self.assertIn(
            "adoption_policy_not_enabled",
            by_id["p2:100"]["blockers"],
        )
        self.assertEqual("inventory_only", by_id["p2:101"]["disposition"])
        self.assertEqual(0, result["summary"]["ready"])

    def test_cloudstack_list_can_be_limited_to_external_candidates(self):
        session = get_session()
        self.add_cs(session, "external", "external", hypervisor="External")
        self.add_cs(session, "vmware", "vmware", hypervisor="VMware")
        session.commit()
        session.close()

        result = app_main.list_cloudstack_vms(hypervisor="External")
        self.assertEqual(["external"], [row["uuid"] for row in result])

    def test_cloudstack_vmid_extractor_accepts_dict_and_list_only(self):
        self.assertEqual(123, self.engine._cloudstack_proxmox_vmid(
            {"details": {"proxmox_vmid": "123"}}
        ))
        self.assertEqual(124, self.engine._cloudstack_proxmox_vmid(
            {"details": [{"name": "proxmox_vmid", "value": "124"}]}
        ))
        self.assertIsNone(self.engine._cloudstack_proxmox_vmid(
            {"details": {"proxmox_vmid": "not-an-int"}}
        ))

    def test_lightweight_migration_adds_inventory_truth_columns(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            "CREATE TABLE proxmox_vms (id VARCHAR PRIMARY KEY, networks TEXT)"
        )
        conn.execute(
            "CREATE TABLE cloudstack_vms (uuid VARCHAR PRIMARY KEY, networks TEXT)"
        )
        conn.commit()
        conn.close()

        init_db(f"sqlite:///{legacy_path}")
        conn = sqlite3.connect(legacy_path)
        px_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(proxmox_vms)")
        }
        cs_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(cloudstack_vms)")
        }
        conn.close()
        self.assertTrue(
            {
                "storage", "template", "current", "config_current",
                "match_source",
            } <= px_columns
        )
        self.assertTrue(
            {"proxmox_vmid", "current", "match_source", "nics_current"}
            <= cs_columns
        )

    def test_reconciliation_rejects_caller_supplied_stale_payload(self):
        self.engine.cs_db = Mock()
        self.engine.detect_drift = Mock(return_value=[])
        result = self.engine.reconcile_vm({
            "type": "host_mismatch",
            "proxmox_id": "p2:100",
            "cloudstack_uuid": "forged",
            "target_cs_host_id": "1",
        })
        self.assertEqual(
            {"error": "Drift item is stale or not authoritative"}, result
        )
        self.engine.cs_db.update_vm_placement_and_state.assert_not_called()

    def test_nic_reconciliation_rejects_caller_supplied_stale_payload(self):
        self.engine.cs_db = Mock()
        self.engine._nic_collection_ready = True
        self.engine.detect_nic_drift = Mock(return_value=[])
        result = self.engine.reconcile_nic({
            "type": "nic_missing_in_cs",
            "proxmox_id": "p2:100",
            "cloudstack_uuid": "forged",
            "device_id": 0,
            "mac": "AA:BB:CC:DD:EE:FF",
        })
        self.assertEqual(
            {"error": "NIC drift item is stale or not authoritative"}, result
        )
        self.engine.cs_db.insert_nic.assert_not_called()


class RegistrationGuardTests(unittest.TestCase):
    def test_legacy_registration_and_repair_are_permanently_unavailable(self):
        req = app_main.RegisterRequest(
            proxmox_id="p2:100", service_offering_id=1,
            account_id=1, domain_id=1,
        )
        with self.assertRaises(HTTPException) as registration:
            app_main.register_vm(req)
        with self.assertRaises(HTTPException) as repair:
            app_main.removed_generic_repair("uuid")
        self.assertEqual(410, registration.exception.status_code)
        self.assertEqual(410, repair.exception.status_code)
        self.assertFalse(hasattr(
            app_main.settings, "legacy_direct_db_registration_enabled"
        ))


if __name__ == "__main__":
    unittest.main()
