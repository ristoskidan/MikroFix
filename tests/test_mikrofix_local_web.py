from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import mikrofix_local_web as web


class LocalWebPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "jobs").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_jobs_payload_contains_owner_facing_state_and_events(self):
        directory = self.workspace / "jobs" / "MF-LOCAL-1"
        directory.mkdir()
        supervisor = directory / "supervisor.json"
        supervisor.write_text(json.dumps({
            "stage": "AWAIT_CODEX_REVIEW",
            "checkpoint": "/candidate/manifest.json",
            "events": [{"at": "now", "stage": "CHECKPOINT", "message": "saved"}],
        }), encoding="utf-8")
        (directory / "request.json").write_text(json.dumps({
            "run_id": "MF-LOCAL-1", "task": "Build safely",
            "parent_run_id": None, "created_at": "2026-09-20T10:00:00Z",
        }), encoding="utf-8")
        (directory / "manager.json").write_text(json.dumps({
            "run_id": "MF-LOCAL-1", "state": "RUNNING_LOCAL",
            "supervisor_state": str(supervisor),
        }), encoding="utf-8")
        payload = web.jobs_payload(self.workspace)["jobs"][0]
        self.assertEqual("READY_FOR_CODEX_REVIEW", payload["state"])
        self.assertEqual("/candidate/manifest.json", payload["checkpoint"])
        self.assertEqual("saved", payload["events"][0]["message"])

    def test_html_is_local_and_exposes_required_owner_actions(self):
        html = web.HTML_FILE.read_text(encoding="utf-8")
        for label in ("Nova naloga", "Pošlji lokalnemu vodji", "Dovoli in izvedi", "Kopiraj kandidata za Codex", "Vprašaj Codex za mnenje", "Živi potek", "Qwen Thinking vodja"):
            self.assertIn(label, html)
        self.assertIn("/api/start", html)
        self.assertIn("/api/talk", html)
        self.assertIn("grid-template-rows:auto minmax(0,1fr) auto", html)
        self.assertEqual("127.0.0.1", web.HOST)

    def test_routed_candidate_is_grouped_under_lead_conversation(self):
        root = self.workspace / "jobs" / "MF-ROOT"
        child = self.workspace / "jobs" / "MF-CHILD"
        root.mkdir()
        child.mkdir()
        (root / "request.json").write_text(json.dumps({
            "run_id": "MF-ROOT", "task": "Discuss first", "created_at": "1",
            "lead_profile": "qwen_thinking", "kind": "lead_conversation",
        }), encoding="utf-8")
        (root / "manager.json").write_text(json.dumps({
            "run_id": "MF-ROOT", "state": "CONVERSATION",
            "lead_profile": "qwen_thinking",
        }), encoding="utf-8")
        (root / "conversation.json").write_text(json.dumps({
            "lead_profile": "qwen_thinking", "turns": [],
            "active_run_id": "MF-CHILD",
        }), encoding="utf-8")
        (child / "request.json").write_text(json.dumps({
            "run_id": "MF-CHILD", "task": "Execute", "created_at": "2",
            "parent_run_id": "MF-ROOT",
        }), encoding="utf-8")
        (child / "manager.json").write_text(json.dumps({
            "run_id": "MF-CHILD", "state": "READY_FOR_OWNER_LEASE",
        }), encoding="utf-8")
        jobs = web.jobs_payload(self.workspace)["jobs"]
        self.assertEqual(1, len(jobs))
        self.assertEqual("MF-ROOT", jobs[0]["run_id"])
        self.assertEqual("MF-CHILD", jobs[0]["action_run_id"])
        self.assertEqual("READY_FOR_OWNER_LEASE", jobs[0]["state"])
        self.assertEqual("qwen_thinking", jobs[0]["lead_profile"])

    def test_existing_browser_window_is_raised_instead_of_duplicated(self):
        search = mock.Mock(returncode=0, stdout="101\n202\n")
        raised = mock.Mock(returncode=0, stdout="")
        focused = mock.Mock(returncode=0, stdout="")
        with (
            mock.patch.object(web.shutil, "which", return_value="/usr/bin/xdotool"),
            mock.patch.object(
                web.subprocess, "run", side_effect=[search, raised, focused]
            ) as run,
        ):
            self.assertTrue(web.activate_open_window())
        self.assertEqual(
            ["/usr/bin/xdotool", "windowfocus", "--sync", "202"],
            run.call_args_list[-1].args[0],
        )

    def test_trace_combines_supervisor_qwen_review_and_redacts_secrets(self):
        directory = self.workspace / "jobs" / "MF-TRACE"
        directory.mkdir()
        run_dir = self.workspace / "runs" / "MF-TRACE"
        qwen = run_dir / "bridge" / "jobs" / "MF-2026-0001"
        qwen.mkdir(parents=True)
        (qwen / "log.txt").write_text("Qwen korak 1\nBearer topsecret\n", encoding="utf-8")
        (qwen / "result.txt").write_text("READY\n", encoding="utf-8")
        supervisor = directory / "supervisor.json"
        supervisor.write_text(json.dumps({
            "stage": "GPT_OSS_REVIEW", "attempt": 1,
            "feedback": "missing valid verdict",
            "test_evidence": "1 test PASS",
            "review_evidence": "Verdict: APPROVE",
            "events": [{"at": "now", "stage": "LOCAL_TESTS", "message": "passed"}],
            "environment": {"run_dir": str(run_dir)},
        }), encoding="utf-8")
        (directory / "request.json").write_text(json.dumps({
            "run_id": "MF-TRACE", "task": "Trace", "created_at": "now",
        }), encoding="utf-8")
        (directory / "manager.json").write_text(json.dumps({
            "run_id": "MF-TRACE", "state": "RUNNING_LOCAL",
            "supervisor_state": str(supervisor),
        }), encoding="utf-8")
        trace = web.build_trace(self.workspace, "MF-TRACE")
        for expected in ("GPT-OSS · IZVEDBA", "Qwen korak 1", "1 test PASS", "Verdict: APPROVE", "missing valid verdict"):
            self.assertIn(expected, trace)
        self.assertNotIn("topsecret", trace)
        self.assertIn("Bearer <skrito>", trace)

    def test_trace_shows_each_model_tool_call_and_actual_tool_result(self):
        directory = self.workspace / "jobs" / "MF-STEPS"
        planner = directory / "planner-runtime" / "qwen-bridge" / "jobs" / "MF-1"
        planner.mkdir(parents=True)
        events = [
            {"type": "model", "role": "Qwen planner", "step": 1,
             "content": "Preverjam pravo pot.", "tool_call_count": 1},
            {"type": "tool_call", "role": "Qwen planner", "step": 1,
             "tool": "project_search", "arguments": {"query": "invoice"}},
            {"type": "tool_result", "role": "Qwen planner", "step": 1,
             "tool": "project_search", "ok": True,
             "result": {"matches": ["app.py:12"], "count": 1}},
        ]
        (planner / "trace.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        (directory / "request.json").write_text(json.dumps({
            "run_id": "MF-STEPS", "task": "Trace", "created_at": "now",
        }), encoding="utf-8")
        (directory / "manager.json").write_text(json.dumps({
            "run_id": "MF-STEPS", "state": "PLANNING",
        }), encoding="utf-8")
        trace = web.build_trace(self.workspace, "MF-STEPS")
        for expected in (
            "QWEN · PREDLOG NAČRTA", "KORAK 1", "Preverjam pravo pot.",
            "project_search", '"query": "invoice"', '"app.py:12"',
        ):
            self.assertIn(expected, trace)

    def test_plan_only_trace_accepts_null_supervisor_path(self):
        directory = self.workspace / "jobs" / "MF-PLAN"
        directory.mkdir()
        (directory / "request.json").write_text(json.dumps({
            "run_id": "MF-PLAN", "task": "Plan", "created_at": "now",
        }), encoding="utf-8")
        (directory / "manager.json").write_text(json.dumps({
            "run_id": "MF-PLAN", "state": "PLAN_READY",
            "supervisor_state": None,
        }), encoding="utf-8")
        trace = web.build_trace(self.workspace, "MF-PLAN")
        self.assertIn("TRENUTNO STANJE", trace)
        self.assertIn("PLAN_READY", trace)

    def test_trace_includes_direct_lead_conversation_steps(self):
        directory = self.workspace / "jobs" / "MF-TALK"
        trace_dir = (
            directory / "conversation-runtime" / "turns" / "turn-1"
            / "bridge" / "jobs" / "MF-1"
        )
        trace_dir.mkdir(parents=True)
        (trace_dir / "trace.jsonl").write_text(json.dumps({
            "type": "model", "role": "Qwen Thinking vodja", "step": 1,
            "content": "Najprej preverim obstoječi tok.", "tool_call_count": 1,
        }) + "\n", encoding="utf-8")
        (directory / "request.json").write_text(json.dumps({
            "run_id": "MF-TALK", "task": "Talk", "created_at": "now",
        }), encoding="utf-8")
        (directory / "manager.json").write_text(json.dumps({
            "run_id": "MF-TALK", "state": "CONVERSATION",
        }), encoding="utf-8")
        trace = web.build_trace(self.workspace, "MF-TALK")
        self.assertIn("LOKALNI VODJA · POGOVOR IN PREMISLEK", trace)
        self.assertIn("Najprej preverim obstoječi tok.", trace)


if __name__ == "__main__":
    unittest.main()
