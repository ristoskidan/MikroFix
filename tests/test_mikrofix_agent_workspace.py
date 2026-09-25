import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
