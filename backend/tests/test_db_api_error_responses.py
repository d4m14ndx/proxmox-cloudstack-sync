import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main


class DatabaseApiErrorResponseTests(unittest.TestCase):
    def setUp(self):
        self.original_engine = app_main.engine

    def tearDown(self):
        app_main.engine = self.original_engine

    def test_repair_error_detail_does_not_include_driver_message(self):
        cs_db = Mock()
        cs_db.get_vm_by_uuid.side_effect = RuntimeError("sensitive-marker")
        app_main.engine = SimpleNamespace(cs_db=cs_db)

        with self.assertRaises(HTTPException) as caught:
            app_main.repair_registered_vm("vm-uuid")

        self.assertEqual(500, caught.exception.status_code)
        self.assertEqual("CloudStack VM repair failed", caught.exception.detail)
        self.assertNotIn("sensitive-marker", caught.exception.detail)

    def test_registration_error_detail_does_not_include_driver_message(self):
        px = SimpleNamespace(
            id="cluster/qemu/137",
            cluster="p3-cluster03",
            node="p3-hv03",
        )
        mapping = SimpleNamespace(
            cloudstack_host_id="32",
            cloudstack_host_name="p3-hv03.infra.example",
        )
        query = Mock()
        query.filter_by.return_value.first.side_effect = [px, mapping]
        session = Mock()
        session.query.return_value = query

        cs_db = Mock()
        cs_db.get_host_by_id.side_effect = RuntimeError("sensitive-marker")
        engine = Mock()
        engine.cs_db = cs_db
        engine._resolve_host_db_id.return_value = 32
        app_main.engine = engine

        request = app_main.RegisterRequest(
            proxmox_id=px.id,
            service_offering_id=1,
            account_id=1,
            domain_id=1,
        )
        with patch("main.get_session", return_value=session):
            with self.assertRaises(HTTPException) as caught:
                app_main.register_vm(request)

        self.assertEqual(500, caught.exception.status_code)
        self.assertEqual("CloudStack VM registration failed", caught.exception.detail)
        self.assertNotIn("sensitive-marker", caught.exception.detail)
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
