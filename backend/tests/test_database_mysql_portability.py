import sys
import unittest
from pathlib import Path

from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.schema import CreateTable

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from database import Base  # noqa: E402


class MySQLSchemaPortabilityTests(unittest.TestCase):
    def test_every_sidecar_table_compiles_for_mysql(self):
        dialect = mysql_dialect()
        statements = {
            table.name: str(CreateTable(table).compile(dialect=dialect))
            for table in Base.metadata.sorted_tables
        }
        self.assertIn("adoption_claims", statements)
        self.assertIn("UNIQUE", statements["adoption_claims"])
        self.assertIn("proxmox_cluster", statements["adoption_claims"])
        self.assertIn("proxmox_vmid", statements["adoption_claims"])


if __name__ == "__main__":
    unittest.main()
