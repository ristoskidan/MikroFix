import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_local_agent.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_local_agent", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)

GENERATOR_PATH = Path(__file__).resolve().parents[1] / "tools" / "build_local_agent_rules.py"
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "build_local_agent_rules", GENERATOR_PATH
)
generator = importlib.util.module_from_spec(GENERATOR_SPEC)
assert GENERATOR_SPEC.loader is not None
GENERATOR_SPEC.loader.exec_module(generator)


class LocalAgentBridgeTests(unittest.TestCase):
    def test_rule_groups_cover_every_canonical_section_exactly_once(self):
        numbers = [
            number
            for definition in generator.GROUPS.values()
            for number in definition["sections"]
        ]
        self.assertEqual(sorted(numbers), list(range(1, 21)))
        self.assertEqual(len(numbers), len(set(numbers)))

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

    def test_gpt_oss_uses_selected_reasoning_instead_of_unsupported_disable(self):
        payload = bridge.build_payload(
            "gpt-oss:20b", "kontekst", "naloga", 32768, 1200, "lekcija",
            "medium",
        )
        self.assertEqual(payload["think"], "medium")

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

    def make_rules(self, root, canonical_text="kanonični kontekst"):
        rules = root / "rules"
        rules.mkdir()
        (rules / "00.md").write_text("jedro", encoding="utf-8")
        (rules / "40.md").write_text("računi", encoding="utf-8")
        (rules / "70.md").write_text("cene", encoding="utf-8")
        index = {
            "source_sha256": hashlib.sha256(
                canonical_text.encode("utf-8")
            ).hexdigest(),
            "always": ["00.md"],
            "scopes": {
                "invoices": {"file": "40.md", "keywords": ["račun", "preveri"]},
                "pricing": {"file": "70.md", "keywords": ["cena"]},
            },
        }
        (rules / "INDEX.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8"
        )
        return rules

    def test_scoped_context_loads_core_and_matching_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "context.md"
            canonical.write_text("kanonični kontekst", encoding="utf-8")
            rules = self.make_rules(root)
            context, loaded, scopes = bridge.load_scoped_context(
                rules, canonical, [], "preveri račun"
            )
            self.assertEqual(scopes, ["invoices"])
            self.assertEqual([path.name for path, _ in loaded], ["00.md", "40.md"])
            self.assertIn("jedro", context)
            self.assertIn("računi", context)
            self.assertNotIn("cene", context)

    def test_unknown_topic_safely_loads_all_topics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "context.md"
            canonical.write_text("kanonični kontekst", encoding="utf-8")
            rules = self.make_rules(root)
            _, loaded, scopes = bridge.load_scoped_context(
                rules, canonical, [], "neznana tema"
            )
            self.assertEqual(set(scopes), {"invoices", "pricing"})
            self.assertEqual(len(loaded), 3)

    def test_dry_run_writes_trace_without_calling_ollama(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / "context.md"
            logs = root / "logs"
            context.write_text("kanonični kontekst", encoding="utf-8")
            lessons = root / "lessons.md"
            lessons.write_text("potrjena lekcija", encoding="utf-8")
            console = root / "agent-console.log"
            rules = self.make_rules(root)
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
                    "--rules-dir",
                    str(rules),
                    "--console-log",
                    str(console),
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
            visible = console.read_text(encoding="utf-8")
            self.assertIn("ZAHTEVA:\npreveri", visible)
            self.assertIn("DRY-RUN", visible)

    def test_console_header_shows_request_but_not_inline_material(self):
        header = bridge.console_header(
            task_id="LOCAL-1",
            created_at="2026-09-19T12:00:00+02:00",
            agent="qwen",
            model="qwen3.5:9b",
            role="pomocnik",
            selected_scopes=["invoices"],
            context_files=[(Path("/tmp/40.md"), "hash")],
            task="preveri konkurencnost",
            requested_output="pet testov",
            material_file=None,
        )
        self.assertIn("AGENT: qwen", header)
        self.assertIn("######################## QWEN", header)
        self.assertIn("ZAHTEVA:\npreveri konkurencnost", header)
        self.assertIn("MODULI: 40.md", header)
        self.assertNotIn("skrivno gradivo", header)


if __name__ == "__main__":
    unittest.main()
