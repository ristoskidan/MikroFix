import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class LoginSessionPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_source = (ROOT / "mikrofix.py").read_text(encoding="utf-8")
        cls.office_source = (ROOT / "mikrofix_office.py").read_text(
            encoding="utf-8"
        )
        cls.main_tree = ast.parse(cls.main_source)
        cls.office_tree = ast.parse(cls.office_source)

    @staticmethod
    def function(tree, name):
        return next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )

    @staticmethod
    def calls(node):
        result = []
        for item in ast.walk(node):
            if not isinstance(item, ast.Call):
                continue
            if isinstance(item.func, ast.Name):
                result.append(item.func.id)
            elif isinstance(item.func, ast.Attribute):
                result.append(item.func.attr)
        return result

    def test_office_opening_never_consumes_main_login_handoff(self):
        self.assertNotIn("MIKROFIX_SESSION_HANDOFF", self.main_source)
        self.assertNotIn("MIKROFIX_SESSION_HANDOFF", self.office_source)
        self.assertNotIn("consume_main_session_handoff", self.office_source)
        self.assertIn("elif run_pin_login(root):", self.office_source)

    def test_main_exit_and_logout_forget_identity(self):
        for name in ("logout_and_lock", "on_close"):
            with self.subTest(name=name):
                self.assertIn(
                    "end_application_user_session",
                    self.calls(self.function(self.main_tree, name)),
                )

    def test_office_close_and_logout_forget_identity(self):
        for name in ("logout_and_lock", "close_office_application"):
            with self.subTest(name=name):
                self.assertIn(
                    "end_application_user_session",
                    self.calls(self.function(self.office_tree, name)),
                )
        self.assertIn(
            'root.protocol("WM_DELETE_WINDOW", close_office_application)',
            self.office_source,
        )


if __name__ == "__main__":
    unittest.main()
