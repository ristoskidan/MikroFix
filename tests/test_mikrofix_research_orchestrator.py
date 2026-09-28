from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_research_orchestrator.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_research_orchestrator", MODULE_PATH)
orchestrator = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = orchestrator
SPEC.loader.exec_module(orchestrator)


class ResearchOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        (self.repository / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.repository / "tests").mkdir()
        self.snapshot = {
            "model_tag": "worker:test", "profile_snapshot_hash": "a" * 64,
        }
        self.task = {
            "research_task_id": "R1", "objective": "Trace one path",
            "question_ids": ["Q1"],
            "allowed_scope": [{"kind": "file", "path": "app.py"}],
            "required_evidence": ["definition"], "reason_for_overlap": None,
        }

    def tearDown(self):
        self.temp.cleanup()

    def complete(self, attempt=1):
        return {
            "status": "COMPLETE", "research_task_id": "R1", "attempt": attempt,
            "answers": [{"question_id": "Q1", "answer": "Found",
                         "evidence_ids": ["E-R1-A1-0001"]}],
            "claims": [{"claim": "It exists", "evidence_ids": ["E-R1-A1-0001"]}],
            "unconfirmed": [], "scope_expansion": None,
        }

    def test_task_set_requires_one_to_three_tasks_and_question_coverage(self):
        tasks = orchestrator.validate_task_set(
            [self.task], repository=self.repository, required_question_ids={"Q1"},
        )
        self.assertEqual("R1", tasks[0]["research_task_id"])
        with self.assertRaisesRegex(orchestrator.ResearchError, "cover every"):
            orchestrator.validate_task_set(
                [self.task], repository=self.repository,
                required_question_ids={"Q1", "Q2"},
            )

    def test_workers_are_sequential_fresh_and_release_model_after_each_attempt(self):
        calls = []
        releases = []

        def runner(request):
            calls.append((request["research_task"]["research_task_id"], request["attempt"]))
            return self.complete(request["attempt"])

        results = orchestrator.run_sequential_workers(
            root=self.root, repository=self.repository,
            tasks=orchestrator.validate_task_set(
                [self.task], repository=self.repository, required_question_ids={"Q1"},
            ),
            worker_snapshot=self.snapshot, worker_runner=runner,
            review_result=lambda task, result: {"decision": "ACCEPT"},
            release_model=releases.append,
        )
        self.assertEqual([("R1", 1)], calls)
        self.assertEqual(["worker:test"], releases)
        self.assertEqual(1, results[0]["attempt"])
        request = self.root / "research-workers/R1/attempt-1/request.json"
        session = self.root / "research-workers/R1/attempt-1/session.json"
        self.assertTrue(request.is_file())
        self.assertIn('"fresh_session": true', session.read_text(encoding="utf-8"))

    def test_revision_is_fresh_limited_and_receives_only_prior_result_and_gap(self):
        seen = []
        releases = []

        def runner(request):
            seen.append(request)
            return self.complete(request["attempt"])

        def review(_task, result):
            if result["attempt"] == 1:
                return {
                    "decision": "REVISE", "reason": "Missing persistence proof",
                    "missing_evidence": ["database write"],
                }
            return {"decision": "ACCEPT"}

        results = orchestrator.run_sequential_workers(
            root=self.root, repository=self.repository,
            tasks=orchestrator.validate_task_set(
                [self.task], repository=self.repository, required_question_ids={"Q1"},
            ),
            worker_snapshot=self.snapshot, worker_runner=runner,
            review_result=review, release_model=releases.append,
        )
        self.assertEqual([1, 2], [item["attempt"] for item in seen])
        self.assertIsNone(seen[0]["correction"])
        self.assertEqual(["database write"], seen[1]["correction"]["missing_evidence"])
        self.assertEqual(2, results[0]["attempt"])
        self.assertEqual(["worker:test", "worker:test"], releases)

    def test_third_failed_attempt_blocks_and_still_releases_each_session(self):
        releases = []
        attempts = []

        def runner(request):
            attempts.append(request["attempt"])
            return self.complete(request["attempt"])

        with self.assertRaisesRegex(orchestrator.ResearchError, "BLOCKED"):
            orchestrator.run_sequential_workers(
                root=self.root, repository=self.repository,
                tasks=orchestrator.validate_task_set(
                    [self.task], repository=self.repository, required_question_ids={"Q1"},
                ), worker_snapshot=self.snapshot, worker_runner=runner,
                review_result=lambda task, result: {
                    "decision": "REVISE", "reason": "Still incomplete",
                    "missing_evidence": ["proof"],
                }, release_model=releases.append,
            )
        self.assertEqual([1, 2, 3], attempts)
        self.assertEqual(["worker:test"] * 3, releases)

    def test_complete_requires_every_assigned_question(self):
        result = self.complete()
        result["answers"] = []
        with self.assertRaisesRegex(orchestrator.ResearchError, "every assigned"):
            orchestrator.validate_worker_result(result, task={
                **self.task, "original_task_sha256": "b" * 64,
            }, attempt=1)


if __name__ == "__main__":
    unittest.main()
