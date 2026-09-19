import datetime as dt
import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_local_agent.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_local_agent", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


class LocalAgentBridgeTests(unittest.TestCase):
    def test_prompt_has_required_sections(self):
        prompt = bridge.build_user_prompt("naloga", "gradivo", "izhod")
        self.assertEqual(
            prompt,
            "NALOGA:\nnaloga\n\nGRADIVO:\ngradivo\n\nZAHTEVANI IZHOD:\nizhod\n",
        )

    def test_task_id_has_timestamp_and_random_suffix(self):
        now = dt.datetime(2026, 9, 19, 12, 34, 56)
        task_id = bridge.make_task_id(now)
        self.assertRegex(task_id, r"^LOCAL-20260919-123456-[0-9A-F]{6}$")

    def test_atomic_write_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dnevnik.md"
            bridge.atomic_write(path, "prva")
            bridge.atomic_write(path, "druga")
            self.assertEqual(path.read_text(encoding="utf-8"), "druga")

    def test_dry_run_writes_trace_without_calling_ollama(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / "context.md"
            logs = root / "logs"
            context.write_text("kanonični kontekst", encoding="utf-8")
            exit_code = bridge.main(
                [
                    "--agent",
                    "reviewer",
                    "--task",
                    "preveri",
                    "--context",
                    str(context),
                    "--log-dir",
                    str(logs),
                    "--dry-run",
                ]
            )
            self.assertEqual(exit_code, 0)
            files = list(logs.glob("LOCAL-*.md"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("Stanje: **PRIPRAVLJENO**", content)
            self.assertIn("Model: `gpt-oss:20b`", content)
            self.assertIn("NALOGA:\npreveri", content)


if __name__ == "__main__":
    unittest.main()
