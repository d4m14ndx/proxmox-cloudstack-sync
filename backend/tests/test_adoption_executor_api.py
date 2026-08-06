import hashlib
import json
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from adoption_executor import ExecutionInvalid
from adoption_registry import bind_claim, reserve_claim
from config import AdoptionPolicy
from database import AdoptionExecution, HostMapping, get_session, init_db


def _guarded_registry_mutation(function):
    def call(*args, **kwargs):
        kwargs.setdefault("write_guard", lambda: None)
        return function(*args, **kwargs)

    return call


reserve_claim = _guarded_registry_mutation(reserve_claim)
bind_claim = _guarded_registry_mutation(bind_claim)

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

    def list_vlan_ip_ranges(self, **kwargs):
        return [{
            "networkid": NETWORK_ID,
            "startip": "10.0.0.2",
            "endip": "10.0.0.254",
        }]

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
                "service_offering": {
                    "id": OFFERING_ID,
                    "customized": True,
                    "root_disk_size_customized": True,
                    "root_disk_size_gib": 20,
                    "details": {
                        "cpuNumber": 4,
                        "cpuSpeed": 1200,
                        "memory": 8192,
                    },
                },
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
        self.assertEqual("20", client.deploy_calls[0]["rootdisksize"])
        serialized = json.dumps(first).lower()
        self.assertNotIn("manifest", serialized)
        self.assertNotIn("external_details", serialized)
        session = get_session()
        try:
            self.assertEqual(1, session.query(AdoptionExecution).count())
        finally:
            session.close()

    def test_external_ipam_omits_cloudstack_ip_allocation(self):
        client = RouteCloudStack()
        engine = Mock()
        engine.cs_client = client
        candidate = self.candidate()
        candidate["adoption_plan"]["networks"][0]["ip_allocation"] = "external"
        request = app_main.ExecuteAdoptionClaimRequest(generation=self.generation)

        with (
            patch.object(app_main, "engine", engine),
            patch.object(
                app_main,
                "list_adoption_candidates",
                return_value={"candidates": [candidate]},
            ),
        ):
            app_main.execute_adoption_claim(self.claim_id, request, None)

        self.assertEqual(1, len(client.deploy_calls))
        params = client.deploy_calls[0]
        self.assertNotIn("iptonetworklist[0].ip", params)
        self.assertEqual(NETWORK_ID, params["iptonetworklist[0].networkid"])
        self.assertEqual(
            "AA:BB:CC:DD:EE:FF",
            params["iptonetworklist[0].mac"],
        )

    def test_unresolved_ip_requires_exact_operator_input_and_freezes_it(self):
        manifest_network = self.manifest["networks"][0]
        manifest_network["device"] = "net0"
        manifest_network.pop("device_id")
        manifest_network["ip"] = None
        manifest_network["ip_override_required"] = True
        self.manifest_json = json.dumps(
            self.manifest, sort_keys=True, separators=(",", ":")
        )
        self.digest = hashlib.sha256(self.manifest_json.encode()).hexdigest()
        session = get_session()
        try:
            claim = session.get(app_main.AdoptionClaim, self.claim_id)
            claim.manifest_json = self.manifest_json
            claim.manifest_sha256 = self.digest
            session.commit()
        finally:
            session.close()

        client = RouteCloudStack()
        engine = Mock()
        engine.cs_client = client
        candidate = self.candidate()
        candidate["adoption_plan"]["networks"][0]["ip"] = None
        planning = {"candidates": [candidate]}

        with (
            patch.object(app_main, "engine", engine),
            patch.object(app_main, "list_adoption_candidates", return_value=planning),
            self.assertRaises(HTTPException) as missing,
        ):
            app_main.execute_adoption_claim(
                self.claim_id,
                app_main.ExecuteAdoptionClaimRequest(generation=self.generation),
                None,
            )
        self.assertEqual(409, missing.exception.status_code)
        self.assertEqual([], client.deploy_calls)

        request = app_main.ExecuteAdoptionClaimRequest(
            generation=self.generation,
            network_ip_overrides=[
                app_main.NetworkIPOverride(device_id=0, ip="10.0.0.115")
            ],
        )
        with (
            patch.object(app_main, "engine", engine),
            patch.object(app_main, "list_adoption_candidates", return_value=planning),
        ):
            response = app_main.execute_adoption_claim(
                self.claim_id, request, None
            )

        self.assertEqual("deploy_submitted", response["state"])
        self.assertEqual(
            "10.0.0.115",
            client.deploy_calls[0]["iptonetworklist[0].ip"],
        )
        session = get_session()
        try:
            execution = session.query(AdoptionExecution).one()
            persisted = json.loads(execution.plan_json)
            self.assertEqual(
                "10.0.0.115",
                persisted["deployment"]["networks"][0]["ip"],
            )
        finally:
            session.close()

    def test_old_generation_execution_does_not_skip_live_ip_validation(self):
        manifest_network = self.manifest["networks"][0]
        manifest_network["device"] = "net0"
        manifest_network.pop("device_id")
        manifest_network["ip"] = None
        manifest_network["ip_override_required"] = True
        self.manifest_json = json.dumps(
            self.manifest, sort_keys=True, separators=(",", ":")
        )
        self.digest = hashlib.sha256(self.manifest_json.encode()).hexdigest()
        self.generation = 2
        session = get_session()
        try:
            claim = session.get(app_main.AdoptionClaim, self.claim_id)
            claim.manifest_json = self.manifest_json
            claim.manifest_sha256 = self.digest
            claim.generation = self.generation
            session.add(AdoptionExecution(
                id=str(uuid.uuid4()),
                claim_id=self.claim_id,
                generation=1,
                plan_sha256="f" * 64,
                plan_json="{}",
                state="failed",
            ))
            session.commit()
        finally:
            session.close()

        client = RouteCloudStack()
        engine = Mock()
        engine.cs_client = client
        candidate = self.candidate()
        candidate["adoption_plan"]["networks"][0]["ip"] = None
        request = app_main.ExecuteAdoptionClaimRequest(
            generation=self.generation,
            network_ip_overrides=[
                app_main.NetworkIPOverride(device_id=0, ip="10.0.0.115")
            ],
        )

        with (
            patch.object(app_main, "engine", engine),
            patch.object(
                app_main,
                "list_adoption_candidates",
                return_value={"candidates": [candidate]},
            ),
            patch.object(
                app_main,
                "_validate_operator_network_ips_live",
                side_effect=ExecutionInvalid("fresh collision"),
            ) as validate_live,
            self.assertRaises(HTTPException) as caught,
        ):
            app_main.execute_adoption_claim(self.claim_id, request, None)

        self.assertEqual(409, caught.exception.status_code)
        validate_live.assert_called_once()
        self.assertEqual([], client.deploy_calls)
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
        self.assertEqual(409, caught.exception.status_code)
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
        candidate = self.candidate()
        candidate["adoption_plan"]["networks"][0]["ip_allocation"] = "external"
        with (
            patch.object(app_main, "engine", engine),
            patch.object(
                app_main,
                "list_adoption_candidates",
                return_value={"candidates": [candidate]},
            ),
        ):
            execution_response = app_main.execute_adoption_claim(
                self.claim_id,
                request,
                None,
            )

        execution_id = execution_response["id"]
        instance_name = "i-2-114-VM"
        worker_lease_id = str(uuid.uuid4())
        session = get_session()
        try:
            persisted_execution = session.query(AdoptionExecution).filter_by(
                id=execution_id
            ).one()
            execution_plan_sha256 = persisted_execution.plan_sha256
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
                execution_plan_sha256=execution_plan_sha256,
                ip_overrides_json="[]",
            )
            execution = session.query(AdoptionExecution).filter_by(
                id=execution_id
            ).one()
            execution.state = "verifying"
            execution.cloudstack_vm_ref = execution_id
            execution.cloudstack_instance_name = instance_name
            execution.worker_lease_id = worker_lease_id
            execution.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=5
            )
            session.add(HostMapping(
                proxmox_cluster="p2",
                proxmox_node="p2-hv07",
                cloudstack_host_id="30",
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
            "hostname": "p2-hv07.example",
            "serviceofferingid": OFFERING_ID,
            "templateid": TEMPLATE_ID,
            "account": "admin",
            "domainid": DOMAIN_ID,
            "projectid": None,
            "cpunumber": 4,
            "cpuspeed": 1200,
            "memory": 8192,
            "details": {
                "External:adopt_existing": "true",
                "External:adopt_claim_id": self.claim_id,
                "External:adopt_claim_generation": str(self.generation),
                "External:adopt_manifest_sha256": self.digest,
                "External:adopt_manifest_json": self.manifest_json,
                "External:proxmox_cluster": "p2",
            },
            "nic": [{
                "deviceid": 0,
                "networkid": NETWORK_ID,
                "macaddress": "AA:BB:CC:DD:EE:00",
                "ipaddress": None,
            }],
        }]
        activation_request = app_main.ActivateAdoptionClaimRequest(
            generation=self.generation,
            execution_id=execution_id,
            worker_lease_id=worker_lease_id,
        )
        with (
            patch.object(app_main, "engine", engine),
            self.assertRaises(HTTPException) as mismatch,
        ):
            app_main.activate_adoption_claim(
                self.claim_id,
                activation_request,
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

        client.vms[0]["details"].update({
            "External:adopt_execution_plan_sha256": (
                execution_plan_sha256
            ),
            "External:adopt_ip_overrides_json": "[]",
        })
        client.vms[0]["nic"][0]["macaddress"] = "AA:BB:CC:DD:EE:FF"
        client.vms[0]["nic"][0]["ipaddress"] = "10.0.0.200"
        with (
            patch.object(app_main, "engine", engine),
            self.assertRaises(HTTPException) as wrong_ip,
        ):
            app_main.activate_adoption_claim(
                self.claim_id,
                activation_request,
                None,
            )
        self.assertEqual(409, wrong_ip.exception.status_code)

        client.vms[0]["nic"][0]["ipaddress"] = None
        replacement_lease_id = str(uuid.uuid4())
        original_list_virtual_machines = client.list_virtual_machines

        def replace_lease_during_cloudstack_read(**kwargs):
            self.assertEqual({"id": execution_id, "details": "all"}, kwargs)
            session = get_session()
            try:
                execution = session.get(AdoptionExecution, execution_id)
                execution.worker_lease_id = replacement_lease_id
                execution.worker_lease_expires_at = datetime.now(
                    timezone.utc
                ) + timedelta(minutes=5)
                session.commit()
            finally:
                session.close()
            return list(client.vms)

        client.list_virtual_machines = replace_lease_during_cloudstack_read
        with (
            patch.object(app_main, "engine", engine),
            self.assertRaises(HTTPException) as stale_worker,
        ):
            app_main.activate_adoption_claim(
                self.claim_id,
                activation_request,
                None,
            )
        self.assertEqual(409, stale_worker.exception.status_code)
        session = get_session()
        try:
            claim = session.get(app_main.AdoptionClaim, self.claim_id)
            execution = session.get(AdoptionExecution, execution_id)
            self.assertEqual("bound", claim.state)
            self.assertEqual("verifying", execution.state)
            self.assertEqual(replacement_lease_id, execution.worker_lease_id)
        finally:
            session.close()

        client.list_virtual_machines = original_list_virtual_machines
        session = get_session()
        try:
            execution = session.get(AdoptionExecution, execution_id)
            execution.worker_lease_id = worker_lease_id
            execution.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=5
            )
            session.commit()
        finally:
            session.close()
        with patch.object(app_main, "engine", engine):
            activated = app_main.activate_adoption_claim(
                self.claim_id,
                activation_request,
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
