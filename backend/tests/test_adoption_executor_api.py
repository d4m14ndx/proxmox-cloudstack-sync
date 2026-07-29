import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from adoption_registry import bind_claim, reserve_claim
from config import AdoptionPolicy
from database import AdoptionExecution, HostMapping, get_session, init_db

ZONE_ID = "30000000-0000-4000-8000-000000000001"
CLUSTER_ID = "30000000-0000-4000-8000-000000000002"
HOST_ID = "30000000-0000-4000-8000-000000000003"
TEMPLATE_ID = "30000000-0000-4000-8000-000000000004"
OFFERING_ID = "30000000-0000-4000-8000-000000000005"
DOMAIN_ID = "30000000-0000-4000-8000-000000000006"
NETWORK_ID = "30000000-0000-4000-8000-000000000007"


class RouteCloudStack:
    def __init__(self):
        self.deploy_calls = []
        self.vms = []

    def list_virtual_machines(self, **kwargs):
        return list(self.vms)

    def deploy_virtual_machine(self, **params):
        self.deploy_calls.append(params)
        return {"jobid": "deploy-job"}

    def query_async_job(self, job_id):
        return {"jobstatus": 0}


class AdoptionExecutorApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'api.db'}")
        self.original_executor = app_main.settings.adoption_executor_enabled
        self.original_registry = app_main.settings.adoption_registry_enabled
        self.original_policy = app_main.settings.adoption_policy
        manifest = {
            "placement": {"cluster": "p2", "node": "p2-hv07"},
            "vmid": 114,
            "name": "existing-name",
            "status": "running",
            "cpus": 4,
            "memory_mib": 8192,
            "networks": [{
                "device_id": 0,
                "mac": "AA:BB:CC:DD:EE:FF",
                "ip": "10.0.0.114",
                "proxmox_bridge": "vmbr0",
                "proxmox_vlan": 100,
                "cloudstack_network_id": NETWORK_ID,
            }],
            "storage": [{
                "device": "scsi0",
                "volume": "ceph:vm-114-disk-0",
                "storage": "ceph",
                "size": "20G",
            }],
        }
        self.manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        self.manifest = manifest
        self.digest = hashlib.sha256(self.manifest_json.encode()).hexdigest()
        session = get_session()
        try:
            self.claim = reserve_claim(
                session,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_json=self.manifest_json,
                manifest_sha256=self.digest,
            ).claim
            self.claim_id = self.claim.id
            self.generation = self.claim.generation
        finally:
            session.close()
        app_main.settings.adoption_registry_enabled = True
        app_main.settings.adoption_executor_enabled = True
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True,
            domain_id=DOMAIN_ID,
            customized_service_offering_id=OFFERING_ID,
            template_id=TEMPLATE_ID,
        )

    def tearDown(self):
        app_main.settings.adoption_executor_enabled = self.original_executor
        app_main.settings.adoption_registry_enabled = self.original_registry
        app_main.settings.adoption_policy = self.original_policy
        self.tmp.cleanup()

    def candidate(self, blockers=None):
        return {
            "proxmox_id": "p2:114",
            "blockers": blockers or [],
            "adoption_plan": {
                "manifest": self.manifest,
                "manifest_sha256": self.digest,
                "host": {
                    "id": HOST_ID,
                    "zone_id": ZONE_ID,
                    "cluster_id": CLUSTER_ID,
                },
                "template": {"id": TEMPLATE_ID},
                "service_offering": {"id": OFFERING_ID, "customized": True},
                "networks": [{
                    "device_id": 0,
                    "cloudstack_network_id": NETWORK_ID,
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "ip": "10.0.0.114",
                }],
            },
        }

    def test_execute_route_is_idempotent_and_secret_free(self):
        client = RouteCloudStack()
        engine = Mock()
        engine.cs_client = client
        planning = {"candidates": [self.candidate()]}
        request = app_main.ExecuteAdoptionClaimRequest(generation=self.generation)

        with (
            patch.object(app_main, "engine", engine),
            patch.object(app_main, "list_adoption_candidates", return_value=planning),
        ):
            first = app_main.execute_adoption_claim(self.claim_id, request, None)
            second = app_main.execute_adoption_claim(self.claim_id, request, None)

        self.assertEqual("deploy_submitted", first["state"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, len(client.deploy_calls))
        self.assertEqual("false", client.deploy_calls[0]["startvm"])
        serialized = json.dumps(first).lower()
        self.assertNotIn("manifest", serialized)
        self.assertNotIn("external_details", serialized)
        session = get_session()
        try:
            self.assertEqual(1, session.query(AdoptionExecution).count())
        finally:
            session.close()

    def test_execute_route_rejects_blockers_before_creating_execution(self):
        client = RouteCloudStack()
        engine = Mock()
        engine.cs_client = client
        planning = {"candidates": [self.candidate(["template_mismatch"])]}
        request = app_main.ExecuteAdoptionClaimRequest(generation=self.generation)
        with (
            patch.object(app_main, "engine", engine),
            patch.object(app_main, "list_adoption_candidates", return_value=planning),
            self.assertRaises(HTTPException) as caught,
        ):
            app_main.execute_adoption_claim(self.claim_id, request, None)
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual([], client.deploy_calls)
        session = get_session()
        try:
            self.assertEqual(0, session.query(AdoptionExecution).count())
        finally:
            session.close()

    def test_execute_route_rejects_gapped_network_devices_before_execution(self):
        client = RouteCloudStack()
        engine = Mock()
        engine.cs_client = client
        candidate = self.candidate()
        candidate["adoption_plan"]["networks"][0]["device_id"] = 1
        request = app_main.ExecuteAdoptionClaimRequest(generation=self.generation)
        with (
            patch.object(app_main, "engine", engine),
            patch.object(
                app_main,
                "list_adoption_candidates",
                return_value={"candidates": [candidate]},
            ),
            self.assertRaises(HTTPException) as caught,
        ):
            app_main.execute_adoption_claim(self.claim_id, request, None)
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual([], client.deploy_calls)
        session = get_session()
        try:
            self.assertEqual(0, session.query(AdoptionExecution).count())
        finally:
            session.close()

    def test_executor_activation_revalidates_exact_nics_and_execution_state(self):
        client = RouteCloudStack()
        engine = Mock()
        engine.cs_client = client
        request = app_main.ExecuteAdoptionClaimRequest(generation=self.generation)
        with (
            patch.object(app_main, "engine", engine),
            patch.object(
                app_main,
                "list_adoption_candidates",
                return_value={"candidates": [self.candidate()]},
            ),
        ):
            execution_response = app_main.execute_adoption_claim(
                self.claim_id,
                request,
                None,
            )

        execution_id = execution_response["id"]
        instance_name = "i-2-114-VM"
        session = get_session()
        try:
            bind_claim(
                session,
                claim_id=self.claim_id,
                generation=self.generation,
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                proxmox_vmid=114,
                manifest_sha256=self.digest,
                cloudstack_vm_ref=execution_id,
                cloudstack_instance_name=instance_name,
            )
            execution = session.query(AdoptionExecution).filter_by(
                id=execution_id
            ).one()
            execution.state = "verifying"
            execution.cloudstack_vm_ref = execution_id
            execution.cloudstack_instance_name = instance_name
            session.add(HostMapping(
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                cloudstack_host_id=HOST_ID,
                cloudstack_host_name="p2-hv07.example",
            ))
            session.commit()
        finally:
            session.close()

        client.vms = [{
            "id": execution_id,
            "name": f"adopt-114-{self.claim_id[:8]}",
            "instancename": instance_name,
            "displayname": "existing-name",
            "hypervisor": "External",
            "state": "Running",
            "hostid": HOST_ID,
            "serviceofferingid": OFFERING_ID,
            "templateid": TEMPLATE_ID,
            "account": "admin",
            "domainid": DOMAIN_ID,
            "projectid": None,
            "cpunumber": 4,
            "memory": 8192,
            "details": {
                "external.proxmox_vmid": "114",
                "external.adopt_existing": "true",
                "external.adopt_claim_id": self.claim_id,
                "external.adopt_claim_generation": str(self.generation),
                "external.adopt_manifest_sha256": self.digest,
                "external.adopt_manifest_json": self.manifest_json,
                "external.proxmox_cluster": "p2",
            },
            "nic": [{
                "deviceid": 0,
                "networkid": NETWORK_ID,
                "macaddress": "AA:BB:CC:DD:EE:00",
                "ipaddress": "10.0.0.114",
            }],
        }]
        with (
            patch.object(app_main, "engine", engine),
            self.assertRaises(HTTPException) as mismatch,
        ):
            app_main.activate_adoption_claim(
                self.claim_id,
                app_main.ActivateAdoptionClaimRequest(generation=self.generation),
                None,
            )
        self.assertEqual(409, mismatch.exception.status_code)
        mismatch_detail = mismatch.exception.detail
        if not isinstance(mismatch_detail, dict):
            self.fail("activation mismatch detail is not structured")
        mismatch_detail = cast(dict, mismatch_detail)
        self.assertIn(
            "cloudstack_execution_plan_mismatch",
            mismatch_detail["mismatches"],
        )

        client.vms[0]["nic"][0]["macaddress"] = "AA:BB:CC:DD:EE:FF"
        with patch.object(app_main, "engine", engine):
            activated = app_main.activate_adoption_claim(
                self.claim_id,
                app_main.ActivateAdoptionClaimRequest(generation=self.generation),
                None,
            )
        self.assertEqual("managed", activated["status"])

    def test_execute_route_is_disabled_by_default_gate(self):
        app_main.settings.adoption_executor_enabled = False
        request = app_main.ExecuteAdoptionClaimRequest(generation=self.generation)
        with self.assertRaises(HTTPException) as caught:
            app_main.execute_adoption_claim(self.claim_id, request, None)
        self.assertEqual(503, caught.exception.status_code)


if __name__ == "__main__":
    unittest.main()
