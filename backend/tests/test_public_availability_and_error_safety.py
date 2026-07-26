import asyncio
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from database import init_db
from sync_engine import SyncEngine


class PublicAvailabilityAndErrorSafetyTests(unittest.TestCase):
    def test_all_blocking_http_handlers_are_threadpool_functions(self):
        blocking_handlers = (
            app_main.index,
            app_main.list_proxmox_vms,
            app_main.list_proxmox_clusters,
            app_main.list_cloudstack_vms,
            app_main.get_drift,
            app_main.list_cloudstack_hosts,
            app_main.list_cs_clusters,
            app_main.list_cs_zones,
            app_main.list_service_offerings,
            app_main.list_cs_networks,
            app_main.list_cs_disk_offerings,
            app_main.list_db_hosts,
            app_main.list_db_accounts,
            app_main.list_db_service_offerings,
            app_main.list_db_guest_os,
            app_main.list_host_mappings,
            app_main.create_host_mapping,
            app_main.delete_host_mapping,
            app_main.list_proxmox_nodes,
            app_main.list_network_mappings,
            app_main.create_network_mapping,
            app_main.delete_network_mapping,
            app_main.list_proxmox_bridges,
            app_main.list_db_networks,
            app_main.list_nics,
            app_main.get_nic_drift,
            app_main.reconcile_nic,
            app_main.reconcile_nics_all,
            app_main.reconcile_vm,
            app_main.reconcile_all,
            app_main.reconcile_status,
            app_main.get_logs,
            app_main.dashboard,
        )
        for handler in blocking_handlers:
            with self.subTest(handler=handler.__name__):
                self.assertFalse(inspect.iscoroutinefunction(handler))

    def test_uncaught_sync_exception_is_sanitized_before_public_status(self):
        marker = "sensitive-marker-host-user-secret"
        original_engine = app_main.engine
        original_result = app_main.last_sync_result
        app_main.engine = Mock()
        app_main.engine.full_sync.side_effect = RuntimeError(marker)
        try:
            result = app_main.run_sync()
            status = asyncio.run(app_main.get_status())
        finally:
            app_main.engine = original_engine
            app_main.last_sync_result = original_result
        serialized = json.dumps({"result": result, "status": status["last_sync"]})
        self.assertNotIn(marker, serialized)
        self.assertIn("RuntimeError", serialized)

    def test_component_sync_errors_expose_type_not_exception_message(self):
        marker = "sensitive-marker-host-user-secret"
        with tempfile.TemporaryDirectory() as tmp:
            init_db(f"sqlite:///{Path(tmp) / 'sync.db'}")
            engine = SyncEngine.__new__(SyncEngine)
            px_client = Mock(cluster_name="p2")
            px_client.get_all_vms.side_effect = RuntimeError(marker)
            engine.proxmox_clients = [px_client]
            px_result = engine.sync_proxmox()

            cs_client = Mock()
            cs_client.list_virtual_machines.side_effect = RuntimeError(marker)
            engine.cs_client = cs_client
            cs_result = engine.sync_cloudstack()

        serialized = json.dumps({"proxmox": px_result, "cloudstack": cs_result})
        self.assertNotIn(marker, serialized)
        self.assertIn("RuntimeError", serialized)

    def test_full_sync_rolls_back_and_closes_final_log_session_on_failure(self):
        engine = SyncEngine.__new__(SyncEngine)
        engine.settings = Mock(
            cloudstack_db=Mock(password=""),
            auto_reconcile=False,
            auto_reconcile_nics=False,
        )
        engine.cs_db = None
        engine.sync_proxmox = Mock(return_value={
            "vms_found": 0,
            "vms_new": 0,
        })
        engine.sync_cloudstack = Mock(return_value={"vms_found": 0})
        engine.match_vms = Mock(return_value={
            "matched": 0,
            "unmatched_proxmox": 0,
            "unmatched_cloudstack": 0,
        })
        engine.sync_nics = Mock(return_value={"px_vms": 0, "cs_vms": 0})

        for failure_point in ("log", "commit"):
            session = Mock()
            engine._log = Mock()
            if failure_point == "log":
                engine._log.side_effect = RuntimeError("log failure")
            else:
                session.commit.side_effect = RuntimeError("commit failure")
            with self.subTest(failure_point=failure_point):
                with patch("sync_engine.get_session", return_value=session):
                    with self.assertRaises(RuntimeError):
                        engine.full_sync()
                session.rollback.assert_called_once_with()
                session.close.assert_called_once_with()

    def test_backend_does_not_interpolate_caught_exception_messages(self):
        production_sources = [
            path
            for path in BACKEND.glob("*.py")
            if path.name != "__init__.py"
        ]
        for path in production_sources:
            source = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotIn("str(e)", source)
                self.assertNotIn("{e}", source)
                self.assertNotIn("repr(e)", source)
                self.assertNotIn(", e)", source)


if __name__ == "__main__":
    unittest.main()
