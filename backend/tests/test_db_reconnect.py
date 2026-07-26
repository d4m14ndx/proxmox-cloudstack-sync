import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import Settings
from sync_engine import SyncEngine


class FakeCloudStackDB:
    outcomes = []
    instances = []

    def __init__(self, config):
        self.config = config
        self.last_connection_error = None
        self.__class__.instances.append(self)

    def test_connection(self):
        outcome = self.__class__.outcomes.pop(0)
        if not outcome:
            self.last_connection_error = {
                "type": "OperationalError",
                "code": 2003,
            }
        return outcome


class DatabaseReconnectTests(unittest.TestCase):
    def setUp(self):
        FakeCloudStackDB.outcomes = []
        FakeCloudStackDB.instances = []

    def settings(self, password="redacted-test-value"):
        return Settings(cloudstack_db={
            "host": "db.example.invalid",
            "port": 3306,
            "user": "cloud",
            "password": password,
            "database": "cloud",
        })

    @patch("sync_engine.CloudStackDB", FakeCloudStackDB)
    def test_failed_startup_probe_can_reconnect_later(self):
        FakeCloudStackDB.outcomes = [False, True]
        engine = SyncEngine(self.settings())

        self.assertIsNone(engine.cs_db)
        self.assertIsNotNone(engine.cs_db_last_error)
        assert engine.cs_db_last_error is not None
        self.assertEqual(2003, engine.cs_db_last_error["code"])
        self.assertNotIn("message", engine.cs_db_last_error)

        self.assertFalse(engine.connect_cloudstack_db())
        self.assertEqual(1, len(FakeCloudStackDB.instances))

        self.assertTrue(engine.connect_cloudstack_db(force=True))
        self.assertIsNotNone(engine.cs_db)
        self.assertIsNone(engine.cs_db_last_error)
        self.assertEqual(2, len(FakeCloudStackDB.instances))

    @patch("sync_engine.CloudStackDB", FakeCloudStackDB)
    def test_missing_password_is_reported_without_connection_attempt(self):
        engine = SyncEngine(self.settings(password=""))

        self.assertFalse(engine.connect_cloudstack_db())
        self.assertIsNone(engine.cs_db)
        self.assertIsNotNone(engine.cs_db_last_error)
        assert engine.cs_db_last_error is not None
        self.assertEqual("ConfigurationError", engine.cs_db_last_error["type"])
        self.assertNotIn("message", engine.cs_db_last_error)
        self.assertEqual([], FakeCloudStackDB.instances)

    @patch("sync_engine.CloudStackDB", FakeCloudStackDB)
    def test_full_sync_respects_backoff_then_retries_database(self):
        FakeCloudStackDB.outcomes = [False, True]
        engine = SyncEngine(self.settings())
        self.assertIsNone(engine.cs_db)

        engine.sync_proxmox = Mock(return_value={
            "clusters": 0, "vms_found": 0, "vms_updated": 0, "vms_new": 0, "errors": []
        })
        engine.sync_cloudstack = Mock(return_value={
            "vms_found": 0, "vms_updated": 0, "vms_new": 0, "errors": []
        })
        engine.match_vms = Mock(return_value={
            "matched": 0, "unmatched_proxmox": 0, "unmatched_cloudstack": 0
        })
        engine.sync_nics = Mock(return_value={"px_vms": 0, "cs_vms": 0, "errors": []})
        session = Mock()

        with patch("sync_engine.get_session", return_value=session):
            engine.full_sync()

        self.assertIsNone(engine.cs_db)
        self.assertEqual(1, len(FakeCloudStackDB.instances))

        engine._cs_db_next_retry_at = 0.0
        with patch("sync_engine.get_session", return_value=session):
            engine.full_sync()

        self.assertIsNotNone(engine.cs_db)
        self.assertIsNone(engine.cs_db_last_error)
        self.assertEqual(2, len(FakeCloudStackDB.instances))


if __name__ == "__main__":
    unittest.main()
