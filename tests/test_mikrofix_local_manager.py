import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


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
        plan = manager.normalize_plan(
            raw, run_id="MF-LOCAL-1", repository=self.repository,
            owner_request="Must preserve requirement A and must not change tests",
        )
        self.assertEqual("MF-LOCAL-1", plan["run_id"])
        self.assertEqual(self.git("rev-parse", "HEAD"), plan["base_checkpoint"])
        self.assertEqual(3, plan["max_retries_per_atom"])
        self.assertEqual("AWAIT_CODEX_REVIEW", plan["final_gate"])
        self.assertIn(
            "Must preserve requirement A and must not change tests",
            plan["acceptance_criteria"][0],
        )
        self.assertIn("Enumerate every explicit requirement", plan["acceptance_criteria"][0])
        self.assertEqual("Regression test passes", plan["acceptance_criteria"][1])

    def test_planner_worker_runs_directly_without_terminal_dependency(self):
        bridge = ["/trusted/python", "/trusted/bridge.py"]
        environment = {"MIKROFIX_BRIDGE_HOME": "/private/runtime"}
        with mock.patch.object(manager, "command", return_value="") as called:
            manager.run_bridge_monitor(
                bridge, environment, self.workspace / "runtime", "MF-2026-0001"
            )
        called.assert_called_once_with(
            bridge + ["monitor", "--once"],
            cwd=manager.PROJECT_ROOT,
            env=environment,
            timeout=1800,
        )

    def test_unknown_fields_and_missing_context_fail_closed(self):
        with self.assertRaisesRegex(manager.ManagerError, "unknown"):
            manager.normalize_plan(
                json.dumps({**model_plan(), "run_id": "MODEL-CONTROLLED"}),
                run_id="MF-LOCAL-1", repository=self.repository,
            )

    def test_parser_accepts_one_structured_object_with_surrounding_text(self):
        raw = "Planner note before\n" + json.dumps(model_plan()) + "\nDone."
        parsed = manager.parse_model_json(raw)
        self.assertEqual("Implement one bounded behavior", parsed["objective"])
        with self.assertRaisesRegex(manager.ManagerError, "exactly one JSON object"):
            manager.parse_model_json(
                json.dumps(model_plan()) + "\n" + json.dumps(model_plan(objective="two"))
            )

    def test_model_checks_are_ignored_and_trusted_checks_are_added(self):
        invalid = model_plan(atoms=[{
            "id": "A1", "task": "Write documentation",
            "checks": [], "expected_files": ["app.py"],
        }])
        plan = manager.normalize_plan(
            json.dumps(invalid), run_id="MF-LOCAL-1",
            repository=self.repository,
        )
        self.assertEqual(
            ["syntax_changed_python", "desktop_unit_tests"],
            plan["atoms"][0]["checks"],
        )
        prompt = manager.planner_prompt("Write documentation", "checks are empty")
        self.assertIn("PREJŠNJI IZHOD JE BIL ZAVRNJEN", prompt)
        self.assertIn("checks are empty", prompt)

    def test_qwen_advises_and_gpt_oss_receives_verified_handoff(self):
        qwen = manager.qwen_planner_prompt("Implement feature", "bad path")
        self.assertIn("Razčleni", qwen)
        self.assertIn("bad path", qwen)
        proposal = json.dumps(model_plan())
        gpt = manager.gpt_validation_prompt(
            "Implement feature", proposal, "abc123", "missing criterion"
        )
        self.assertIn("Qwen ne odloča", gpt)
        self.assertIn("abc123", gpt)
        self.assertIn(proposal, gpt)
        self.assertIn("missing criterion", gpt)

    def test_semantic_feedback_reports_all_shape_and_context_errors(self):
        invalid = model_plan(
            constraints="not a list",
            acceptance_criteria="not a list",
            context_links={
                "canon": ["missing-canon.md"],
                "code": ["app.py"],
                "tests": ["missing-test.py"],
            },
        )
        with self.assertRaises(manager.ManagerError) as raised:
            manager.normalize_plan(
                json.dumps(invalid), run_id="MF-LOCAL-1",
                repository=self.repository,
            )
        message = str(raised.exception)
        self.assertIn("constraints must be a non-empty list", message)
        self.assertIn("acceptance_criteria must be a non-empty list", message)
        trusted = manager.normalize_plan(
            json.dumps(model_plan(context_links={
                "canon": ["missing.md"], "code": ["missing.py"],
                "tests": ["missing-test.py"],
            })), run_id="MF-LOCAL-1", repository=self.repository,
        )
        self.assertEqual(["AGENTS.md"], trusted["context_links"]["canon"])
        self.assertEqual(["tests/test_app.py"], trusted["context_links"]["tests"])

    def test_tool_names_cannot_be_smuggled_in_as_allowed_files(self):
        invalid = model_plan(
            allowed_files=["project_status"],
            atoms=[{
                "id": "A1", "task": "Inspect status",
                "expected_files": ["project_status"],
            }],
        )
        with self.assertRaisesRegex(manager.ManagerError, "tool names"):
            manager.normalize_plan(
                json.dumps(invalid), run_id="MF-LOCAL-1",
                repository=self.repository,
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
        self.assertEqual("Owner task", plan["owner_request"])
        self.assertEqual(0, (job_dir / "plan.json").stat().st_mode & 0o222)

    def test_follow_up_requires_existing_parent_and_persists_link(self):
        with self.assertRaisesRegex(manager.ManagerError, "Parent run does not exist"):
            manager.submit(
                "Clarification", workspace=self.workspace,
                generator=lambda *_args, **_kwargs: json.dumps(model_plan()),
                initialize=False, parent_run_id="MF-MISSING",
            )
        parent = self.workspace / "jobs" / "MF-PARENT"
        parent.mkdir()
        (parent / "request.json").write_text("{}\n", encoding="utf-8")
        state = manager.submit(
            "Clarification", workspace=self.workspace,
            generator=lambda *_args, **_kwargs: json.dumps(model_plan()),
            initialize=False, parent_run_id="MF-PARENT",
        )
        request = json.loads(
            (Path(state["plan"]).parent / "request.json").read_text(encoding="utf-8")
        )
        self.assertEqual("MF-PARENT", request["parent_run_id"])

    def test_execute_run_reaches_review_gate_and_stops_workers(self):
        run_id = "MF-READY"
        job_dir = self.workspace / "jobs" / run_id
        job_dir.mkdir()
        supervisor_state = job_dir / "supervisor.json"
        supervisor_state.write_text(json.dumps({
            "stage": "PLANNED", "checkpoint": None, "feedback": "",
        }), encoding="utf-8")
        (job_dir / "manager.json").write_text(json.dumps({
            "schema_version": 1, "run_id": run_id,
            "state": "READY_FOR_OWNER_LEASE",
            "supervisor_state": str(supervisor_state),
        }), encoding="utf-8")
        calls = []

        def fake_command(args, **_kwargs):
            calls.append(args)
            if "resume" in args:
                supervisor_state.write_text(json.dumps({
                    "stage": "AWAIT_CODEX_REVIEW",
                    "checkpoint": "/candidate/manifest.json", "feedback": "",
                }), encoding="utf-8")
            return "ok"

        with mock.patch.object(manager, "command", side_effect=fake_command):
            state = manager.execute_run(run_id, workspace=self.workspace)
        self.assertEqual("READY_FOR_CODEX_REVIEW", state["state"])
        self.assertEqual("/candidate/manifest.json", state["checkpoint"])
        self.assertTrue(any("grant-lease" in call for call in calls))
        self.assertTrue(any("stop" in call for call in calls))

    def test_owner_prompt_timeout_keeps_validated_plan_retryable(self):
        run_id = "MF-LEASE-TIMEOUT"
        job_dir = self.workspace / "jobs" / run_id
        job_dir.mkdir()
        supervisor_state = job_dir / "supervisor.json"
        supervisor_state.write_text(json.dumps({
            "stage": "PLANNED", "checkpoint": None, "feedback": "",
        }), encoding="utf-8")
        manager_file = job_dir / "manager.json"
        manager_file.write_text(json.dumps({
            "schema_version": 1, "run_id": run_id,
            "state": "READY_FOR_OWNER_LEASE",
            "supervisor_state": str(supervisor_state),
        }), encoding="utf-8")

        def fake_command(args, **_kwargs):
            if "grant-lease" in args:
                raise subprocess.TimeoutExpired(args, 120)
            return "ok"

        with (
            mock.patch.object(manager, "command", side_effect=fake_command),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            manager.execute_run(run_id, workspace=self.workspace)
        state = json.loads(manager_file.read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_OWNER_LEASE", state["state"])


if __name__ == "__main__":
    unittest.main()
