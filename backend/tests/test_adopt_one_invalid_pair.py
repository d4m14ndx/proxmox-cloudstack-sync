import sys
import tempfile
import unittest
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from adopt_one import OperatorStop, parse_target, run_one
from config import AdoptionPolicy
from database import AdoptionClaim, AdoptionExecution, get_session, init_db


def safe_live_catalog(_token):
    return {
        "runtime_safety": {
            "adoption_executor_enabled": False,
            "auto_reconcile": False,
            "auto_reconcile_nics": False,
        }
    }


class AdoptOneInvalidPairTests(unittest.TestCase):
    def test_managed_planned_stops_before_cloudstack_client_construction(self):
        original = {
            "database_url": app_main.settings.database_url,
            "registry": app_main.settings.adoption_registry_enabled,
            "executor": app_main.settings.adoption_executor_enabled,
            "auto": app_main.settings.auto_reconcile,
            "auto_nics": app_main.settings.auto_reconcile_nics,
            "policy": app_main.settings.adoption_policy,
            "engine": app_main.engine,
        }
        digest = "b" * 64
        constructed = []
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app_main.settings.database_url = f"sqlite:///{temp_dir}/sidecar.db"
                app_main.settings.adoption_registry_enabled = True
                app_main.settings.adoption_executor_enabled = False
                app_main.settings.auto_reconcile = False
                app_main.settings.auto_reconcile_nics = False
                app_main.settings.adoption_policy = AdoptionPolicy(
                    enabled=True,
                    domain_id=str(uuid.uuid4()),
                    customized_service_offering_id=str(uuid.uuid4()),
                    template_id=str(uuid.uuid4()),
                )
                app_main.engine = None
                init_db(app_main.settings.database_url)
                claim_id = str(uuid.uuid4())
                execution_id = str(uuid.uuid4())
                session = get_session()
                session.add(
                    AdoptionClaim(
                        id=claim_id,
                        proxmox_cluster="p3-cluster03",
                        proxmox_node="p3-hv02",
                        proxmox_vmid=110,
                        manifest_sha256=digest,
                        manifest_json='{"cluster":"p3-cluster03","vmid":110}',
                        state="managed",
                        generation=1,
                        cloudstack_vm_ref=execution_id,
                        cloudstack_instance_name="i-2-164-VM",
                    )
                )
                session.add(
                    AdoptionExecution(
                        id=execution_id,
                        claim_id=claim_id,
                        generation=1,
                        plan_sha256="c" * 64,
                        plan_json="{}",
                        state="planned",
                    )
                )
                session.commit()
                session.close()

                def client_factory():
                    constructed.append(True)
                    raise AssertionError("CloudStack client must not be constructed")

                with self.assertRaisesRegex(OperatorStop, "phase_pair_invalid"):
                    run_one(
                        parse_target("p3-cluster03:110", digest),
                        timeout_seconds=30,
                        poll_seconds=1,
                        client_factory=client_factory,
                        live_catalog_loader=safe_live_catalog,
                    )
                self.assertEqual([], constructed)
                session = get_session()
                self.assertEqual(
                    "planned",
                    session.query(AdoptionExecution).filter_by(id=execution_id).one().state,
                )
                session.close()
        finally:
            app_main.settings.database_url = original["database_url"]
            app_main.settings.adoption_registry_enabled = original["registry"]
            app_main.settings.adoption_executor_enabled = original["executor"]
            app_main.settings.auto_reconcile = original["auto"]
            app_main.settings.auto_reconcile_nics = original["auto_nics"]
            app_main.settings.adoption_policy = original["policy"]
            app_main.engine = original["engine"]


if __name__ == "__main__":
    unittest.main()
