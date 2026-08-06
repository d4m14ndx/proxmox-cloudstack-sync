import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import adopt_one as runner
import main as app_main
from config import AdoptionPolicy
from database import AdoptionClaim, AdoptionExecution, get_session

DIGEST = "d" * 64


def candidate_catalog(*, executor_enabled):
    plan = {
        "manifest_sha256": DIGEST,
        "manifest": {"cluster": "p3-cluster03", "vmid": 110},
        "host": {"id": "host-id"},
        "service_offering": {"id": "offering-id"},
        "networks": [{"device": "net0", "id": "network-id"}],
    }
    if executor_enabled:
        plan["template"] = {"id": "template-id"}
    return {
        "runtime_safety": {
            "adoption_executor_enabled": False,
            "auto_reconcile": False,
            "auto_reconcile_nics": False,
        },
        "freshness": {
            "inventory_collection_current": True,
            "nic_collection_current": True,
        },
        "candidates": [{
            "proxmox_id": "p3-cluster03:110",
            "blockers": [] if executor_enabled else ["adoption_executor_not_enabled"],
            "adoption_plan": plan,
        }],
    }


class Delegate:
    def __init__(self):
        self.deploy = 0
        self.start = 0
        self.destroy = 0
        self.queries = []
        self.inventory_calls = []

    def deploy_virtual_machine(self, **params):
        self.deploy += 1
        self.assert_start_disabled = params.get("startvm") == "false"
        return {"jobid": "deploy-job"}

    def start_virtual_machine(self, vm_id):
        self.start += 1
        self.started_id = vm_id
        return {"jobid": "start-job"}

    def destroy_virtual_machine(self, vm_id, expunge=True):
        self.destroy += 1
        raise AssertionError("destroy must never be called")

    def query_async_job(self, job_id):
        self.queries.append(job_id)
        return {"jobstatus": 1}

    def list_virtual_machines(self, **params):
        self.inventory_calls.append(params)
        return []


class AdoptOneRunPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = {
            "database_url": app_main.settings.database_url,
            "registry": app_main.settings.adoption_registry_enabled,
            "executor": app_main.settings.adoption_executor_enabled,
            "auto": app_main.settings.auto_reconcile,
            "auto_nics": app_main.settings.auto_reconcile_nics,
            "policy": app_main.settings.adoption_policy,
            "engine": app_main.engine,
            "token": app_main.settings.api_auth_token,
        }
        app_main.settings.database_url = f"sqlite:///{self.temp.name}/sidecar.db"
        app_main.settings.adoption_registry_enabled = True
        app_main.settings.adoption_executor_enabled = False
        app_main.settings.auto_reconcile = False
        app_main.settings.auto_reconcile_nics = False
        app_main.settings.api_auth_token = "operator-token-for-test-only"
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id="40000000-0000-4000-8000-000000000001",
            customized_service_offering_id="40000000-0000-4000-8000-000000000002",
            template_id="40000000-0000-4000-8000-000000000003",
        )
        app_main.engine = None
        self.target = runner.parse_target("p3-cluster03:110", DIGEST)

    def tearDown(self):
        app_main.settings.database_url = self.saved["database_url"]
        app_main.settings.adoption_registry_enabled = self.saved["registry"]
        app_main.settings.adoption_executor_enabled = self.saved["executor"]
        app_main.settings.auto_reconcile = self.saved["auto"]
        app_main.settings.auto_reconcile_nics = self.saved["auto_nics"]
        app_main.settings.adoption_policy = self.saved["policy"]
        app_main.settings.api_auth_token = self.saved["token"]
        app_main.engine = self.saved["engine"]
        self.temp.cleanup()

    def test_each_unsafe_live_flag_stops_before_sidecar_or_provider_mutation(self):
        constructed = []
        for field in (
            "adoption_executor_enabled",
            "auto_reconcile",
            "auto_reconcile_nics",
        ):
            catalog = candidate_catalog(executor_enabled=False)
            catalog["runtime_safety"][field] = True
            with self.subTest(field=field), self.assertRaisesRegex(
                runner.OperatorStop, "live_runtime_safety_gate_failed"
            ):
                runner.run_one(
                    self.target,
                    timeout_seconds=30,
                    poll_seconds=1,
                    client_factory=lambda: constructed.append(True),
                    live_catalog_loader=lambda _token, value=catalog: value,
                )
            session = get_session()
            try:
                self.assertEqual(0, session.query(AdoptionClaim).count())
                self.assertEqual(0, session.query(AdoptionExecution).count())
            finally:
                session.close()
        self.assertEqual([], constructed)

    def test_normal_path_has_exact_bounded_calls_and_final_state(self):
        delegate = Delegate()
        state = {"claim": None, "execution": None}
        claim_id = "claim-id"
        execution_id = "execution-id"

        def reserve(_request, _operator):
            state["claim"] = {
                "id": claim_id,
                "cluster": "p3-cluster03",
                "vmid": 110,
                "manifest_sha256": DIGEST,
                "generation": 1,
                "state": "reserved",
                "cloudstack_vm_ref": None,
                "cloudstack_instance_name": None,
                "operation_lease_present": False,
            }

        def execute(_claim_id, _request, _operator):
            state["execution"] = {
                "id": execution_id,
                "claim_id": claim_id,
                "generation": 1,
                "state": "planned",
                "deploy_job_id": None,
                "start_job_id": None,
                "cleanup_job_id": None,
                "cloudstack_vm_ref": None,
                "cloudstack_instance_name": None,
                "error_code": None,
                "worker_lease_present": False,
            }
            app_main.engine.cs_client.list_virtual_machines(
                hypervisor="External",
                details="all",
                _max_pages=20,
                _deadline_monotonic=10**20,
            )
            response = app_main.engine.cs_client.deploy_virtual_machine(startvm="false")
            state["execution"]["state"] = "deploy_submitted"
            state["execution"]["deploy_job_id"] = response["jobid"]

        def reconcile(_execution_id, _operator):
            current = state["execution"]["state"]
            if current == "deploy_submitted":
                state["execution"].update(
                    state="deploy_succeeded",
                    cloudstack_vm_ref=execution_id,
                    cloudstack_instance_name="i-2-164-VM",
                )
            elif current == "deploy_succeeded":
                response = app_main.engine.cs_client.start_virtual_machine(execution_id)
                state["execution"].update(
                    state="start_submitted", start_job_id=response["jobid"]
                )
            elif current == "start_submitted":
                state["execution"]["state"] = "verifying"
            elif current == "verifying":
                state["claim"].update(
                    state="managed",
                    cloudstack_vm_ref=execution_id,
                    cloudstack_instance_name="i-2-164-VM",
                )
                state["execution"].update(state="succeeded", error_code=None)
            else:
                raise AssertionError(current)

        with (
            patch.object(runner, "_load_target_state", side_effect=lambda _target: state),
            patch.object(
                app_main,
                "list_adoption_candidates",
                side_effect=lambda: candidate_catalog(executor_enabled=True),
            ),
            patch.object(app_main, "create_adoption_claim", side_effect=reserve),
            patch.object(
                app_main,
                "_execute_adoption_claim_under_authority",
                side_effect=execute,
            ),
            patch.object(
                app_main,
                "_reconcile_adoption_execution_under_authority",
                side_effect=reconcile,
            ),
        ):
            result = runner.run_one(
                self.target,
                timeout_seconds=30,
                poll_seconds=0.5,
                client_factory=lambda: delegate,
                live_catalog_loader=lambda _token: candidate_catalog(
                    executor_enabled=False
                ),
            )

        self.assertEqual("managed", result["claim"]["state"])
        self.assertEqual("succeeded", result["execution"]["state"])
        self.assertEqual(
            {"deploy": 1, "start": 1, "destroy": 0, "job_queries": 2},
            result["calls_this_run"],
        )
        self.assertEqual(["deploy-job", "start-job"], delegate.queries)
        self.assertEqual(1, len(delegate.inventory_calls))
        self.assertEqual(20, delegate.inventory_calls[0]["_max_pages"])
        self.assertLess(delegate.inventory_calls[0]["_deadline_monotonic"], 10**20)
        self.assertTrue(delegate.assert_start_disabled)
        self.assertEqual(execution_id, delegate.started_id)
        self.assertFalse(app_main.settings.adoption_executor_enabled)
        self.assertIsNone(app_main.engine)


if __name__ == "__main__":
    unittest.main()
