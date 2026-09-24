from __future__ import annotations

import unittest
from unittest import mock

from tools import mikrofix_openwebui_gateway as gateway


class OpenWebUIGatewayTests(unittest.TestCase):
    def test_extracts_latest_user_message_and_visible_run_marker(self):
        message, run_id = gateway.extract_request([
            {"role": "user", "content": "Prva zahteva"},
            {"role": "assistant", "content": "Odgovor\n\nMikroFIX pogovor: MF-LOCAL-1"},
            {"role": "user", "content": "Nadaljuj"},
        ])
        self.assertEqual("Nadaljuj", message)
        self.assertEqual("MF-LOCAL-1", run_id)

    def test_new_conversation_is_locked_to_gpt_oss_and_returns_marker(self):
        conversation = {
            "parent_run_id": "MF-LOCAL-2",
            "turns": [{"role": "lead", "content": "Ugotovitev"}],
        }
        with mock.patch.object(
            gateway, "start_conversation", return_value=conversation,
        ) as start:
            result = gateway.route_message("Preveri napako", "")
        self.assertIn("Ugotovitev", result)
        self.assertIn("MF-LOCAL-2", result)
        self.assertEqual("gpt_oss", start.call_args.kwargs["lead_profile"])

    def test_prepare_plan_is_dispatched_without_reaching_a_model(self):
        with (
            mock.patch.object(
                gateway, "dispatch_owner_command",
                return_value={"message": "Načrt se pripravlja."},
            ) as dispatch,
            mock.patch.object(gateway, "talk") as talk,
        ):
            result = gateway.route_message("/prepare-plan", "MF-LOCAL-3")
        dispatch.assert_called_once_with(
            gateway.WORKSPACE, "MF-LOCAL-3", "/prepare-plan",
        )
        talk.assert_not_called()
        self.assertIn("Načrt se pripravlja.", result)
        self.assertIn("MF-LOCAL-3", result)

    def test_command_without_conversation_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "obstoječi MikroFIX pogovor"):
            gateway.route_message("/prepare-plan", "")

    def test_unknown_command_fails_before_routing(self):
        with mock.patch.object(gateway, "talk") as talk:
            with self.assertRaisesRegex(Exception, "Neznan ukaz"):
                gateway.route_message("/do-anything", "MF-LOCAL-4")
        talk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
