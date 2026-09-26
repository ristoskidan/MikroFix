import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_agent_workspace.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_agent_workspace", MODULE_PATH)
workspace_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = workspace_module
SPEC.loader.exec_module(workspace_module)


class AgentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.workspace = self.root / "workspace"
        self.source.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        required = {
            "AGENTS.md": "rules\n",
            "docs/kanon/00_KAZALO.md": "index\n",
            "docs/kanon/01_POSLOVNA_PRAVILA.md": "business\n",
            "docs/kanon/02_CILJNA_ARHITEKTURA.md": "architecture\n",
            "docs/kanon/03_SPREJEMNI_TESTI.md": "acceptance\n",
            "docs/kanon/04_STANJE_IZVEDBE.md": "state\n",
            "docs/kanon/05_ZGODOVINA_ODLOCITEV.md": "history\n",
            "docs/kanon/06_LOKALNI_AGENTI.md": "agents\n",
            "docs/local_agents/README.md": "readme\n",
            "docs/local_agents/PRAVILNIK_VERIGE.md": "chain\n",
            "docs/local_agents/pravila/00_AVTORITETA.md": "core\n",
            "docs/local_agents/roles/EXECUTOR_SYSTEM.md": "executor\n",
            "app.py": "print('baseline')\n",
        }
        for relative, content in required.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.source), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def test_init_creates_isolated_committed_clone_and_frozen_knowledge(self):
        (self.source / "app.py").write_text("dirty\n", encoding="utf-8")
        result = workspace_module.init_workspace(self.source, self.workspace)
        self.assertEqual("OK", result["status"])
        self.assertEqual("print('baseline')\n", (
            self.workspace / "repository" / "app.py"
        ).read_text(encoding="utf-8"))
        self.assertEqual(0, (self.workspace / "knowledge" / "AGENTS.md").stat().st_mode & 0o222)
        self.assertEqual(0, (self.workspace / "knowledge").stat().st_mode & 0o222)
        self.assertEqual("owner_only", result["notification_policy"])
        dirty_report = self.workspace / "reports" / "SOURCE_DIRTY_AT_CREATION.txt"
        self.assertIn("app.py", dirty_report.read_text(encoding="utf-8"))
        config = json.loads((self.workspace / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(str(self.source.resolve()), config["trusted_control_root"])
        manifest = json.loads((self.workspace / "KNOWLEDGE_MANIFEST.json").read_text())
        self.assertIn("docs/kanon/06_LOKALNI_AGENTI.md", manifest["files"])

    def test_verify_detects_knowledge_tampering(self):
        workspace_module.init_workspace(self.source, self.workspace)
        knowledge = self.workspace / "knowledge" / "AGENTS.md"
        knowledge.chmod(0o600)
        knowledge.write_text("changed\n", encoding="utf-8")
        result = workspace_module.verify_workspace(self.workspace)
        self.assertEqual("INVALID", result["status"])
        self.assertEqual(["AGENTS.md"], result["knowledge_mismatches"])

    def test_verify_detects_unexpected_knowledge_file_and_writable_root(self):
        workspace_module.init_workspace(self.source, self.workspace)
        knowledge = self.workspace / "knowledge"
        knowledge.chmod(0o700)
        (knowledge / "injected.md").write_text("not canonical\n", encoding="utf-8")
        result = workspace_module.verify_workspace(self.workspace)
        self.assertEqual("INVALID", result["status"])
        self.assertEqual(
            ["UNEXPECTED:injected.md", "WRITABLE:."],
            result["knowledge_mismatches"],
        )

    def test_init_refuses_existing_workspace(self):
        self.workspace.mkdir()
        with self.assertRaises(workspace_module.WorkspaceError):
            workspace_module.init_workspace(self.source, self.workspace)

    def test_inventory_includes_empty_directories_files_and_symlinks(self):
        root = self.root / "inventory"
        (root / "empty").mkdir(parents=True)
        (root / "value.txt").write_text("value", encoding="utf-8")
        (root / "link").symlink_to("value.txt")
        first = workspace_module.workspace_inventory(root)
        self.assertEqual(1, first["directories"])
        self.assertEqual(1, first["files"])
        self.assertEqual(1, first["symlinks"])
        self.assertEqual(first, workspace_module.workspace_inventory(root))

    def test_rotation_lock_refuses_exclusive_access_while_writer_holds_shared_lock(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_writer_lock():
            with workspace_module.workspace_lock(self.workspace):
                entered.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold_writer_lock)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        try:
            with self.assertRaisesRegex(RuntimeError, "workspace is busy"):
                with workspace_module.workspace_lock(
                    self.workspace, exclusive=True, blocking=False,
                ):
                    self.fail("exclusive lock must not be acquired")
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_rotation_preserves_old_workspace_and_uses_new_commit(self):
        workspace_module.init_workspace(self.source, self.workspace)
        evidence = self.workspace / "jobs" / "MF-OLD" / "audit.json"
        evidence.parent.mkdir()
        evidence.write_text("old evidence\n", encoding="utf-8")
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        (self.source / "app.py").write_text("print('new')\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-m", "new checkpoint")

        class Services:
            def is_active(self, _name): return False
            def stop(self, _name): raise AssertionError("inactive service stopped")
            def start(self, _name): raise AssertionError("inactive service started")

        result = workspace_module.rotate_workspace(
            self.source, self.workspace, services=Services(), process_checker=lambda _path: [],
        )
        self.assertEqual("OK", result["status"])
        rotation = result["rotation"]
        archive = Path(rotation["archive"])
        self.assertEqual(old_inventory, workspace_module.workspace_inventory(archive))
        self.assertEqual("old evidence\n", (archive / "jobs/MF-OLD/audit.json").read_text())
        self.assertEqual(self.git("rev-parse", "HEAD"), workspace_module.verify_workspace(
            self.workspace
        )["baseline_commit"])

    def test_failed_rotation_restores_old_workspace_without_deletion(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)

        class Services:
            def is_active(self, _name): return False
            def stop(self, _name): pass
            def start(self, _name): pass

        original = workspace_module.init_workspace
        with mock.patch.object(
            workspace_module, "init_workspace", side_effect=RuntimeError("injected")
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                workspace_module.rotate_workspace(
                    self.source, self.workspace, services=Services(),
                    process_checker=lambda _path: [],
                )
        self.assertEqual(old_inventory, workspace_module.workspace_inventory(self.workspace))
        journals = list(workspace_module.rotation_root(self.workspace).glob("*.json"))
        self.assertEqual(1, len(journals))
        self.assertEqual("ROLLED_BACK", json.loads(journals[0].read_text())["phase"])

    def test_unfinished_rotation_blocks_new_rotation_and_requires_explicit_recovery(self):
        workspace_module.init_workspace(self.source, self.workspace)
        old_inventory = workspace_module.workspace_inventory(self.workspace)
        archive = self.root / "archived-workspace"
        os.replace(self.workspace, archive)
        workspace_module.init_workspace(self.source, self.workspace)
        rotation_id = "interrupted-test"
        journal_path = workspace_module.rotation_root(self.workspace) / f"{rotation_id}.json"
        journal = {
            "schema_version": 1,
            "rotation_id": rotation_id,
            "workspace": str(self.workspace),
            "archive": str(archive),
            "failed_candidate": str(self.root / "failed-candidate"),
            "old_checkpoint": self.git("rev-parse", "HEAD"),
            "new_checkpoint": self.git("rev-parse", "HEAD"),
            "old_inventory": old_inventory,
            "services_before": {},
        }
        workspace_module.update_rotation(journal_path, journal, "NEW_INITIALIZED")

        with self.assertRaisesRegex(workspace_module.WorkspaceError, "requires recovery"):
            workspace_module.rotate_workspace(
                self.source, self.workspace, process_checker=lambda _path: [],
            )
        result = workspace_module.recover_rotation(
            self.workspace, rotation_id, complete=False,
            process_checker=lambda _path: [],
        )
        self.assertEqual("ROLLED_BACK", result["rotation"]["phase"])
        self.assertEqual(old_inventory, workspace_module.workspace_inventory(self.workspace))

    def test_recovery_refuses_to_move_candidate_when_archive_hash_changed(self):
        workspace_module.init_workspace(self.source, self.workspace)
        archive = self.root / "archived-workspace"
        shutil.copytree(self.workspace, archive)
        expected = workspace_module.workspace_inventory(archive)
        (archive / "changed.txt").write_text("changed\n", encoding="utf-8")
        rotation_id = "damaged-archive"
        journal_path = workspace_module.rotation_root(self.workspace) / f"{rotation_id}.json"
        journal = {
            "schema_version": 1,
            "rotation_id": rotation_id,
            "workspace": str(self.workspace),
            "archive": str(archive),
            "failed_candidate": str(self.root / "failed-candidate"),
            "old_checkpoint": self.git("rev-parse", "HEAD"),
            "new_checkpoint": self.git("rev-parse", "HEAD"),
            "old_inventory": expected,
            "services_before": {},
        }
        workspace_module.update_rotation(journal_path, journal, "OLD_ARCHIVED")
        candidate_before = workspace_module.workspace_inventory(self.workspace)

        with self.assertRaisesRegex(workspace_module.WorkspaceError, "archive changed"):
            workspace_module.recover_rotation(
                self.workspace, rotation_id, complete=False,
                process_checker=lambda _path: [],
            )
        self.assertEqual(candidate_before, workspace_module.workspace_inventory(self.workspace))


if __name__ == "__main__":
    unittest.main()
