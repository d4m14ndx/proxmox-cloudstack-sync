import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from config import Settings


class OperatorAuthTests(unittest.TestCase):
    def setUp(self):
        self.original_token = app_main.settings.api_auth_token

    def tearDown(self):
        app_main.settings.api_auth_token = self.original_token
        if app_main.sync_lock.locked():
            app_main.sync_lock.release()

    def test_short_configured_token_is_rejected(self):
        with self.assertRaises(ValidationError):
            Settings(api_auth_token="too-short")

    def test_operator_dependency_fails_closed_when_unconfigured(self):
        app_main.settings.api_auth_token = ""
        with self.assertRaises(HTTPException) as caught:
            app_main.require_operator(None, None)
        self.assertEqual(503, caught.exception.status_code)

    def test_operator_dependency_accepts_header_or_bearer_only(self):
        app_main.settings.api_auth_token = "a" * 32
        for api_key, authorization in (
            (None, None),
            ("b" * 32, None),
            (None, f"Bearer {'b' * 32}"),
        ):
            with self.subTest(api_key=api_key, authorization=authorization):
                with self.assertRaises(HTTPException) as caught:
                    app_main.require_operator(api_key, authorization)
                self.assertEqual(401, caught.exception.status_code)

        self.assertIsNone(app_main.require_operator("a" * 32, None))
        self.assertIsNone(
            app_main.require_operator(None, f"Bearer {'a' * 32}")
        )

    def test_every_mutation_and_sensitive_inventory_route_declares_auth_dependency(self):
        sensitive_routes = {
            ("POST", "/api/sync"),
            ("GET", "/api/proxmox/vms"),
            ("GET", "/api/proxmox/clusters"),
            ("GET", "/api/adoption/candidates"),
            ("GET", "/api/drift"),
            ("GET", "/api/logs"),
            ("POST", "/api/match"),
            ("POST", "/api/unmatch/{proxmox_id}"),
            ("POST", "/api/register"),
            ("POST", "/api/cloudstack/repair-vm/{uuid}"),
            ("GET", "/api/cloudstack/vms"),
            ("GET", "/api/cloudstack/db-hosts"),
            ("GET", "/api/cloudstack/db-accounts"),
            ("GET", "/api/cloudstack/db-service-offerings"),
            ("GET", "/api/cloudstack/db-guest-os"),
            ("GET", "/api/cloudstack/db-networks"),
            ("GET", "/api/host-mappings"),
            ("POST", "/api/host-mappings"),
            ("DELETE", "/api/host-mappings/{mapping_id}"),
            ("GET", "/api/host-mappings/proxmox-nodes"),
            ("GET", "/api/network-mappings"),
            ("POST", "/api/network-mappings"),
            ("DELETE", "/api/network-mappings/{mapping_id}"),
            ("GET", "/api/network-mappings/proxmox-bridges"),
            ("GET", "/api/nics"),
            ("GET", "/api/nics/drift"),
            ("POST", "/api/reconcile/nic"),
            ("POST", "/api/reconcile/nics-all"),
            ("POST", "/api/reconcile/vm"),
            ("POST", "/api/reconcile/all"),
            ("GET", "/api/reconcile/status"),
        }
        routes = {
            (method, route.path): route
            for route in app_main.app.routes
            for method in getattr(route, "methods", set())
        }
        self.assertEqual(set(), sensitive_routes - routes.keys())
        for route_key in sensitive_routes:
            dependencies = {
                dependency.call
                for dependency in routes[route_key].dependant.dependencies
            }
            with self.subTest(route=route_key):
                self.assertIn(app_main.require_operator, dependencies)

    def test_sync_is_single_flight(self):
        with patch("main.run_sync", return_value={"ok": True}):
            self.assertEqual({"ok": True}, app_main.trigger_sync(None))

        app_main.sync_lock.acquire()
        with self.assertRaises(HTTPException) as caught:
            app_main.trigger_sync(None)
        self.assertEqual(409, caught.exception.status_code)

    def test_db_bound_http_handlers_are_sync_threadpool_functions(self):
        for handler in (
            app_main.trigger_sync,
            app_main.register_vm,
            app_main.removed_generic_repair,
            app_main.list_db_hosts,
            app_main.list_db_accounts,
            app_main.list_db_service_offerings,
            app_main.list_db_guest_os,
            app_main.list_db_networks,
            app_main.list_proxmox_vms,
            app_main.list_cloudstack_vms,
            app_main.list_adoption_candidates,
            app_main.list_proxmox_bridges,
        ):
            with self.subTest(handler=handler.__name__):
                self.assertFalse(inspect.iscoroutinefunction(handler))

    def test_frontend_uses_tab_scoped_token_and_disables_sync_by_default(self):
        source = (
            Path(__file__).resolve().parents[2] / "frontend" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("sessionStorage.getItem('operatorApiToken')", source)
        self.assertNotIn("localStorage", source)
        self.assertIn('id="syncBtn" disabled', source)
        self.assertIn("operator_auth_configured", source)
        self.assertIn("'X-API-Key': operatorToken", source)


if __name__ == "__main__":
    unittest.main()
