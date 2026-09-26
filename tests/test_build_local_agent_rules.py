import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools/build_local_agent_rules.py"
SPEC = importlib.util.spec_from_file_location("build_local_agent_rules", MODULE_PATH)
rules = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = rules
SPEC.loader.exec_module(rules)


class BuildLocalAgentRulesTests(unittest.TestCase):
    def test_check_detects_stale_and_unexpected_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            original = rules.OUTPUT
            rules.OUTPUT = output
            try:
                expected = rules.expected_outputs()
                for name, content in expected.items():
                    (output / name).write_text(content, encoding="utf-8")
                self.assertEqual([], rules.check_outputs())
                (output / "10_POSLOVNA_PRAVILA.md").write_text("stale\n")
                (output / "EXTRA.md").write_text("extra\n")
                self.assertEqual(
                    ["stale:10_POSLOVNA_PRAVILA.md", "unexpected:EXTRA.md"],
                    rules.check_outputs(),
                )
            finally:
                rules.OUTPUT = original


if __name__ == "__main__":
    unittest.main()
