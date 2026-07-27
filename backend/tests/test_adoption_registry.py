import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from adoption_registry import (
    ClaimConflict,
    ClaimInvalid,
    bind_claim,
    bound_status_map,
    finalize_retiring_claim,
    public_claim,
    reserve_claim,
    retire_claim,
)
from database import AdoptionClaim, get_session, init_db
import main as app_main


class AdoptionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "registry.db"
        init_db(f"sqlite:///{self.db_path}")
        self.original_registry_enabled = app_main.settings.adoption_registry_enabled
        self.original_registry_token = (
            app_main.settings.adoption_registry_internal_token
        )
        app_main.settings.adoption_registry_enabled = True
        app_main.settings.adoption_registry_internal_token = "r" * 32

    def tearDown(self):
        app_main.settings.adoption_registry_enabled = self.original_registry_enabled
        app_main.settings.adoption_registry_internal_token = (
            self.original_registry_token
        )
        self.tmp.cleanup()

    @staticmethod
    def manifest(disks=1):
        value = {
            "placement": {"cluster": "p2", "node": "p2-hv07"},
            "vmid": 114,
            "name": "existing-name",
            "status": "running",
            "cpus": 4,
            "memory_mib": 8192,
            "networks": [],
            "storage": [
                {
                    "device": f"scsi{index}",
                    "volume": f"ceph:vm-114-disk-{index}",
                    "storage": "ceph",
                    "size": "20G",
                }
                for index in range(disks)
            ],
        }
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return canonical, hashlib.sha256(canonical.encode()).hexdigest()

    def reserve(self, disks=1):
        manifest, digest = self.manifest(disks)
        session = get_session()
        try:
            return reserve_claim(
                session,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_json=manifest,
                manifest_sha256=digest,
            )
        finally:
            session.close()

    def bind(self, reservation, vm_ref="cs-vm-a", instance_name="i-2-114-VM"):
        session = get_session()
        try:
            return bind_claim(
                session,
                claim_id=reservation.claim.id,
                nonce=reservation.nonce,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=reservation.claim.manifest_sha256,
                cloudstack_vm_ref=vm_ref,
                cloudstack_instance_name=instance_name,
            )
        finally:
            session.close()

    def test_unique_cluster_vmid_claim_and_nonce_is_digest_only(self):
        reservation = self.reserve()
        self.assertNotEqual(reservation.nonce, reservation.claim.nonce_sha256)
        self.assertEqual(
            hashlib.sha256(reservation.nonce.encode()).hexdigest(),
            reservation.claim.nonce_sha256,
        )
        self.assertNotIn("nonce", public_claim(reservation.claim))
        with self.assertRaises(ClaimConflict):
            self.reserve()

    def test_bind_is_idempotent_only_for_the_same_cloudstack_vm(self):
        reservation = self.reserve()
        first = self.bind(reservation)
        second = self.bind(reservation)
        self.assertEqual("bound", first.state)
        self.assertEqual(first.cloudstack_vm_ref, second.cloudstack_vm_ref)
        with self.assertRaises(ClaimConflict):
            self.bind(reservation, vm_ref="cs-vm-b", instance_name="i-2-115-VM")

    def test_wrong_nonce_and_manifest_are_rejected(self):
        reservation = self.reserve()
        session = get_session()
        try:
            kwargs = {
                "claim_id": reservation.claim.id,
                "nonce": "x" * 43,
                "proxmox_cluster": "p2",
                "proxmox_node": "p2-hv07",
                "proxmox_vmid": 114,
                "manifest_sha256": reservation.claim.manifest_sha256,
                "cloudstack_vm_ref": "cs-vm-a",
                "cloudstack_instance_name": "i-2-114-VM",
            }
            with self.assertRaises(ClaimInvalid):
                bind_claim(session, **kwargs)
            kwargs["nonce"] = reservation.nonce
            kwargs["manifest_sha256"] = "0" * 64
            with self.assertRaises(ClaimInvalid):
                bind_claim(session, **kwargs)
        finally:
            session.close()

    def test_concurrent_bind_has_exactly_one_winner(self):
        reservation = self.reserve()
        barrier = threading.Barrier(2)
        outcomes = []
        output_lock = threading.Lock()

        def worker(vm_ref, instance_name):
            session = get_session()
            try:
                barrier.wait()
                try:
                    claim = bind_claim(
                        session,
                        claim_id=reservation.claim.id,
                        nonce=reservation.nonce,
                        proxmox_cluster="p2",
                        proxmox_node="p2-hv07",
                        proxmox_vmid=114,
                        manifest_sha256=reservation.claim.manifest_sha256,
                        cloudstack_vm_ref=vm_ref,
                        cloudstack_instance_name=instance_name,
                    )
                    result = ("bound", claim.cloudstack_vm_ref)
                except ClaimConflict:
                    result = ("conflict", vm_ref)
                with output_lock:
                    outcomes.append(result)
            finally:
                session.close()

        threads = [
            threading.Thread(target=worker, args=("cs-vm-a", "i-2-114-VM")),
            threading.Thread(target=worker, args=("cs-vm-b", "i-2-115-VM")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(1, sum(result[0] == "bound" for result in outcomes))
        self.assertEqual(1, sum(result[0] == "conflict" for result in outcomes))
        session = get_session()
        try:
            claims = session.query(AdoptionClaim).all()
            self.assertEqual(1, len(claims))
            mapping = bound_status_map(session, proxmox_cluster="p2")
            self.assertEqual(
                {"114": claims[0].cloudstack_instance_name},
                mapping,
            )
        finally:
            session.close()

    def test_multi_disk_manifest_is_preserved_as_opaque_identity(self):
        reservation = self.reserve(disks=3)
        manifest = json.loads(reservation.claim.manifest_json)
        self.assertEqual(3, len(manifest["storage"]))
        self.assertEqual("reserved", reservation.claim.state)

    def test_concurrent_identical_retirement_is_idempotent_and_generation_bound(self):
        reservation = self.reserve()
        bound = self.bind(reservation)
        barrier = threading.Barrier(2)
        outcomes = []
        output_lock = threading.Lock()

        def worker():
            session = get_session()
            try:
                barrier.wait()
                try:
                    claim = retire_claim(
                        session,
                        claim_id=reservation.claim.id,
                        nonce=reservation.nonce,
                        proxmox_cluster="p2",
                        proxmox_node="p2-hv07",
                        proxmox_vmid=114,
                        manifest_sha256=reservation.claim.manifest_sha256,
                        cloudstack_vm_ref=bound.cloudstack_vm_ref,
                        cloudstack_instance_name=bound.cloudstack_instance_name,
                    )
                    result = claim.state
                except Exception as exc:  # pragma: no cover - assertion records type
                    result = type(exc).__name__
                with output_lock:
                    outcomes.append(result)
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(["retiring", "retiring"], sorted(outcomes))
        with self.assertRaises(ClaimConflict):
            self.reserve()

        finalize_session = get_session()
        try:
            released = finalize_retiring_claim(
                finalize_session,
                claim_id=reservation.claim.id,
                cloudstack_vm_ref=bound.cloudstack_vm_ref,
            )
            self.assertEqual("released", released.state)
        finally:
            finalize_session.close()

        next_reservation = self.reserve()
        self.assertEqual(2, next_reservation.claim.generation)
        stale_session = get_session()
        try:
            with self.assertRaises(ClaimInvalid):
                retire_claim(
                    stale_session,
                    claim_id=reservation.claim.id,
                    nonce=reservation.nonce,
                    proxmox_cluster="p2",
                    proxmox_node="p2-hv07",
                    proxmox_vmid=114,
                    manifest_sha256=reservation.claim.manifest_sha256,
                    cloudstack_vm_ref=bound.cloudstack_vm_ref,
                    cloudstack_instance_name=bound.cloudstack_instance_name,
                )
        finally:
            stale_session.close()

    def test_retirement_keeps_status_and_blocks_reuse_until_verified_finalize(self):
        reservation = self.reserve(disks=2)
        bound = self.bind(reservation)
        session = get_session()
        try:
            retiring = retire_claim(
                session,
                claim_id=reservation.claim.id,
                nonce=reservation.nonce,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=reservation.claim.manifest_sha256,
                cloudstack_vm_ref=bound.cloudstack_vm_ref,
                cloudstack_instance_name=bound.cloudstack_instance_name,
            )
            self.assertEqual("retiring", retiring.state)
            self.assertEqual(
                {"114": "i-2-114-VM"},
                bound_status_map(session, proxmox_cluster="p2"),
            )
        finally:
            session.close()

        with self.assertRaises(ClaimConflict):
            self.reserve(disks=2)

        session = get_session()
        try:
            released = finalize_retiring_claim(
                session,
                claim_id=reservation.claim.id,
                cloudstack_vm_ref=bound.cloudstack_vm_ref,
            )
            self.assertEqual("released", released.state)
            self.assertEqual({}, bound_status_map(session, proxmox_cluster="p2"))
        finally:
            session.close()

        next_reservation = self.reserve(disks=2)
        self.assertEqual(reservation.claim.id, next_reservation.claim.id)
        self.assertEqual(2, next_reservation.claim.generation)
        self.assertNotEqual(reservation.nonce, next_reservation.nonce)
        self.assertEqual("reserved", next_reservation.claim.state)

    def test_internal_bind_and_status_route_wrappers_return_secret_free_mapping(self):
        reservation = self.reserve(disks=2)
        request = app_main.BindAdoptionClaimRequest(
            nonce=reservation.nonce,
            proxmox_cluster="p2",
            proxmox_node="p2-hv07",
            proxmox_vmid=114,
            manifest_sha256=reservation.claim.manifest_sha256,
            cloudstack_vm_ref="cloudstack-vm-uuid",
            cloudstack_instance_name="i-2-114-VM",
        )
        bound = app_main.bind_adoption_claim(
            reservation.claim.id,
            request,
            None,
        )
        self.assertEqual("bound", bound["status"])
        self.assertNotIn("nonce", bound["claim"])
        mapping = app_main.adoption_status_map("p2", None)
        self.assertEqual(
            {"114": "i-2-114-VM"},
            mapping["vmid_to_instance_name"],
        )
        self.assertEqual(
            "existing-name",
            mapping["bindings"]["114"]["expected_proxmox_name"],
        )
        retiring = app_main.retire_adoption_claim(
            reservation.claim.id,
            request,
            None,
        )
        self.assertEqual("retiring", retiring["status"])
        self.assertEqual(
            {"114": "i-2-114-VM"},
            app_main.adoption_status_map("p2", None)["vmid_to_instance_name"],
        )

        original_engine = app_main.engine
        try:
            app_main.engine = Mock()
            app_main.engine.cs_client.list_virtual_machines.return_value = [
                {"id": "cloudstack-vm-uuid", "state": "Destroyed"}
            ]
            with self.assertRaises(app_main.HTTPException) as caught:
                app_main.finalize_adoption_claim_release(
                    reservation.claim.id,
                    None,
                )
            self.assertEqual(409, caught.exception.status_code)

            app_main.engine.cs_client.list_virtual_machines.side_effect = RuntimeError(
                "sensitive upstream detail"
            )
            with self.assertRaises(app_main.HTTPException) as caught:
                app_main.finalize_adoption_claim_release(
                    reservation.claim.id,
                    None,
                )
            self.assertEqual(503, caught.exception.status_code)
            self.assertNotIn("sensitive", str(caught.exception.detail))

            app_main.engine.cs_client.list_virtual_machines.side_effect = None
            app_main.engine.cs_client.list_virtual_machines.return_value = []
            released = app_main.finalize_adoption_claim_release(
                reservation.claim.id,
                None,
            )
            self.assertEqual("released", released["status"])
            self.assertEqual(
                {},
                app_main.adoption_status_map("p2", None)["vmid_to_instance_name"],
            )
            app_main.engine.cs_client.list_virtual_machines.assert_called_with(
                id="cloudstack-vm-uuid"
            )
        finally:
            app_main.engine = original_engine


if __name__ == "__main__":
    unittest.main()
