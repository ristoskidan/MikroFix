import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT / "tools" / "mikrofix_bridge.py"
THREAD_ID = "12345678-1234-5678-9234-567812345678"


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")


class BridgeCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.import_temp = tempfile.TemporaryDirectory()
        root = Path(cls.import_temp.name)
        system_prompt = root / "system.md"
        system_prompt.write_text("base prompt", encoding="utf-8")
        config = root / "config.json"
        config.write_text(json.dumps({
            "ollama_url": "http://127.0.0.1:1",
            "model": "test-model",
            "executor_url": "http://127.0.0.1:2",
            "token_file": str(root / "token"),
            "project": str(PROJECT),
            "system_prompt": str(system_prompt),
            "max_steps": 3,
        }), encoding="utf-8")
        with mock.patch.dict(os.environ, {
            "MIKROFIX_BRIDGE_HOME": str(root / "bridge"),
            "MIKROFIX_BRIDGE_CONFIG": str(config),
        }):
            spec = importlib.util.spec_from_file_location("mikrofix_bridge_test", MODULE_PATH)
            cls.bridge = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            sys.modules[spec.name] = cls.bridge
            spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls):
        cls.import_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bridge.BASE = root
        self.bridge.JOBS = root / "jobs"
        self.bridge.LOGS = root / "logs"
        self.bridge.COMPLETION_OUTBOX = root / "completion-outbox"
        self.bridge.JOBS.mkdir()
        self.bridge.LOGS.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_dispatch_binds_origin_thread_and_human_name(self):
        job = self.bridge.create_job(
            "Detailed task body", display_name="Readable task",
            codex_thread_id=THREAD_ID,
        )
        persisted = self.bridge.load_job(job["id"])
        self.assertEqual(THREAD_ID, persisted["codex_thread_id"])
        self.assertEqual("Readable task", persisted["display_name"])

    def test_optional_response_schema_is_forwarded_to_ollama(self):
        schema = {"type": "object", "required": ["objective"]}
        calls = []

        def fake_request(url, **kwargs):
            calls.append((url, kwargs))
            return {"message": {"content": "{}"}}

        with (
            mock.patch.object(self.bridge, "RESPONSE_FORMAT", schema),
            mock.patch.object(self.bridge, "request_json", side_effect=fake_request),
        ):
            self.bridge.call_qwen(
                [{"role": "user", "content": "plan"}], structured=True,
            )
        self.assertEqual(schema, calls[0][1]["data"]["format"])

    def test_tool_mode_omits_competing_response_schema(self):
        schema = {"type": "object", "required": ["reply"]}
        calls = []

        def fake_request(_url, **kwargs):
            calls.append(kwargs["data"])
            return {"message": {"tool_calls": []}}

        with (
            mock.patch.object(self.bridge, "RESPONSE_FORMAT", schema),
            mock.patch.object(self.bridge, "request_json", side_effect=fake_request),
        ):
            self.bridge.call_qwen([{"role": "user", "content": "inspect"}])
        self.assertNotIn("format", calls[0])
        self.assertIn("tools", calls[0])

    def test_runtime_generation_options_are_forwarded_to_ollama(self):
        calls = []

        def fake_request(_url, **kwargs):
            calls.append(kwargs["data"])
            return {"message": {"content": "done"}}

        with (
            mock.patch.object(self.bridge, "request_json", side_effect=fake_request),
            mock.patch.object(self.bridge, "NUM_CTX", 49152),
            mock.patch.object(self.bridge, "RUNTIME_OPTIONS", {
                "num_predict": 12288, "temperature": 0.6,
                "top_k": 20, "top_p": 0.95,
            }),
        ):
            self.bridge.call_qwen([{"role": "user", "content": "inspect"}])
        self.assertEqual(49152, calls[0]["options"]["num_ctx"])
        self.assertEqual(12288, calls[0]["options"]["num_predict"])
        self.assertEqual(0.6, calls[0]["options"]["temperature"])

    def test_structured_mode_omits_tools(self):
        schema = {"type": "object", "required": ["reply"]}
        calls = []

        def fake_request(_url, **kwargs):
            calls.append(kwargs["data"])
            return {"message": {"content": '{"reply":"done"}'}}

        with (
            mock.patch.object(self.bridge, "RESPONSE_FORMAT", schema),
            mock.patch.object(self.bridge, "request_json", side_effect=fake_request),
        ):
            self.bridge.call_qwen(
                [{"role": "user", "content": "finalize"}], structured=True,
            )
        self.assertEqual(schema, calls[0]["format"])
        self.assertNotIn("tools", calls[0])

    def test_terminal_notification_is_at_most_once_across_replay(self):
        job = self.bridge.create_job(
            "Finish safely", display_name="Safe finish", codex_thread_id=THREAD_ID
        )
        result_path = self.bridge.job_dir(job["id"]) / "result.txt"
        self.bridge.atomic_text(result_path, "done\n")
        job.update(status="completed", result="done", finished_at=self.bridge.now())
        self.bridge.save_job(job)
        runner = FakeRunner()
        first = self.bridge.notify_job_completion(job, runner=runner)
        second = self.bridge.notify_job_completion(
            self.bridge.load_job(job["id"]), runner=runner
        )
        self.assertEqual("delivered", first["status"])
        self.assertEqual("delivered", second["status"])
        self.assertEqual(1, len(runner.calls))
        message = json.loads(runner.calls[0][0][-1])
        self.assertEqual("Safe finish", message["display_name"])
        event = json.loads(Path(message["event_path"]).read_text(encoding="utf-8"))
        self.assertEqual("COMPLETED", event["payload"]["state"])
        self.assertIn(str(result_path.resolve()), event["payload"]["evidence_paths"])

    def test_queued_and_running_jobs_do_not_notify(self):
        runner = FakeRunner()
        job = self.bridge.create_job("Wait", codex_thread_id=THREAD_ID)
        self.assertIsNone(self.bridge.notify_job_completion(job, runner=runner))
        job["status"] = "running"
        self.assertIsNone(self.bridge.notify_job_completion(job, runner=runner))
        self.assertEqual([], runner.calls)

    def test_supervisor_only_policy_suppresses_inner_job_event(self):
        job = self.bridge.create_job("Inner worker", codex_thread_id=THREAD_ID)
        job.update(status="completed", result="done", finished_at=self.bridge.now())
        self.bridge.save_job(job)
        runner = FakeRunner()
        with mock.patch.object(self.bridge, "COMPLETION_POLICY", "supervisor_only"):
            result = self.bridge.notify_job_completion(job, runner=runner)
        self.assertEqual("suppressed_supervisor_only", result["status"])
        self.assertEqual([], runner.calls)
        self.assertFalse(self.bridge.COMPLETION_OUTBOX.exists())

    def test_no_thread_replay_keeps_one_stable_event(self):
        job = self.bridge.create_job("No origin thread")
        job.update(status="failed", error="bounded failure", finished_at=self.bridge.now())
        self.bridge.save_job(job)
        first = self.bridge.notify_job_completion(job)
        second = self.bridge.notify_job_completion(self.bridge.load_job(job["id"]))
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(
            1, len(list((self.bridge.COMPLETION_OUTBOX / "events").iterdir()))
        )

    def test_process_persists_result_before_terminal_callback(self):
        job = self.bridge.create_job("Use one tool")
        responses = iter([
            {"message": {"tool_calls": [{
                "function": {"name": "project_status", "arguments": {}}
            }]}},
            {"message": {"content": "finished"}},
        ])
        observed = []

        def callback(terminal_job):
            persisted = self.bridge.load_job(terminal_job["id"])
            observed.append((
                persisted["status"],
                (self.bridge.job_dir(terminal_job["id"]) / "result.txt").read_text(
                    encoding="utf-8"
                ),
            ))

        with mock.patch.object(self.bridge, "call_qwen", side_effect=lambda _m, **_k: next(responses)), \
                mock.patch.object(self.bridge, "execute_tool", return_value={"ok": True}), \
                mock.patch.object(self.bridge, "notify_job_completion", side_effect=callback):
            self.bridge.process_job(job["id"])
        self.assertEqual([("completed", "finished\n")], observed)

    def test_process_persists_model_returned_thinking_in_trace(self):
        job = self.bridge.create_job("Inspect with visible reasoning")
        responses = iter([
            {"message": {
                "thinking": "Najprej moram preveriti stanje projekta.",
                "tool_calls": [{
                    "function": {"name": "project_status", "arguments": {}}
                }],
            }},
            {"message": {
                "thinking": "Dokaz je zbran, zato lahko odgovorim.",
                "content": "Preverjeno.",
            }},
        ])
        with (
            mock.patch.object(
                self.bridge, "call_qwen",
                side_effect=lambda _messages, **_kwargs: next(responses),
            ),
            mock.patch.object(
                self.bridge, "execute_tool", return_value={"ok": True},
            ),
            mock.patch.object(self.bridge, "MINIMUM_SUCCESSFUL_TOOL_CALLS", 1),
        ):
            self.bridge.process_job(job["id"])

        events = [
            json.loads(line)
            for line in (
                self.bridge.job_dir(job["id"]) / "trace.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        model_events = [event for event in events if event["type"] == "model"]
        self.assertEqual([
            "Najprej moram preveriti stanje projekta.",
            "Dokaz je zbran, zato lahko odgovorim.",
        ], [event["thinking"] for event in model_events])

    def test_tool_review_and_structured_finalization_are_separate_calls(self):
        job = self.bridge.create_job("Inspect then answer")
        responses = iter([
            {"message": {"tool_calls": [{
                "function": {"name": "project_status", "arguments": {}}
            }]}},
            {"message": {"content": "Preverjeno; pripravljam odgovor."}},
            {"message": {"content": '{"reply":"Končni odgovor"}'}},
        ])
        modes = []

        def answer(_messages, **kwargs):
            modes.append(kwargs.get("structured", False))
            return next(responses)

        with (
            mock.patch.object(self.bridge, "call_qwen", side_effect=answer),
            mock.patch.object(self.bridge, "execute_tool", return_value={"ok": True}),
            mock.patch.object(
                self.bridge, "RESPONSE_FORMAT",
                {"type": "object", "required": ["reply"]},
            ),
            mock.patch.object(self.bridge, "MINIMUM_SUCCESSFUL_TOOL_CALLS", 1),
        ):
            self.bridge.process_job(job["id"])
        persisted = self.bridge.load_job(job["id"])
        self.assertEqual([False, False, True], modes)
        self.assertEqual("completed", persisted["status"])
        self.assertEqual('{"reply":"Končni odgovor"}', persisted["result"])

    def test_plain_conversation_reply_is_wrapped_without_second_model_call(self):
        job = self.bridge.create_job("Inspect then answer once")
        responses = iter([
            {"message": {"tool_calls": [{
                "function": {"name": "project_status", "arguments": {}}
            }]}},
            {"message": {"content": "Dokazan naravni odgovor."}},
        ])
        calls = []

        def answer(_messages, **kwargs):
            calls.append(kwargs.get("structured", False))
            return next(responses)

        with (
            mock.patch.object(self.bridge, "call_qwen", side_effect=answer),
            mock.patch.object(self.bridge, "execute_tool", return_value={"ok": True}),
            mock.patch.object(
                self.bridge, "RESPONSE_FORMAT",
                {"type": "object", "required": ["reply"]},
            ),
            mock.patch.object(self.bridge, "MINIMUM_SUCCESSFUL_TOOL_CALLS", 1),
            mock.patch.object(self.bridge, "DETERMINISTIC_REPLY_WRAPPER", True),
        ):
            self.bridge.process_job(job["id"])
        persisted = self.bridge.load_job(job["id"])
        self.assertEqual([False, False], calls)
        self.assertEqual("completed", persisted["status"])
        self.assertEqual(
            {"reply": "Dokazan naravni odgovor."},
            json.loads(persisted["result"]),
        )

    def test_failed_tool_call_does_not_satisfy_required_project_inspection(self):
        job = self.bridge.create_job("Verify with a successful tool")
        responses = iter([
            {"message": {"tool_calls": [{
                "function": {"name": "project_read", "arguments": {"path": "missing"}}
            }]}},
            {"message": {"content": "premature"}},
            {"message": {"tool_calls": [{
                "function": {"name": "project_status", "arguments": {}}
            }]}},
            {"message": {"content": "verified"}},
        ])
        tool_results = iter([RuntimeError("not found"), {"ok": True}])

        def execute(_name, _arguments):
            result = next(tool_results)
            if isinstance(result, Exception):
                raise result
            return result

        old_steps = self.bridge.MAX_STEPS
        self.bridge.MAX_STEPS = 4
        try:
            with mock.patch.object(
                self.bridge, "call_qwen", side_effect=lambda _m, **_k: next(responses)
            ), mock.patch.object(self.bridge, "execute_tool", side_effect=execute):
                self.bridge.process_job(job["id"])
        finally:
            self.bridge.MAX_STEPS = old_steps
        persisted = self.bridge.load_job(job["id"])
        self.assertEqual("completed", persisted["status"])
        self.assertEqual("verified", persisted["result"])

    def test_invalid_tool_arguments_return_actionable_session_guidance(self):
        job = self.bridge.create_job("Correct a malformed call")
        responses = iter([
            {"message": {"tool_calls": [{
                "function": {"name": "project_search", "arguments": "{bad-json"}
            }]}},
            {"message": {"tool_calls": [{
                "function": {"name": "project_status", "arguments": {}}
            }]}},
            {"message": {"content": "corrected"}},
        ])
        received_messages = []

        def answer(messages, **_kwargs):
            received_messages.append(json.loads(json.dumps(messages)))
            return next(responses)

        with (
            mock.patch.object(self.bridge, "call_qwen", side_effect=answer),
            mock.patch.object(
                self.bridge, "execute_tool", return_value={"exit_code": 0},
            ) as execute,
        ):
            self.bridge.process_job(job["id"])
        feedback = json.loads(received_messages[1][-1]["content"])
        self.assertEqual("TOOL_CALL_INVALID", feedback["error_code"])
        self.assertIn("project_search", feedback["guidance"]["available_tools"])
        search_help = feedback["guidance"]["use_them_as_follows"][0]
        self.assertEqual("project_search", search_help["name"])
        self.assertEqual("*.py", search_help["example"]["file_glob"])
        execute.assert_called_once_with("project_status", {})
        self.assertEqual("completed", self.bridge.load_job(job["id"])["status"])

    def test_identical_search_warns_blocks_then_fails_as_loop(self):
        job = self.bridge.create_job("Do not repeat one search")
        call = {"message": {"tool_calls": [{
            "function": {
                "name": "project_search",
                "arguments": {"query": "invoice", "path": ".", "file_glob": "*.py"},
            }
        }]}}
        responses = iter([call, call, call, call])
        old_steps = self.bridge.MAX_STEPS
        self.bridge.MAX_STEPS = 4
        try:
            with (
                mock.patch.object(
                    self.bridge, "call_qwen",
                    side_effect=lambda _messages, **_kwargs: next(responses),
                ),
                mock.patch.object(
                    self.bridge, "execute_tool",
                    return_value={"matches": [], "count": 0, "exit_code": 1},
                ) as execute,
            ):
                self.bridge.process_job(job["id"])
        finally:
            self.bridge.MAX_STEPS = old_steps
        persisted = self.bridge.load_job(job["id"])
        self.assertEqual("failed", persisted["status"])
        self.assertIn("LOOP_DETECTED", persisted["error"])
        self.assertEqual(3, persisted["duplicate_tool_calls"])
        self.assertEqual(2, persisted["blocked_tool_calls"])
        self.assertEqual(1, execute.call_count)
        events = [
            json.loads(line) for line in (
                self.bridge.job_dir(job["id"]) / "trace.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        codes = [event.get("error_code") for event in events]
        self.assertIn("DUPLICATE_CALL_WARNING", codes)
        self.assertIn("DUPLICATE_CALL_BLOCKED", codes)

    def test_repeated_symbol_as_file_glob_is_guided_then_blocked(self):
        job = self.bridge.create_job("Correct semantic search misuse")
        responses = iter([
            {"message": {"tool_calls": [{"function": {
                "name": "project_search",
                "arguments": {"query": "alpha", "file_glob": "alpha"},
            }}]}},
            {"message": {"tool_calls": [{"function": {
                "name": "project_search",
                "arguments": {"query": "beta", "file_glob": "beta"},
            }}]}},
            {"message": {"tool_calls": [{"function": {
                "name": "project_search",
                "arguments": {"query": "gamma", "file_glob": "gamma"},
            }}]}},
            {"message": {"tool_calls": [{"function": {
                "name": "project_search",
                "arguments": {"query": "delta", "file_glob": "delta"},
            }}]}},
        ])
        old_steps = self.bridge.MAX_STEPS
        self.bridge.MAX_STEPS = 4
        try:
            with (
                mock.patch.object(
                    self.bridge, "call_qwen",
                    side_effect=lambda _messages, **_kwargs: next(responses),
                ),
                mock.patch.object(
                    self.bridge, "execute_tool",
                    return_value={"matches": [], "count": 0, "exit_code": 1},
                ) as execute,
            ):
                self.bridge.process_job(job["id"])
        finally:
            self.bridge.MAX_STEPS = old_steps
        persisted = self.bridge.load_job(job["id"])
        self.assertEqual("failed", persisted["status"])
        self.assertIn("LOOP_DETECTED", persisted["error"])
        self.assertEqual(2, execute.call_count)
        events = [
            json.loads(line) for line in (
                self.bridge.job_dir(job["id"]) / "trace.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        warnings = [
            event.get("warning", {}).get("code") for event in events
            if event.get("warning")
        ]
        codes = [event.get("error_code") for event in events]
        self.assertEqual(["SEARCH_ARGUMENT_HINT", "SEARCH_MISUSE_WARNING"], warnings)
        self.assertIn("SEARCH_MISUSE_BLOCKED", codes)

    def test_corrected_search_after_hint_can_complete(self):
        job = self.bridge.create_job("Correct the search and finish")
        responses = iter([
            {"message": {"tool_calls": [{"function": {
                "name": "project_search",
                "arguments": {"query": "invoice", "file_glob": "invoice"},
            }}]}},
            {"message": {"tool_calls": [{"function": {
                "name": "project_search",
                "arguments": {"query": "invoice", "file_glob": "*.py"},
            }}]}},
            {"message": {"content": "verified"}},
        ])
        results = iter([
            {"matches": [], "count": 0, "exit_code": 1},
            {"matches": ["app.py:1:invoice"], "count": 1, "exit_code": 0},
        ])
        with (
            mock.patch.object(
                self.bridge, "call_qwen",
                side_effect=lambda _messages, **_kwargs: next(responses),
            ),
            mock.patch.object(
                self.bridge, "execute_tool", side_effect=lambda *_args: next(results),
            ),
        ):
            self.bridge.process_job(job["id"])
        persisted = self.bridge.load_job(job["id"])
        self.assertEqual("completed", persisted["status"])
        self.assertEqual("verified", persisted["result"])
        self.assertEqual(1, persisted["successful_tool_calls"])
        self.assertEqual(1, persisted["tool_feedback_events"])

    def test_swapped_search_arguments_warn_and_do_not_count_as_evidence(self):
        job = self.bridge.create_job("Correct swapped search arguments")
        responses = iter([
            {"message": {"tool_calls": [{"function": {
                "name": "project_search",
                "arguments": {
                    "query": "*.py", "file_glob": "reversal_of_invoice_uuid",
                },
            }}]}},
            {"message": {"tool_calls": [{"function": {
                "name": "project_search",
                "arguments": {
                    "query": "reversal_of_invoice_uuid", "file_glob": "*.py",
                },
            }}]}},
            {"message": {"content": "verified"}},
        ])
        results = iter([
            {"matches": [], "count": 0, "exit_code": 1},
            {"matches": ["app.py:1:reversal_of_invoice_uuid"], "count": 1,
             "exit_code": 0},
        ])
        with (
            mock.patch.object(
                self.bridge, "call_qwen",
                side_effect=lambda _messages, **_kwargs: next(responses),
            ),
            mock.patch.object(
                self.bridge, "execute_tool", side_effect=lambda *_args: next(results),
            ),
        ):
            self.bridge.process_job(job["id"])
        persisted = self.bridge.load_job(job["id"])
        self.assertEqual("completed", persisted["status"])
        self.assertEqual(1, persisted["successful_tool_calls"])
        self.assertEqual(1, persisted["tool_feedback_events"])

    def test_repeated_textual_tool_names_fail_closed(self):
        job = self.bridge.create_job("Inspect through tools")
        responses = iter([
            {"message": {"content": "Uporabil bom project_search."}},
            {"message": {"content": "Orodni poziv: project_search."}},
        ])
        with (
            mock.patch.object(
                self.bridge, "call_qwen",
                side_effect=lambda _m, **_k: next(responses),
            ),
            mock.patch.object(self.bridge, "MAX_TOOLLESS_RESPONSES", 2),
        ):
            self.bridge.process_job(job["id"])
        persisted = self.bridge.load_job(job["id"])
        self.assertEqual("failed", persisted["status"])
        self.assertIn("strukturiranega klica", persisted["error"])

    def test_runtime_prompt_always_contains_persistent_lead_rules(self):
        prompt = self.bridge.runtime_system_prompt()
        for required in (
            "Meja avtoritete",
            "Vloga in model sta ločeni identiteti",
            "Besedilo z imenom",
            "Natančen vzrok:",
            "project_diff",
            "število testov",
        ):
            self.assertIn(required, prompt)

    def test_normalizes_empty_project_paths_and_bounded_limits(self):
        self.assertEqual(
            {"path": ".", "max_entries": 500},
            self.bridge.normalize_tool_arguments(
                "project_list", {"path": "", "max_entries": 1000}
            ),
        )
        self.assertEqual(
            {"path": ".", "query": "Office", "max_results": 200},
            self.bridge.normalize_tool_arguments(
                "project_search",
                {"path": "   ", "query": "Office", "max_results": 999},
            ),
        )
        self.assertEqual(
            {
                "query": "invoice", "path": ".", "file_glob": None,
                "max_results": 80,
            },
            self.bridge.canonical_tool_arguments(
                "project_search", {"query": "invoice"},
            ),
        )
        implicit = self.bridge.tool_fingerprint(
            "project_search",
            self.bridge.canonical_tool_arguments(
                "project_search", {"query": "invoice"},
            ),
        )
        explicit = self.bridge.tool_fingerprint(
            "project_search",
            self.bridge.canonical_tool_arguments(
                "project_search", {
                    "query": "invoice", "path": ".", "file_glob": None,
                    "max_results": 80,
                },
            ),
        )
        self.assertEqual(implicit, explicit)
        self.assertTrue(self.bridge.suspicious_search_arguments({
            "query": "reversal_of_invoice_uuid",
            "file_glob": "reversal_of_invoice_uuid",
        }))
        self.assertTrue(self.bridge.suspicious_search_arguments({
            "query": "*.py", "file_glob": "reversal_of_invoice_uuid",
        }))
        self.assertFalse(self.bridge.suspicious_search_arguments({
            "query": "invoice", "file_glob": "Makefile",
        }))

    def test_validate_tool_arguments_enforces_max_length(self):
        with self.assertRaisesRegex(
            self.bridge.ToolCallValidationError, "query.*predolg",
        ):
            self.bridge.validate_tool_arguments(
                "project_search", {"query": "x" * 501},
            )
        with self.assertRaisesRegex(
            self.bridge.ToolCallValidationError, "content.*predolg",
        ):
            self.bridge.validate_tool_arguments(
                "project_create",
                {
                    "path": "allowed.txt",
                    "content": "x" * 128001,
                    "summary": "valid summary",
                },
            )

    def test_execute_tool_sends_normalized_arguments(self):
        with mock.patch.object(
            self.bridge, "executor_request", return_value={"ok": True}
        ) as request:
            self.bridge.execute_tool(
                "project_list", {"path": "", "max_entries": 1000}
            )
        request.assert_called_once_with(
            "POST", "/v1/project/list", {"path": ".", "max_entries": 500}
        )

    def test_session_tool_allowlist_hides_and_rejects_mutating_functions(self):
        allowed = frozenset({"project_status", "project_read"})
        with (
            mock.patch.object(self.bridge, "ALLOWED_TOOL_NAMES", allowed),
            mock.patch.object(self.bridge, "executor_request") as request,
        ):
            with self.assertRaisesRegex(RuntimeError, "ni dovoljeno"):
                self.bridge.execute_tool(
                    "project_apply_patch",
                    {"patch": "x", "summary": "must not run"},
                )
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
