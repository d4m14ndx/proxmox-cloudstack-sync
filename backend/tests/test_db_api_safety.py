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

    def test_raw_db_diagnostic_endpoint_is_not_exposed(self):
        source = MAIN_SOURCE.read_text()
        self.assertNotIn('/api/cloudstack/db-vm/', source)
        self.assertNotIn("SELECT * FROM user_vm", source)

    def test_client_errors_do_not_embed_caught_exception_text(self):
        source = MAIN_SOURCE.read_text()
        self.assertNotIn('"error": str(e)', source)
        self.assertNotIn('Registration failed: {e}', source)
        self.assertNotIn('Repair failed: {e}', source)


if __name__ == "__main__":
    unittest.main()
