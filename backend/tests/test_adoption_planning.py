import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from pydantic import ValidationError

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from adoption import adoption_manifest_hash, select_exact_service_offering
from config import AdoptionPolicy, Settings
from database import HostMapping, NetworkMapping, ProxmoxVM, get_session, init_db
from sync_engine import SyncEngine


DOMAIN_ID = "6317d9b3-8c8d-11f0-9947-00505689d4e8"
CUSTOM_OFFERING_ID = "518c4044-5347-4dea-a843-cf5a27cd2e88"
NETWORK_ID = "35b2aab0-d9a7-4c75-afaa-4486ff464e68"


class CatalogClient:
    def __init__(
        self,
        existing_macs=None,
        existing_ips=None,
        host_overrides=None,
        ip_ranges=None,
    ):
        self.existing_macs = existing_macs or []
        self.existing_ips = existing_ips or []
        self.host_overrides = host_overrides or {}
        self.ip_ranges = ip_ranges or [("10.120.0.2", "10.120.0.254")]

    def list_domains(self, **kwargs):
        return [{"id": DOMAIN_ID, "name": "ROOT", "path": "ROOT"}]

    def list_service_offerings(self):
        return [{
            "id": CUSTOM_OFFERING_ID,
            "name": "VM Flex Std",
            "iscustomized": True,
            "state": "Enabled",
        }]

    def list_networks(self):
        return [{"id": NETWORK_ID, "name": "Canary L2"}]

    def list_hosts(self, **kwargs):
        host = {
            "id": "host-uuid",
            "name": "p2-hv11.infra.example",
            "hypervisor": "External",
            "state": "Up",
            "resourcestate": "Enabled",
            "details": {
                "proxmox_cluster": "p2",
                "adoption_status_registry_required": "true",
            },
        }
        host.update(self.host_overrides)
        return [host]

    def list_vlan_ip_ranges(self):
        return [
            {"networkid": NETWORK_ID, "startip": start, "endip": end}
            for start, end in self.ip_ranges
        ]

    def list_virtual_machines(self, **kwargs):
        return [{
            "id": "existing",
            "nic": [
                {"macaddress": mac, "ipaddress": ip}
                for mac, ip in zip(
                    self.existing_macs + [""] * len(self.existing_ips),
                    self.existing_ips + [""] * len(self.existing_macs),
                )
            ],
        }]


class CandidateProxmoxClient:
    cluster_name = "p2"

    def __init__(self):
        self.agent_calls = []

    def get_vm_config(self, node, vmid, vm_type):
        return {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=120",
            "scsi0": "p2-rbd:vm-100-disk-0,size=32G",
        }

    def get_guest_ifaces(self, node, vmid):
        self.agent_calls.append((node, vmid))
        return {
            "AA:BB:CC:DD:EE:FF": {
                "ip": "10.120.0.100",
                "netmask": "255.255.255.0",
            }
        }


class AdoptionPlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'sync.db'}")
        self.original_policy = app_main.settings.adoption_policy

    def tearDown(self):
        app_main.settings.adoption_policy = self.original_policy
        self.tmp.cleanup()

    def test_policy_is_root_admin_only_and_requires_ids_when_enabled(self):
        with self.assertRaises(ValidationError):
            AdoptionPolicy.model_validate({"enabled": True, "account": "customer"})
        with self.assertRaises(ValidationError):
            AdoptionPolicy(enabled=True)
        policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
        )
        self.assertEqual("admin", policy.account)
        self.assertFalse(hasattr(policy, "project_id"))

    def test_exact_static_offering_wins_without_custom_parameters(self):
        plan, blockers = select_exact_service_offering(
            4,
            8192,
            [{
                "id": "static",
                "name": "VM 3030i",
                "cpunumber": 4,
                "memory": 8192,
                "iscustomized": False,
                "state": "Enabled",
            }],
            CUSTOM_OFFERING_ID,
        )
        self.assertEqual([], blockers)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual("static", plan["id"])
        self.assertIsNone(plan["details"])

    def test_custom_offering_persists_exact_cpu_and_memory(self):
        plan, blockers = select_exact_service_offering(
            8,
            32768,
            [{
                "id": CUSTOM_OFFERING_ID,
                "name": "VM Flex Std",
                "iscustomized": "true",
                "state": "Enabled",
            }],
            CUSTOM_OFFERING_ID,
        )
        self.assertEqual([], blockers)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual({"cpuNumber": 8, "memory": 32768}, plan["details"])

    def test_ambiguous_static_or_wrong_custom_offering_fails_closed(self):
        duplicate = {
            "name": "duplicate static",
            "cpunumber": 2,
            "memory": 2048,
            "iscustomized": False,
            "state": "Enabled",
        }
        plan, blockers = select_exact_service_offering(
            2,
            2048,
            [dict(duplicate, id="a"), dict(duplicate, id="b")],
            CUSTOM_OFFERING_ID,
        )
        self.assertIsNone(plan)
        self.assertEqual(["service_offering_exact_static_ambiguous"], blockers)

        plan, blockers = select_exact_service_offering(
            2,
            4096,
            [{
                "id": "wrong",
                "iscustomized": True,
                "state": "Enabled",
            }],
            CUSTOM_OFFERING_ID,
        )
        self.assertIsNone(plan)
        self.assertEqual(["service_offering_exact_match_unavailable"], blockers)

    def test_manifest_hash_is_canonical_and_resource_sensitive(self):
        networks = [
            {"device_id": 1, "mac": "00:00:00:00:00:02"},
            {"device_id": 0, "mac": "00:00:00:00:00:01"},
        ]
        storage = [
            {"device": "scsi1", "volume": "pool:b"},
            {"device": "scsi0", "volume": "pool:a"},
        ]
        first = adoption_manifest_hash(
            cluster="p2", node="p2-hv11", vmid=100, name="canary",
            cpus=4, memory_mb=8192, networks=networks, storage=storage,
        )
        reordered = adoption_manifest_hash(
            cluster="p2", node="p2-hv11", vmid=100, name="canary",
            cpus=4, memory_mb=8192,
            networks=list(reversed(networks)), storage=list(reversed(storage)),
        )
        resized = adoption_manifest_hash(
            cluster="p2", node="p2-hv11", vmid=100, name="canary",
            cpus=4, memory_mb=16384, networks=networks, storage=storage,
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, resized)
        self.assertEqual(64, len(first))

    def test_unmatched_running_qemu_gets_read_only_guest_agent_ip_enrichment(self):
        engine = SyncEngine.__new__(SyncEngine)
        engine.settings = Settings(nic_sync_enabled=True)
        client = CandidateProxmoxClient()
        engine.proxmox_clients = cast(Any, [client])
        engine.cs_client = None
        engine.cs_db = None
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = False

        session = get_session()
        session.add(ProxmoxVM(
            id="p2:100",
            cluster="p2",
            node="p2-hv11",
            vmid=100,
            name="canary",
            status="running",
            vm_type="qemu",
            template=False,
            current=True,
            matched=False,
            cpus=4,
            memory_mb=8192,
        ))
        session.commit()
        session.close()

        result = engine.sync_nics()
        self.assertTrue(result["collection_current"])
        self.assertEqual([("p2-hv11", 100)], client.agent_calls)
        session = get_session()
        networks = json.loads(session.get(ProxmoxVM, "p2:100").networks)
        session.close()
        self.assertEqual("10.120.0.100", networks[0]["ip"])

    def _add_complete_candidate(self):
        session = get_session()
        session.add(ProxmoxVM(
            id="p2:100",
            cluster="p2",
            node="p2-hv11",
            vmid=100,
            name="canary",
            status="running",
            vm_type="qemu",
            template=False,
            current=True,
            config_current=True,
            matched=False,
            cpus=8,
            memory_mb=32768,
            networks=json.dumps([{
                "device_id": 0,
                "model": "virtio",
                "mac": "AA:BB:CC:DD:EE:FF",
                "bridge": "vmbr0",
                "vlan": 120,
                "ip": "10.120.0.100",
                "netmask": "255.255.255.0",
                "gateway": "10.120.0.1",
            }]),
            storage=json.dumps([{
                "device": "scsi0",
                "volume": "p2-rbd:vm-100-disk-0",
                "storage": "p2-rbd",
                "size": "32G",
                "media": "disk",
            }]),
        ))
        session.add(HostMapping(
            proxmox_cluster="p2",
            proxmox_node="p2-hv11",
            cloudstack_host_id="host-uuid",
            cloudstack_host_name="p2-hv11.infra.example",
        ))
        session.add(NetworkMapping(
            proxmox_cluster="p2",
            proxmox_bridge="vmbr0",
            proxmox_vlan=120,
            cloudstack_network_id=NETWORK_ID,
            cloudstack_network_name="Canary L2",
        ))
        session.commit()
        session.close()

    def test_complete_candidate_gets_root_no_project_exact_read_only_plan(self):
        self._add_complete_candidate()
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client = CatalogClient()
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
        )

        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()

        row = result["candidates"][0]
        self.assertEqual(
            ["adopt_existing_orchestrator_not_implemented"],
            row["blockers"],
        )
        plan = row["adoption_plan"]
        self.assertEqual(
            {"account": "admin", "domain_id": DOMAIN_ID, "project_id": None},
            plan["owner"],
        )
        self.assertEqual(
            {"cpuNumber": 8, "memory": 32768},
            plan["service_offering"]["details"],
        )
        self.assertEqual(NETWORK_ID, plan["networks"][0]["cloudstack_network_id"])
        manifest = plan["manifest"]
        external_details = plan["extension_external_details"]
        self.assertEqual("net0", manifest["networks"][0]["device"])
        self.assertEqual("vmbr0", manifest["networks"][0]["bridge"])
        self.assertEqual("true", external_details["adopt_existing"])
        self.assertEqual(
            plan["manifest_sha256"],
            external_details["adopt_manifest_sha256"],
        )
        self.assertEqual(
            manifest,
            json.loads(external_details["adopt_manifest_json"]),
        )
        self.assertEqual(64, len(plan["manifest_sha256"]))

    def test_existing_cloudstack_mac_blocks_plan_and_suppresses_manifest(self):
        self._add_complete_candidate()
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client = CatalogClient(existing_macs=["AA:BB:CC:DD:EE:FF"])
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
        )

        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()
        row = result["candidates"][0]
        self.assertIn("nic0_mac_already_in_cloudstack", row["blockers"])
        self.assertIsNone(row["adoption_plan"]["manifest_sha256"])
        self.assertIsNone(row["adoption_plan"]["manifest"])
        self.assertIsNone(row["adoption_plan"]["extension_external_details"])

    def test_down_cloudstack_host_blocks_plan(self):
        self._add_complete_candidate()
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client = CatalogClient(host_overrides={"state": "Disconnected"})
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
        )

        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()
        row = result["candidates"][0]
        self.assertIn(
            "cloudstack_host_identity_or_state_mismatch", row["blockers"]
        )
        self.assertIsNone(row["adoption_plan"]["host"])
        self.assertIsNone(row["adoption_plan"]["manifest_sha256"])

    def test_host_status_registry_mode_must_be_explicit_and_cluster_bound(self):
        self._add_complete_candidate()
        cases = (
            {},
            {
                "proxmox_cluster": "p2",
                "adoption_status_registry_required": "false",
            },
            {
                "proxmox_cluster": "different-cluster",
                "adoption_status_registry_required": "true",
            },
        )
        for details in cases:
            with self.subTest(details=details):
                engine = Mock()
                engine._inventory_collection_ready = True
                engine._nic_collection_ready = True
                engine.cs_client = CatalogClient(
                    host_overrides={"details": details}
                )
                app_main.settings.adoption_policy = AdoptionPolicy(
                    enabled=True,
                    domain_id=DOMAIN_ID,
                    customized_service_offering_id=CUSTOM_OFFERING_ID,
                )

                with patch.object(app_main, "engine", engine):
                    result = app_main.list_adoption_candidates()
                row = result["candidates"][0]
                self.assertIn(
                    "cloudstack_host_adoption_status_registry_not_enabled",
                    row["blockers"],
                )
                self.assertIsNone(row["adoption_plan"]["host"])
                self.assertIsNone(row["adoption_plan"]["manifest_sha256"])

    def test_allocated_or_out_of_range_ip_blocks_plan(self):
        for client, expected in (
            (
                CatalogClient(existing_ips=["10.120.0.100"]),
                "nic0_ip_already_in_cloudstack",
            ),
            (
                CatalogClient(ip_ranges=[("10.120.0.2", "10.120.0.99")]),
                "nic0_ip_outside_cloudstack_range",
            ),
        ):
            with self.subTest(expected=expected):
                session = get_session()
                if session.get(ProxmoxVM, "p2:100") is None:
                    session.close()
                    self._add_complete_candidate()
                else:
                    session.close()
                engine = Mock()
                engine._inventory_collection_ready = True
                engine._nic_collection_ready = True
                engine.cs_client = client
                app_main.settings.adoption_policy = AdoptionPolicy(
                    enabled=True,
                    domain_id=DOMAIN_ID,
                    customized_service_offering_id=CUSTOM_OFFERING_ID,
                )
                with patch.object(app_main, "engine", engine):
                    result = app_main.list_adoption_candidates()
                row = result["candidates"][0]
                self.assertIn(expected, row["blockers"])
                self.assertIsNone(row["adoption_plan"]["manifest_sha256"])

    def test_catalog_error_does_not_leak_exception_and_never_plans(self):
        self._add_complete_candidate()
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client.list_domains.side_effect = RuntimeError("secret-marker")
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
        )

        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()
        self.assertNotIn("secret-marker", repr(result))
        row = result["candidates"][0]
        self.assertIn("adoption_catalog_lookup_failed", row["blockers"])
        self.assertIsNone(row["adoption_plan"]["manifest_sha256"])


if __name__ == "__main__":
    unittest.main()
