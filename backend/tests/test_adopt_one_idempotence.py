import sys
import tempfile
import unittest
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from adopt_one import parse_target, run_one
from config import AdoptionPolicy
from database import AdoptionClaim, AdoptionExecution, get_session, init_db


class NoCallCloudStack:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected CloudStack call: {name}")


class AdoptOneIdempotenceTests(unittest.TestCase):
    def test_run_one_is_idempotent_for_managed_succeeded_target(self):
        saved = {
            "database_url": app_main.settings.database_url,
            "registry": app_main.settings.adoption_registry_enabled,
            "executor": app_main.settings.adoption_executor_enabled,
            "auto": app_main.settings.auto_reconcile,
            "auto_nics": app_main.settings.auto_reconcile_nics,
            "policy": app_main.settings.adoption_policy,
            "engine": app_main.engine,
        }
        digest = "a" * 64
        execution_id = str(uuid.uuid4())
        claim_id = str(uuid.uuid4())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                app_main.settings.database_url = (
                    f"sqlite:///{Path(tmp) / 'operator.db'}"
                )
                app_main.settings.adoption_registry_enabled = True
                app_main.settings.adoption_executor_enabled = False
                app_main.settings.auto_reconcile = False
                app_main.settings.auto_reconcile_nics = False
                app_main.settings.adoption_policy = AdoptionPolicy(
                    enabled=True,
                    domain_id="30000000-0000-4000-8000-000000000001",
                    customized_service_offering_id=(
                        "30000000-0000-4000-8000-000000000002"
                    ),
                    template_id="30000000-0000-4000-8000-000000000003",
                )
                app_main.engine = None
                init_db(app_main.settings.database_url)
                session = get_session()
                try:
                    session.add(AdoptionClaim(
                        id=claim_id,
                        proxmox_cluster="p3-cluster03",
                        proxmox_node="p3-hv02",
                        proxmox_vmid=110,
                        manifest_sha256=digest,
                        manifest_json="{}",
                        generation=1,
                        state="managed",
                        cloudstack_vm_ref=execution_id,
                        cloudstack_instance_name="i-2-164-VM",
                    ))
                    session.add(AdoptionExecution(
                        id=execution_id,
                        claim_id=claim_id,
                        generation=1,
                        plan_sha256=digest,
                        plan_json="{}",
                        state="succeeded",
                        deploy_job_id="deploy-job",
                        start_job_id="start-job",
                        cloudstack_vm_ref=execution_id,
                        cloudstack_instance_name="i-2-164-VM",
                        attempt_count=5,
                    ))
                    session.commit()
                finally:
                    session.close()

                result = run_one(
                    parse_target("p3-cluster03:110", digest),
                    timeout_seconds=30,
                    poll_seconds=1,
                    client_factory=NoCallCloudStack,
                    live_catalog_loader=lambda _token: {
                        "runtime_safety": {
                            "adoption_executor_enabled": False,
                            "auto_reconcile": False,
                            "auto_reconcile_nics": False,
                        }
                    },
                )
                self.assertEqual("managed", result["claim"]["state"])
                self.assertEqual("succeeded", result["execution"]["state"])
                self.assertEqual(
                    {"deploy": 0, "start": 0, "destroy": 0, "job_queries": 0},
                    result["calls_this_run"],
                )
                self.assertFalse(app_main.settings.adoption_executor_enabled)
        finally:
            app_main.settings.database_url = saved["database_url"]
            app_main.settings.adoption_registry_enabled = saved["registry"]
            app_main.settings.adoption_executor_enabled = saved["executor"]
            app_main.settings.auto_reconcile = saved["auto"]
            app_main.settings.auto_reconcile_nics = saved["auto_nics"]
            app_main.settings.adoption_policy = saved["policy"]
            app_main.engine = saved["engine"]


if __name__ == "__main__":
    unittest.main()
