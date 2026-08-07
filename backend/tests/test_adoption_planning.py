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
from adoption import (
    adoption_manifest_hash,
    custom_root_disk_size_gib,
    select_exact_service_offering,
)
from config import AdoptionPolicy, Settings
from database import HostMapping, NetworkMapping, ProxmoxVM, get_session, init_db
from sync_engine import SyncEngine

DOMAIN_ID = "6317d9b3-8c8d-11f0-9947-00505689d4e8"
CUSTOM_OFFERING_ID = "518c4044-5347-4dea-a843-cf5a27cd2e88"
DISK_OFFERING_ID = "8f52fab6-1599-4c1b-97fa-e6199e9ca891"
NETWORK_ID = "35b2aab0-d9a7-4c75-afaa-4486ff464e68"
HOST_ID = "0f000000-0000-4000-8000-000000000001"
DB_HOST_ID = "30"
ZONE_ID = "0f000000-0000-4000-8000-000000000002"
CLUSTER_ID = "0f000000-0000-4000-8000-000000000003"
TEMPLATE_ID = "0f000000-0000-4000-8000-000000000004"
EXTENSION_ID = "0f000000-0000-4000-8000-000000000005"


class CatalogClient:
    def __init__(
        self,
        existing_macs=None,
        existing_ips=None,
        host_overrides=None,
        network_overrides=None,
        ip_ranges=None,
    ):
        self.existing_macs = existing_macs or []
        self.existing_ips = existing_ips or []
        self.host_overrides = host_overrides or {}
        self.network_overrides = network_overrides or {}
        self.host_call_kwargs = []
        self.ip_ranges = (
            [("10.120.0.2", "10.120.0.254")]
            if ip_ranges is None
            else ip_ranges
        )

    def list_domains(self, **kwargs):
        return [{"id": DOMAIN_ID, "name": "ROOT", "path": "ROOT"}]

    def list_service_offerings(self):
        return [{
            "id": CUSTOM_OFFERING_ID,
            "name": "VM Flex Std",
            "iscustomized": True,
            "state": "Enabled",
            "rootdisksize": 0,
            "diskofferingid": DISK_OFFERING_ID,
        }]

    def list_networks(self):
        network = {
            "id": NETWORK_ID,
            "name": "Canary L2",
            "type": "Isolated",
            "broadcastdomaintype": "Vlan",
            "vlan": "120",
            "state": "Setup",
            "canusefordeploy": True,
            "account": "admin",
            "domainid": DOMAIN_ID,
            "domain": "ROOT",
            "domainpath": "ROOT",
            "zoneid": ZONE_ID,
        }
        network.update(self.network_overrides)
        return [network]

    def list_hosts(self, **kwargs):
        self.host_call_kwargs.append(kwargs)
        host = {
            "id": HOST_ID,
            "name": "p2-hv11.infra.example",
            "hypervisor": "External",
            "state": "Up",
            "resourcestate": "Enabled",
            "zoneid": ZONE_ID,
            "details": {
                "proxmox_cluster": "p2",
                "adoption_status_registry_required": "true",
            },
        }
        host.update(self.host_overrides)
        if kwargs.get("details") != "all":
            host.pop("details", None)
        return [host]

    def list_clusters(self, **kwargs):
        return [{
            "id": CLUSTER_ID,
            "zoneid": ZONE_ID,
            "hypervisortype": "External",
            "extensionid": EXTENSION_ID,
        }]

    def list_templates(self, **kwargs):
        return [{
            "id": TEMPLATE_ID,
            "name": "Adoption metadata template",
            "zoneid": ZONE_ID,
            "hypervisor": "External",
            "extensionid": EXTENSION_ID,
            "isready": True,
        }]

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
        self.original_executor_enabled = app_main.settings.adoption_executor_enabled

    def tearDown(self):
        app_main.settings.adoption_policy = self.original_policy
        app_main.settings.adoption_executor_enabled = self.original_executor_enabled
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
        self.assertEqual(1200, policy.customized_service_offering_cpu_speed_mhz)
        self.assertFalse(hasattr(policy, "project_id"))
        for invalid_cpu_speed in (True, False, 1200.0, "1200"):
            with self.subTest(invalid_cpu_speed=invalid_cpu_speed):
                with self.assertRaises(ValidationError):
                    AdoptionPolicy.model_validate(
                        {
                            "customized_service_offering_cpu_speed_mhz": invalid_cpu_speed,
                        }
                    )

    def test_cloudstack_db_host_id_requires_canonical_positive_ascii_decimal(self):
        self.assertTrue(app_main._is_cloudstack_db_host_id("30"))
        for value in (None, "", "0", "030", "+30", " 30", "30 ", "٣٠"):
            with self.subTest(value=value):
                self.assertFalse(app_main._is_cloudstack_db_host_id(value))

    def test_executor_requires_registry_enabled_policy_and_template(self):
        with self.assertRaises(ValidationError):
            Settings(adoption_executor_enabled=True)
        with self.assertRaises(ValidationError):
            Settings(
                adoption_registry_enabled=True,
                adoption_registry_internal_token="r" * 32,
                adoption_executor_enabled=True,
                adoption_policy=AdoptionPolicy(
                    enabled=True,
                    domain_id=DOMAIN_ID,
                    customized_service_offering_id=CUSTOM_OFFERING_ID,
                ),
            )
        settings = Settings(
            adoption_registry_enabled=True,
            adoption_registry_internal_token="r" * 32,
            adoption_executor_enabled=True,
            adoption_policy=AdoptionPolicy(
                enabled=True,
                domain_id=DOMAIN_ID,
                customized_service_offering_id=CUSTOM_OFFERING_ID,
                template_id=TEMPLATE_ID,
            ),
        )
        self.assertTrue(settings.adoption_executor_enabled)

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
                "state": "Active",
            }],
            CUSTOM_OFFERING_ID,
            1200,
        )
        self.assertEqual([], blockers)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual("static", plan["id"])
        self.assertIsNone(plan["details"])

    def test_custom_offering_persists_exact_cpu_speed_and_memory(self):
        plan, blockers = select_exact_service_offering(
            8,
            32768,
            [{
                "id": CUSTOM_OFFERING_ID,
                "name": "VM Flex Std",
                "iscustomized": "true",
                "state": "Active",
            }],
            CUSTOM_OFFERING_ID,
            1200,
        )
        self.assertEqual([], blockers)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            {"cpuNumber": 8, "cpuSpeed": 1200, "memory": 32768},
            plan["details"],
        )

    def test_custom_offering_rejects_invalid_cpu_speed(self):
        offering = {
            "id": CUSTOM_OFFERING_ID,
            "name": "VM Flex Std",
            "iscustomized": True,
            "state": "Active",
        }
        for cpu_speed in (0, -1, True, 2147483648):
            with self.subTest(cpu_speed=cpu_speed):
                plan, blockers = select_exact_service_offering(
                    8,
                    8192,
                    [offering],
                    CUSTOM_OFFERING_ID,
                    cpu_speed,
                )
                self.assertIsNone(plan)
                self.assertEqual(
                    ["customized_service_offering_cpu_speed_invalid"],
                    blockers,
                )

    def test_custom_offering_rejects_malformed_root_disk_contract(self):
        for root_disk_size in (True, "0", -1, None):
            with self.subTest(root_disk_size=root_disk_size):
                plan, blockers = select_exact_service_offering(
                    8,
                    8192,
                    [{
                        "id": CUSTOM_OFFERING_ID,
                        "name": "VM Flex Std",
                        "iscustomized": True,
                        "state": "Active",
                        "rootdisksize": root_disk_size,
                        "diskofferingid": DISK_OFFERING_ID,
                    }],
                    CUSTOM_OFFERING_ID,
                    1200,
                )
                self.assertIsNone(plan)
                self.assertEqual(
                    ["service_offering_root_disk_contract_invalid"],
                    blockers,
                )

    def test_custom_offering_rejects_malformed_disk_offering_identity(self):
        for disk_offering_id in (
            None,
            "",
            " ",
            "custom-root-disk",
            DISK_OFFERING_ID.upper(),
            True,
        ):
            with self.subTest(disk_offering_id=disk_offering_id):
                plan, blockers = select_exact_service_offering(
                    8,
                    8192,
                    [{
                        "id": CUSTOM_OFFERING_ID,
                        "name": "VM Flex Std",
                        "iscustomized": True,
                        "state": "Active",
                        "rootdisksize": 0,
                        "diskofferingid": disk_offering_id,
                    }],
                    CUSTOM_OFFERING_ID,
                    1200,
                )
                self.assertIsNone(plan)
                self.assertEqual(
                    ["service_offering_root_disk_contract_invalid"],
                    blockers,
                )

    def test_custom_offering_rejects_incomplete_root_disk_contract(self):
        for root_disk_size in (None, "not-an-int", True, -1, 0, 20):
            with self.subTest(root_disk_size=root_disk_size):
                plan, blockers = select_exact_service_offering(
                    8,
                    8192,
                    [{
                        "id": CUSTOM_OFFERING_ID,
                        "name": "VM Flex Std",
                        "iscustomized": True,
                        "state": "Active",
                        "rootdisksize": root_disk_size,
                    }],
                    CUSTOM_OFFERING_ID,
                    1200,
                )
                self.assertIsNone(plan)
                self.assertEqual(
                    ["service_offering_root_disk_contract_invalid"],
                    blockers,
                )

        plan, blockers = select_exact_service_offering(
            8,
            8192,
            [{
                "id": CUSTOM_OFFERING_ID,
                "name": "VM Flex Std",
                "iscustomized": True,
                "state": "Active",
                "diskofferingid": DISK_OFFERING_ID,
            }],
            CUSTOM_OFFERING_ID,
            1200,
        )
        self.assertIsNone(plan)
        self.assertEqual(
            ["service_offering_root_disk_contract_invalid"],
            blockers,
        )

    def test_inactive_or_missing_custom_offering_state_fails_closed(self):
        for state in ("Inactive", "Disabled", None):
            with self.subTest(state=state):
                offering = {
                    "id": CUSTOM_OFFERING_ID,
                    "name": "VM Flex Std",
                    "iscustomized": True,
                }
                if state is not None:
                    offering["state"] = state
                plan, blockers = select_exact_service_offering(
                    8,
                    8192,
                    [offering],
                    CUSTOM_OFFERING_ID,
                    1200,
                )
                self.assertIsNone(plan)
                self.assertEqual(
                    ["service_offering_exact_match_unavailable"], blockers
                )

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
            1200,
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
            1200,
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
            cloudstack_host_id=DB_HOST_ID,
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
            ["adoption_executor_not_enabled"],
            row["blockers"],
        )
        plan = row["adoption_plan"]
        self.assertEqual(
            {"account": "admin", "domain_id": DOMAIN_ID, "project_id": None},
            plan["owner"],
        )
        self.assertEqual(
            {"cpuNumber": 8, "cpuSpeed": 1200, "memory": 32768},
            plan["service_offering"]["details"],
        )
        self.assertTrue(
            plan["service_offering"]["root_disk_size_customized"]
        )
        self.assertEqual(32, plan["service_offering"]["root_disk_size_gib"])
        self.assertEqual(NETWORK_ID, plan["networks"][0]["cloudstack_network_id"])
        self.assertEqual(HOST_ID, plan["host"]["id"])
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

    def test_unresolved_ip_keeps_exact_manifest_for_cloudstack_dhcp(self):
        self._add_complete_candidate()
        session = get_session()
        try:
            vm = session.query(ProxmoxVM).one()
            networks = json.loads(vm.networks)
            networks[0]["ip"] = None
            vm.networks = json.dumps(networks)
            session.commit()
        finally:
            session.close()

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
            row = app_main.list_adoption_candidates()["candidates"][0]

        self.assertNotIn("nic0_ip_unresolved", row["blockers"])
        plan = row["adoption_plan"]
        self.assertIsNone(plan["networks"][0]["ip"])
        manifest_network = plan["manifest"]["networks"][0]
        self.assertIsNone(manifest_network["ip"])
        self.assertEqual("cloudstack", manifest_network["ip_allocation"])
        self.assertNotIn("ip_override_required", manifest_network)
        self.assertEqual(64, len(plan["manifest_sha256"]))

    def test_custom_root_disk_size_requires_one_integral_qemu_disk(self):
        self.assertEqual(
            100,
            custom_root_disk_size_gib([{
                "device": "scsi0",
                "volume": "ceph:vm-124-disk-0",
                "size": "100G",
            }]),
        )
        for storage in (
            [],
            [{"device": "scsi0", "volume": "ceph:disk", "size": "1200M"}],
            [{"device": "efidisk0", "volume": "ceph:efi", "size": "4M"}],
            [
                {"device": "scsi0", "volume": "ceph:disk-0", "size": "32G"},
                {"device": "scsi1", "volume": "ceph:disk-1", "size": "64G"},
            ],
        ):
            with self.subTest(storage=storage):
                self.assertIsNone(custom_root_disk_size_gib(storage))

    def test_custom_root_disk_plan_blocks_ambiguous_multiple_disks(self):
        self._add_complete_candidate()
        session = get_session()
        try:
            vm = session.query(ProxmoxVM).one()
            storage = json.loads(vm.storage)
            storage.append({
                "device": "scsi1",
                "volume": "p2-rbd:vm-100-disk-1",
                "storage": "p2-rbd",
                "size": "64G",
                "media": "disk",
            })
            vm.storage = json.dumps(storage)
            session.commit()
        finally:
            session.close()

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
        self.assertIn(
            "custom_root_disk_size_missing_or_ambiguous",
            row["blockers"],
        )
        self.assertIsNone(row["adoption_plan"]["manifest_sha256"])

    def test_executor_ready_requires_exact_host_cluster_template_extension_chain(self):
        self._add_complete_candidate()
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client = CatalogClient(
            host_overrides={
                "id": HOST_ID,
                "zoneid": ZONE_ID,
                "clusterid": CLUSTER_ID,
            }
        )
        app_main.settings.adoption_executor_enabled = True
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
            template_id=TEMPLATE_ID,
        )

        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()

        row = result["candidates"][0]
        self.assertEqual([], row["blockers"])
        self.assertEqual("ready", row["disposition"])
        self.assertEqual(1, result["summary"]["ready"])
        self.assertEqual(ZONE_ID, row["adoption_plan"]["host"]["zone_id"])
        self.assertEqual(CLUSTER_ID, row["adoption_plan"]["host"]["cluster_id"])
        self.assertEqual(TEMPLATE_ID, row["adoption_plan"]["template"]["id"])
        self.assertEqual(
            EXTENSION_ID,
            row["adoption_plan"]["template"]["extension_id"],
        )

    def test_non_database_host_id_never_falls_back_to_hostname(self):
        self._add_complete_candidate()
        session = get_session()
        try:
            session.query(HostMapping).one().cloudstack_host_id = "stale-host-id"
            session.commit()
        finally:
            session.close()
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

        self.assertIn(
            "cloudstack_host_missing",
            result["candidates"][0]["blockers"],
        )

    def test_executor_template_extension_mismatch_fails_closed(self):
        self._add_complete_candidate()
        session = get_session()
        try:
            session.query(HostMapping).one().cloudstack_host_id = HOST_ID
            session.commit()
        finally:
            session.close()
        client = CatalogClient(
            host_overrides={
                "id": HOST_ID,
                "zoneid": ZONE_ID,
                "clusterid": CLUSTER_ID,
            }
        )
        client.list_templates = Mock(return_value=[{
            "id": TEMPLATE_ID,
            "zoneid": ZONE_ID,
            "hypervisor": "External",
            "extensionid": "0f000000-0000-4000-8000-000000000099",
            "isready": True,
        }])
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client = client
        app_main.settings.adoption_executor_enabled = True
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
            template_id=TEMPLATE_ID,
        )
        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()
        self.assertIn(
            "adoption_template_not_ready_or_ambiguous",
            result["candidates"][0]["blockers"],
        )

    def test_executor_accepts_ready_cross_zone_template_for_exact_extension(self):
        self._add_complete_candidate()
        session = get_session()
        try:
            session.query(HostMapping).one().cloudstack_host_id = HOST_ID
            session.commit()
        finally:
            session.close()
        client = CatalogClient(
            host_overrides={
                "id": HOST_ID,
                "zoneid": ZONE_ID,
                "clusterid": CLUSTER_ID,
            }
        )
        client.list_templates = Mock(return_value=[{
            "id": TEMPLATE_ID,
            "crosszones": True,
            "hypervisor": "External",
            "extensionid": EXTENSION_ID,
            "isready": True,
        }])
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client = client
        app_main.settings.adoption_executor_enabled = True
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
            template_id=TEMPLATE_ID,
        )
        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()
        self.assertEqual("ready", result["candidates"][0]["disposition"])
        self.assertEqual([], result["candidates"][0]["blockers"])

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

    def test_host_status_registry_accepts_exact_external_wire_aliases(self):
        self._add_complete_candidate()
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client = CatalogClient(host_overrides={
            "details": {
                "External:proxmox_cluster": "p2",
                "External:adoption_status_registry_required": "true",
            },
        })
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
        )

        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()

        row = result["candidates"][0]
        self.assertNotIn(
            "cloudstack_host_adoption_status_registry_not_enabled",
            row["blockers"],
        )
        self.assertIsNotNone(row["adoption_plan"]["host"])
        self.assertIsNotNone(row["adoption_plan"]["manifest_sha256"])

    def test_host_status_registry_rejects_conflicting_wire_aliases(self):
        self._add_complete_candidate()
        conflicts = (
            {
                "proxmox_cluster": "p2",
                "External:proxmox_cluster": "different-cluster",
                "adoption_status_registry_required": "true",
                "External:adoption_status_registry_required": "true",
            },
            {
                "proxmox_cluster": "p2",
                "External:proxmox_cluster": "p2",
                "adoption_status_registry_required": "true",
                "External:adoption_status_registry_required": "false",
            },
        )
        for details in conflicts:
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

    def test_host_catalog_requests_full_details_for_registry_gate(self):
        self._add_complete_candidate()
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        client = CatalogClient()
        engine.cs_client = client
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
        )

        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()

        self.assertEqual(
            [{"hypervisor": "External", "details": "all"}],
            client.host_call_kwargs,
        )
        self.assertNotIn(
            "cloudstack_host_adoption_status_registry_not_enabled",
            result["candidates"][0]["blockers"],
        )

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

    def test_exact_l2_network_uses_dhcp_without_static_ip(self):
        self._add_complete_candidate()
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client = CatalogClient(
            network_overrides={"type": "L2"},
            ip_ranges=[],
        )
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
        )

        with patch.object(app_main, "engine", engine):
            result = app_main.list_adoption_candidates()

        row = result["candidates"][0]
        self.assertNotIn("nic0_ip_outside_cloudstack_range", row["blockers"])
        self.assertNotIn("nic0_l2_network_identity_mismatch", row["blockers"])
        self.assertIsNone(row["adoption_plan"]["networks"][0]["ip"])
        self.assertIsNone(row["adoption_plan"]["networks"][0]["netmask"])
        self.assertIsNone(row["adoption_plan"]["networks"][0]["gateway"])
        self.assertEqual(
            "dhcp",
            row["adoption_plan"]["networks"][0]["ip_allocation"],
        )
        manifest_network = row["adoption_plan"]["manifest"]["networks"][0]
        self.assertIsNone(manifest_network["ip"])
        self.assertEqual(
            "dhcp",
            manifest_network["ip_allocation"],
        )
        self.assertNotIn("ip_override_required", manifest_network)

    def test_untagged_source_can_map_explicitly_to_vlan_one_l2_dhcp(self):
        self._add_complete_candidate()
        session = get_session()
        try:
            vm = session.query(ProxmoxVM).one()
            networks = json.loads(vm.networks)
            networks[0]["vlan"] = None
            networks[0]["ip"] = None
            vm.networks = json.dumps(networks)
            mapping = session.query(NetworkMapping).one()
            mapping.proxmox_vlan = None
            session.commit()
        finally:
            session.close()
        engine = Mock()
        engine._inventory_collection_ready = True
        engine._nic_collection_ready = True
        engine.cs_client = CatalogClient(
            network_overrides={"type": "L2", "vlan": "1"},
            ip_ranges=[],
        )
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=CUSTOM_OFFERING_ID,
        )

        with patch.object(app_main, "engine", engine):
            row = app_main.list_adoption_candidates()["candidates"][0]

        self.assertNotIn("nic0_l2_network_identity_mismatch", row["blockers"])
        manifest_network = row["adoption_plan"]["manifest"]["networks"][0]
        self.assertIsNone(manifest_network["tag"])
        self.assertIsNone(manifest_network["ip"])
        self.assertEqual("dhcp", manifest_network["ip_allocation"])
        self.assertNotIn("ip_override_required", manifest_network)

    def test_l2_dhcp_requires_exact_live_network_identity(self):
        self._add_complete_candidate()
        for field, value in (
            ("name", "Other L2"),
            ("vlan", "121"),
            ("account", "other"),
            ("domainid", "other-domain"),
            ("zoneid", "other-zone"),
            ("canusefordeploy", False),
        ):
            with self.subTest(field=field):
                engine = Mock()
                engine._inventory_collection_ready = True
                engine._nic_collection_ready = True
                engine.cs_client = CatalogClient(
                    network_overrides={"type": "L2", field: value},
                    ip_ranges=[],
                )
                app_main.settings.adoption_policy = AdoptionPolicy(
                    enabled=True,
                    domain_id=DOMAIN_ID,
                    customized_service_offering_id=CUSTOM_OFFERING_ID,
                )
                with patch.object(app_main, "engine", engine):
                    result = app_main.list_adoption_candidates()
                self.assertIn(
                    "nic0_l2_network_identity_mismatch",
                    result["candidates"][0]["blockers"],
                )

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
