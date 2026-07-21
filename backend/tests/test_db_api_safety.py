import unittest
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
MAIN_SOURCE = BACKEND / "main.py"


class DatabaseApiSafetyTests(unittest.TestCase):
    def test_main_has_no_direct_pymysql_connections(self):
        source = MAIN_SOURCE.read_text()
        self.assertNotIn("pymysql.connect", source)

    def test_no_request_driven_reconnect_endpoint(self):
        source = MAIN_SOURCE.read_text()
        self.assertNotIn("/api/reconcile/reconnect", source)

    def test_debug_vm_query_does_not_select_all_encrypted_fields(self):
        source = MAIN_SOURCE.read_text()
        self.assertNotIn("SELECT * FROM vm_instance WHERE uuid", source)


if __name__ == "__main__":
    unittest.main()
