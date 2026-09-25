from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from tools import mikrofix_openwebui_gateway as gateway


class OpenWebUIGatewayTests(unittest.TestCase):
    def test_extracts_latest_user_message_and_visible_run_marker(self):
        message, run_id, model_tag, runtime = gateway.extract_request([
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
        self.assertEqual({}, runtime)

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

    def test_new_conversation_passes_selected_runtime_to_manager(self):
        conversation = {
            "parent_run_id": "MF-LOCAL-2A",
            "turns": [{"role": "lead", "content": "Ugotovitev"}],
        }
        with mock.patch.object(
            gateway, "start_conversation", return_value=conversation,
        ) as start:
            gateway.route_message(
                "Preveri napako", "", "conversation_lead",
                "qwen3:4b-thinking-2507-q8_0",
                selected_runtime={"num_ctx": 49152, "num_predict": 12288},
            )
        self.assertEqual(
            {"num_ctx": 49152, "num_predict": 12288},
            start.call_args.kwargs["runtime_overrides"],
        )

    def test_prepare_plan_returns_reviewable_revision_in_same_conversation(self):
        conversation = {
            "parent_run_id": "MF-LOCAL-3",
            "turns": [{"role": "lead", "content": "IZVEDBENI NAČRT · revizija `abc`"}],
        }
        with (
            mock.patch.object(
                gateway, "prepare_plan", return_value=conversation,
            ) as prepare,
            mock.patch.object(gateway, "dispatch_owner_command") as dispatch,
            mock.patch.object(gateway, "talk") as talk,
        ):
            with mock.patch.object(
                gateway, "_locked_model", return_value="qwen3:14b-q4_K_M",
            ) as locked:
                result = gateway.route_message(
                    "/prepare-plan", "MF-LOCAL-3", "conversation_lead",
                    "qwen3:14b-q4_K_M",
                )
        prepare.assert_called_once_with(
            "MF-LOCAL-3", workspace=gateway.WORKSPACE,
            progress_callback=None,
        )
        dispatch.assert_not_called()
        talk.assert_not_called()
        locked.assert_called_once_with("MF-LOCAL-3", "conversation_lead")
        self.assertIn("IZVEDBENI NAČRT", result)
        self.assertIn("MF-LOCAL-3", result)

    def test_plan_dispatch_uses_trusted_command_path_without_model(self):
        with (
            mock.patch.object(
                gateway, "dispatch_owner_command",
                return_value={"message": "Revizija se preverja."},
            ) as dispatch,
            mock.patch.object(gateway, "prepare_plan") as prepare,
            mock.patch.object(
                gateway, "_locked_model", return_value="qwen3:14b-q4_K_M",
            ),
        ):
            result = gateway.route_message(
                "/plan-dispatch", "MF-LOCAL-3", "conversation_lead",
                "qwen3:14b-q4_K_M",
            )
        dispatch.assert_called_once_with(
            gateway.WORKSPACE, "MF-LOCAL-3", "/plan-dispatch", wait=True,
        )
        prepare.assert_not_called()
        self.assertIn("Revizija se preverja.", result)

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
        with (
            mock.patch.object(
                gateway, "installed_ollama_models", return_value=["qwen3:14b"],
            ),
            mock.patch.object(gateway, "_read_token", return_value="test-secret"),
            mock.patch.object(gateway, "model_runtime_presets", return_value={}),
            mock.patch.object(gateway, "resolve_runtime_selection", return_value={}),
            mock.patch.object(gateway, "model_runtime_settings", return_value={
                "conversation_num_ctx": 16384, "think": True,
            }),
        ):
            result = gateway.route_message(
                "/llm qwen3:14b", "", "conversation_lead", "",
            )
        self.assertIn("MikroFIX izbrani model: qwen3:14b", result)
        self.assertIn("MikroFIX izbor:", result)

    def test_model_command_round_trips_signed_preset_and_overrides(self):
        with (
            mock.patch.object(
                gateway, "installed_ollama_models",
                return_value=["qwen3:4b-thinking-2507-q8_0"],
            ),
            mock.patch.object(gateway, "_read_token", return_value="test-secret"),
        ):
            response = gateway.route_message(
                "/llm qwen3:4b-thinking-2507-q8_0 preset=deep temperature=0.5",
                "", "conversation_lead", "",
            )
            message, run_id, model, runtime = gateway.extract_request([
                {"role": "assistant", "content": response},
                {"role": "user", "content": "Preveri napako"},
            ])
        self.assertEqual("Preveri napako", message)
        self.assertEqual("", run_id)
        self.assertEqual("qwen3:4b-thinking-2507-q8_0", model)
        self.assertEqual(32768, runtime["num_ctx"])
        self.assertEqual(12288, runtime["num_predict"])
        self.assertEqual(0.5, runtime["temperature"])

    def test_tampered_model_selection_fails_closed(self):
        with mock.patch.object(gateway, "_read_token", return_value="test-secret"):
            token = gateway._encode_selection("qwen3:14b", {"num_ctx": 4096})
            tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
            with self.assertRaisesRegex(ValueError, "Podpis"):
                gateway.extract_request([
                    {"role": "assistant", "content": f"MikroFIX izbor: {tampered}"},
                    {"role": "user", "content": "Test"},
                ])

    def test_reset_marker_clears_previous_selection(self):
        with mock.patch.object(gateway, "_read_token", return_value="test-secret"):
            token = gateway._encode_selection("qwen3:14b", {})
            _message, _run_id, model, runtime = gateway.extract_request([
                {"role": "assistant", "content": f"MikroFIX izbor: {token}"},
                {"role": "user", "content": "/llm-reset"},
                {"role": "assistant", "content": gateway.SELECTION_RESET_MARKER},
                {"role": "user", "content": "Nova naloga"},
            ])
        self.assertEqual("", model)
        self.assertEqual({}, runtime)

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

    def test_progress_reasoning_exposes_thinking_without_request_content(self):
        self.assertEqual(
            "Preverjam dejansko implementacijo.\n",
            gateway.progress_reasoning({
                "type": "model",
                "thinking": "Preverjam dejansko implementacijo.",
            }),
        )
        self.assertIn(
            "project_search",
            gateway.progress_reasoning({
                "type": "tool_call", "tool": "project_search",
                "arguments": {"query": "decimal_value"},
            }),
        )
        self.assertEqual(
            "", gateway.progress_reasoning({
                "type": "request", "content": "zasebna zahteva",
            }),
        )

    def test_stream_emits_reasoning_content_and_final_answer(self):
        async def collect() -> list[str]:
            progress: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            progress.put_nowait({
                "type": "model", "thinking": "Iščem simbol.",
            })
            progress.put_nowait({
                "type": "tool_result", "tool": "project_search", "ok": True,
            })
            return [
                chunk async for chunk in gateway._stream_response(
                    "chatcmpl-test", "mikrofix::conversation_lead",
                    lambda: "Končni odgovor", progress,
                )
            ]

        chunks = asyncio.run(collect())
        rendered = "".join(chunks)
        self.assertIn('"reasoning_content": "Iščem simbol.\\n"', rendered)
        self.assertIn("Rezultat `project_search`: uspešno.", rendered)
        self.assertIn('"content": "Končni odgovor"', rendered)
        self.assertEqual("data: [DONE]\n\n", chunks[-1])


if __name__ == "__main__":
    unittest.main()
