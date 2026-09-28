from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools import mikrofix_local_manager as manager


class DelegatedResearchEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.workspace = self.root / "workspace"
        (self.repository / "docs/local_agents").mkdir(parents=True)
        (self.repository / "docs/kanon").mkdir(parents=True)
        (self.repository / "tests").mkdir(parents=True)
        files = {
            "AGENTS.md": "test rules\n",
            "app.py": "def value():\n    return 1\n",
            "tests/test_app.py": "# test\n",
            "docs/local_agents/MIKROFIX_PROJECT_MAP.md": "app.py: application\n",
            "docs/kanon/00_KAZALO.md": "canon\n",
        }
        for relative, content in files.items():
            (self.repository / relative).write_text(content, encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        (self.workspace / "jobs").mkdir(parents=True)
        (self.workspace / "workspace.json").write_text(json.dumps({
            "repository": str(self.repository),
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repository), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    @staticmethod
    def snapshot(profile_id: str, *, model_tag=None, allow_disabled=False, **_kwargs):
        return manager.snapshot_for_new_task(
            profile_id, model_tag=model_tag or "gpt-oss:20b",
            digest_resolver=lambda _url, _model: "a" * 64,
            allow_disabled=allow_disabled,
        )

    @staticmethod
    def envelope() -> str:
        return (
            "worker_model=worker:test\n"
            "[OWNER_REQUEST]\nPreveri value\n[/OWNER_REQUEST]\n\n"
            "[CODEX_RESEARCH_PROMPT]\nPreberi app.py in test.\n"
            "[/CODEX_RESEARCH_PROMPT]\n\n"
            "[REQUIRED_QUESTIONS]\nQ1: Kaj vrne value?\n"
            "[/REQUIRED_QUESTIONS]"
        )

    @staticmethod
    def decomposition(_prompt: str, **_kwargs):
        return {"tasks": [{
            "research_task_id": "R1", "objective": "Preveri value",
            "question_ids": ["Q1"],
            "allowed_scope": [{"kind": "file", "path": "app.py"}],
            "required_evidence": ["Prebrana definicija"],
            "reason_for_overlap": None,
        }]}

    @staticmethod
    def worker(request, **_kwargs):
        attempt = request["attempt"]
        result = {
            "status": "COMPLETE", "research_task_id": "R1",
            "attempt": attempt,
            "answers": [{
                "question_id": "Q1", "answer": "value vrne 1.",
                "evidence_ids": ["READ-1"],
            }],
            "claims": [{"claim": "value vrne 1.", "evidence_ids": ["READ-1"]}],
            "unconfirmed": [], "scope_expansion": None,
        }
        events = [
            {"type": "tool_call", "step": 1, "tool": "project_read",
             "arguments": {"path": "app.py", "start_line": 1, "end_line": 2}},
            {"type": "tool_result", "step": 1, "tool": "project_read", "ok": True,
             "result": {"path": "app.py", "start_line": 1, "end_line": 2,
                        "raw_content": "def value():\n    return 1\n"}},
        ]
        return result, events

    @staticmethod
    def accept(_task, _result, **_kwargs):
        return {"decision": "ACCEPT", "reason": "Dokazi so popolni",
                "missing_evidence": []}

    @staticmethod
    def synthesis(_accepted, _cards, **_kwargs):
        return {"synthesis": [{
            "claim": "value vrne 1.",
            "evidence_ids": ["E-R1-A1-0001"],
        }], "unresolved": []}

    def run_research(self, **overrides):
        values = {
            "decomposition_runner": self.decomposition,
            "worker_runner": self.worker,
            "review_runner": self.accept,
            "synthesis_runner": self.synthesis,
        }
        values.update(overrides)
        return manager.start_research(
            self.envelope(), workspace=self.workspace,
            lead_profile="conversation_lead", model_tag="gpt-oss:20b",
            snapshot_factory=self.snapshot, model_releaser=lambda _model: None,
            model_identity_checker=lambda _lead, _worker: None,
            **values,
        )

    def test_full_research_approval_and_prepare_plan_path(self):
        order = []

        def decomposition(*args, **kwargs):
            order.append("decomposition")
            return self.decomposition(*args, **kwargs)

        def worker(*args, **kwargs):
            order.append("worker")
            return self.worker(*args, **kwargs)

        def review(*args, **kwargs):
            order.append("review")
            return self.accept(*args, **kwargs)

        def synthesis(*args, **kwargs):
            order.append("synthesis")
            return self.synthesis(*args, **kwargs)

        conversation = self.run_research(
            decomposition_runner=decomposition, worker_runner=worker,
            review_runner=review, synthesis_runner=synthesis,
        )
        self.assertEqual(
            ["decomposition", "worker", "review", "synthesis"], order,
        )
        run_id = conversation["parent_run_id"]
        job_dir = self.workspace / "jobs" / run_id
        state = json.loads((job_dir / "manager.json").read_text())
        self.assertEqual("AWAITING_CODEX_RESEARCH_REVIEW", state["research_state"])
        dossier_hash = conversation["research_dossier_sha256"]
        with self.assertRaisesRegex(manager.ManagerError, "Codex-approved"):
            manager.prepare_plan(run_id, workspace=self.workspace, responder=lambda *_a, **_k: None)
        manager.record_research_review(
            run_id, dossier_sha256=dossier_hash, verdict="APPROVE",
            workspace=self.workspace,
        )

        def plan(_message, **_kwargs):
            return {
                "reply": "Načrt", "routing": "READY_FOR_PROCESSING",
                "objective": "Spremeni value", "allowed_files": ["app.py"],
                "atoms": [{"id": "A1", "task": "Spremeni value",
                           "expected_files": ["app.py"]}],
                "constraints": ["Brez drugih sprememb"],
                "acceptance_criteria": ["Test uspe"],
                "objective_source_turns": ["turn-0001-owner"],
                "decision_source_turns": [],
                "discussion_only_turns": ["turn-0002-lead"],
            }, {"bridge_job_id": "PLAN-1", "prompt_hash": "b" * 64}

        prepared = manager.prepare_plan(
            run_id, workspace=self.workspace, responder=plan,
        )
        self.assertIn("IZVEDBENI NAČRT", prepared["turns"][-1]["content"])

    def test_invalid_decomposition_or_lead_review_fails_closed(self):
        with self.assertRaisesRegex(Exception, "LEAD_DECOMPOSITION"):
            self.run_research(decomposition_runner=lambda *_a, **_k: {})
        blocked = sorted((self.workspace / "jobs").glob("MF-LOCAL-*"))[-1]
        self.assertEqual("BLOCKED", json.loads((blocked / "manager.json").read_text())["state"])

        with self.assertRaisesRegex(Exception, "Lead review"):
            self.run_research(review_runner=lambda *_a, **_k: {})
        blocked = sorted((self.workspace / "jobs").glob("MF-LOCAL-*"))[-1]
        self.assertEqual("BLOCKED", json.loads((blocked / "manager.json").read_text())["state"])

    def test_worker_schema_checkpoint_change_and_fourth_attempt_fail_closed(self):
        def invalid_worker(request, **kwargs):
            _result, events = self.worker(request, **kwargs)
            return {}, events
        with self.assertRaisesRegex(Exception, "worker_result"):
            self.run_research(worker_runner=invalid_worker)

        changed = {"done": False}
        def changing_worker(*args, **kwargs):
            result = self.worker(*args, **kwargs)
            if not changed["done"]:
                (self.repository / "app.py").write_text("changed\n", encoding="utf-8")
                changed["done"] = True
            return result
        with self.assertRaisesRegex(Exception, "clean protected repository|RESEARCH_BASE_CHANGED"):
            self.run_research(worker_runner=changing_worker)
        (self.repository / "app.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8",
        )

        attempts = []
        def revise(_task, result, **_kwargs):
            attempts.append(result["attempt"])
            return {"decision": "REVISE", "reason": "Manjka dokaz",
                    "missing_evidence": ["več"]}
        with self.assertRaisesRegex(Exception, "BLOCKED after at most three attempts"):
            self.run_research(review_runner=revise)
        self.assertEqual([1, 2, 3], attempts)

    def test_model_digest_change_during_flow_fails_closed(self):
        checks = {"count": 0}

        def changed_after_decomposition(_lead, _worker):
            checks["count"] += 1
            if checks["count"] > 1:
                raise manager.ResearchError("RESEARCH_WORKER_MODEL_CHANGED")

        with self.assertRaisesRegex(Exception, "RESEARCH_WORKER_MODEL_CHANGED"):
            manager.start_research(
                self.envelope(), workspace=self.workspace,
                lead_profile="conversation_lead", model_tag="gpt-oss:20b",
                snapshot_factory=self.snapshot,
                decomposition_runner=self.decomposition,
                worker_runner=self.worker, review_runner=self.accept,
                synthesis_runner=self.synthesis,
                model_releaser=lambda _model: None,
                model_identity_checker=changed_after_decomposition,
            )

    def test_prepare_plan_without_dossier_is_blocked(self):
        def legacy(_message, **_kwargs):
            return {"reply": "legacy"}, {"bridge_job_id": "OLD"}

        conversation = manager.start_conversation(
            "Preveri value", workspace=self.workspace,
            lead_profile="conversation_lead", model_tag="gpt-oss:20b",
            responder=legacy, snapshot_factory=self.snapshot,
        )
        job_dir = self.workspace / "jobs" / conversation["parent_run_id"]
        (job_dir / "research-input.json").write_text(
            json.dumps(manager.parse_research_envelope(self.envelope())),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(manager.ManagerError, "Codex-approved research dossier"):
            manager.prepare_plan(
                conversation["parent_run_id"], workspace=self.workspace,
                responder=lambda *_a, **_k: None,
            )


if __name__ == "__main__":
    unittest.main()
