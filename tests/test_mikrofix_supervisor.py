import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_supervisor.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_supervisor", MODULE_PATH)
supervisor_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = supervisor_module
SPEC.loader.exec_module(supervisor_module)


GateResult = supervisor_module.GateResult
LocalAdapter = supervisor_module.LocalAdapter
RunEnvironment = supervisor_module.RunEnvironment
Stage = supervisor_module.Stage
Supervisor = supervisor_module.Supervisor
atomic_write_json = supervisor_module.atomic_write_json
initial_run = supervisor_module.initial_run
validate_plan = supervisor_module.validate_plan


def plan(**overrides):
    value = {
        "run_id": "MF-RUN-TEST",
        "objective": "Prepare one bounded change",
        "base_checkpoint": "abc123",
        "allowed_files": ["app.py", "tests/test_app.py"],
        "atoms": [{
            "id": "A1", "task": "Change the greeting",
            "checks": ["local_agent_tests"], "expected_files": ["app.py"],
        }],
        "max_retries_per_atom": 3,
        "final_gate": "AWAIT_CODEX_REVIEW",
    }
    value.update(overrides)
    return value


class FakeAdapter:
    def __init__(self):
        self.lease = True
        self.files = {}
        self.jobs = []
        self.job_results = [("completed", "implemented")]
        self.tests = GateResult(True, "ok", "tests passed")
        self.reviews = [GateResult(True, "approved", "VERDICT: APPROVE")]
        self.checkpoint_calls = 0

    def lease_active(self):
        return self.lease

    def snapshot(self):
        return dict(self.files)

    def dispatch_qwen(self, prompt):
        self.jobs.append(prompt)
        return f"JOB-{len(self.jobs)}"

    def qwen_result(self, job_id):
        status, result = (
            self.job_results.pop(0)
            if self.job_results
            else ("completed", "implemented after gate feedback")
        )
        if status == "completed":
            self.files["app.py"] = f"changed-{job_id}"
        return status, result

    def diff(self, paths):
        return "diff --git a/app.py b/app.py\n+changed\n" if "app.py" in paths else ""

    def run_tests(self, checks):
        return self.tests

    def review(self, prompt, material):
        return self.reviews.pop(0)

    def checkpoint(self, run, diff):
        self.checkpoint_calls += 1
        return "/state/artifacts/MF-RUN-TEST/manifest.json"


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "run.json"
        self.adapter = FakeAdapter()
        atomic_write_json(self.state, initial_run(plan(), self.adapter.snapshot()))
        self.supervisor = Supervisor(self.state, self.adapter)

    def tearDown(self):
        self.temp.cleanup()

    def run_until_terminal(self, limit=30):
        for _ in range(limit):
            progressed = self.supervisor.step()
            state = self.supervisor.load()
            if state["stage"] in {Stage.AWAIT_CODEX_REVIEW.value, Stage.BLOCKED.value}:
                return state
            self.assertTrue(progressed)
        self.fail("state machine did not terminate")

    def test_happy_path_reaches_human_gate_without_merging(self):
        state = self.run_until_terminal()
        self.assertEqual(Stage.AWAIT_CODEX_REVIEW.value, state["stage"])
        self.assertEqual(["A1"], [item["id"] for item in state["completed_atoms"]])
        self.assertEqual(1, self.adapter.checkpoint_calls)
        stages = [item["stage"] for item in state["events"]]
        for required in ("PLANNED", "DISPATCHED_TO_QWEN", "DIFF_VERIFICATION",
                         "LOCAL_TESTS", "GPT_OSS_REVIEW", "CHECKPOINT",
                         "AWAIT_CODEX_REVIEW"):
            self.assertIn(required, stages)

    def test_missing_lease_pauses_and_resumes(self):
        self.adapter.lease = False
        self.assertTrue(self.supervisor.step())
        self.assertEqual(Stage.NEEDS_WRITE_LEASE.value, self.supervisor.load()["stage"])
        self.assertFalse(self.supervisor.step())
        self.assertEqual([], self.adapter.jobs)
        self.adapter.lease = True
        self.assertTrue(self.supervisor.step())
        self.assertEqual(Stage.PLANNED.value, self.supervisor.load()["stage"])

    def test_out_of_scope_change_blocks_immediately(self):
        self.supervisor.step()
        self.supervisor.step()
        self.adapter.files["secrets.txt"] = "unexpected"
        self.supervisor.step()
        state = self.supervisor.load()
        self.assertEqual(Stage.BLOCKED.value, state["stage"])
        self.assertIn("outside", state["feedback"].lower())

    def test_retries_are_bounded_at_three(self):
        self.adapter.job_results = [("completed", "claimed success")] * 3

        def no_change(_job_id):
            return self.adapter.job_results.pop(0)

        self.adapter.qwen_result = no_change
        state = self.run_until_terminal()
        self.assertEqual(Stage.BLOCKED.value, state["stage"])
        self.assertEqual(3, state["attempt"])
        self.assertEqual(3, len(self.adapter.jobs))

    def test_review_rejection_is_fed_to_next_attempt(self):
        self.adapter.reviews = [
            GateResult(False, "missing regression", "VERDICT: REJECT"),
            GateResult(True, "approved", "VERDICT: APPROVE"),
        ]
        state = self.run_until_terminal()
        self.assertEqual(Stage.AWAIT_CODEX_REVIEW.value, state["stage"])
        self.assertEqual(2, len(self.adapter.jobs))
        self.assertIn("missing regression", self.adapter.jobs[1])

    def test_active_job_timeout_blocks_without_dispatching_a_retry(self):
        timeout_plan = plan(job_timeout_seconds=30)
        atomic_write_json(
            self.state, initial_run(timeout_plan, self.adapter.snapshot())
        )
        self.adapter.job_results = [("running", "")]
        self.assertTrue(self.supervisor.step())
        state = self.supervisor.load()
        state["job_dispatched_at"] = time.time() - 31
        atomic_write_json(self.state, state)

        self.assertTrue(self.supervisor.step())
        state = self.supervisor.load()
        self.assertEqual(Stage.BLOCKED.value, state["stage"])
        self.assertIn("worker must be inspected", state["feedback"])
        self.assertEqual(1, len(self.adapter.jobs))


