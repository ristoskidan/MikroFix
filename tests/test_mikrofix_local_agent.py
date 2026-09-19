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

    def test_system_prompt_skips_chat_only_acknowledgement(self):
        prompt = bridge.build_system_prompt("kanonicni kontekst", "potrjena lekcija")
        self.assertTrue(prompt.startswith("kanonicni kontekst"))
        self.assertIn("potrjena lekcija", prompt)
        self.assertIn("Uvodna potrditev", prompt)
        self.assertIn("Odgovori neposredno na", prompt)

    def test_payload_disables_reasoning_only_output_and_keeps_requested_context(self):
        payload = bridge.build_payload(
            "qwen3.5:9b", "kontekst", "naloga", 32768, 1200, "lekcija"
        )
        self.assertIs(payload["think"], False)
        self.assertEqual(
            payload["options"], {"num_ctx": 32768, "num_predict": 1200}
        )
        self.assertEqual(payload["messages"][1]["content"], "naloga")

    def test_gpt_oss_uses_low_reasoning_instead_of_unsupported_disable(self):
        payload = bridge.build_payload(
            "gpt-oss:20b", "kontekst", "naloga", 32768, 1200, "lekcija"
        )
        self.assertEqual(payload["think"], "low")

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
            lessons = root / "lessons.md"
            lessons.write_text("potrjena lekcija", encoding="utf-8")
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
                    "--lessons",
                    str(lessons),
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
