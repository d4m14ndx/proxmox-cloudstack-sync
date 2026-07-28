import hashlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy.exc import OperationalError as SAOperationalError

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from adoption_registry import (
    ClaimConflict,
    ClaimInvalid,
    _is_retryable_operational_error,
    acquire_managed_operation_lease,
    activate_bound_claim,
    bind_claim,
    bound_status_map,
    complete_managed_operation_lease,
    finalize_retiring_claim,
    public_claim,
    reserve_claim,
    retire_claim,
    validated_claim_state,
)
from database import (
    AdoptionClaim,
    AdoptionOperationLease,
    HostMapping,
    get_session,
    init_db,
)
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
        self.original_policy_enabled = app_main.settings.adoption_policy.enabled
        app_main.settings.adoption_registry_enabled = True
        app_main.settings.adoption_registry_internal_token = "r" * 32
        app_main.settings.adoption_policy.enabled = True

    def tearDown(self):
        app_main.settings.adoption_registry_enabled = self.original_registry_enabled
        app_main.settings.adoption_registry_internal_token = (
            self.original_registry_token
        )
        app_main.settings.adoption_policy.enabled = self.original_policy_enabled
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
                generation=reservation.claim.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=reservation.claim.manifest_sha256,
                cloudstack_vm_ref=vm_ref,
                cloudstack_instance_name=instance_name,
            )
        finally:
            session.close()

    def activate(self, reservation, vm_ref="cs-vm-a"):
        session = get_session()
        try:
            return activate_bound_claim(
                session,
                claim_id=reservation.claim.id,
                generation=reservation.claim.generation,
                cloudstack_vm_ref=vm_ref,
            )
        finally:
            session.close()

    @staticmethod
    def lifecycle_identity(reservation):
        return {
            "claim_id": reservation.claim.id,
            "generation": reservation.claim.generation,
            "proxmox_cluster": "p2",
            "proxmox_node": "p2-hv07",
            "proxmox_vmid": 114,
            "manifest_sha256": reservation.claim.manifest_sha256,
            "cloudstack_vm_ref": "cs-vm-a",
            "cloudstack_instance_name": "i-2-114-VM",
        }

    def test_unique_cluster_vmid_claim_uses_nonsecret_generation_fence(self):
        reservation = self.reserve()
        self.assertEqual(1, reservation.claim.generation)
        self.assertFalse(hasattr(reservation.claim, "nonce_sha256"))
        self.assertNotIn("nonce", public_claim(reservation.claim))
        retry = self.reserve()
        self.assertEqual(reservation.claim.id, retry.claim.id)
        self.assertEqual(reservation.claim.generation, retry.claim.generation)

    def test_only_known_mysql_concurrency_errors_are_retryable(self):
        def error(code):
            return SAOperationalError("statement", {}, Exception(code, "detail"))

        for code in (1020, 1205, 1213):
            with self.subTest(code=code):
                self.assertTrue(_is_retryable_operational_error(error(code)))
        for code in (1045, 2003):
            with self.subTest(code=code):
                self.assertFalse(_is_retryable_operational_error(error(code)))

    def test_reservation_payload_contains_no_bearer_claim_secret(self):
        canonical, digest = self.manifest()
        candidate = {
            "proxmox_id": "p2:114",
            "cluster": "p2",
            "node": "p2-hv07",
            "vmid": 114,
            "blockers": ["adopt_existing_orchestrator_not_implemented"],
            "adoption_plan": {
                "manifest": json.loads(canonical),
                "manifest_sha256": digest,
            },
        }
        request = app_main.ReserveAdoptionClaimRequest(
            proxmox_id="p2:114", manifest_sha256=digest
        )
        with patch.object(
            app_main,
            "list_adoption_candidates",
            return_value={"candidates": [candidate]},
        ):
            response = app_main.create_adoption_claim(request, None)
        details = response["extension_external_details"]
        self.assertEqual(1, details["adopt_claim_generation"])
        self.assertFalse([key for key in details if "nonce" in key.lower()])
        self.assertNotIn("claim_nonce", response)

    def test_bind_is_idempotent_only_for_the_same_cloudstack_vm(self):
        reservation = self.reserve()
        first = self.bind(reservation)
        second = self.bind(reservation)
        self.assertEqual("bound", first.state)
        self.assertEqual(first.cloudstack_vm_ref, second.cloudstack_vm_ref)
        with self.assertRaises(ClaimConflict):
            self.bind(reservation, vm_ref="cs-vm-b", instance_name="i-2-115-VM")

    def test_managed_transition_is_fenced_idempotent_and_retires(self):
        reservation = self.reserve()
        bound = self.bind(reservation)
        managed = self.activate(reservation)
        self.assertEqual("managed", managed.state)
        self.assertEqual("managed", self.activate(reservation).state)

        session = get_session()
        try:
            state = validated_claim_state(
                session,
                claim_id=reservation.claim.id,
                generation=reservation.claim.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=reservation.claim.manifest_sha256,
                cloudstack_vm_ref=bound.cloudstack_vm_ref,
                cloudstack_instance_name=bound.cloudstack_instance_name,
            )
            self.assertEqual("managed", state)
            self.assertEqual(
                {"114": "i-2-114-VM"},
                bound_status_map(session, proxmox_cluster="p2"),
            )
            retiring = retire_claim(
                session,
                claim_id=reservation.claim.id,
                generation=reservation.claim.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=reservation.claim.manifest_sha256,
                cloudstack_vm_ref=bound.cloudstack_vm_ref,
                cloudstack_instance_name=bound.cloudstack_instance_name,
            )
            self.assertEqual("retiring", retiring.state)
        finally:
            session.close()

        with self.assertRaises(ClaimConflict):
            self.activate(reservation)

    def test_managed_operation_lease_blocks_retirement_until_exact_completion(self):
        reservation = self.reserve()
        self.bind(reservation)
        self.activate(reservation)
        identity = self.lifecycle_identity(reservation)
        session = get_session()
        try:
            lease = acquire_managed_operation_lease(
                session, **identity, action="restore_snapshot"
            )
            self.assertEqual(
                "operating", validated_claim_state(session, **identity)
            )
            self.assertEqual(
                {"114": "i-2-114-VM"},
                bound_status_map(session, proxmox_cluster="p2"),
            )
            with self.assertRaises(ClaimConflict):
                acquire_managed_operation_lease(
                    session, **identity, action="stop"
                )
            session.rollback()
            with self.assertRaises(ClaimConflict):
                retire_claim(session, **identity)
            session.rollback()

            self.assertEqual(
                "managed",
                complete_managed_operation_lease(
                    session,
                    **identity,
                    action="restore_snapshot",
                    lease_id=lease.id,
                ),
            )
            self.assertEqual("retiring", retire_claim(session, **identity).state)
        finally:
            session.close()

    def test_expired_operation_lease_can_be_retired_without_reopening_managed(self):
        reservation = self.reserve()
        self.bind(reservation)
        self.activate(reservation)
        identity = self.lifecycle_identity(reservation)
        session = get_session()
        try:
            acquire_managed_operation_lease(session, **identity, action="stop")
            lease = session.query(AdoptionOperationLease).one()
            lease.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()

            self.assertEqual("retiring", retire_claim(session, **identity).state)
            self.assertEqual(0, session.query(AdoptionOperationLease).count())
            self.assertEqual(
                "retiring", validated_claim_state(session, **identity)
            )
        finally:
            session.close()

    def test_managed_transition_rejects_stale_generation_and_vm_reference(self):
        reservation = self.reserve()
        self.bind(reservation)
        session = get_session()
        try:
            with self.assertRaises(ClaimInvalid):
                activate_bound_claim(
                    session,
                    claim_id=reservation.claim.id,
                    generation=reservation.claim.generation + 1,
                    cloudstack_vm_ref="cs-vm-a",
                )
            with self.assertRaises(ClaimInvalid):
                activate_bound_claim(
                    session,
                    claim_id=reservation.claim.id,
                    generation=reservation.claim.generation,
                    cloudstack_vm_ref="cs-vm-b",
                )
        finally:
            session.close()

    def test_stale_generation_and_wrong_manifest_are_rejected(self):
        reservation = self.reserve()
        session = get_session()
        try:
            kwargs = {
                "claim_id": reservation.claim.id,
                "generation": reservation.claim.generation + 1,
                "proxmox_cluster": "p2",
                "proxmox_node": "p2-hv07",
                "proxmox_vmid": 114,
                "manifest_sha256": reservation.claim.manifest_sha256,
                "cloudstack_vm_ref": "cs-vm-a",
                "cloudstack_instance_name": "i-2-114-VM",
            }
            with self.assertRaises(ClaimInvalid):
                bind_claim(session, **kwargs)
            kwargs["generation"] = reservation.claim.generation
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
                        generation=reservation.claim.generation,
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
                        generation=reservation.claim.generation,
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
                    generation=reservation.claim.generation,
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
                generation=reservation.claim.generation,
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
        self.assertEqual("reserved", next_reservation.claim.state)

    def test_activation_route_verifies_exact_cloudstack_identity_before_managed(self):
        reservation = self.reserve(disks=2)
        bound = self.bind(reservation)
        session = get_session()
        try:
            session.add(
                HostMapping(
                    proxmox_cluster="p2",
                    proxmox_node="p2-hv07",
                    cloudstack_host_id="host-uuid",
                    cloudstack_host_name="p2-hv07.example",
                )
            )
            session.commit()
        finally:
            session.close()

        cloudstack_vm = {
            "id": bound.cloudstack_vm_ref,
            "instancename": bound.cloudstack_instance_name,
            "hypervisor": "External",
            "state": "Running",
            "details": {"proxmox_vmid": "114"},
            "cpunumber": 4,
            "memory": 8192,
            "account": "admin",
            "domainid": app_main.settings.adoption_policy.domain_id,
            "hostid": "host-uuid",
        }
        original_engine = app_main.engine
        try:
            app_main.engine = Mock()
            request = app_main.ActivateAdoptionClaimRequest(
                generation=reservation.claim.generation
            )
            app_main.settings.adoption_policy.enabled = False
            with self.assertRaises(app_main.HTTPException) as disabled:
                app_main.activate_adoption_claim(
                    reservation.claim.id, request, None
                )
            self.assertEqual(503, disabled.exception.status_code)
            app_main.settings.adoption_policy.enabled = True

            mismatch_cases = {
                "uuid": ({"id": "other-vm"}, "cloudstack_vm_uuid_mismatch"),
                "name": (
                    {"instancename": "i-2-999-VM"},
                    "cloudstack_instance_name_mismatch",
                ),
                "hypervisor": (
                    {"hypervisor": "VMware"},
                    "cloudstack_hypervisor_mismatch",
                ),
                "state": ({"state": "Stopped"}, "cloudstack_vm_not_running"),
                "vmid": (
                    {"details": {"proxmox_vmid": "999"}},
                    "cloudstack_proxmox_vmid_mismatch",
                ),
                "cpu": ({"cpunumber": 2}, "cloudstack_cpu_mismatch"),
                "memory": ({"memory": 4096}, "cloudstack_memory_mismatch"),
                "account": ({"account": "other"}, "cloudstack_account_mismatch"),
                "domain": (
                    {"domainid": "other-domain"},
                    "cloudstack_domain_mismatch",
                ),
                "project": (
                    {"projectid": "project-uuid"},
                    "cloudstack_project_present",
                ),
                "host": ({"hostid": "other-host"}, "cloudstack_host_mismatch"),
            }
            for name, (changes, expected_mismatch) in mismatch_cases.items():
                with self.subTest(name=name):
                    app_main.engine.cs_client.list_virtual_machines.return_value = [
                        {**cloudstack_vm, **changes}
                    ]
                    with self.assertRaises(app_main.HTTPException) as caught:
                        app_main.activate_adoption_claim(
                            reservation.claim.id, request, None
                        )
                    self.assertEqual(409, caught.exception.status_code)
                    detail = caught.exception.detail
                    if not isinstance(detail, dict):
                        self.fail(f"Expected structured mismatch detail, got {detail!r}")
                    self.assertIn(expected_mismatch, detail["mismatches"])

            session = get_session()
            try:
                self.assertEqual(
                    "bound", session.get(AdoptionClaim, reservation.claim.id).state
                )
            finally:
                session.close()

            app_main.engine.cs_client.list_virtual_machines.return_value = [
                cloudstack_vm
            ]
            response = app_main.activate_adoption_claim(
                reservation.claim.id, request, None
            )
            self.assertEqual("managed", response["status"])
            self.assertEqual("managed", response["claim"]["state"])
            app_main.engine.cs_client.list_virtual_machines.assert_called_with(
                id=bound.cloudstack_vm_ref
            )

            lifecycle_request = app_main.BindAdoptionClaimRequest(
                generation=reservation.claim.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=reservation.claim.manifest_sha256,
                cloudstack_vm_ref=bound.cloudstack_vm_ref,
                cloudstack_instance_name=bound.cloudstack_instance_name,
            )
            lifecycle = app_main.adoption_claim_lifecycle_state(
                reservation.claim.id, lifecycle_request, None
            )
            self.assertEqual({"status": "ok", "state": "managed"}, lifecycle)
        finally:
            app_main.engine = original_engine

    def test_internal_operation_lease_routes_fence_retirement(self):
        reservation = self.reserve()
        self.bind(reservation)
        self.activate(reservation)
        request = app_main.ManagedOperationLeaseRequest(
            generation=reservation.claim.generation,
            proxmox_cluster="p2",
            proxmox_node="p2-hv07",
            proxmox_vmid=114,
            manifest_sha256=reservation.claim.manifest_sha256,
            cloudstack_vm_ref="cs-vm-a",
            cloudstack_instance_name="i-2-114-VM",
            action="console",
        )
        acquired = app_main.acquire_adoption_lifecycle_lease(
            reservation.claim.id, request, None
        )
        self.assertEqual("operating", acquired["status"])

        retire_request = app_main.BindAdoptionClaimRequest(
            **request.model_dump(exclude={"action"})
        )
        with self.assertRaises(app_main.HTTPException) as caught:
            app_main.retire_adoption_claim(
                reservation.claim.id, retire_request, None
            )
        self.assertEqual(409, caught.exception.status_code)

        completed = app_main.complete_adoption_lifecycle_lease(
            reservation.claim.id,
            app_main.CompleteManagedOperationLeaseRequest(
                **request.model_dump(), lease_id=acquired["lease_id"]
            ),
            None,
        )
        self.assertEqual("managed", completed["state"])

    def test_internal_bind_and_status_route_wrappers_return_secret_free_mapping(self):
        reservation = self.reserve(disks=2)
        request = app_main.BindAdoptionClaimRequest(
            generation=reservation.claim.generation,
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
