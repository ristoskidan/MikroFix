import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_local_manager.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_local_manager", MODULE_PATH)
manager = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = manager
SPEC.loader.exec_module(manager)


def model_plan(**overrides):
    value = {
        "objective": "Implement one bounded behavior",
        "allowed_files": ["app.py", "tests/test_app.py"],
        "atoms": [{
            "id": "A1", "task": "Change the behavior and its regression test",
            "checks": ["desktop_unit_tests"],
            "expected_files": ["app.py", "tests/test_app.py"],
        }],
        "constraints": ["Do not change unrelated behavior"],
        "context_links": {
            "canon": ["AGENTS.md"],
            "code": ["app.py"],
            "tests": ["tests/test_app.py"],
        },
        "acceptance_criteria": ["Regression test passes"],
    }
    value.update(overrides)
    return value


class LocalManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.workspace = self.root / "workspace"
        (self.repository / "tests").mkdir(parents=True)
        for relative in ("AGENTS.md", "app.py", "tests/test_app.py"):
            (self.repository / relative).write_text("baseline\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        (self.workspace / "jobs").mkdir(parents=True)
        (self.workspace / "workspace.json").write_text(json.dumps({
            "repository": str(self.repository),
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repository), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def test_normalize_overrides_all_trusted_fields(self):
        raw = "```json\n" + json.dumps(model_plan()) + "\n```"
        plan = manager.normalize_plan(raw, run_id="MF-LOCAL-1", repository=self.repository)
        self.assertEqual("MF-LOCAL-1", plan["run_id"])
        self.assertEqual(self.git("rev-parse", "HEAD"), plan["base_checkpoint"])
        self.assertEqual(3, plan["max_retries_per_atom"])
        self.assertEqual("AWAIT_CODEX_REVIEW", plan["final_gate"])

    def test_unknown_fields_and_missing_context_fail_closed(self):
        with self.assertRaisesRegex(manager.ManagerError, "unknown"):
            manager.normalize_plan(
                json.dumps({**model_plan(), "run_id": "MODEL-CONTROLLED"}),
                run_id="MF-LOCAL-1", repository=self.repository,
            )

    def test_empty_atom_checks_are_semantic_failure_for_retry(self):
        invalid = model_plan(atoms=[{
            "id": "A1", "task": "Write documentation",
            "checks": [], "expected_files": ["app.py"],
        }])
        with self.assertRaisesRegex(ValueError, "checks must be a non-empty list"):
            manager.normalize_plan(
                json.dumps(invalid), run_id="MF-LOCAL-1",
                repository=self.repository,
            )
        prompt = manager.planner_prompt("Write documentation", "checks are empty")
        self.assertIn("PREJŠNJI IZHOD JE BIL ZAVRNJEN", prompt)
        self.assertIn("checks are empty", prompt)
        with self.assertRaisesRegex(manager.ManagerError, "missing context"):
            manager.normalize_plan(
                json.dumps(model_plan(context_links={
                    "canon": ["missing.md"], "code": ["app.py"],
                    "tests": ["tests/test_app.py"],
                })), run_id="MF-LOCAL-1", repository=self.repository,
            )

    def test_submit_persists_request_and_validated_plan_without_codex(self):
        calls = []

        def generator(task, **kwargs):
            calls.append((task, kwargs))
            return json.dumps(model_plan())

        state = manager.submit(
            "Owner task", workspace=self.workspace,
            generator=generator, initialize=False,
        )
        self.assertEqual("PLAN_READY", state["state"])
        self.assertEqual(1, len(calls))
        job_dir = Path(state["plan"]).parent
        request = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
        plan = json.loads((job_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual("Owner task", request["task"])
        self.assertEqual(request["run_id"], plan["run_id"])
        self.assertEqual(0, (job_dir / "plan.json").stat().st_mode & 0o222)


if __name__ == "__main__":
    unittest.main()
