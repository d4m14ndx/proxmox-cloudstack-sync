import hashlib
import json
import os
import sys
import threading
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import database
from adoption_executor import (
    ExecutionConflict,
    acquire_execution,
    authorize_cleanup_delete,
    create_execution,
)
from adoption_registry import ClaimConflict, bind_claim, reserve_claim
from database import AdoptionClaim, Base, get_session, init_db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is not configured")
class MariaDBAdoptionExecutorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db(TEST_DATABASE_URL)
        Base.metadata.drop_all(database._engine)
        Base.metadata.create_all(database._engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(database._engine)

    def setUp(self):
        Base.metadata.drop_all(database._engine)
        Base.metadata.create_all(database._engine)

    @staticmethod
    def manifest():
        value = {
            "placement": {"cluster": "p2", "node": "p2-hv07"},
            "vmid": 114,
            "name": "mariadb-test",
            "status": "running",
            "cpus": 4,
            "memory_mib": 8192,
            "networks": [{
                "device_id": 0,
                "mac": "AA:BB:CC:DD:EE:FF",
                "ip": "10.0.0.114",
                "proxmox_bridge": "vmbr0",
                "proxmox_vlan": 100,
                "cloudstack_network_id": "40000000-0000-4000-8000-000000000001",
            }],
            "storage": [{
                "device": "scsi0",
                "volume": "ceph:vm-114-disk-0",
                "storage": "ceph",
                "size": "20G",
            }],
        }
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return serialized, hashlib.sha256(serialized.encode()).hexdigest()

    @classmethod
    def plan(cls, claim):
        return {
            "claim": {
                "id": claim.id,
                "generation": claim.generation,
                "manifest_sha256": claim.manifest_sha256,
            },
            "deployment": {
                "zone_id": "40000000-0000-4000-8000-000000000002",
                "cluster_id": "40000000-0000-4000-8000-000000000003",
                "host_id": "40000000-0000-4000-8000-000000000004",
                "template_id": "40000000-0000-4000-8000-000000000005",
                "service_offering_id": "40000000-0000-4000-8000-000000000006",
                "service_offering_customized": True,
                "cpu_speed_mhz": 1200,
                "account": "admin",
                "domain_id": "40000000-0000-4000-8000-000000000007",
                "project_id": None,
                "name": "adopt-114-mariadb",
                "display_name": "mariadb-test",
                "cpus": 4,
                "memory_mib": 8192,
                "networks": [
                    {
                        "device_id": 0,
                        "network_id": "40000000-0000-4000-8000-000000000001",
                        "mac": "AA:BB:CC:DD:EE:FF",
                        "ip": "10.0.0.114",
                    }
                ],
                "external_details": {
                    "adopt_existing": "true",
                    "adopt_claim_id": claim.id,
                    "adopt_claim_generation": str(claim.generation),
                    "adopt_manifest_sha256": claim.manifest_sha256,
                    "adopt_manifest_json": claim.manifest_json,
                    "proxmox_cluster": "p2",
                },
            },
        }

    def test_claim_execution_and_worker_cas_are_unique(self):
        manifest_json, digest = self.manifest()
        session = get_session()
        try:
            claim = reserve_claim(
                session,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_json=manifest_json,
                manifest_sha256=digest,
            ).claim
            claim_id = claim.id
            generation = claim.generation
            plan = self.plan(claim)
        finally:
            session.close()

        barrier = threading.Barrier(2)
        execution_ids = []
        lock = threading.Lock()

        def create_worker():
            local = get_session()
            try:
                barrier.wait()
                execution = create_execution(
                    local,
                    claim_id=claim_id,
                    generation=generation,
                    plan=json.loads(json.dumps(plan)),
                )
                with lock:
                    execution_ids.append(execution.id)
            finally:
                local.close()

        threads = [threading.Thread(target=create_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(2, len(execution_ids))
        self.assertEqual(1, len(set(execution_ids)))

        lease_barrier = threading.Barrier(2)
        winners = []

        def lease_worker():
            local = get_session()
            try:
                lease_barrier.wait()
                acquired = acquire_execution(local, execution_ids[0], 60)
                with lock:
                    winners.append(acquired is not None)
            finally:
                local.close()

        threads = [threading.Thread(target=lease_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([False, True], sorted(winners))

    def test_cleanup_authorization_and_bind_have_one_mariadb_winner(self):
        manifest_json, digest = self.manifest()
        session = get_session()
        try:
            claim = reserve_claim(
                session,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_json=manifest_json,
                manifest_sha256=digest,
            ).claim
            execution = create_execution(
                session,
                claim_id=claim.id,
                generation=claim.generation,
                plan=self.plan(claim),
            )
            execution.state = "cleanup_submitting"
            execution.cloudstack_vm_ref = execution.id
            execution.cloudstack_instance_name = "i-2-114-VM"
            session.commit()
            claim_id = claim.id
            generation = claim.generation
            execution_id = execution.id
        finally:
            session.close()

        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def authorize_worker():
            local = get_session()
            try:
                barrier.wait()
                authorize_cleanup_delete(
                    local,
                    claim_id=claim_id,
                    generation=generation,
                    proxmox_cluster="p2",
                    proxmox_node="p2-hv07",
                    proxmox_vmid=114,
                    manifest_sha256=digest,
                    cloudstack_vm_ref=execution_id,
                    cloudstack_instance_name="i-2-114-VM",
                )
                outcome = "authorize"
            except ExecutionConflict:
                outcome = "authorize_conflict"
            finally:
                local.close()
            with lock:
                outcomes.append(outcome)

        def bind_worker():
            local = get_session()
            try:
                barrier.wait()
                bind_claim(
                    local,
                    claim_id=claim_id,
                    generation=generation,
                    proxmox_cluster="p2",
                    proxmox_node="p2-hv07",
                    proxmox_vmid=114,
                    manifest_sha256=digest,
                    cloudstack_vm_ref=execution_id,
                    cloudstack_instance_name="i-2-114-VM",
                )
                outcome = "bind"
            except ClaimConflict:
                outcome = "bind_conflict"
            finally:
                local.close()
            with lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=authorize_worker),
            threading.Thread(target=bind_worker),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertIn(
            sorted(outcomes),
            (["authorize", "bind_conflict"], ["authorize_conflict", "bind"]),
        )
        session = get_session()
        try:
            claim = session.get(AdoptionClaim, claim_id)
            expected_state = "cleanup" if "authorize" in outcomes else "bound"
            self.assertEqual(expected_state, claim.state)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
