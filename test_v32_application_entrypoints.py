import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class ApplicationEntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service_source = (ROOT / "mikrofix.py").read_text(encoding="utf-8")
        cls.launcher_source = (ROOT / "zazeni_mikrofix.py").read_text(
            encoding="utf-8"
        )
        cls.service_tree = ast.parse(cls.service_source)

    def test_three_production_entrypoints_exist(self):
        expected = {
            "zazeni_mikrofix_service.py": 'launch("service")',
            "zazeni_mikrofix_office.py": 'launch("office")',
            "zazeni_mikrofix_blagajna.py": 'launch("cashier")',
        }
        for filename, call in expected.items():
            with self.subTest(filename=filename):
                source = (ROOT / filename).read_text(encoding="utf-8")
                ast.parse(source, filename=filename)
                self.assertIn(call, source)

    def test_each_entrypoint_executes_a_separate_process_target(self):
        self.assertIn('"service": ("mikrofix.py", "--service-only")', self.launcher_source)
        self.assertIn(
            '"office": ("mikrofix_central_desktop.py", "--application", "OFFICE")',
            self.launcher_source,
        )
        self.assertIn(
            '"cashier": ("mikrofix_central_desktop.py", "--application", "CASHIER")',
            self.launcher_source,
        )
        self.assertNotIn('"office": ("mikrofix_office.py",)', self.launcher_source)
        self.assertIn("os.execv", self.launcher_source)

    def test_standalone_service_enters_service_workspace_after_login(self):
        self.assertIn('SERVICE_ONLY_APP = "--service-only" in sys.argv', self.service_source)
        self.assertIn(
            "show_service_application if SERVICE_ONLY_APP else show_start_menu",
            self.service_source,
        )

    def test_standalone_service_does_not_offer_cashier_or_direct_claim_creation(self):
        for label in (
            "ODPRI BLAGAJNO ZA IZBRANI SERVIS",
            "ODPRI BLAGAJNO ZA ZADNJI SERVIS",
            "REKLAMACIJA ZA IZBRANI SERVIS",
        ):
            with self.subTest(label=label):
                location = self.service_source.index(label)
                guard = self.service_source.rfind("if not SERVICE_ONLY_APP:", 0, location)
                self.assertGreater(guard, 0)

    def test_service_only_close_does_not_return_to_shared_menu(self):
        function = next(
            node for node in self.service_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "on_close"
        )
        source = ast.get_source_segment(self.service_source, function)
        self.assertIn('not SERVICE_ONLY_APP and ACTIVE_SCREEN != "MENU"', source)


if __name__ == "__main__":
    unittest.main()
