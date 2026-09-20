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

        with mock.patch.object(self.bridge, "call_qwen", side_effect=lambda _m: next(responses)), \
                mock.patch.object(self.bridge, "execute_tool", return_value={"ok": True}), \
                mock.patch.object(self.bridge, "notify_job_completion", side_effect=callback):
            self.bridge.process_job(job["id"])
        self.assertEqual([("completed", "finished\n")], observed)

    def test_runtime_prompt_always_contains_persistent_lead_rules(self):
        prompt = self.bridge.runtime_system_prompt()
        for required in (
            "Codex — vodja projekta in končni pregled",
            "GPT-OSS — lokalni vodja izvedbe",
            "Qwen — lokalni izvajalec",
            "Bridge — nadzorni most",
            "Natančen vzrok:",
            "project_diff",
            "dejanskim številom testov",
        ):
            self.assertIn(required, prompt)


if __name__ == "__main__":
    unittest.main()
