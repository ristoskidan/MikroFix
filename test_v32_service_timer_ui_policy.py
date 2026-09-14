import ast
import unittest
from pathlib import Path
from unittest.mock import Mock

from mikrofix_user_ui import SessionAuthUI


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
        self.assertIn("end_application_user_session", calls)
        self.assertIn("stop_service_sync_worker", calls)
        self.assertIn("start_service_sync_worker", calls)
        self.assertNotIn("destroy", calls)

    def test_auto_lock_keeps_same_user_and_does_not_pause_time(self):
        calls = self.calls_in("lock_for_inactivity")
        self.assertIn("run_login_or_close_safely", calls)
        self.assertNotIn("pause_active_service_timer", calls)
        function_source = ast.get_source_segment(
            self.source, self.functions["lock_for_inactivity"]
        )
        self.assertIn("verify_central=False", function_source)
        self.assertNotIn("destroy", calls)

    def test_service_outbox_writes_receive_central_signing_identity(self):
        for function_name in (
            "_save_service", "save_intake_from_tab",
            "record_removed_part_for_selected_service",
        ):
            with self.subTest(function=function_name):
                self.assertIn(
                    "central_service_operation_identity",
                    self.calls_in(function_name),
                )

    def test_explicit_central_reauthentication_stops_old_worker_first(self):
        calls = self.calls_in("reauthenticate_and_sync_service")
        self.assertIn("stop_service_sync_worker", calls)
        self.assertIn("run_pin_login", calls)
        self.assertIn("start_service_sync_worker", calls)
        self.assertIn("request_service_sync", calls)

    def test_main_close_uses_service_time_gate(self):
        calls = self.calls_in("on_close")
        self.assertIn("service_time_blocks_application_close", calls)
        self.assertIn("end_application_user_session", calls)

    def test_cancelled_relogin_forgets_session_before_process_exit(self):
        calls = self.calls_in("run_login_or_close_safely")
        self.assertIn("end_application_user_session", calls)
        self.assertIn("destroy", calls)

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


class EphemeralSessionTests(unittest.TestCase):
    def test_end_session_forgets_identity_and_permissions(self):
        session = SessionAuthUI.__new__(SessionAuthUI)
        session.current = {
            "username": "serviser", "display_name": "Serviser",
            "role": "SERVICE", "is_active": 1,
            "permissions": {"MODULE_SERVICE"},
        }
        session._authenticated = True
        session._caption_vars = []
        session._admin_widgets = []

        session.end_session()

        self.assertFalse(session.is_authenticated)
        self.assertEqual(session.username, "")
        self.assertEqual(session.permissions, set())

    def test_end_session_forgets_local_login_when_central_logout_fails(self):
        session = SessionAuthUI.__new__(SessionAuthUI)
        session.current = {
            "username": "serviser", "display_name": "Serviser",
            "role": "SERVICE", "is_active": 1,
            "permissions": {"MODULE_SERVICE"},
        }
        session._authenticated = True
        session._caption_vars = []
        session._admin_widgets = []
        session.external_end_session = Mock(side_effect=RuntimeError("offline"))

        session.end_session()

        session.external_end_session.assert_called_once_with()
        self.assertFalse(session.is_authenticated)
        self.assertEqual(session.username, "")


if __name__ == "__main__":
    unittest.main()
