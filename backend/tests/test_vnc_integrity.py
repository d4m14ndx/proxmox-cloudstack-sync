import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cloudstack_db import CloudStackDB
from config import CloudStackDBConfig


class FakeCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1
        self._last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self._last_sql = sql
        self.calls.append((sql, params))
        return 1

    def fetchone(self):
        if "vm_instance_seq" in self._last_sql:
            return {"value": 200}
        return None


class FakeConnection:
    def __init__(self):
        self.fake_cursor = FakeCursor()
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.commits += 1


class VncIntegrityTests(unittest.TestCase):
    def make_db(self):
        db = CloudStackDB(CloudStackDBConfig())
        conn = FakeConnection()
        db._connect = lambda: conn
        return db, conn

    def test_external_registration_writes_empty_vnc_value(self):
        db, conn = self.make_db()
        result = db.register_existing_vm({
            "name": "TEST-EXTERNAL-VM",
            "instance_name": "TEST-EXTERNAL-VM",
            "host_id": 32,
            "zone_id": 1,
            "pod_id": 3,
            "service_offering_id": 4,
            "account_id": 8,
            "domain_id": 7,
            "guest_os_id": 1,
            "hypervisor_type": "External",
            "proxmox_vmid": 999,
            "state": "Running",
            "vm_template_id": 4,
            "private_mac_address": "BC:24:11:00:00:01",
        })

        insert = next(
            (params for sql, params in conn.fake_cursor.calls
             if "INSERT INTO vm_instance" in sql),
            None,
        )
        self.assertIsNotNone(insert)
        self.assertEqual("", insert[12])
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(200, result["id"])
        self.assertEqual(1, conn.commits)

    def test_repair_can_explicitly_clear_plaintext_vnc_value(self):
        db, conn = self.make_db()
        self.assertTrue(db.repair_registered_vm(
            "00000000-0000-0000-0000-000000000001",
            4,
            "BC:24:11:00:00:01",
            "",
        ))
        sql, params = conn.fake_cursor.calls[-1]
        self.assertIn("vnc_password = %s", sql)
        self.assertEqual("", params[-2])

    def test_repair_none_leaves_vnc_value_untouched(self):
        db, conn = self.make_db()
        self.assertTrue(db.repair_registered_vm(
            "00000000-0000-0000-0000-000000000001",
            4,
            "BC:24:11:00:00:01",
            None,
        ))
        sql, _ = conn.fake_cursor.calls[-1]
        self.assertNotIn("vnc_password = %s", sql)


if __name__ == "__main__":
    unittest.main()
