import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cloudstack_db import CloudStackDB
from config import CloudStackDBConfig


class DatabaseConnectionConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
