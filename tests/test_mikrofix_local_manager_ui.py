import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_local_manager_ui.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_local_manager_ui", MODULE_PATH)
ui = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = ui
SPEC.loader.exec_module(ui)


class LocalManagerUIPureTests(unittest.TestCase):
    def test_existing_window_is_raised_by_second_shortcut_click(self):
        search = mock.Mock(returncode=0, stdout="111\n222\n")
        activate = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(ui.subprocess, "run", side_effect=[search, activate]) as run:
            self.assertTrue(ui.activate_existing_window())
        self.assertEqual(
            ["xdotool", "windowactivate", "--sync", "222"],
            run.call_args_list[1].args[0],
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "jobs").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_job(self, run_id, task, state, *, parent=None, created="2026-09-20T10:00:00Z"):
        directory = self.workspace / "jobs" / run_id
        directory.mkdir()
        (directory / "request.json").write_text(json.dumps({
            "run_id": run_id, "task": task, "parent_run_id": parent,
            "created_at": created,
        }), encoding="utf-8")
        (directory / "manager.json").write_text(json.dumps({
            "run_id": run_id, "state": state,
        }), encoding="utf-8")
        return directory

    def test_jobs_are_sorted_and_parent_is_rendered_as_conversation(self):
        self.write_job("MF-1", "Initial request", "BLOCKED", created="2026-09-20T10:00:00Z")
        self.write_job("MF-2", "Owner clarification", "PLANNING", parent="MF-1",
                       created="2026-09-20T11:00:00Z")
        jobs = ui.list_jobs(self.workspace)
        self.assertEqual(["MF-2", "MF-1"], [job["run_id"] for job in jobs])
        rendered = ui.render_job(jobs[0])
        self.assertIn("Owner clarification", rendered)
        self.assertIn("Nadaljevanje naloge: MF-1", rendered)
        self.assertIn("Qwen razčlenjuje", rendered)
        self.assertIn("GPT-OSS pa preverja", rendered)

    def test_ready_candidate_renders_checkpoint(self):
        directory = self.write_job("MF-3", "Finished request", "READY_FOR_CODEX_REVIEW")
        (directory / "manager.json").write_text(json.dumps({
            "run_id": "MF-3", "state": "READY_FOR_CODEX_REVIEW",
            "checkpoint": "/safe/candidate/manifest.json",
        }), encoding="utf-8")
        rendered = ui.render_job(ui.list_jobs(self.workspace)[0])
        self.assertIn("Kandidat je pripravljen", rendered)
        self.assertIn("/safe/candidate/manifest.json", rendered)

    def test_supervisor_terminal_state_overrides_stale_manager_state(self):
        directory = self.write_job("MF-4", "Manually executed", "READY_FOR_OWNER_LEASE")
        supervisor = directory / "supervisor.json"
        supervisor.write_text(json.dumps({
            "stage": "AWAIT_CODEX_REVIEW",
            "checkpoint": "/candidate/manual.json",
        }), encoding="utf-8")
        manager_data = json.loads(
            (directory / "manager.json").read_text(encoding="utf-8")
        )
        manager_data["supervisor_state"] = str(supervisor)
        (directory / "manager.json").write_text(json.dumps(manager_data), encoding="utf-8")
        job = ui.list_jobs(self.workspace)[0]
        self.assertEqual("READY_FOR_CODEX_REVIEW", job["state"])
        self.assertIn("/candidate/manual.json", ui.render_job(job))


if __name__ == "__main__":
    unittest.main()
