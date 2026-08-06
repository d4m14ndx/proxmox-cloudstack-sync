import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from adopt_one import BoundedCloudStackClient
from adoption_authority import (
    AuthorityConflict,
    acquire_write_authority,
    assert_write_authority,
    release_write_authority,
    renew_write_authority,
)
from cloudstack_db import CloudStackDB
from database import (
    AdoptionClaim,
    AdoptionExecution,
    AdoptionWriteAuthority,
    get_session,
    init_db,
)
from sync_engine import SyncEngine


class AdoptionAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.temp.name) / 'authority.db'}")

    def tearDown(self):
        self.temp.cleanup()

    def _expire_current_and_take_over(self) -> str:
        session = get_session()
        row = session.get(AdoptionWriteAuthority, 1)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
        session.close()
        owner = acquire_write_authority(
            mode="operator", target="p3:110", lease_seconds=60
        )
        self.assertIsNotNone(owner)
        return owner

    def test_operator_and_automatic_modes_are_mutually_exclusive(self):
        operator = acquire_write_authority(
            mode="operator", target="p3:110", lease_seconds=60
        )
        self.assertIsNotNone(operator)
        self.assertIsNone(
            acquire_write_authority(
                mode="automatic", target="vm_reconciliation", lease_seconds=60
            )
        )
        assert_write_authority(owner_id=operator, mode="operator")
        renew_write_authority(
            owner_id=operator,
            mode="operator",
            target="p3:110",
            lease_seconds=60,
        )
        self.assertTrue(release_write_authority(owner_id=operator, mode="operator"))

        automatic = acquire_write_authority(
            mode="automatic", target="vm_reconciliation", lease_seconds=60
        )
        self.assertIsNotNone(automatic)
        self.assertIsNone(
            acquire_write_authority(
                mode="operator", target="p3:110", lease_seconds=60
            )
        )
        self.assertTrue(
            release_write_authority(owner_id=automatic, mode="automatic")
        )

    def test_expired_crash_lease_can_be_safely_taken_over(self):
        operator = acquire_write_authority(
            mode="operator", target="p3:110", lease_seconds=60
        )
        session = get_session()
        row = session.get(AdoptionWriteAuthority, 1)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
        session.close()

        with self.assertRaises(AuthorityConflict):
            assert_write_authority(owner_id=operator, mode="operator")
        automatic = acquire_write_authority(
            mode="automatic", target="vm_reconciliation", lease_seconds=60
        )
        self.assertIsNotNone(automatic)
        self.assertFalse(release_write_authority(owner_id=operator, mode="operator"))
        self.assertTrue(
            release_write_authority(owner_id=automatic, mode="automatic")
        )

    def test_operator_fence_blocks_automatic_vm_and_nic_reconciliation(self):
        operator = acquire_write_authority(
            mode="operator", target="p3:110", lease_seconds=60
        )
        engine = SyncEngine.__new__(SyncEngine)
        engine.cs_db = object()
        engine._nic_collection_ready = True
        engine.detect_drift = Mock(return_value=[{"type": "state_mismatch"}])
        engine.detect_nic_drift = Mock(return_value=[{"type": "nic_mac_mismatch"}])
        engine.reconcile_vm = Mock()
        engine.reconcile_nic = Mock()

        vm_result = engine.reconcile_all()
        nic_result = engine.reconcile_nics_all(dry_run=False)
        self.assertEqual("operator write authority is active", vm_result["skipped"])
        self.assertEqual("operator write authority is active", nic_result["skipped"])
        engine.detect_drift.assert_not_called()
        engine.detect_nic_drift.assert_not_called()
        engine.reconcile_vm.assert_not_called()
        engine.reconcile_nic.assert_not_called()
        self.assertTrue(release_write_authority(owner_id=operator, mode="operator"))

    def test_vm_write_guard_rejects_takeover_after_slow_preflight(self):
        item = {
            "type": "state_mismatch",
            "proxmox_id": "p3:110",
            "cloudstack_uuid": "vm-uuid",
            "source_cs_host_id": "host-1",
            "target_cs_host_id": "host-1",
            "proxmox_state": "running",
        }
        engine = SyncEngine.__new__(SyncEngine)
        engine.cs_db = Mock()
        engine.detect_drift = Mock(return_value=[item])
        provider_writes = []

        def guarded_vm_write(*_args, write_guard):
            write_guard()
            provider_writes.append(True)

        engine.cs_db.update_vm_placement_and_state.side_effect = guarded_vm_write
        takeover = []

        def resolve_after_takeover(_host_ref):
            takeover.append(self._expire_current_and_take_over())
            return 1

        engine._resolve_host_db_id = resolve_after_takeover
        result = engine.reconcile_vm(item)

        self.assertEqual("AuthorityConflict", result["error_type"])
        self.assertEqual([], provider_writes)
        self.assertTrue(
            release_write_authority(owner_id=takeover[0], mode="operator")
        )

    def test_nic_write_guard_rejects_takeover_after_slow_preflight(self):
        item = {
            "type": "nic_mac_mismatch",
            "proxmox_id": "p3:110",
            "cloudstack_uuid": "vm-uuid",
            "device_id": 0,
            "cs_nic_id": 42,
            "mac": "02:00:00:00:00:01",
        }
        engine = SyncEngine.__new__(SyncEngine)
        engine.cs_db = Mock()
        engine._nic_collection_ready = True
        provider_writes = []

        def guarded_nic_write(*_args, write_guard, **_kwargs):
            write_guard()
            provider_writes.append(True)

        engine.cs_db.update_nic.side_effect = guarded_nic_write
        takeover = []

        def drift_after_takeover():
            takeover.append(self._expire_current_and_take_over())
            return [item]

        engine.detect_nic_drift = drift_after_takeover
        result = engine.reconcile_nic(item)

        self.assertEqual("AuthorityConflict", result["error_type"])
        self.assertEqual([], provider_writes)
        self.assertTrue(
            release_write_authority(owner_id=takeover[0], mode="operator")
        )

    def test_lifecycle_guard_rejects_takeover_before_deploy_and_start(self):
        operator = acquire_write_authority(
            mode="operator", target="p3:110", lease_seconds=60
        )
        self.assertIsNotNone(operator)
        delegate = Mock()
        automatic = []

        def stale_operator_guard():
            if not automatic:
                session = get_session()
                row = session.get(AdoptionWriteAuthority, 1)
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                session.commit()
                session.close()
                owner = acquire_write_authority(
                    mode="automatic",
                    target="adoption_executor",
                    lease_seconds=60,
                )
                self.assertIsNotNone(owner)
                automatic.append(owner)
            renew_write_authority(
                owner_id=operator,
                mode="operator",
                target="p3:110",
                lease_seconds=60,
            )

        client = BoundedCloudStackClient(
            delegate,
            allow_deploy=True,
            allow_start=True,
            authority_guard=stale_operator_guard,
        )
        with self.assertRaises(AuthorityConflict):
            client.deploy_virtual_machine(startvm="false")
        with self.assertRaises(AuthorityConflict):
            client.start_virtual_machine("vm-uuid")
        delegate.deploy_virtual_machine.assert_not_called()
        delegate.start_virtual_machine.assert_not_called()
        self.assertEqual(0, client.deploy_calls)
        self.assertEqual(0, client.start_calls)
        self.assertTrue(
            release_write_authority(owner_id=automatic[0], mode="automatic")
        )

    def test_nic_insert_guard_runs_after_preflight_before_transaction(self):
        database = CloudStackDB.__new__(CloudStackDB)
        database.get_nics_columns = Mock(
            return_value={"uuid", "instance_id", "network_id"}
        )
        database.sample_nic_on_network = Mock(return_value=None)
        database._connect = Mock()

        with self.assertRaises(AuthorityConflict):
            database.insert_nic(
                {"instance_id": 1, "network_id": 2},
                write_guard=Mock(side_effect=AuthorityConflict("stale")),
            )

        database.get_nics_columns.assert_called_once_with()
        database.sample_nic_on_network.assert_called_once_with(2)
        database._connect.assert_not_called()

    def test_operator_fence_blocks_all_executor_mutation_routes(self):
        operator = acquire_write_authority(
            mode="operator", target="p3:110", lease_seconds=60
        )
        self.assertIsNotNone(operator)

        calls = (
            lambda: app_main.execute_adoption_claim(
                "00000000-0000-4000-8000-000000000001",
                app_main.ExecuteAdoptionClaimRequest(generation=1),
                None,
            ),
            lambda: app_main.reconcile_adoption_execution(
                "00000000-0000-4000-8000-000000000001", None
            ),
            lambda: app_main.cleanup_adoption_execution(
                "00000000-0000-4000-8000-000000000001", None
            ),
            lambda: app_main.retry_adoption_execution(
                "00000000-0000-4000-8000-000000000001", None
            ),
            app_main.create_adoption_claim,
            app_main.authorize_adoption_cleanup_delete,
            app_main.bind_adoption_claim,
            app_main.acquire_adoption_lifecycle_lease,
            app_main.complete_adoption_lifecycle_lease,
            app_main.activate_adoption_claim,
            app_main.retire_adoption_claim,
            app_main.finalize_adoption_claim_release,
        )
        for call in calls:
            with self.assertRaises(HTTPException) as caught:
                call()
            self.assertEqual(409, caught.exception.status_code)

        self.assertTrue(
            release_write_authority(owner_id=operator, mode="operator")
        )

    def test_exact_operator_bind_callback_is_allowed_cross_request_only(self):
        claim_id = "20000000-0000-4000-8000-000000000001"
        execution_id = "20000000-0000-4000-8000-000000000002"
        manifest_sha256 = "a" * 64
        session = get_session()
        try:
            session.add(
                AdoptionClaim(
                    id=claim_id,
                    proxmox_cluster="p3",
                    proxmox_node="p3-hv02",
                    proxmox_vmid=110,
                    manifest_sha256=manifest_sha256,
                    manifest_json="{}",
                    generation=1,
                    state="reserved",
                )
            )
            session.add(
                AdoptionExecution(
                    id=execution_id,
                    claim_id=claim_id,
                    generation=1,
                    plan_sha256="b" * 64,
                    plan_json="{}",
                    state="deploy_submitting",
                )
            )
            session.commit()
        finally:
            session.close()

        operator = acquire_write_authority(
            mode="operator", target="p3:110", lease_seconds=60
        )
        self.assertIsNotNone(operator)
        request = app_main.BindAdoptionClaimRequest(
            generation=1,
            proxmox_cluster="p3",
            proxmox_node="p3-hv02",
            proxmox_vmid=110,
            manifest_sha256=manifest_sha256,
            cloudstack_vm_ref=execution_id,
            cloudstack_instance_name="i-2-164-VM",
        )

        response = app_main.bind_adoption_claim(claim_id, request, None)
        self.assertEqual("bound", response["status"])
        lifecycle = app_main.adoption_claim_lifecycle_state(claim_id, request, None)
        self.assertEqual("bound", lifecycle["state"])

        mismatches = (
            request.model_copy(update={"generation": 2}),
            request.model_copy(update={"proxmox_vmid": 111}),
            request.model_copy(update={"manifest_sha256": "c" * 64}),
            request.model_copy(
                update={
                    "cloudstack_vm_ref": "20000000-0000-4000-8000-000000000003"
                }
            ),
            request.model_copy(update={"cloudstack_instance_name": "i-2-999-VM"}),
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch.model_dump()):
                with self.assertRaises(HTTPException) as caught:
                    app_main.bind_adoption_claim(claim_id, mismatch, None)
                self.assertEqual(409, caught.exception.status_code)

        self.assertTrue(
            release_write_authority(owner_id=operator, mode="operator")
        )

    def test_operator_fence_blocks_scheduled_adoption_executor(self):
        operator = acquire_write_authority(
            mode="operator", target="p3:110", lease_seconds=60
        )
        saved_executor = app_main.settings.adoption_executor_enabled
        saved_engine = app_main.engine
        try:
            app_main.settings.adoption_executor_enabled = True
            app_main.engine = SimpleNamespace(cs_client=object())
            with patch.object(app_main, "reconcile_active_executions") as reconcile:
                result = app_main.run_adoption_executor()
            self.assertEqual("operator write authority is active", result["skipped"])
            reconcile.assert_not_called()
        finally:
            app_main.settings.adoption_executor_enabled = saved_executor
            app_main.engine = saved_engine
            self.assertTrue(
                release_write_authority(owner_id=operator, mode="operator")
            )


if __name__ == "__main__":
    unittest.main()
