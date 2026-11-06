"""Focused synthetic tenant-ownership regression checks; no live credentials."""

import json
import sys
import unittest
from pathlib import Path

from unittest.mock import Mock, patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import main as app_main
import test_adoption_planning as fixtures
from adoption_executor import (
    ExecutionConflict, _deploy_params, _vm_matches_plan,
    create_execution, validate_execution_plan,
)
from adoption_ownership import validated_manifest_ownership
from adoption_registry import bind_claim, reserve_claim
from config import AdoptionPolicy, AdoptionTarget
from database import AdoptionClaim, AdoptionExecution, get_session
from pydantic import ValidationError

TENANT_DOMAIN = "11111111-1111-4111-8111-111111111111"
TENANT_NETWORK = "22222222-2222-4222-8222-222222222222"
OWNER = {"account": "TenantA", "domain_id": TENANT_DOMAIN, "project_id": None}
TARGET = {
    "proxmox_id": "p2:100", **{k: OWNER[k] for k in ("account", "domain_id")},
    "network_overrides": {"net0": TENANT_NETWORK}, "preserve_guest_ip": True,
}


class TenantCatalog(fixtures.CatalogClient):
    account_state = "enabled"
    network_account = "TenantA"

    def __init__(self):
        super().__init__(host_overrides={"clusterid": fixtures.CLUSTER_ID})

    def list_domains(self, **kwargs):
        if kwargs.get("id") == TENANT_DOMAIN:
            return [{"id": TENANT_DOMAIN, "name": "TenantA", "path": "ROOT/TenantA", "state": "Active"}]
        return super().list_domains(**kwargs)

    def list_accounts(self, **kwargs):
        return [{"id": "33333333-3333-4333-8333-333333333333", "name": "TenantA", "domainid": TENANT_DOMAIN, "state": self.account_state}]

    def list_networks(self):
        network = dict(super().list_networks()[0])
        network.update(id=TENANT_NETWORK, name="Tenant L2", type="L2", account=self.network_account,
                       domainid=TENANT_DOMAIN, domain="TenantA", domainpath="ROOT/TenantA")
        return super().list_networks() + [network]


class TenantOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdoptionPlanningTests("runTest")
        self.fixture.setUp()
        self.fixture._add_complete_candidate()
        self.client = TenantCatalog()
        self.engine = Mock(cs_client=self.client)
        self.engine._inventory_collection_ready = True
        self.engine._nic_collection_ready = True
        self.engine_patch = patch.object(app_main, "engine", self.engine)
        self.engine_patch.start()
        app_main.settings.adoption_executor_enabled = True
        self.policy()

    def tearDown(self):
        self.engine_patch.stop()
        self.fixture.tearDown()

    def policy(self, targets=None):
        app_main.settings.adoption_policy = AdoptionPolicy(
            enabled=True, domain_id=fixtures.DOMAIN_ID,
            customized_service_offering_id=fixtures.CUSTOM_OFFERING_ID,
            template_id=fixtures.TEMPLATE_ID,
            targets=[AdoptionTarget.model_validate(t) for t in ([TARGET] if targets is None else targets)],
        )

    def candidate(self):
        return app_main.list_adoption_candidates()["candidates"][0]

    def reserve(self, row):
        plan = row["adoption_plan"]
        session = get_session()
        try:
            return reserve_claim(
                session, proxmox_cluster="p2", proxmox_node="p2-hv11", proxmox_vmid=100,
                manifest_json=json.dumps(plan["manifest"], sort_keys=True, separators=(",", ":")),
                manifest_sha256=plan["manifest_sha256"], write_guard=lambda: None,
            ).claim
        finally:
            session.close()

    def vm(self, plan, execution):
        d = plan["deployment"]
        details = {f"external.{key}": value for key, value in d["external_details"].items()}
        details.update({"external.adopt_execution_plan_sha256": execution.plan_sha256,
                        "external.adopt_ip_overrides_json": "[]"})
        return {"id": execution.id, "name": d["name"], "displayname": d["display_name"],
                "instancename": "i-9-100-VM", "hypervisor": "External", "state": "Running",
                "hostid": d["host_id"], "hostname": "p2-hv11.infra.example",
                "serviceofferingid": d["service_offering_id"], "templateid": d["template_id"],
                "account": d["account"], "domainid": d["domain_id"], "cpunumber": d["cpus"],
                "memory": d["memory_mib"], "cpuspeed": d["cpu_speed_mhz"], "details": details,
                "nic": [{"deviceid": "0", "networkid": d["networks"][0]["network_id"],
                         "macaddress": d["networks"][0]["mac"], "ipaddress": None if d["networks"][0]["ip_allocation"] == "external" else d["networks"][0]["ip"]}]}

    def test_tenant_plan_freezes_owner_and_matches_only_that_owner(self):
        row = self.candidate()
        self.assertEqual([], row["blockers"])
        self.assertEqual(OWNER, row["adoption_plan"]["manifest"]["ownership"])
        self.assertEqual(TENANT_NETWORK, row["adoption_plan"]["networks"][0]["cloudstack_network_id"])
        self.assertEqual("external", row["adoption_plan"]["networks"][0]["ip_allocation"])
        self.assertEqual("10.120.0.100", row["adoption_plan"]["manifest"]["networks"][0]["ip"])
        claim = self.reserve(row)
        plan = app_main._build_execution_plan(row, claim)
        validate_execution_plan(plan, claim)
        session = get_session()
        try:
            execution = create_execution(session, claim_id=claim.id, generation=claim.generation,
                                         plan=plan, write_guard=lambda: None)
            params = _deploy_params(execution, plan)
            self.assertEqual(("TenantA", TENANT_DOMAIN, "false"), (params["account"], params["domainid"], params["startvm"]))
            self.assertNotIn("iptonetworklist[0].ip", params)
            vm = self.vm(plan, execution)
            self.assertTrue(_vm_matches_plan(vm, execution, plan))
            for key, wrong in [("account", "admin"), ("domainid", fixtures.DOMAIN_ID), ("projectid", "foreign-project")]:
                with self.subTest(key=key):
                    bad = dict(vm, **{key: wrong})
                    self.assertFalse(_vm_matches_plan(bad, execution, plan))
            execution.state = "verifying"
            session.commit()
            bound = bind_claim(
                session, claim_id=claim.id, generation=claim.generation,
                proxmox_cluster="p2", proxmox_node="p2-hv11", proxmox_vmid=100,
                manifest_sha256=claim.manifest_sha256, cloudstack_vm_ref=execution.id,
                cloudstack_instance_name=vm["instancename"],
                execution_plan_sha256=execution.plan_sha256, ip_overrides_json="[]",
                write_guard=lambda: None,
            )
            self.assertEqual([], app_main._cloudstack_activation_mismatches(session, bound, vm))
            wrong = dict(vm, account="admin")
            self.assertIn("cloudstack_account_mismatch", app_main._cloudstack_activation_mismatches(session, bound, wrong))
        finally:
            session.close()

    def test_owner_change_after_reservation_fails_without_creating_execution(self):
        row = self.candidate()
        claim = self.reserve(row)
        changed = dict(TARGET, account="OtherTenant")
        self.policy([changed])
        with self.assertRaises(ExecutionConflict):
            app_main._build_execution_plan(row, claim)
        session = get_session()
        try:
            self.assertEqual(0, session.query(AdoptionExecution).count())
        finally:
            session.close()

    def test_foreign_network_or_disabled_account_suppresses_manifest(self):
        for field, wrong in [("network_account", "OtherTenant"), ("account_state", "disabled")]:
            with self.subTest(field=field):
                original = getattr(self.client, field)
                setattr(self.client, field, wrong)
                row = self.candidate()
                self.assertTrue(row["blockers"])
                self.assertIsNone(row["adoption_plan"]["manifest"])
                setattr(self.client, field, original)
        session = get_session()
        try:
            self.assertEqual(0, session.query(AdoptionClaim).count())
        finally:
            session.close()

    def test_default_root_plan_and_other_sources_are_unchanged(self):
        self.policy([])
        row = self.candidate()
        self.assertEqual([], row["blockers"])
        self.assertEqual("admin", row["adoption_plan"]["owner"]["account"])
        self.assertNotIn("ownership", row["adoption_plan"]["manifest"])
        claim = self.reserve(row)
        plan = app_main._build_execution_plan(row, claim)
        validate_execution_plan(plan, claim)
        self.assertEqual("admin", plan["deployment"]["account"])
        self.assertEqual(fixtures.NETWORK_ID, plan["deployment"]["networks"][0]["network_id"])

    def test_malformed_target_or_manifest_ownership_is_rejected(self):
        self.assertIsNone(validated_manifest_ownership({}))
        for bad in [None, {}, dict(OWNER, project_id="project"), dict(OWNER, account=" TenantA"), dict(OWNER, domain_id=TENANT_DOMAIN.upper()), dict(OWNER, extra="field")]:
            # This UUID contains only digits, so make the uppercase case genuinely noncanonical.
            if isinstance(bad, dict) and bad.get("domain_id") == TENANT_DOMAIN and bad == OWNER:
                bad = dict(OWNER, domain_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")
            with self.subTest(ownership=bad):
                with self.assertRaises(ValueError):
                    validated_manifest_ownership({"ownership": bad})
        for bad in [dict(TARGET, proxmox_id="p2:0100"), dict(TARGET, account=" TenantA"), dict(TARGET, preserve_guest_ip="true"), dict(TARGET, network_overrides={"net00": TENANT_NETWORK})]:
            with self.subTest(target=bad):
                with self.assertRaises(ValidationError):
                    AdoptionTarget.model_validate(bad)
        with self.assertRaises(ValidationError):
            self.policy([TARGET, TARGET])


if __name__ == "__main__":
    unittest.main()
