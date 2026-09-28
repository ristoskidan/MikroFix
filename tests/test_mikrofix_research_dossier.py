from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


def load(name):
    path = Path(__file__).resolve().parents[1] / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


research = load("mikrofix_research")
dossier = load("mikrofix_research_dossier")


class ResearchDossierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "app.py").write_text("def save():\n    db.write()\n", encoding="utf-8")
        (self.repo / "MAP.md").write_text("map\n", encoding="utf-8")
        (self.repo / "docs/canon.md").write_text("canon\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.identity = research.repository_identity(
            self.repo, project_map=self.repo / "MAP.md",
            canon_paths=[self.repo / "docs/canon.md"],
        )
        self.task = research.validate_research_task({
            "research_task_id": "R1", "objective": "Find persistence",
            "question_ids": ["Q1"],
            "allowed_scope": [{"kind": "file", "path": "app.py"}],
            "required_evidence": ["write"], "reason_for_overlap": None,
        }, self.repo)
        self.events = [
            {"type": "tool_call", "step": 1, "tool": "project_read",
             "arguments": {"path": "app.py", "start_line": 1, "end_line": 2}},
            {"type": "tool_result", "step": 1, "tool": "project_read", "ok": True,
             "result": {"path": "app.py", "start_line": 1, "end_line": 2,
                        "content": "def save():\n    db.write()",
                        "raw_content": "def save():\n    db.write()"}},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def accepted(self, evidence_id):
        return [{
            "task": self.task, "attempt": 1, "attempt_id": "R1-A1-test",
            "result": {
                "status": "COMPLETE", "research_task_id": "R1", "attempt": 1,
                "answers": [{"question_id": "Q1", "answer": "db.write",
                             "evidence_ids": [evidence_id]}],
                "claims": [{"claim": "save calls db.write",
                            "evidence_ids": [evidence_id]}],
                "unconfirmed": [], "scope_expansion": None,
            },
            "lead_decision": {"decision": "ACCEPT"},
        }]

    def test_cards_come_only_from_successful_structured_read_results(self):
        cards = dossier.evidence_cards_from_trace(self.events + [
            {"type": "tool_result", "step": 2, "tool": "project_search",
             "ok": True, "result": {"matches": []}},
            {"type": "tool_result", "step": 3, "tool": "project_read",
             "ok": False, "result": {"content": "blocked"}},
        ], task=self.task, attempt=1)
        self.assertEqual(1, len(cards))
        self.assertEqual("app.py", cards[0]["canonical_path"])
        self.assertEqual(64, len(cards[0]["raw_tool_result_sha256"]))
        self.assertEqual(64, len(cards[0]["content_bytes_sha256"]))

    def test_gate_rejects_unknown_evidence_and_stale_checkpoint(self):
        cards = dossier.evidence_cards_from_trace(self.events, task=self.task, attempt=1)
        with self.assertRaisesRegex(dossier.ResearchError, "unknown evidence"):
            dossier.evidence_gate(
                identity=self.identity, repository=self.repo,
                project_map=self.repo / "MAP.md", canon_paths=[self.repo / "docs/canon.md"],
                tasks=[self.task], accepted_results=self.accepted("invented"),
                evidence_cards=cards, required_question_ids={"Q1"},
            )
        (self.repo / "app.py").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(dossier.ResearchError, "clean|BASE_CHANGED"):
            dossier.evidence_gate(
                identity=self.identity, repository=self.repo,
                project_map=self.repo / "MAP.md", canon_paths=[self.repo / "docs/canon.md"],
                tasks=[self.task], accepted_results=self.accepted(cards[0]["evidence_id"]),
                evidence_cards=cards, required_question_ids={"Q1"},
            )

    def test_gate_rejects_evidence_from_another_task_attempt_or_scope(self):
        card = dossier.evidence_cards_from_trace(self.events, task=self.task, attempt=1)[0]
        for field, value in (
            ("research_task_id", "R2"),
            ("attempt", 2),
            ("canonical_path", "docs/canon.md"),
        ):
            foreign = dict(card)
            foreign[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                dossier.ResearchError, "another task, attempt or scope",
            ):
                dossier.evidence_gate(
                    identity=self.identity, repository=self.repo,
                    project_map=self.repo / "MAP.md",
                    canon_paths=[self.repo / "docs/canon.md"], tasks=[self.task],
                    accepted_results=self.accepted(foreign["evidence_id"]),
                    evidence_cards=[foreign], required_question_ids={"Q1"},
                )
    def test_scope_expansion_checks_identity_and_text_not_semantics(self):
        card = dossier.evidence_cards_from_trace(self.events, task=self.task, attempt=1)[0]
        dossier.check_scope_expansion({
            "path": "db.py", "symbol": "db.write", "reason": "follow dependency",
            "evidence_ids": [card["evidence_id"]],
        }, evidence_by_id={card["evidence_id"]: card})
        with self.assertRaisesRegex(dossier.ResearchError, "does not contain"):
            dossier.check_scope_expansion({
                "path": "unrelated.py", "symbol": None, "reason": "guess",
                "evidence_ids": [card["evidence_id"]],
            }, evidence_by_id={card["evidence_id"]: card})

    def test_dossier_revisions_and_pointer_are_immutable_and_chained(self):
        card = dossier.evidence_cards_from_trace(self.events, task=self.task, attempt=1)[0]
        accepted = self.accepted(card["evidence_id"])
        first = dossier.create_dossier_revision(
            root=self.root, identity=self.identity,
            required_questions=[{"id": "Q1", "question": "Where?"}],
            accepted_results=accepted, evidence_cards=[card],
            synthesis=[{"claim": "Write is visible", "evidence_ids": [card["evidence_id"]]}],
            unresolved=[],
        )
        second = dossier.create_dossier_revision(
            root=self.root, identity=self.identity,
            required_questions=[{"id": "Q1", "question": "Where?"}],
            accepted_results=accepted, evidence_cards=[card],
            synthesis=[{"claim": "Write is visible", "evidence_ids": [card["evidence_id"]]}],
            unresolved=["transaction boundary"],
        )
        self.assertEqual(1, first["dossier_revision"])
        self.assertEqual(first["dossier_sha256"], second["previous_dossier_sha256"])
        pointer = json.loads((self.root / "research-dossiers/current.json").read_text())
        self.assertEqual(second["dossier_sha256"], pointer["dossier_sha256"])
        old_file = next((self.root / "research-dossiers").glob("revision-0001-*.json"))
        self.assertFalse(old_file.stat().st_mode & stat.S_IWUSR)

    def test_only_current_exact_codex_approval_unlocks_dossier(self):
        card = dossier.evidence_cards_from_trace(self.events, task=self.task, attempt=1)[0]
        accepted = self.accepted(card["evidence_id"])
        first = dossier.create_dossier_revision(
            root=self.root, identity=self.identity,
            required_questions=[{"id": "Q1", "question": "Where?"}],
            accepted_results=accepted, evidence_cards=[card],
            synthesis=[{"claim": "Write", "evidence_ids": [card["evidence_id"]]}],
            unresolved=[],
        )
        dossier.record_codex_research_verdict(
            root=self.root, dossier_sha256=first["dossier_sha256"], verdict="APPROVE",
        )
        self.assertEqual(
            first["dossier_sha256"],
            dossier.require_current_codex_approval(self.root)["dossier_sha256"],
        )
        second = dossier.create_dossier_revision(
            root=self.root, identity=self.identity,
            required_questions=[{"id": "Q1", "question": "Where?"}],
            accepted_results=accepted, evidence_cards=[card],
            synthesis=[{"claim": "Write", "evidence_ids": [card["evidence_id"]]}],
            unresolved=["new gap"],
        )
        with self.assertRaisesRegex(dossier.ResearchError, "not Codex-approved"):
            dossier.require_current_codex_approval(self.root)
        with self.assertRaisesRegex(dossier.ResearchError, "current dossier"):
            dossier.record_codex_research_verdict(
                root=self.root, dossier_sha256=first["dossier_sha256"], verdict="REJECT",
                reason="old revision",
            )
        dossier.record_codex_research_verdict(
            root=self.root, dossier_sha256=second["dossier_sha256"], verdict="REVISE",
            research_task_id="R1", reason="Need caller proof",
        )
        with self.assertRaisesRegex(dossier.ResearchError, "not Codex-approved"):
            dossier.require_current_codex_approval(self.root)


if __name__ == "__main__":
    unittest.main()
