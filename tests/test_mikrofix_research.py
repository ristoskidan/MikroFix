from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_research.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_research", MODULE_PATH)
research = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = research
SPEC.loader.exec_module(research)


class ResearchContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Path(self.temp.name) / "repo"
        (self.repository / "docs/kanon").mkdir(parents=True)
        (self.repository / "src").mkdir()
        (self.repository / ".gitignore").write_text("runtime/\n", encoding="utf-8")
        (self.repository / "PROJECT_MAP.md").write_text("map\n", encoding="utf-8")
        (self.repository / "docs/kanon/rules.md").write_text("rules\n", encoding="utf-8")
        (self.repository / "src/app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def identity(self):
        return research.repository_identity(
            self.repository,
            project_map=self.repository / "PROJECT_MAP.md",
            canon_paths=[self.repository / "docs/kanon/rules.md"],
        )

    def test_inventory_contains_only_tracked_content_and_ignores_runtime(self):
        before = self.identity()
        (self.repository / "runtime").mkdir()
        (self.repository / "runtime/cache.bin").write_bytes(b"ignored")
        after = self.identity()
        self.assertEqual(before, after)
        self.assertEqual(4, after["tracked_entry_count"])

    def test_inventory_changes_with_tracked_content_and_mode(self):
        before = self.identity()
        app = self.repository / "src/app.py"
        app.write_text("def value():\n    return 2\n", encoding="utf-8")
        self.git("add", "src/app.py")
        self.git("commit", "-qm", "content")
        content = self.identity()
        self.assertNotEqual(
            before["repository_inventory_sha256"],
            content["repository_inventory_sha256"],
        )
        app.chmod(app.stat().st_mode | stat.S_IXUSR)
        self.git("add", "src/app.py")
        self.git("commit", "-qm", "mode")
        mode = self.identity()
        self.assertNotEqual(
            content["repository_inventory_sha256"],
            mode["repository_inventory_sha256"],
        )

    def test_identity_requires_separately_clean_git_status(self):
        (self.repository / "untracked.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(research.ResearchError, "clean"):
            self.identity()

    def test_scope_rejects_escape_secret_symlink_binary_and_redundancy(self):
        (self.repository / "secret.key").write_text("secret", encoding="utf-8")
        (self.repository / "image.bin").write_bytes(b"\x00")
        (self.repository / "link.py").symlink_to("src/app.py")
        invalid = [
            [{"kind": "file", "path": "../outside.py"}],
            [{"kind": "file", "path": "/tmp/outside.py"}],
            [{"kind": "file", "path": "secret.key"}],
            [{"kind": "file", "path": "image.bin"}],
            [{"kind": "file", "path": "link.py"}],
            [
                {"kind": "directory", "path": "src"},
                {"kind": "file", "path": "src/app.py"},
            ],
        ]
        for scope in invalid:
            with self.subTest(scope=scope), self.assertRaises(research.ResearchError):
                research.normalize_scope(scope, self.repository)

    def test_scope_accepts_explicit_file_and_directory(self):
        self.assertEqual(
            [
                {"kind": "file", "path": "PROJECT_MAP.md"},
                {"kind": "directory", "path": "src"},
            ],
            research.normalize_scope([
                {"kind": "directory", "path": "src"},
                {"kind": "file", "path": "PROJECT_MAP.md"},
            ], self.repository),
        )

    def test_questions_and_task_are_bounded_and_task_hash_is_stable(self):
        questions = research.validate_questions([
            {"id": "Q1", "question": "Where is the write performed?"},
        ])
        self.assertEqual("Q1", questions[0]["id"])
        task = {
            "research_task_id": "R1",
            "objective": "Trace the persistence path",
            "question_ids": ["Q1"],
            "allowed_scope": [{"kind": "directory", "path": "src"}],
            "required_evidence": ["definition", "caller", "test"],
            "reason_for_overlap": None,
        }
        first = research.validate_research_task(task, self.repository)
        second = research.validate_research_task(json.loads(json.dumps(task)), self.repository)
        self.assertEqual(first["original_task_sha256"], second["original_task_sha256"])
        with self.assertRaisesRegex(research.ResearchError, "1..20"):
            research.validate_questions([])
        with self.assertRaisesRegex(research.ResearchError, "R1, R2 or R3"):
            research.validate_research_task({**task, "research_task_id": "R4"}, self.repository)


if __name__ == "__main__":
    unittest.main()
