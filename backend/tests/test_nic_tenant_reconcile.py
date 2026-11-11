import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import Settings
from database import (
    CloudStackVM,
    HostMapping,
    NetworkMapping,
    ProxmoxVM,
    get_session,
    init_db,
)
from proxmox_client import ProxmoxClient
from sync_engine import SyncEngine

MAC = "AA:BB:CC:DD:EE:FF"
ADMIN, TENANT = 2, 7


def net(net_id, account, vlan=300, guest_type="L2"):
    return {
        "id": net_id, "uuid": f"net-{net_id}", "name": f"net{net_id}",
        "broadcast_uri": f"vlan://{vlan}", "guest_type": guest_type,
        "account_id": account, "data_center_id": 1,
        "gateway": None, "cidr": None, "netmask": None,
    }


class TenantAwareNicDriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        url = f"sqlite:///{Path(self.tmp.name) / 'sync.db'}"
        init_db(url)
        self.engine = SyncEngine.__new__(SyncEngine)
        self.engine.settings = Settings(database_url=url, nic_sync_enabled=True)
        self.engine.proxmox_clients = []
        self.engine.cs_client = None
        self.engine._inventory_collection_ready = True
        self.engine._nic_collection_ready = True
        self.engine.cs_db = Mock()
        self.engine.cs_db.list_guest_networks.return_value = [
            net(211, ADMIN), net(291, TENANT), net(231, ADMIN, 1061, "Isolated"),
        ]
        self.engine.cs_db.get_external_vm_owners.return_value = {
            "vm-uuid": {"uuid": "vm-uuid", "account_id": TENANT, "data_center_id": 1},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, px_nic, cs_nics, vlan=300):
        session = get_session()
        session.add(ProxmoxVM(
            id="p2:100", cluster="p2", node="hv01", vmid=100, name="vm",
            status="running", vm_type="qemu", template=False, current=True,
            config_current=True, cpus=2, memory_mb=2048, disk_gb=20.0, tags="",
            matched=True, cloudstack_uuid="vm-uuid", match_source="manual",
            networks=json.dumps([{"device_id": 0, "mac": MAC, "bridge": "vmbr2",
                                  "vlan": vlan, **px_nic}]),
        ))
        session.add(CloudStackVM(
            uuid="vm-uuid", name="vm", display_name="vm", instance_name="i-7-1-VM",
            state="Running", host_name="p2-hv01.example", hypervisor="External",
            proxmox_vmid=100, current=True, matched=True, proxmox_id="p2:100",
            match_source="manual", nics=json.dumps(cs_nics), nics_current=True,
        ))
        session.add(HostMapping(
            proxmox_cluster="p2", proxmox_node="hv01",
            cloudstack_host_id="host-uuid", cloudstack_host_name="p2-hv01.example",
        ))
        session.add(NetworkMapping(
            proxmox_cluster="p2", proxmox_bridge="vmbr2", proxmox_vlan=300,
            cloudstack_network_id="net-211", cloudstack_network_name="net211",
        ))
        session.commit()
        session.close()

    def test_nic_on_tenant_copy_of_same_vlan_is_not_drift(self):
        self.seed({"ip": None}, [
            {"id": 9, "mac_address": MAC.lower(), "network_id": 291, "ip4_address": None},
        ])
        self.assertEqual([], self.engine.detect_nic_drift())

    def test_missing_nic_targets_the_vm_owners_network(self):
        self.seed({"ip": None}, [])
        drift = self.engine.detect_nic_drift()
        self.assertEqual(["nic_missing_in_cs"], [d["type"] for d in drift])
        self.assertEqual(291, drift[0]["target_network_id"])

    def test_missing_nic_is_not_placed_on_another_tenants_network(self):
        self.engine.cs_db.list_guest_networks.return_value = [net(218, 99, 3002)]
        self.seed({"ip": None}, [], vlan=3002)
        drift = self.engine.detect_nic_drift()
        self.assertEqual(["nic_missing_in_cs"], [d["type"] for d in drift])
        self.assertTrue(drift[0]["cross_account_target"])
        self.assertEqual(0, self.engine.reconcile_nics_all(dry_run=True)["updated"])
        self.engine.cs_db.insert_nic.assert_not_called()

    def test_nic_on_a_different_vlan_is_still_reported(self):
        self.seed({"ip": None}, [
            {"id": 9, "mac_address": MAC, "network_id": 231, "ip4_address": "10.180.1.5"},
        ])
        drift = self.engine.detect_nic_drift()
        self.assertEqual(["nic_network_mismatch"], [d["type"] for d in drift])
        # The NIC sits on a CloudStack-managed (Isolated) network: report only.
        self.assertTrue(drift[0]["cloudstack_managed"])
        self.assertEqual(0, self.engine.reconcile_nics_all(dry_run=True)["updated"])

    def test_any_guest_address_matching_cloudstack_is_not_drift(self):
        self.seed({"ip": "10.0.0.9", "ips": ["10.0.0.9", "10.0.0.5"]}, [
            {"id": 9, "mac_address": MAC, "network_id": 291, "ip4_address": "10.0.0.5"},
        ])
        self.assertEqual([], self.engine.detect_nic_drift())

    def test_link_local_guest_address_never_replaces_cloudstack_ip(self):
        self.seed({"ip": "169.254.30.197"}, [
            {"id": 9, "mac_address": MAC, "network_id": 291, "ip4_address": "10.0.0.5"},
        ])
        self.assertEqual([], self.engine.detect_nic_drift())

    def test_changed_guest_ip_on_l2_network_is_fixable_drift(self):
        self.seed({"ip": "10.0.0.9", "ips": ["10.0.0.9"]}, [
            {"id": 9, "mac_address": MAC, "network_id": 291, "ip4_address": "10.0.0.5"},
        ])
        drift = self.engine.detect_nic_drift()
        self.assertEqual(["nic_ip_mismatch"], [d["type"] for d in drift])
        self.assertFalse(drift[0]["cloudstack_managed"])
        self.assertEqual("10.0.0.9", drift[0]["ip"])


    def test_observed_ip_needs_two_sightings_and_never_overrides_the_guest(self):
        self.engine.record_observed_ips([{"mac": MAC.lower(), "ip": "10.0.0.9"}])
        session = get_session()
        self.assertEqual({}, self.engine._observed_ips(session))
        self.engine.record_observed_ips([{"mac": MAC, "ip": "10.0.0.9"}])
        observed = self.engine._observed_ips(session)
        self.assertEqual({MAC: "10.0.0.9"}, observed)

        blank = [{"mac": MAC, "ip": None}]
        self.engine._fill_observed_ips(blank, observed)
        self.assertEqual(("10.0.0.9", ["10.0.0.9"]), (blank[0]["ip"], blank[0]["ips"]))
        from_agent = [{"mac": MAC, "ip": "10.0.0.5"}]
        self.engine._fill_observed_ips(from_agent, observed)
        self.assertEqual("10.0.0.5", from_agent[0]["ip"])

        # A different address starts the count again.
        self.engine.record_observed_ips([{"mac": MAC, "ip": "10.0.0.77"}])
        self.assertEqual({}, self.engine._observed_ips(session))
        session.close()


class GuestAgentAddressTests(unittest.TestCase):
    def test_link_local_and_loopback_addresses_are_ignored(self):
        client = ProxmoxClient.__new__(ProxmoxClient)
        client.cluster_name = "p2"
        client.api = Mock()
        agent = client.api.nodes.return_value.qemu.return_value.agent.return_value
        agent.get.return_value = {"result": [
            {"hardware-address": MAC.lower(), "ip-addresses": [
                {"ip-address-type": "ipv4", "ip-address": "169.254.30.197", "prefix": 16},
                {"ip-address-type": "ipv4", "ip-address": "10.172.0.25", "prefix": 24},
                {"ip-address-type": "ipv4", "ip-address": "10.172.0.26", "prefix": 24},
            ]},
            {"hardware-address": "00:11:22:33:44:55", "ip-addresses": [
                {"ip-address-type": "ipv4", "ip-address": "169.254.1.1", "prefix": 16},
            ]},
        ]}
        self.assertEqual(
            {MAC: {"ip": "10.172.0.25", "netmask": "255.255.255.0",
                   "ips": ["10.172.0.25", "10.172.0.26"]}},
            client.get_guest_ifaces("hv01", 100),
        )


if __name__ == "__main__":
    unittest.main()
