import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from adopt_one import (
    BoundedCloudStackClient,
    OperatorStop,
    drive_execution,
    parse_target,
    strict_job_status,
    wait_for_job,
)


class FakeDelegate:
    def __init__(self):
        self.deploys = []
        self.starts = []
        self.destroys = []
        self.inventory_calls = []
        self.ip_range_calls = []

    def deploy_virtual_machine(self, **params):
        self.deploys.append(params)
        return {"jobid": "deploy-job"}

    def start_virtual_machine(self, vm_id, *, host_id=None):
        self.starts.append(vm_id)
        return {"jobid": "start-job"}

    def destroy_virtual_machine(self, vm_id, *, expunge=True):
        self.destroys.append((vm_id, expunge))
        return {"jobid": "destroy-job"}

    def query_async_job(self, job_id):
        return {"jobstatus": 1, "jobid": job_id}

    def list_virtual_machines(self, **params):
        self.inventory_calls.append(params)
        return []

    def list_vlan_ip_ranges(self, **params):
        self.ip_range_calls.append(params)
        return []


class AdoptOneTests(unittest.TestCase):
    def test_bounded_inventory_allows_only_network_scoped_ip_ranges(self):
        delegate = FakeDelegate()
        client = BoundedCloudStackClient(
            delegate,
            allow_deploy=False,
            allow_start=False,
            authority_guard=lambda: None,
        )

        self.assertEqual([], client.list_vlan_ip_ranges(networkid="network-1"))
        self.assertEqual([{"networkid": "network-1"}], delegate.ip_range_calls)
        with self.assertRaises(OperatorStop):
            client.list_vlan_ip_ranges(networkid="")

    def test_bounded_inventory_consumes_and_clamps_existing_bounds_once(self):
        delegate = FakeDelegate()
        client = BoundedCloudStackClient(
            delegate,
            allow_deploy=False,
            allow_start=False,
            authority_guard=lambda: None,
        )

        rows = client.list_virtual_machines(
            hypervisor="External",
            _max_pages=50,
            _deadline_monotonic=10**20,
        )

        self.assertEqual([], rows)
        self.assertEqual(1, len(delegate.inventory_calls))
        call = delegate.inventory_calls[0]
        self.assertEqual(20, call["_max_pages"])
        self.assertLess(call["_deadline_monotonic"], 10**20)
        self.assertEqual("External", call["hypervisor"])

    def test_parse_target_requires_canonical_identity_and_hash(self):
        digest = "a" * 64
        target = parse_target("p3-cluster03:110", digest)
        self.assertEqual("p3-cluster03", target.cluster)
        self.assertEqual(110, target.vmid)
        self.assertEqual(digest, target.manifest_sha256)

        for proxmox_id in (" p3:110", "p3:110 ", "p3:0110", "p3:+110", "p3:0", "p3", "p3:1:2"):
            with self.subTest(proxmox_id=proxmox_id), self.assertRaises(OperatorStop):
                parse_target(proxmox_id, digest)
        for invalid_hash in ("A" * 64, "a" * 63, "g" * 64, " a" * 32):
            with self.subTest(hash=invalid_hash), self.assertRaises(OperatorStop):
                parse_target("p3:110", invalid_hash)

    def test_parse_target_accepts_exact_network_ip_overrides(self):
        digest = "a" * 64
        target = parse_target(
            "p3-cluster03:110",
            digest,
            ["net2=192.0.2.12", "net0=192.0.2.10"],
        )
        self.assertEqual(
            ((0, "192.0.2.10"), (2, "192.0.2.12")),
            target.network_ip_overrides,
        )
        for values in (
            ["net00=192.0.2.10"],
            ["net0=192.0.2.010"],
            ["net0=2001:db8::1"],
            ["net0=192.0.2.10", "net0=192.0.2.11"],
            ["net0=192.0.2.10", "net1=192.0.2.10"],
        ):
            with self.subTest(values=values), self.assertRaises(OperatorStop):
                parse_target("p3-cluster03:110", digest, values)

    def test_strict_job_status_rejects_malformed_wire_values(self):
        for value in (True, False, "1", 3, -1, None, 1.0):
            with self.subTest(value=value), self.assertRaises(OperatorStop):
                strict_job_status({"jobstatus": value})
        for value in (0, 1, 2):
            self.assertEqual(value, strict_job_status({"jobstatus": value}))
        with self.assertRaises(OperatorStop):
            strict_job_status([])

    def test_bounded_client_allows_one_scoped_deploy_and_start_never_destroy(self):
        delegate = FakeDelegate()
        client = BoundedCloudStackClient(
            delegate,
            allow_deploy=True,
            allow_start=True,
            authority_guard=lambda: None,
        )
        client.deploy_virtual_machine(startvm="false")
        client.start_virtual_machine("vm-id")
        with self.assertRaises(OperatorStop):
            client.deploy_virtual_machine(startvm="false")
        with self.assertRaises(OperatorStop):
            client.start_virtual_machine("vm-id")
        with self.assertRaises(OperatorStop):
            client.destroy_virtual_machine("vm-id")
        self.assertEqual(1, len(delegate.deploys))
        self.assertEqual(1, len(delegate.starts))
        self.assertEqual([], delegate.destroys)

    def test_bounded_client_rejects_startvm_true_before_delegate(self):
        delegate = FakeDelegate()
        client = BoundedCloudStackClient(
            delegate,
            allow_deploy=True,
            allow_start=False,
            authority_guard=lambda: None,
        )
        with self.assertRaises(OperatorStop):
            client.deploy_virtual_machine(startvm="true")
        self.assertEqual([], delegate.deploys)

    def test_wait_for_job_polls_pending_then_returns_terminal(self):
        results = iter(({"jobstatus": 0}, {"jobstatus": 0}, {"jobstatus": 1}))
        sleeps = []
        status = wait_for_job(
            lambda _job_id: next(results),
            "job-id",
            deadline=10,
            poll_seconds=0.5,
            monotonic=lambda: 0,
            sleep=sleeps.append,
        )
        self.assertEqual(1, status)
        self.assertEqual([0.5, 0.5], sleeps)

    def test_wait_for_job_timeout_is_safe_to_resume(self):
        clock = iter((0, 11))
        with self.assertRaisesRegex(OperatorStop, "async_job_wait_timeout_safe_to_resume"):
            wait_for_job(
                lambda _job_id: {"jobstatus": 0},
                "job-id",
                deadline=10,
                poll_seconds=1,
                monotonic=lambda: next(clock),
                sleep=lambda _seconds: None,
            )

    def test_driver_completes_normal_sequence(self):
        state = {
            "claim": {"state": "reserved"},
            "execution": {
                "id": "execution-id",
                "state": "planned",
                "deploy_job_id": None,
                "start_job_id": None,
            },
        }
        transitions = {
            "planned": ("deploy_submitted", "deploy_job_id", "deploy-job"),
            "deploy_submitted": ("deploy_succeeded", None, None),
            "deploy_succeeded": ("start_submitted", "start_job_id", "start-job"),
            "start_submitted": ("verifying", None, None),
            "verifying": ("succeeded", None, None),
        }
        reconciled = []
        queried = []

        def load_state():
            return state

        def reconcile(_execution_id):
            current = state["execution"]["state"]
            reconciled.append(current)
            next_state, field, value = transitions[current]
            state["execution"]["state"] = next_state
            if field:
                state["execution"][field] = value
            if next_state == "succeeded":
                state["claim"]["state"] = "managed"
            return state["execution"]

        def query(job_id):
            queried.append(job_id)
            return {"jobstatus": 1}

        result = drive_execution(
            load_state=load_state,
            reconcile=reconcile,
            query_job=query,
            deadline=10,
            poll_seconds=1,
            monotonic=lambda: 0,
            sleep=lambda _seconds: None,
        )
        self.assertEqual("succeeded", result["execution"]["state"])
        self.assertEqual("managed", result["claim"]["state"])
        self.assertEqual(
            ["planned", "deploy_submitted", "deploy_succeeded", "start_submitted", "verifying"],
            reconciled,
        )
        self.assertEqual(["deploy-job", "start-job"], queried)

    def test_driver_resume_from_deploy_submitted_never_runs_planned_transition(self):
        state = {
            "claim": {"state": "bound"},
            "execution": {
                "id": "execution-id",
                "state": "deploy_submitted",
                "deploy_job_id": "deploy-job",
                "start_job_id": None,
            },
        }
        reconciled = []

        def reconcile(_execution_id):
            current = state["execution"]["state"]
            reconciled.append(current)
            states = {
                "deploy_submitted": "deploy_succeeded",
                "deploy_succeeded": "start_submitted",
                "start_submitted": "verifying",
                "verifying": "succeeded",
            }
            state["execution"]["state"] = states[current]
            if current == "deploy_succeeded":
                state["execution"]["start_job_id"] = "start-job"
            if current == "verifying":
                state["claim"]["state"] = "managed"
            return state["execution"]

        drive_execution(
            load_state=lambda: state,
            reconcile=reconcile,
            query_job=lambda _job_id: {"jobstatus": 1},
            deadline=10,
            poll_seconds=1,
            monotonic=lambda: 0,
            sleep=lambda _seconds: None,
        )
        self.assertNotIn("planned", reconciled)

    def test_driver_stops_nonprogressing_ambiguous_state_after_one_observation(self):
        state = {
            "claim": {"state": "reserved"},
            "execution": {"id": "execution-id", "state": "submission_unknown"},
        }
        calls = []
        with self.assertRaisesRegex(OperatorStop, "ambiguous_submission_unknown"):
            drive_execution(
                load_state=lambda: state,
                reconcile=lambda execution_id: calls.append(execution_id),
                query_job=lambda _job_id: {"jobstatus": 1},
                deadline=10,
                poll_seconds=1,
                monotonic=lambda: 0,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(["execution-id"], calls)

    def test_driver_stops_nonprogressing_activation(self):
        state = {
            "claim": {"state": "bound"},
            "execution": {"id": "execution-id", "state": "verifying"},
        }
        with self.assertRaisesRegex(OperatorStop, "reconciliation_made_no_progress"):
            drive_execution(
                load_state=lambda: state,
                reconcile=lambda _execution_id: state["execution"],
                query_job=lambda _job_id: {"jobstatus": 1},
                deadline=10,
                poll_seconds=1,
                monotonic=lambda: 0,
                sleep=lambda _seconds: None,
            )

    def test_driver_rejects_succeeded_execution_without_managed_claim(self):
        state = {
            "claim": {"state": "bound"},
            "execution": {"id": "execution-id", "state": "succeeded"},
        }
        with self.assertRaisesRegex(OperatorStop, "without_managed_claim"):
            drive_execution(
                load_state=lambda: state,
                reconcile=lambda _execution_id: None,
                query_job=lambda _job_id: {"jobstatus": 1},
                deadline=10,
                poll_seconds=1,
                monotonic=lambda: 0,
                sleep=lambda _seconds: None,
            )


if __name__ == "__main__":
    unittest.main()