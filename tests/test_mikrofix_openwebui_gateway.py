from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from tools import mikrofix_openwebui_gateway as gateway


class OpenWebUIGatewayTests(unittest.TestCase):
    def test_extracts_latest_user_message_and_visible_run_marker(self):
        message, run_id, model_tag = gateway.extract_request([
            {"role": "user", "content": "Prva zahteva"},
            {"role": "assistant", "content": (
                "Odgovor\n\nMikroFIX pogovor: MF-LOCAL-1"
                "\nMikroFIX izbrani model: qwen3:14b"
            )},
            {"role": "user", "content": "Nadaljuj"},
        ])
        self.assertEqual("Nadaljuj", message)
        self.assertEqual("MF-LOCAL-1", run_id)
        self.assertEqual("qwen3:14b", model_tag)

    def test_new_conversation_keeps_role_and_model_independent(self):
        conversation = {
            "parent_run_id": "MF-LOCAL-2",
            "turns": [{"role": "lead", "content": "Ugotovitev"}],
        }
        with mock.patch.object(
            gateway, "start_conversation", return_value=conversation,
        ) as start:
            result = gateway.route_message(
                "Preveri napako", "", "conversation_lead", "deepseek-r1:14b",
            )
        self.assertIn("Ugotovitev", result)
        self.assertIn("MF-LOCAL-2", result)
        self.assertEqual("conversation_lead", start.call_args.kwargs["lead_profile"])
        self.assertEqual("deepseek-r1:14b", start.call_args.kwargs["model_tag"])

    def test_prepare_plan_is_dispatched_without_reaching_a_model(self):
        with (
            mock.patch.object(
                gateway, "dispatch_owner_command",
                return_value={"message": "Načrt se pripravlja."},
            ) as dispatch,
            mock.patch.object(gateway, "talk") as talk,
        ):
            with mock.patch.object(
                gateway, "_locked_model", return_value="qwen3:14b-q4_K_M",
            ) as locked:
                result = gateway.route_message(
                    "/prepare-plan", "MF-LOCAL-3", "conversation_lead",
                    "qwen3:14b-q4_K_M",
                )
        dispatch.assert_called_once_with(
            gateway.WORKSPACE, "MF-LOCAL-3", "/prepare-plan",
        )
        talk.assert_not_called()
        locked.assert_called_once_with("MF-LOCAL-3", "conversation_lead")
        self.assertIn("Načrt se pripravlja.", result)
        self.assertIn("MF-LOCAL-3", result)

    def test_command_without_conversation_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "obstoječi MikroFIX pogovor"):
            gateway.route_message(
                "/prepare-plan", "", "conversation_lead", "gpt-oss:20b",
            )

    def test_unknown_command_fails_before_routing(self):
        with mock.patch.object(gateway, "talk") as talk:
            with self.assertRaisesRegex(Exception, "Neznan ukaz"):
                gateway.route_message(
                    "/do-anything", "MF-LOCAL-4", "conversation_lead",
                    "gpt-oss:20b",
                )
        talk.assert_not_called()

    def test_virtual_models_contain_each_role_exactly_once(self):
        with mock.patch.object(gateway, "enabled_role_profiles", return_value={
            "conversation_lead": {"display_name": "Pogovorni vodja"},
        }):
            values = gateway.available_virtual_models()
        self.assertEqual(["mikrofix::conversation_lead"], [v["id"] for v in values])
        self.assertEqual(["Pogovorni vodja"], [v["name"] for v in values])

    def test_existing_conversation_uses_immutable_locked_model(self):
        with mock.patch.object(gateway, "read_profile_snapshot", return_value={
            "profile_id": "conversation_lead", "model_tag": "gpt-oss:20b",
        }):
            self.assertEqual(
                "gpt-oss:20b",
                gateway._locked_model("MF-LOCAL-5", "conversation_lead"),
            )

    def test_model_command_selects_installed_model_before_task(self):
        with mock.patch.object(
            gateway, "installed_ollama_models", return_value=["qwen3:14b"],
        ):
            result = gateway.route_message(
                "/llm qwen3:14b", "", "conversation_lead", "",
            )
        self.assertIn("MikroFIX izbrani model: qwen3:14b", result)

    def test_model_switch_after_task_start_fails_closed(self):
        with mock.patch.object(
            gateway, "_locked_model", return_value="gpt-oss:20b",
        ):
            with self.assertRaisesRegex(ValueError, "zaklenjena"):
                gateway.route_message(
                    "/llm qwen3:14b", "MF-LOCAL-5",
                    "conversation_lead", "gpt-oss:20b",
                )

    def test_new_task_requires_explicit_model_choice(self):
        with self.assertRaisesRegex(ValueError, "Najprej izberi"):
            gateway.route_message(
                "Preveri napako", "", "conversation_lead", "",
            )

    def test_stream_finishes_cleanly_when_manager_operation_fails(self):
        def fail() -> str:
            raise RuntimeError("Dosežena omejitev korakov")

        async def collect() -> list[str]:
            return [
                chunk async for chunk in gateway._stream_response(
                    "chatcmpl-test", "mikrofix::conversation_lead", fail,
                )
            ]

        chunks = asyncio.run(collect())
        self.assertIn("MikroFIX je zahtevo varno zavrnil", "".join(chunks))
        self.assertEqual("data: [DONE]\n\n", chunks[-1])


if __name__ == "__main__":
    unittest.main()
