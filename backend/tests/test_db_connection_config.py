import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pymysql
from pydantic import ValidationError

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cloudstack_db import CloudStackDB
from config import CloudStackDBConfig


class DatabaseConnectionConfigTests(unittest.TestCase):
    def test_timeouts_are_rejected_outside_operational_bounds(self):
        for field in (
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "write_timeout_seconds",
        ):
            for value in (0, -1, 121, 999999999):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        CloudStackDBConfig(**{field: value})

    def test_reconnect_backoff_is_bounded(self):
        for value in (0, 4, 301, 999999999):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    CloudStackDBConfig(reconnect_backoff_seconds=value)

    @patch("cloudstack_db.pymysql.connect")
    def test_explicit_bounded_timeouts_are_passed_to_pymysql(self, connect):
        config = CloudStackDBConfig(
            host="db.example.invalid",
            user="cloud",
            password="test-only",
            connect_timeout_seconds=31,
            read_timeout_seconds=32,
            write_timeout_seconds=33,
        )

        CloudStackDB(config)._connect()

        kwargs = connect.call_args.kwargs
        self.assertEqual(31, kwargs["connect_timeout"])
        self.assertEqual(32, kwargs["read_timeout"])
        self.assertEqual(33, kwargs["write_timeout"])
        self.assertNotIn("test-only", repr({k: v for k, v in kwargs.items() if k != "password"}))

    @patch("cloudstack_db.pymysql.connect")
    def test_connection_status_does_not_retain_driver_message(self, connect):
        connect.side_effect = pymysql.err.OperationalError(
            1045, "Access denied for user 'cloud'@'db.internal'"
        )
        db = CloudStackDB(CloudStackDBConfig(password="test-only"))

        self.assertFalse(db.test_connection())
        self.assertEqual({"type": "OperationalError", "code": 1045}, db.last_connection_error)

    @patch("cloudstack_db.pymysql.connect")
    def test_non_numeric_exception_argument_is_not_exposed_as_code(self, connect):
        connect.side_effect = RuntimeError("sensitive-marker")
        db = CloudStackDB(CloudStackDBConfig(password="test-only"))

        self.assertFalse(db.test_connection())
        self.assertEqual({"type": "RuntimeError", "code": None}, db.last_connection_error)


if __name__ == "__main__":
    unittest.main()
