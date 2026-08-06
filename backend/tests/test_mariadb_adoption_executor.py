import hashlib
import json
import os
import sys
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
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
from adoption_registry import (
    ClaimConflict,
    activate_bound_claim,
    bind_claim,
    reserve_claim,
)
from database import AdoptionClaim, AdoptionExecution, Base, get_session, init_db


def _guarded_registry_mutation(function):
    def call(*args, **kwargs):
        kwargs.setdefault("write_guard", lambda: None)
        return function(*args, **kwargs)

    return call


reserve_claim = _guarded_registry_mutation(reserve_claim)
bind_claim = _guarded_registry_mutation(bind_claim)
activate_bound_claim = _guarded_registry_mutation(activate_bound_claim)
create_execution = _guarded_registry_mutation(create_execution)
authorize_cleanup_delete = _guarded_registry_mutation(authorize_cleanup_delete)

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
                "root_disk_size_customized": True,
                "root_disk_size_gib": 20,
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
                acquired = acquire_execution(
                    local,
                    execution_ids[0],
                    60,
                    write_guard=lambda: None,
                )
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

    def test_activation_requires_exact_live_mariadb_worker_lease(self):
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
            bind_claim(
                session,
                claim_id=claim.id,
                generation=claim.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=digest,
                cloudstack_vm_ref=execution.id,
                cloudstack_instance_name="i-2-114-VM",
            )
            stale_lease_id = str(uuid.uuid4())
            live_lease_id = str(uuid.uuid4())
            execution = session.get(AdoptionExecution, execution.id)
            execution.state = "verifying"
            execution.worker_lease_id = live_lease_id
            execution.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=5
            )
            session.commit()

            with self.assertRaises(ClaimConflict):
                activate_bound_claim(
                    session,
                    claim_id=claim.id,
                    generation=claim.generation,
                    cloudstack_vm_ref=execution.id,
                    execution_id=execution.id,
                    worker_lease_id=stale_lease_id,
                )
            session.expire_all()
            self.assertEqual("bound", session.get(AdoptionClaim, claim.id).state)
            self.assertEqual(
                live_lease_id,
                session.get(AdoptionExecution, execution.id).worker_lease_id,
            )

            managed = activate_bound_claim(
                session,
                claim_id=claim.id,
                generation=claim.generation,
                cloudstack_vm_ref=execution.id,
                execution_id=execution.id,
                worker_lease_id=live_lease_id,
            )
            self.assertEqual("managed", managed.state)
        finally:
            session.close()


    def test_activation_rejects_lease_expired_during_mariadb_lock_wait(self):
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
            bind_claim(
                session,
                claim_id=claim.id,
                generation=claim.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=digest,
                cloudstack_vm_ref=execution.id,
                cloudstack_instance_name="i-2-114-VM",
            )
            lease_id = str(uuid.uuid4())
            execution = session.get(AdoptionExecution, execution.id)
            execution.state = "verifying"
            execution.worker_lease_id = lease_id
            execution.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=2
            )
            session.commit()
            claim_id = claim.id
            generation = claim.generation
            execution_id = execution.id
        finally:
            session.close()

        lock_acquired = threading.Event()
        release_lock = threading.Event()
        activation_started = threading.Event()
        outcomes = []

        def lock_execution_row():
            local = get_session()
            try:
                (
                    local.query(AdoptionExecution)
                    .filter_by(id=execution_id)
                    .with_for_update()
                    .one()
                )
                lock_acquired.set()
                if not release_lock.wait(10):
                    raise AssertionError("timed out waiting to release execution lock")
                local.commit()
            finally:
                local.close()

        def activate_after_lock():
            local = get_session()
            try:
                activation_started.set()
                activate_bound_claim(
                    local,
                    claim_id=claim_id,
                    generation=generation,
                    cloudstack_vm_ref=execution_id,
                    execution_id=execution_id,
                    worker_lease_id=lease_id,
                )
                outcomes.append("managed")
            except ClaimConflict:
                outcomes.append("conflict")
            finally:
                local.close()

        locker = threading.Thread(target=lock_execution_row)
        activator = threading.Thread(target=activate_after_lock)
        locker.start()
        self.assertTrue(lock_acquired.wait(5))
        activator.start()
        self.assertTrue(activation_started.wait(5))
        time.sleep(3)
        release_lock.set()
        locker.join(10)
        activator.join(10)
        self.assertFalse(locker.is_alive())
        self.assertFalse(activator.is_alive())
        self.assertEqual(["conflict"], outcomes)

        session = get_session()
        try:
            claim = session.get(AdoptionClaim, claim_id)
            execution = session.get(AdoptionExecution, execution_id)
            self.assertEqual("bound", claim.state)
            self.assertEqual("verifying", execution.state)
            self.assertEqual(lease_id, execution.worker_lease_id)
            self.assertLess(
                execution.worker_lease_expires_at,
                datetime.now(timezone.utc).replace(tzinfo=None),
            )
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
