import unittest
from pathlib import Path


class FrontendSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            Path(__file__).resolve().parents[2] / "frontend" / "index.html"
        ).read_text(encoding="utf-8")

    def test_legacy_import_is_removed_from_ui(self):
        self.assertIn("Adoption blocked", self.source)
        self.assertIn("Direct-DB registration has been removed", self.source)
        self.assertNotIn("/api/register", self.source)
        self.assertNotIn("legacyRegistrationEnabled", self.source)
        self.assertNotIn('id="importModal"', self.source)

    def test_match_picker_only_requests_external_cloudstack_rows(self):
        self.assertGreaterEqual(
            self.source.count(
                "/api/cloudstack/vms?matched=false&hypervisor=External"
            ),
            2,
        )

    def test_lxc_and_templates_have_honest_disabled_actions(self):
        self.assertIn("Template excluded", self.source)
        self.assertIn("LXC inventory only", self.source)
        self.assertIn("stock CloudStack Proxmox extension supports QEMU only", self.source)


if __name__ == "__main__":
    unittest.main()
