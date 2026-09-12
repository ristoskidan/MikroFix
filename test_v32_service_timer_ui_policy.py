import ast
import unittest
from pathlib import Path


SOURCE_PATH = Path(__file__).with_name("mikrofix.py")


class ServiceTimerUiPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source, filename=str(SOURCE_PATH))
        cls.functions = {
            node.name: node
            for node in cls.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def calls_in(self, function_name):
        function = self.functions[function_name]
        calls = []
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)
        return calls

    def test_navigation_never_pauses_service_time(self):
        for function_name in (
            "show_service_history",
            "show_start_menu",
            "on_top_tab_changed",
        ):
            with self.subTest(function=function_name):
                self.assertNotIn(
                    "pause_active_service_timer",
                    self.calls_in(function_name),
                )

    def test_manual_logout_is_blocked_by_running_service_time(self):
        calls = self.calls_in("logout_and_lock")
        self.assertIn("service_time_blocks_logout", calls)
        self.assertIn("persist_active_service_draft", calls)
        self.assertIn("run_login_or_close_safely", calls)
        self.assertNotIn("destroy", calls)

    def test_auto_lock_keeps_same_user_and_does_not_pause_time(self):
        calls = self.calls_in("lock_for_inactivity")
        self.assertIn("run_login_or_close_safely", calls)
        self.assertNotIn("pause_active_service_timer", calls)
        self.assertNotIn("destroy", calls)

    def test_main_close_uses_service_time_gate(self):
        calls = self.calls_in("on_close")
        self.assertIn("service_time_blocks_application_close", calls)

    def test_only_manual_pause_button_and_completion_end_normal_work_time(self):
        forbidden_reasons = {"MODULE_SWITCH", "LEAVE_WORKSPACE", "MAIN_MENU"}
        used_reasons = {
            node.value
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertTrue(forbidden_reasons.isdisjoint(used_reasons))

    def test_global_timer_banner_refreshes_and_can_open_running_service(self):
        refresh_calls = self.calls_in("refresh_running_service_banner")
        self.assertIn("list_running_service_work_sessions", self.source)
        self.assertIn("after", refresh_calls)
        self.assertIn(
            "open_service_order_for_work",
            self.calls_in("open_running_service_from_banner"),
        )

    def test_view_switches_persist_complete_service_draft(self):
        self.assertIn(
            "persist_active_service_draft", self.calls_in("show_service_history")
        )
        self.assertIn(
            "persist_active_service_draft", self.calls_in("show_start_menu")
        )
        self.assertIn(
            "restore_active_service_draft",
            self.calls_in("open_service_order_for_work"),
        )


if __name__ == "__main__":
    unittest.main()
