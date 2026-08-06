import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main
from database import init_db


class AdoptionRuntimeSafetyPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.temp.name) / 'sync.db'}")
        self.saved = (
            app_main.settings.adoption_executor_enabled,
            app_main.settings.auto_reconcile,
            app_main.settings.auto_reconcile_nics,
        )

    def tearDown(self):
        (
            app_main.settings.adoption_executor_enabled,
            app_main.settings.auto_reconcile,
            app_main.settings.auto_reconcile_nics,
        ) = self.saved
        self.temp.cleanup()

    def test_candidate_payload_exposes_only_exact_live_runtime_booleans(self):
        app_main.settings.adoption_executor_enabled = False
        app_main.settings.auto_reconcile = False
        app_main.settings.auto_reconcile_nics = False
        with patch.object(app_main, "engine", None):
            result = app_main.list_adoption_candidates()
        self.assertEqual(
            {
                "adoption_executor_enabled": False,
                "auto_reconcile": False,
                "auto_reconcile_nics": False,
            },
            result["runtime_safety"],
        )

        app_main.settings.auto_reconcile = True
        app_main.settings.auto_reconcile_nics = True
        with patch.object(app_main, "engine", None):
            result = app_main.list_adoption_candidates()
        self.assertEqual(
            {
                "adoption_executor_enabled": False,
                "auto_reconcile": True,
                "auto_reconcile_nics": True,
            },
            result["runtime_safety"],
        )


if __name__ == "__main__":
    unittest.main()