class PlanValidationTests(unittest.TestCase):
    def test_rejects_path_escape(self):
        with self.assertRaises(ValueError):
            validate_plan(plan(allowed_files=["../outside.py"]))

    def test_rejects_git_internal_path(self):
        with self.assertRaises(ValueError):
            validate_plan(plan(allowed_files=[".git/config"]))

    def test_rejects_more_than_three_retries(self):
        with self.assertRaises(ValueError):
            validate_plan(plan(max_retries_per_atom=4))

    def test_rejects_unknown_check_profile(self):
        invalid = plan()
        invalid["atoms"][0]["checks"] = ["arbitrary_shell"]
        with self.assertRaises(ValueError):
            validate_plan(invalid)

    def test_rejects_control_plane_paths(self):
        for protected in (
            "tools/mikrofix_supervisor.py",
            "tools/mikrofix_bridge.py",
            "tools/local_executor/server.py",
            "tools/mikrofix_local_agent.py",
            "docs/local_agents/izvajalec/QWEN_IZVAJALEC_SYSTEM.md",
            "docs/local_agents/pravila/00_JEDRO.md",
            "docs/local_agents/LEKCIJE.md",
        ):
            with self.subTest(protected=protected), self.assertRaises(ValueError):
                validate_plan(plan(allowed_files=[protected]))


class RunEnvironmentTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ).stdout.strip()

    def add_server_python(self, source):
        interpreter = source / ".venv-server" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o755)
        return interpreter

    def add_trusted_agent_tools(self, source):
        tools = source / "tools"
        tools.mkdir(exist_ok=True)
        bridge = tools / "mikrofix_bridge.py"
        reviewer = tools / "mikrofix_local_agent.py"
        bridge.write_text("# trusted bridge\n", encoding="utf-8")
        reviewer.write_text("# trusted reviewer\n", encoding="utf-8")
        return bridge, reviewer

    def test_prepares_isolated_worktree_config_manifest_and_lease_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            state_dir = base / "state"
            source.mkdir()
            self.git(source, "init", "-q")
            self.git(source, "config", "user.email", "test@example.invalid")
            self.git(source, "config", "user.name", "Supervisor Test")
            (source / "app.py").write_text("print('main')\n", encoding="utf-8")
            server_python = self.add_server_python(source)
            trusted_bridge, _ = self.add_trusted_agent_tools(source)
            self.git(source, "add", "app.py", ".venv-server/bin/python", "tools")
            self.git(source, "commit", "-qm", "base")
            head = self.git(source, "rev-parse", "HEAD")
            before_status = self.git(source, "status", "--porcelain=v1")
            test_plan = plan(base_checkpoint=head)
            environment = RunEnvironment.create(
                source,
                state_dir,
                test_plan,
                {
                    "ollama_url": "http://127.0.0.1:11434/api/chat",
                    "model": "fake-model",
                    "num_ctx": 65536,
                    "max_steps": 12,
                },
            )

            self.assertEqual(state_dir / "worktrees" / "MF-RUN-TEST",
                             environment.worktree)
            self.assertEqual(head, self.git(environment.worktree, "rev-parse", "HEAD"))
            self.assertEqual(before_status, self.git(source, "status", "--porcelain=v1"))
            self.assertEqual("print('main')\n",
                             (source / "app.py").read_text(encoding="utf-8"))
            (environment.worktree / "app.py").write_text(
                "print('isolated')\n", encoding="utf-8"
            )
            self.assertEqual("print('main')\n",
                             (source / "app.py").read_text(encoding="utf-8"))

            bridge = json.loads(environment.bridge_config.read_text(encoding="utf-8"))
            self.assertEqual(str(environment.worktree), bridge["project"])
            self.assertEqual("fake-model", bridge["model"])
            self.assertEqual(str(environment.executor_config_dir / "token"),
                             bridge["token_file"])
            allowed = json.loads(
                environment.allowed_files_manifest.read_text(encoding="utf-8")
            )
            self.assertEqual(test_plan["allowed_files"], allowed["allowed_files"])
            self.assertEqual(0, os.stat(environment.immutable_plan).st_mode & 0o222)
            self.assertEqual(0, os.stat(environment.allowed_files_manifest).st_mode & 0o222)

            metadata = json.loads(environment.metadata_file.read_text(encoding="utf-8"))
            self.assertEqual(str(environment.worktree), metadata["bridge"]["cwd"])
            self.assertEqual(str(environment.worktree), metadata["executor"]["cwd"])
            self.assertEqual(str(server_python), metadata["executor"]["argv"][0])
            self.assertEqual(str(trusted_bridge), metadata["bridge"]["argv"][1])
            executor_env = metadata["executor"]["env"]
            self.assertEqual(str(environment.worktree),
                             executor_env["MIKROFIX_EXECUTOR_ROOT"])
            self.assertEqual(str(environment.allowed_files_manifest),
                             executor_env["MIKROFIX_EXECUTOR_ALLOWED_FILES"])
            self.assertEqual("MF-RUN-TEST", executor_env["MIKROFIX_EXECUTOR_RUN_ID"])
            self.assertEqual(environment.plan_sha256,
                             executor_env["MIKROFIX_EXECUTOR_PLAN_SHA256"])
            self.assertEqual(environment.allowed_sha256,
                             executor_env["MIKROFIX_EXECUTOR_ALLOWED_FILES_SHA256"])
            self.assertEqual(
                environment.executor_state_dir / "executor-lease.json",
                environment.lease_file,
            )
            self.assertFalse(environment.lease_file.exists())

            adapter = LocalAdapter(
                environment.worktree, state_dir,
                lease_file=environment.lease_file,
                bridge_env=environment.bridge_env(), source_root=source,
                expected_lease=environment.lease_identity(),
            )
            self.assertFalse(adapter.lease_active())
            environment.lease_file.write_text(json.dumps({
                "run_id": "MF-RUN-TEST", "expires_at": time.time() + 60,
            }), encoding="utf-8")
            self.assertFalse(adapter.lease_active())
            lease = environment.lease_identity()
            lease["expires_at"] = time.time() + 60
            environment.lease_file.write_text(json.dumps(lease), encoding="utf-8")
            self.assertTrue(adapter.lease_active())

    def test_tampered_immutable_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            self.git(source, "init", "-q")
            self.git(source, "config", "user.email", "test@example.invalid")
            self.git(source, "config", "user.name", "Supervisor Test")
            (source / "app.py").write_text("pass\n", encoding="utf-8")
            self.add_server_python(source)
            self.add_trusted_agent_tools(source)
            self.git(source, "add", "app.py", ".venv-server/bin/python", "tools")
            self.git(source, "commit", "-qm", "base")
            environment = RunEnvironment.create(
                source, base / "state",
                plan(base_checkpoint=self.git(source, "rev-parse", "HEAD")), {},
            )
            os.chmod(environment.allowed_files_manifest, 0o600)
            environment.allowed_files_manifest.write_text(
                '{"allowed_files": ["evil.py"]}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "manifest changed"):
                RunEnvironment.from_dict(environment.to_dict())

    def test_missing_server_interpreter_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            self.git(source, "init", "-q")
            self.git(source, "config", "user.email", "test@example.invalid")
            self.git(source, "config", "user.name", "Supervisor Test")
            (source / "app.py").write_text("pass\n", encoding="utf-8")
            self.git(source, "add", "app.py")
            self.git(source, "commit", "-qm", "base")
            with self.assertRaisesRegex(RuntimeError, "interpreter is missing"):
                RunEnvironment.create(
                    source, base / "state",
                    plan(base_checkpoint=self.git(source, "rev-parse", "HEAD")), {},
                )


class LocalAdapterTests(unittest.TestCase):
    def test_bridge_and_reviewer_use_absolute_trusted_source_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "trusted-source"
            worktree = base / "worktree"
            state = base / "state"
            worktree.mkdir()
            adapter = LocalAdapter(worktree, state, source_root=source)
            calls = []

            def fake_run(args, timeout=900, check=True, env=None):
                calls.append(list(args))
                output = "JOB-1\n" if "dispatch" in args else "VERDICT: APPROVE\n"
                return subprocess.CompletedProcess(args, 0, output, "")

            adapter._run = fake_run
            self.assertEqual("JOB-1", adapter.dispatch_qwen("bounded task"))
            review = adapter.review("review it", "diff")
            self.assertTrue(review.ok)
            python = str(source / ".venv-server" / "bin" / "python")
            self.assertEqual(
                [python, str(source / "tools" / "mikrofix_bridge.py")],
                calls[0][:2],
            )
            self.assertEqual(
                [python, str(source / "tools" / "mikrofix_local_agent.py")],
                calls[1][:2],
            )

    def test_syntax_check_includes_unstaged_staged_and_untracked_python(self):
        adapter = LocalAdapter(Path("/worktree"), Path("/state"),
                               source_root=Path("/source"))
        calls = []

        def fake_run(args, timeout=900, check=True, env=None):
            calls.append(list(args))
            if args[:2] == ["git", "diff"] and "--cached" not in args:
                output = "modified.py\n"
            elif args[:3] == ["git", "diff", "--cached"]:
                output = "staged.py\n"
            elif args[:3] == ["git", "ls-files", "--others"]:
                output = "new.py\n"
            else:
                output = "syntax ok\n"
            return subprocess.CompletedProcess(args, 0, output, "")

        adapter._run = fake_run
        result = adapter.run_tests(["syntax_changed_python"])

        self.assertTrue(result.ok)
        self.assertEqual(
            [str(Path("/source/.venv/bin/python")), "-m", "py_compile",
             "modified.py", "new.py", "staged.py"],
            calls[-1],
        )
        self.assertIn(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM",
             "--", "*.py"],
            calls,
        )
        self.assertIn(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
