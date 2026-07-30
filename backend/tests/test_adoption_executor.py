import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from adoption_executor import (
    ExecutionConflict,
    ExecutionInvalid,
    _deploy_params,
    acquire_execution,
    authorize_cleanup_delete,
    create_execution,
    public_execution,
    reconcile_execution,
    request_execution_cleanup,
    request_execution_retry,
)
from adoption_registry import (
    ClaimConflict,
    activate_bound_claim,
    bind_claim,
    reserve_claim,
)
from database import AdoptionClaim, AdoptionExecution, get_session, init_db

ZONE_ID = "10000000-0000-4000-8000-000000000001"
CLUSTER_ID = "10000000-0000-4000-8000-000000000002"
HOST_ID = "10000000-0000-4000-8000-000000000003"
TEMPLATE_ID = "10000000-0000-4000-8000-000000000004"
OFFERING_ID = "10000000-0000-4000-8000-000000000005"
DOMAIN_ID = "10000000-0000-4000-8000-000000000006"
NETWORK_ID = "10000000-0000-4000-8000-000000000007"


def retryable_operational_error(code=1213):
    return OperationalError(
        "conditional update", {}, RuntimeError(code, "synthetic race")
    )


def fail_first_commit(session, code=1213):
    original_commit = session.commit
    attempts = 0

    def flaky_commit():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise retryable_operational_error(code)
        return original_commit()

    session.commit = flaky_commit


class FakeCloudStack:
    def __init__(self):
        self.vm: dict | None = None
        self.deploy_calls = []
        self.start_calls = []
        self.destroy_calls = []
        self.jobs = {}
        self.deploy_error: Exception | None = None
        self.start_error: Exception | None = None
        self.destroy_error: Exception | None = None

    def list_virtual_machines(self, **kwargs):
        if self.vm and self.vm["id"] == kwargs.get("id"):
            return [dict(self.vm)]
        return []

    def deploy_virtual_machine(self, **params):
        self.deploy_calls.append(params)
        if self.deploy_error:
            raise self.deploy_error
        return {"jobid": "deploy-job"}

    def start_virtual_machine(self, vm_id):
        self.start_calls.append(vm_id)
        if self.start_error:
            raise self.start_error
        return {"jobid": "start-job"}

    def destroy_virtual_machine(self, vm_id, *, expunge):
        self.destroy_calls.append((vm_id, expunge))
        if self.destroy_error:
            raise self.destroy_error
        return {"jobid": "cleanup-job"}

    def query_async_job(self, job_id):
        return dict(self.jobs[job_id])


class AdoptionExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'executor.db'}")
        manifest = {
            "placement": {"cluster": "p2", "node": "p2-hv07"},
            "vmid": 114,
            "name": "existing-name",
            "status": "running",
            "cpus": 4,
            "memory_mib": 8192,
            "networks": [
                {
                    "device_id": 0,
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "ip": "10.0.0.114",
                    "proxmox_bridge": "vmbr0",
                    "proxmox_vlan": 100,
                    "cloudstack_network_id": NETWORK_ID,
                }
            ],
            "storage": [
                {
                    "device": "scsi0",
                    "volume": "ceph:vm-114-disk-0",
                    "storage": "ceph",
                    "size": "20G",
                }
            ],
        }
        self.manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        self.manifest_sha256 = hashlib.sha256(self.manifest_json.encode()).hexdigest()
        session = get_session()
        try:
            self.reservation = reserve_claim(
                session,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_json=self.manifest_json,
                manifest_sha256=self.manifest_sha256,
            )
        finally:
            session.close()

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        claim = self.reservation.claim
        return {
            "claim": {
                "id": claim.id,
                "generation": claim.generation,
                "manifest_sha256": claim.manifest_sha256,
            },
            "deployment": {
                "zone_id": ZONE_ID,
                "cluster_id": CLUSTER_ID,
                "host_id": HOST_ID,
                "template_id": TEMPLATE_ID,
                "service_offering_id": OFFERING_ID,
                "service_offering_customized": True,
                "account": "admin",
                "domain_id": DOMAIN_ID,
                "project_id": None,
                "name": f"adopt-114-{claim.id[:8]}",
                "display_name": "existing-name",
                "cpus": 4,
                "memory_mib": 8192,
                "networks": [
                    {
                        "device_id": 0,
                        "network_id": NETWORK_ID,
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

    def create(self):
        session = get_session()
        try:
            return create_execution(
                session,
                claim_id=self.reservation.claim.id,
                generation=self.reservation.claim.generation,
                plan=self.plan(),
            )
        finally:
            session.close()

    def vm(self, execution, state="Stopped"):
        deployment = self.plan()["deployment"]
        return {
            "id": execution.id,
            "name": deployment["name"],
            "displayname": deployment["display_name"],
            "instancename": "i-2-114-VM",
            "state": state,
            "hypervisor": "External",
            "hostid": HOST_ID,
            "serviceofferingid": OFFERING_ID,
            "templateid": TEMPLATE_ID,
            "account": "admin",
            "domainid": DOMAIN_ID,
            "cpunumber": 4,
            "memory": 8192,
            "details": {
                f"external.{key}": value
                for key, value in deployment["external_details"].items()
            },
            "nic": [
                {
                    # CloudStack 4.22 NicResponse serializes deviceid as String.
                    "deviceid": "0",
                    "networkid": NETWORK_ID,
                    "macaddress": "AA:BB:CC:DD:EE:FF",
                    "ipaddress": "10.0.0.114",
                }
            ],
        }

    def bind(self, execution):
        session = get_session()
        try:
            bind_claim(
                session,
                claim_id=self.reservation.claim.id,
                generation=self.reservation.claim.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=self.manifest_sha256,
                cloudstack_vm_ref=execution.id,
                cloudstack_instance_name="i-2-114-VM",
            )
        finally:
            session.close()

    def activate(self, claim_id, generation):
        session = get_session()
        try:
            activate_bound_claim(
                session,
                claim_id=claim_id,
                generation=generation,
                cloudstack_vm_ref=self.execution.id,
            )
        finally:
            session.close()

    def reconcile(self, client):
        return reconcile_execution(
            self.execution.id,
            client=client,
            lease_seconds=60,
            activate=self.activate,
        )

    def test_create_is_idempotent_only_for_same_generation_and_plan(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first.id, second.id)
        self.assertEqual("planned", first.state)
        self.assertNotIn("plan_json", public_execution(first))

        changed = self.plan()
        changed["deployment"]["display_name"] = "different"
        session = get_session()
        try:
            with self.assertRaises(ExecutionConflict):
                create_execution(
                    session,
                    claim_id=self.reservation.claim.id,
                    generation=self.reservation.claim.generation,
                    plan=changed,
                )
        finally:
            session.close()

    def test_released_claim_can_create_one_execution_for_the_next_generation(self):
        first = self.create()
        session = get_session()
        try:
            execution = session.query(AdoptionExecution).filter_by(id=first.id).one()
            claim = session.query(AdoptionClaim).filter_by(
                id=self.reservation.claim.id
            ).one()
            execution.state = "rolled_back"
            claim.state = "released"
            session.commit()
            self.reservation = reserve_claim(
                session,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_json=self.manifest_json,
                manifest_sha256=self.manifest_sha256,
            )
            second = create_execution(
                session,
                claim_id=self.reservation.claim.id,
                generation=self.reservation.claim.generation,
                plan=self.plan(),
            )
            second_generation = self.reservation.claim.generation
            second_id = second.id
        finally:
            session.close()

        self.assertEqual(2, second_generation)
        self.assertNotEqual(first.id, second_id)

    def test_plan_rejects_credentials_and_non_admin_project_ownership(self):
        for name, change in (
            ("credential", lambda p: p["deployment"]["external_details"].update({"token": "x"})),
            ("project", lambda p: p["deployment"].update({"project_id": "x"})),
            ("account", lambda p: p["deployment"].update({"account": "customer"})),
        ):
            with self.subTest(name=name):
                plan = self.plan()
                change(plan)
                session = get_session()
                try:
                    with self.assertRaises(ExecutionInvalid):
                        create_execution(
                            session,
                            claim_id=self.reservation.claim.id,
                            generation=self.reservation.claim.generation,
                            plan=plan,
                        )
                finally:
                    session.close()

    def test_plan_rejects_gapped_or_reordered_network_devices(self):
        for device_id in (1, True, "0"):
            with self.subTest(device_id=device_id):
                plan = self.plan()
                plan["deployment"]["networks"][0]["device_id"] = device_id
                session = get_session()
                try:
                    with self.assertRaises(ExecutionInvalid):
                        create_execution(
                            session,
                            claim_id=self.reservation.claim.id,
                            generation=self.reservation.claim.generation,
                            plan=plan,
                        )
                finally:
                    session.close()

    def test_plan_rejects_unknown_network_ip_allocation_mode(self):
        plan = self.plan()
        plan["deployment"]["networks"][0]["ip_allocation"] = "unmanaged"
        session = get_session()
        try:
            with self.assertRaises(ExecutionInvalid):
                create_execution(
                    session,
                    claim_id=self.reservation.claim.id,
                    generation=self.reservation.claim.generation,
                    plan=plan,
                )
        finally:
            session.close()

    def test_deploy_payload_is_start_disabled_exact_and_secret_free(self):
        execution = self.create()
        params = _deploy_params(execution, self.plan())
        self.assertEqual(execution.id, params["customid"])
        self.assertEqual("false", params["startvm"])
        self.assertEqual("4", params["details[0].cpuNumber"])
        self.assertEqual("8192", params["details[0].memory"])
        self.assertEqual(NETWORK_ID, params["iptonetworklist[0].networkid"])
        self.assertEqual("AA:BB:CC:DD:EE:FF", params["iptonetworklist[0].mac"])
        self.assertNotIn("projectid", params)
        serialized = json.dumps(params).lower()
        self.assertNotIn("secret", serialized)
        self.assertNotIn("nonce", serialized)
        self.assertNotIn("token", serialized)

        static_plan = self.plan()
        static_plan["deployment"]["service_offering_customized"] = False
        static_params = _deploy_params(execution, static_plan)
        self.assertNotIn("details[0].cpuNumber", static_params)
        self.assertNotIn("details[0].memory", static_params)

    def test_full_two_job_flow_reaches_managed_success(self):
        self.execution = self.create()
        client = FakeCloudStack()

        result = self.reconcile(client)
        self.assertEqual("deploy_submitted", result["state"])
        self.assertEqual(1, len(client.deploy_calls))
        self.assertEqual("false", client.deploy_calls[0]["startvm"])

        client.vm = self.vm(self.execution, "Stopped")
        client.jobs["deploy-job"] = {"jobstatus": 1}
        result = self.reconcile(client)
        self.assertEqual("deploy_succeeded", result["state"])

        result = self.reconcile(client)
        self.assertEqual("start_submitted", result["state"])
        self.assertEqual([self.execution.id], client.start_calls)

        self.bind(self.execution)
        client.vm = self.vm(self.execution, "Running")
        client.jobs["start-job"] = {"jobstatus": 1}
        result = self.reconcile(client)
        self.assertEqual("verifying", result["state"])
        result = self.reconcile(client)
        self.assertEqual("succeeded", result["state"])

    def test_ambiguous_deploy_submission_never_replays_deploy(self):
        self.execution = self.create()
        client = FakeCloudStack()
        client.deploy_error = TimeoutError("lost response")
        result = self.reconcile(client)
        self.assertEqual("submission_unknown", result["state"])
        self.assertEqual(1, len(client.deploy_calls))

        client.deploy_error = None
        result = self.reconcile(client)
        self.assertEqual("submission_unknown", result["state"])
        self.assertEqual(1, len(client.deploy_calls))

        client.vm = self.vm(self.execution, "Stopped")
        result = self.reconcile(client)
        self.assertEqual("deploy_succeeded", result["state"])
        self.assertEqual(1, len(client.deploy_calls))

    def test_explicit_retry_rechecks_uuid_then_resubmits_once_when_absent(self):
        self.execution = self.create()
        client = FakeCloudStack()
        client.deploy_error = TimeoutError("lost response")
        first = self.reconcile(client)
        if first is None:
            self.fail("executor lease was not acquired")
        self.assertEqual("submission_unknown", first["state"])
        client.deploy_error = None

        retry = request_execution_retry(self.execution.id, client=client)
        self.assertEqual("planned", retry["state"])
        retried = self.reconcile(client)
        if retried is None:
            self.fail("executor lease was not acquired")
        self.assertEqual("deploy_submitted", retried["state"])
        self.assertEqual(2, len(client.deploy_calls))

    def test_explicit_retry_never_redeploys_existing_or_in_progress_vm(self):
        for state, expected in (("Stopped", "deploy_succeeded"), ("Starting", None)):
            with self.subTest(state=state):
                self.execution = self.create()
                session = get_session()
                try:
                    row = session.query(AdoptionExecution).filter_by(
                        id=self.execution.id
                    ).one()
                    row.state = "submission_unknown"
                    session.commit()
                finally:
                    session.close()
                client = FakeCloudStack()
                client.vm = self.vm(self.execution, state)
                if expected is None:
                    with self.assertRaises(ExecutionConflict):
                        request_execution_retry(self.execution.id, client=client)
                else:
                    result = request_execution_retry(self.execution.id, client=client)
                    self.assertEqual(expected, result["state"])
                self.assertEqual([], client.deploy_calls)

    def test_bound_stopped_start_unknown_requires_explicit_retry(self):
        self.execution = self.create()
        client = FakeCloudStack()
        client.vm = self.vm(self.execution, "Stopped")
        session = get_session()
        try:
            bind_claim(
                session,
                claim_id=self.execution.claim_id,
                generation=self.execution.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=self.reservation.claim.manifest_sha256,
                cloudstack_vm_ref=self.execution.id,
                cloudstack_instance_name=client.vm["name"],
            )
            row = session.query(AdoptionExecution).filter_by(
                id=self.execution.id
            ).one()
            row.state = "start_unknown"
            session.commit()
        finally:
            session.close()

        retry = request_execution_retry(self.execution.id, client=client)
        self.assertEqual("deploy_succeeded", retry["state"])
        self.assertEqual([], client.start_calls)

        submitted = self.reconcile(client)
        if submitted is None:
            self.fail("executor lease was not acquired")
        self.assertEqual("start_submitted", submitted["state"])
        self.assertEqual([self.execution.id], client.start_calls)

    def test_identity_mismatch_never_starts_vm(self):
        self.execution = self.create()
        client = FakeCloudStack()
        self.reconcile(client)
        client.vm = self.vm(self.execution, "Stopped")
        client.vm["hostid"] = "20000000-0000-4000-8000-000000000003"
        client.jobs["deploy-job"] = {"jobstatus": 1}
        result = self.reconcile(client)
        self.assertEqual("cleanup_required", result["state"])
        self.assertEqual([], client.start_calls)

    def test_worker_lease_has_exactly_one_concurrent_winner(self):
        execution = self.create()
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker():
            session = get_session()
            try:
                barrier.wait()
                acquired = acquire_execution(session, execution.id, 60)
                with lock:
                    outcomes.append(acquired is not None)
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([False, True], sorted(outcomes))
        session = get_session()
        try:
            row = session.query(AdoptionExecution).filter_by(id=execution.id).one()
            self.assertEqual(1, row.attempt_count)
        finally:
            session.close()


    def test_create_and_worker_cas_retry_known_mariadb_errors(self):
        session = get_session()
        try:
            fail_first_commit(session, 1020)
            execution = create_execution(
                session,
                claim_id=self.reservation.claim.id,
                generation=self.reservation.claim.generation,
                plan=self.plan(),
            )
        finally:
            session.close()

        session = get_session()
        try:
            fail_first_commit(session, 1205)
            acquired = acquire_execution(session, execution.id, 60)
            if acquired is None:
                self.fail("retryable worker acquisition did not recover")
            self.assertEqual(execution.id, acquired[0].id)
        finally:
            session.close()

    def test_nonretryable_executor_database_error_propagates(self):
        session = get_session()
        try:
            fail_first_commit(session, 9999)
            with self.assertRaises(OperationalError):
                create_execution(
                    session,
                    claim_id=self.reservation.claim.id,
                    generation=self.reservation.claim.generation,
                    plan=self.plan(),
                )
        finally:
            session.close()

    def test_retry_cas_retries_known_mariadb_error(self):
        execution = self.create()
        session = get_session()
        try:
            row = session.get(AdoptionExecution, execution.id)
            row.state = "submission_unknown"
            session.commit()
        finally:
            session.close()

        session = get_session()
        fail_first_commit(session)
        try:
            with patch("adoption_executor.get_session", return_value=session):
                result = request_execution_retry(execution.id, client=FakeCloudStack())
        finally:
            session.close()
        self.assertEqual("planned", result["state"])

    def test_cleanup_cas_retries_known_mariadb_error_once(self):
        execution = self.create()
        self._require_cleanup(execution)
        client = FakeCloudStack()
        client.vm = self.vm(execution, "Stopped")
        session = get_session()
        fail_first_commit(session)
        try:
            with patch("adoption_executor.get_session", return_value=session):
                result = request_execution_cleanup(execution.id, client=client)
        finally:
            session.close()
        self.assertEqual("cleanup_submitted", result["state"])
        self.assertEqual([(execution.id, True)], client.destroy_calls)

    def _require_cleanup(self, execution):
        session = get_session()
        try:
            row = session.query(AdoptionExecution).filter_by(id=execution.id).one()
            row.state = "cleanup_required"
            row.error_code = "test_cleanup"
            session.commit()
        finally:
            session.close()

    def _prepare_authorized_cleanup(self):
        self.execution = self.create()
        self._require_cleanup(self.execution)
        client = FakeCloudStack()
        client.vm = self.vm(self.execution, "Stopped")
        request_execution_cleanup(self.execution.id, client=client)
        session = get_session()
        try:
            authorize_cleanup_delete(
                session,
                claim_id=self.reservation.claim.id,
                generation=self.reservation.claim.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=self.manifest_sha256,
                cloudstack_vm_ref=self.execution.id,
                cloudstack_instance_name=self.vm(self.execution)["instancename"],
            )
        finally:
            session.close()
        client.jobs["cleanup-job"] = {"jobstatus": 1}
        client.vm = None
        return client

    def _assert_cleanup_release_retries(self, code):
        client = self._prepare_authorized_cleanup()
        session_calls = 0

        def sessions():
            nonlocal session_calls
            session_calls += 1
            session = get_session()
            if session_calls == 2:
                fail_first_commit(session, code)
            return session

        with patch("adoption_executor.get_session", side_effect=sessions):
            result = self.reconcile(client)
        self.assertIsNotNone(result)
        self.assertEqual("rolled_back", result["state"])
        self.assertEqual(1, len(client.destroy_calls))
        session = get_session()
        try:
            claim = session.get(AdoptionClaim, self.reservation.claim.id)
            self.assertEqual("released", claim.state)
        finally:
            session.close()

    def test_explicit_exact_stopped_cleanup_releases_reserved_claim_after_absence(self):
        self.execution = self.create()
        self._require_cleanup(self.execution)
        client = FakeCloudStack()
        client.vm = self.vm(self.execution, "Stopped")

        result = request_execution_cleanup(self.execution.id, client=client)
        self.assertEqual("cleanup_submitted", result["state"])
        self.assertEqual([(self.execution.id, True)], client.destroy_calls)

        session = get_session()
        try:
            authorized = authorize_cleanup_delete(
                session,
                claim_id=self.reservation.claim.id,
                generation=self.reservation.claim.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=self.manifest_sha256,
                cloudstack_vm_ref=self.execution.id,
                cloudstack_instance_name=self.vm(self.execution)["instancename"],
            )
            self.assertEqual(self.execution.id, authorized.id)
            claim = session.get(AdoptionClaim, self.reservation.claim.id)
            self.assertEqual("cleanup", claim.state)
            with self.assertRaises(ClaimConflict):
                bind_claim(
                    session,
                    claim_id=self.reservation.claim.id,
                    generation=self.reservation.claim.generation,
                    proxmox_cluster="p2",
                    proxmox_node="p2-hv07",
                    proxmox_vmid=114,
                    manifest_sha256=self.manifest_sha256,
                    cloudstack_vm_ref=self.execution.id,
                    cloudstack_instance_name="i-2-114-VM",
                )
        finally:
            session.close()

        client.jobs["cleanup-job"] = {"jobstatus": 1}
        client.vm = None
        result = self.reconcile(client)
        self.assertEqual("rolled_back", result["state"])
        session = get_session()
        try:
            claim = session.get(type(self.reservation.claim), self.reservation.claim.id)
            self.assertEqual("released", claim.state)
        finally:
            session.close()

    def test_cleanup_release_retries_mysql_current_read(self):
        self._assert_cleanup_release_retries(1020)

    def test_cleanup_release_retries_mysql_lock_timeout(self):
        self._assert_cleanup_release_retries(1205)

    def test_cleanup_release_retries_mysql_deadlock(self):
        self._assert_cleanup_release_retries(1213)

    def test_cleanup_release_propagates_nonretryable_database_error(self):
        client = self._prepare_authorized_cleanup()
        session_calls = 0

        def sessions():
            nonlocal session_calls
            session_calls += 1
            session = get_session()
            if session_calls == 2:
                fail_first_commit(session, 9999)
            return session

        with patch("adoption_executor.get_session", side_effect=sessions):
            with self.assertRaises(OperationalError):
                self.reconcile(client)
        self.assertEqual(1, len(client.destroy_calls))
        session = get_session()
        try:
            claim = session.get(AdoptionClaim, self.reservation.claim.id)
            self.assertEqual("cleanup", claim.state)
        finally:
            session.close()

    def test_cleanup_rejects_running_or_bound_vm_before_destroy(self):
        self.execution = self.create()
        self._require_cleanup(self.execution)
        running_client = FakeCloudStack()
        running_client.vm = self.vm(self.execution, "Running")
        with self.assertRaises(ExecutionConflict):
            request_execution_cleanup(self.execution.id, client=running_client)
        self.assertEqual([], running_client.destroy_calls)

        self.bind(self.execution)
        stopped_client = FakeCloudStack()
        stopped_client.vm = self.vm(self.execution, "Stopped")
        with self.assertRaises(ExecutionConflict):
            request_execution_cleanup(self.execution.id, client=stopped_client)
        self.assertEqual([], stopped_client.destroy_calls)

    def test_ambiguous_cleanup_submission_is_never_replayed(self):
        self.execution = self.create()
        self._require_cleanup(self.execution)
        client = FakeCloudStack()
        client.vm = self.vm(self.execution, "Stopped")
        client.destroy_error = TimeoutError("lost response")

        result = request_execution_cleanup(self.execution.id, client=client)
        self.assertEqual("cleanup_submitting", result["state"])
        self.assertEqual(1, len(client.destroy_calls))
        result = self.reconcile(client)
        self.assertEqual("cleanup_submitting", result["state"])
        self.assertEqual(1, len(client.destroy_calls))

        client.vm = None
        result = self.reconcile(client)
        self.assertEqual("rolled_back", result["state"])
        self.assertEqual(1, len(client.destroy_calls))

    def test_concurrent_cleanup_has_exactly_one_destroy_submission(self):
        self.execution = self.create()
        self._require_cleanup(self.execution)
        barrier = threading.Barrier(2)

        class BarrierCloudStack(FakeCloudStack):
            def list_virtual_machines(inner_self, **kwargs):
                result = super().list_virtual_machines(**kwargs)
                barrier.wait()
                return result

        client = BarrierCloudStack()
        client.vm = self.vm(self.execution, "Stopped")
        outcomes = []
        lock = threading.Lock()

        def worker():
            try:
                request_execution_cleanup(self.execution.id, client=client)
                outcome = "submitted"
            except ExecutionConflict:
                outcome = "conflict"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(["conflict", "submitted"], sorted(outcomes))
        self.assertEqual([(self.execution.id, True)], client.destroy_calls)

    def test_malformed_or_misidentified_vm_fails_closed_before_start(self):
        for field, value in (
            ("cpunumber", "not-an-integer"),
            ("nic", ["not-an-object"]),
            ("instancename", None),
            ("name", "wrong-name"),
            ("nic", [{
                "deviceid": 1,
                "networkid": NETWORK_ID,
                "macaddress": "AA:BB:CC:DD:EE:FF",
                "ipaddress": "10.0.0.114",
            }]),
            ("nic", [{
                "deviceid": "00",
                "networkid": NETWORK_ID,
                "macaddress": "AA:BB:CC:DD:EE:FF",
                "ipaddress": "10.0.0.114",
            }]),
            ("nic", [{
                "deviceid": " 0",
                "networkid": NETWORK_ID,
                "macaddress": "AA:BB:CC:DD:EE:FF",
                "ipaddress": "10.0.0.114",
            }]),
            ("nic", [{
                "deviceid": True,
                "networkid": NETWORK_ID,
                "macaddress": "AA:BB:CC:DD:EE:FF",
                "ipaddress": "10.0.0.114",
            }]),
        ):
            with self.subTest(field=field):
                self.execution = self.create()
                session = get_session()
                try:
                    row = session.query(AdoptionExecution).filter_by(
                        id=self.execution.id
                    ).one()
                    row.state = "deploy_submitted"
                    row.deploy_job_id = "deploy-job"
                    row.worker_lease_id = None
                    row.worker_lease_expires_at = None
                    session.commit()
                finally:
                    session.close()
                client = FakeCloudStack()
                client.vm = self.vm(self.execution, "Stopped")
                client.vm[field] = value
                client.jobs["deploy-job"] = {"jobstatus": 1}
                result = self.reconcile(client)
                self.assertIsNotNone(result)
                self.assertEqual("cleanup_required", result["state"])
                self.assertEqual([], client.start_calls)


if __name__ == "__main__":
    unittest.main()
