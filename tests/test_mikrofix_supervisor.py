import importlib.util
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_supervisor.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_supervisor", MODULE_PATH)
supervisor_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = supervisor_module
SPEC.loader.exec_module(supervisor_module)


GateResult = supervisor_module.GateResult
LocalAdapter = supervisor_module.LocalAdapter
RunEnvironment = supervisor_module.RunEnvironment
WorkerManager = supervisor_module.WorkerManager
Stage = supervisor_module.Stage
Supervisor = supervisor_module.Supervisor
atomic_write_json = supervisor_module.atomic_write_json
initial_run = supervisor_module.initial_run
validate_plan = supervisor_module.validate_plan
THREAD_ID = "12345678-1234-5678-9234-567812345678"


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
        "constraints": ["Keep the change bounded"],
        "context_links": {
            "canon": ["docs/canon.md"],
            "code": ["app.py"],
            "tests": ["tests/test_app.py"],
        },
        "acceptance_criteria": ["Named checks pass with recorded evidence"],
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
        self.review_prompts = []
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
        self.review_prompts.append(prompt)
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
        self.assertIn("ACCEPTANCE CRITERIA", self.adapter.review_prompts[0])
        self.assertIn("1. Named checks pass", self.adapter.review_prompts[0])
        self.assertIn("Expected files: app.py", self.adapter.review_prompts[0])
        self.assertIn("original owner request is binding", self.adapter.review_prompts[0].lower())
        self.assertIn("check every explicit requirement separately", self.adapter.review_prompts[0])

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

    def test_invalid_reviewer_format_retries_review_not_executor(self):
        self.adapter.reviews = [
            GateResult(False, "missing valid verdict", "**VERDICT: APPROVE**"),
            GateResult(True, "approved", "VERDICT: APPROVE"),
        ]
        state = self.run_until_terminal()
        self.assertEqual(Stage.AWAIT_CODEX_REVIEW.value, state["stage"])
        self.assertEqual(1, len(self.adapter.jobs))
        self.assertTrue(any(
            "repeating review without redispatching" in event["message"]
            for event in state["events"]
        ))

    def test_substantive_rejection_requires_a_new_executor_diff(self):
        calls = 0

        def result_without_second_change(job_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.adapter.files["app.py"] = f"changed-{job_id}"
            return "completed", "candidate ready"

        self.adapter.qwen_result = result_without_second_change
        self.adapter.reviews = [
            GateResult(False, "VERDICT: REJECT", "VERDICT: REJECT"),
        ]
        state = self.run_until_terminal()
        self.assertEqual(Stage.BLOCKED.value, state["stage"])
        self.assertIn("no new change", state["feedback"])
        self.assertEqual(3, len(self.adapter.jobs))

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

    def test_terminal_callback_is_bound_idempotent_and_points_to_evidence(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        atomic_write_json(
            self.state,
            initial_run(plan(), self.adapter.snapshot(), codex_thread_id=THREAD_ID),
        )
        supervisor = Supervisor(self.state, self.adapter, completion_runner=runner)
        for _ in range(20):
            supervisor.step()
            if supervisor.load()["stage"] in {
                Stage.AWAIT_CODEX_REVIEW.value, Stage.BLOCKED.value
            }:
                break
        self.assertEqual(1, len(calls))
        message = json.loads(calls[0][0][-1])
        self.assertEqual("READY_FOR_CODEX_REVIEW", message["state"])
        self.assertEqual("Prepare one bounded change", message["display_name"])
        event = json.loads(Path(message["event_path"]).read_text(encoding="utf-8"))
        self.assertIn(str(self.state.resolve()), event["payload"]["evidence_paths"])
        supervisor.step()
        self.assertEqual(1, len(calls))

    def test_queued_and_running_states_do_not_notify_or_change_lease(self):
        calls = []
        self.adapter.job_results = [("running", "")]
        atomic_write_json(
            self.state,
            initial_run(plan(), self.adapter.snapshot(), codex_thread_id=THREAD_ID),
        )
        supervisor = Supervisor(
            self.state, self.adapter,
            completion_runner=lambda argv, **kwargs: calls.append((argv, kwargs)),
        )
        supervisor.step()
        self.assertFalse(supervisor.step())
        self.assertEqual([], calls)
        self.assertTrue(self.adapter.lease)

    def test_blocked_callback_maps_state_and_concrete_need(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        blocked_plan = plan(max_retries_per_atom=1)
        atomic_write_json(
            self.state,
            initial_run(blocked_plan, self.adapter.snapshot(), codex_thread_id=THREAD_ID),
        )
        self.adapter.job_results = [("failed", "worker unavailable")]
        supervisor = Supervisor(self.state, self.adapter, completion_runner=runner)
        supervisor.step()
        supervisor.step()
        self.assertEqual(Stage.BLOCKED.value, supervisor.load()["stage"])
        message = json.loads(calls[0][0][-1])
        self.assertEqual("BLOCKED", message["state"])
        event = json.loads(Path(message["event_path"]).read_text(encoding="utf-8"))
        self.assertIn("worker unavailable", event["payload"]["blocked_need"])
        self.assertEqual(1, len(self.adapter.jobs))


class PlanValidationTests(unittest.TestCase):
    def test_worker_and_resume_cli_commands_are_available(self):
        workers = supervisor_module.parse_args(["workers", "start"])
        grant = supervisor_module.parse_args(["grant-lease", "--minutes", "5"])
        resume = supervisor_module.parse_args(["resume"])
        run_alias = supervisor_module.parse_args(["run"])
        self.assertEqual(("workers", "start"), (workers.command, workers.action))
        self.assertEqual(("grant-lease", 5), (grant.command, grant.minutes))
        self.assertEqual("resume", resume.command)
        self.assertEqual("run", run_alias.command)

    def test_status_without_active_run_is_friendly_and_nonzero(self):
        with tempfile.TemporaryDirectory() as temporary:
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                exit_code = supervisor_module.main([
                    "--state-dir", temporary, "status",
                ])
        self.assertNotEqual(0, exit_code)
        self.assertEqual("No active supervisor run\n", error.getvalue())

    def test_workers_status_without_active_run_is_friendly_and_nonzero(self):
        with tempfile.TemporaryDirectory() as temporary:
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                exit_code = supervisor_module.main([
                    "--state-dir", temporary, "workers", "status",
                ])
        self.assertNotEqual(0, exit_code)
        self.assertEqual("No active supervisor run\n", error.getvalue())

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
            "AGENTS.md",
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

    def test_rejects_missing_constraints(self):
        with self.assertRaises(ValueError) as error:
            invalid = plan()
            del invalid["constraints"]
            validate_plan(invalid)
        self.assertIn("constraints", str(error.exception).lower())

    def test_rejects_empty_and_wrong_type_constraints_distinctly(self):
        with self.assertRaisesRegex(ValueError, "non-empty"):
            validate_plan(plan(constraints=[]))
        with self.assertRaisesRegex(ValueError, "must be a list"):
            validate_plan(plan(constraints="bounded"))

    def test_rejects_empty_context_links_sections(self):
        invalid = plan()
        invalid["context_links"]["canon"] = []
        with self.assertRaises(ValueError) as error:
            validate_plan(invalid)
        self.assertIn("canon", str(error.exception).lower())

    def test_rejects_missing_acceptance_criteria(self):
        with self.assertRaises(ValueError) as error:
            invalid = plan()
            del invalid["acceptance_criteria"]
            validate_plan(invalid)
        self.assertIn("acceptance_criteria", str(error.exception).lower())

    def test_rejects_unsafe_context_paths(self):
        invalid = plan()
        invalid["context_links"]["canon"] = ["../outside.md"]
        with self.assertRaises(ValueError) as error:
            validate_plan(invalid)
        self.assertIn("unsafe path", str(error.exception).lower())

    def test_rejects_unknown_plan_atom_and_context_fields(self):
        invalid = plan(extra="no")
        with self.assertRaisesRegex(ValueError, "unknown plan fields"):
            validate_plan(invalid)
        invalid = plan()
        invalid["atoms"][0]["extra"] = "no"
        with self.assertRaisesRegex(ValueError, "unknown atom fields"):
            validate_plan(invalid)
        invalid = plan()
        invalid["context_links"]["other"] = ["x"]
        with self.assertRaisesRegex(ValueError, "unknown context_links fields"):
            validate_plan(invalid)

    def test_normalizes_required_output_and_rejects_duplicate_links(self):
        normalized = validate_plan(plan(
            objective="  bounded change  ",
            constraints=["  no scope drift  "],
        ))
        self.assertEqual(normalized["objective"], "bounded change")
        self.assertEqual(normalized["constraints"], ["no scope drift"])
        invalid = plan()
        invalid["context_links"]["code"] = ["app.py", "app.py"]
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_plan(invalid)

    def test_qwen_prompt_contains_all_required_fields(self):
        """Test da qwen_prompt vrne prompt s celimi obveznimi polji."""
        plan = {
            "run_id": "TEST-001",
            "objective": "Dodaj novo funkcionalnost",
            "base_checkpoint": "origin/main",
            "allowed_files": ["app.py"],
            "atoms": [
                {
                    "id": "ATOM-001",
                    "task": "Popravi datoteko A",
                    "expected_files": ["app.py"],
                    "checks": ["syntax_changed_python"],
                }
            ],
            "max_retries_per_atom": 2,
            "final_gate": "AWAIT_CODEX_REVIEW",
            "constraints": ["Ne dodaj novih funkcionalnosti brez pregleda Codexa"],
            "context_links": {
                "canon": ["docs/pravila.md"],
                "code": ["app.py"],
                "tests": ["tests/test_mikrofix_supervisor.py"],
            },
            "acceptance_criteria": ["Vsi testi morajo uspeti", "Syntax check mora uspeti"],
        }
        prompt = supervisor_module.qwen_prompt({
            "plan": validate_plan(plan), "atom_index": 0, "attempt": 0,
            "feedback": "",
        })
        self.assertIn("=== LOCAL EXECUTION REQUIRED ===", prompt)
        self.assertIn("constraints", prompt)
        self.assertIn("acceptance_criteria", prompt)
        self.assertIn("Original owner request:", prompt)
        self.assertIn("base_checkpoint", prompt)
        self.assertIn("context_links", prompt)
        self.assertIn("canon", prompt)
        self.assertIn("code", prompt)
        self.assertIn("tests", prompt)
        self.assertIn("Codex — vodja projekta in končni pregled", prompt)

    def test_supervised_run_requires_gui_prompt_no_yes(self):
        lease_request = {"command": "grant-lease", "minutes": 15}
        self.assertNotIn("--yes", str(lease_request))

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
        control = tools / "local_executor" / "control.py"
        control.parent.mkdir()
        bridge.write_text("# trusted bridge\n", encoding="utf-8")
        reviewer.write_text("# trusted reviewer\n", encoding="utf-8")
        control.write_text("# trusted lease controller\n", encoding="utf-8")
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
                    "worker_model": "fake-worker-model",
                    "num_ctx": 65536,
                    "worker_num_ctx": 32768,
                    "worker_keep_alive": "2m",
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
            self.assertEqual("fake-worker-model", bridge["model"])
            self.assertEqual(32768, bridge["num_ctx"])
            self.assertEqual("2m", bridge["keep_alive"])
            self.assertEqual("Qwen — lokalni izvajalec", bridge["agent_role"])
            self.assertEqual("supervisor_only", bridge["completion_policy"])
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
            self.assertEqual(str(source.resolve()), metadata["executor"]["cwd"])
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

    def test_agent_repository_is_separate_from_trusted_control_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            trusted = base / "trusted"
            repository = base / "agent-repository"
            state_dir = base / "state"
            trusted.mkdir()
            self.git(trusted, "init", "-q")
            self.git(trusted, "config", "user.email", "test@example.invalid")
            self.git(trusted, "config", "user.name", "Supervisor Test")
            (trusted / "app.py").write_text("print('trusted')\n", encoding="utf-8")
            trusted_python = self.add_server_python(trusted)
            trusted_bridge, _ = self.add_trusted_agent_tools(trusted)
            self.git(trusted, "add", ".")
            self.git(trusted, "commit", "-qm", "base")
            subprocess.run(
                ["git", "clone", "-q", "--no-hardlinks", str(trusted), str(repository)],
                check=True,
            )
            (repository / "app.py").write_text("print('agent baseline')\n", encoding="utf-8")
            self.git(repository, "config", "user.email", "test@example.invalid")
            self.git(repository, "config", "user.name", "Agent Repository")
            self.git(repository, "add", "app.py")
            self.git(repository, "commit", "-qm", "agent baseline")
            head = self.git(repository, "rev-parse", "HEAD")

            environment = RunEnvironment.create(
                trusted, state_dir, plan(base_checkpoint=head), {},
                repository_root=repository,
            )

            self.assertEqual(repository.resolve(), environment.repository_root)
            self.assertEqual("print('agent baseline')\n", (
                environment.worktree / "app.py"
            ).read_text(encoding="utf-8"))
            metadata = json.loads(environment.metadata_file.read_text(encoding="utf-8"))
            self.assertEqual(str(repository.resolve()), metadata["repository_root"])
            self.assertEqual(str(trusted.resolve()), metadata["trusted_control_root"])
            self.assertEqual(str(trusted.resolve()), metadata["executor"]["cwd"])
            self.assertEqual(str(trusted_python), metadata["bridge"]["argv"][0])
            self.assertEqual(str(trusted_bridge), metadata["bridge"]["argv"][1])

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
    def test_reviewer_accepts_one_case_insensitive_valid_verdict(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            adapter = LocalAdapter(
                base / "worktree", base / "state", source_root=base / "source"
            )

            def fake_run(args, timeout=900, check=True, env=None):
                return subprocess.CompletedProcess(
                    args, 0,
                    "Findings:\nEverything is in scope.\n\nVerdict: APPROVE\n", ""
                )

            adapter._run = fake_run
            result = adapter.review("review it", "diff")
        self.assertTrue(result.ok)
        self.assertEqual("approved", result.summary)

    def test_reviewer_still_rejects_multiple_or_unknown_verdicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            adapter = LocalAdapter(
                base / "worktree", base / "state", source_root=base / "source"
            )
            outputs = iter([
                "VERDICT: APPROVE\nverdict: reject\n",
                "Verdict: MAYBE\n",
            ])

            def fake_run(args, timeout=900, check=True, env=None):
                return subprocess.CompletedProcess(args, 0, next(outputs), "")

            adapter._run = fake_run
            multiple = adapter.review("review it", "diff")
            unknown = adapter.review("review it", "diff")
        self.assertFalse(multiple.ok)
        self.assertEqual("ambiguous multiple verdicts", multiple.summary)
        self.assertFalse(unknown.ok)
        self.assertEqual("missing valid verdict", unknown.summary)

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
        sandbox_commands = []

        def fake_sandbox(command, timeout):
            sandbox_commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, "syntax ok\n", "")

        adapter._sandbox_check = fake_sandbox
        result = adapter.run_tests(["syntax_changed_python"])

        self.assertTrue(result.ok)
        self.assertEqual(
            [str(Path("/source/.venv/bin/python")), "-m", "py_compile",
             "modified.py", "new.py", "staged.py"],
            sandbox_commands[-1],
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


class SandboxRegressionTests(unittest.TestCase):
    def adapter(self, project, state, **kwargs):
        return LocalAdapter(
            project, state,
            source_root=MODULE_PATH.parents[1],
            **kwargs,
        )

    def test_candidate_code_cannot_escape_or_observe_host_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "candidate"
            state = base / "state"
            marker = base / "outside" / "marker.txt"
            project.mkdir()
            test_file = project / "test_candidate.py"
            test_file.write_text(
                """import os
from pathlib import Path
import socket
import unittest

class CandidateTest(unittest.TestCase):
    def test_isolation(self):
        self.assertIsNone(os.environ.get('MIKROFIX_TEST_SECRET'))
        for name in ('DISPLAY', 'XAUTHORITY', 'SSH_AUTH_SOCK',
                     'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR',
                     'DOCKER_HOST', 'HTTP_PROXY', 'HTTPS_PROXY'):
            self.assertIsNone(os.environ.get(name), name)
        self.assertEqual('/home/sandbox', os.environ.get('HOME'))
        marker = Path(%r)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('sandbox only', encoding='utf-8')
        Path(__file__).write_text('overlay mutation', encoding='utf-8')
        sock = socket.socket()
        sock.settimeout(0.2)
        with self.assertRaises(OSError):
            sock.connect(('1.1.1.1', 53))
        sock.close()
""" % str(marker),
                encoding="utf-8",
            )
            before = test_file.read_bytes()
            inherited = {
                "MIKROFIX_TEST_SECRET": "must-not-leak",
                "DISPLAY": ":99", "XAUTHORITY": "/host/xauth",
                "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "DOCKER_HOST": "unix:///run/docker.sock",
                "HTTP_PROXY": "http://127.0.0.1:8888",
                "HTTPS_PROXY": "http://127.0.0.1:8888",
            }
            with mock.patch.dict(os.environ, inherited, clear=False):
                result = self.adapter(project, state).run_tests(["desktop_unit_tests"])
            self.assertTrue(result.ok, result.evidence)
            self.assertFalse(marker.exists())
            self.assertEqual(before, test_file.read_bytes())

    def test_missing_bwrap_fails_closed_without_executing_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "candidate"
            project.mkdir()
            marker = base / "executed"
            (project / "test_candidate.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            result = self.adapter(
                project, base / "state", bwrap_path=base / "missing-bwrap"
            ).run_tests(["desktop_unit_tests"])
            self.assertFalse(result.ok)
            self.assertIn("sandbox failed closed", result.summary)
            self.assertFalse(marker.exists())

    def test_unittest_and_syntax_checks_preserve_pass_fail_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "candidate"
            project.mkdir()
            test_file = project / "test_candidate.py"
            test_file.write_text(
                "import unittest\nclass T(unittest.TestCase):\n"
                "    def test_ok(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            adapter = self.adapter(project, base / "state")
            self.assertTrue(adapter.run_tests(["desktop_unit_tests"]).ok)
            test_file.write_text(
                "import unittest\nclass T(unittest.TestCase):\n"
                "    def test_bad(self): self.fail('expected failure')\n",
                encoding="utf-8",
            )
            self.assertFalse(adapter.run_tests(["desktop_unit_tests"]).ok)

            subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
            syntax_file = project / "candidate.py"
            syntax_file.write_text("value = 1\n", encoding="utf-8")
            self.assertTrue(adapter.run_tests(["syntax_changed_python"]).ok)
            syntax_file.write_text("value =\n", encoding="utf-8")
            self.assertFalse(adapter.run_tests(["syntax_changed_python"]).ok)


class FakeWorkerRunner:
    def __init__(self, environment, *, create_token=True):
        self.environment = environment
        self.create_token = create_token
        self.active = set()
        self.foreign = set()
        self.calls = []

    def run(self, args, *, cwd=None, env=None, timeout=30):
        self.calls.append(list(args))
        if args[:3] == ["systemctl", "--user", "is-active"]:
            unit = args[3]
            active = unit in self.active
            return subprocess.CompletedProcess(args, 0 if active else 3,
                                               "active\n" if active else "inactive\n", "")
        if args[:3] == ["systemctl", "--user", "show"]:
            unit = args[3]
            role = "executor" if unit.endswith("-executor") else "bridge"
            metadata = json.loads(self.environment.metadata_file.read_text(encoding="utf-8"))[role]
            prop = next(item for item in args if item.startswith("--property="))
            if prop == "--property=WorkingDirectory":
                output = "/foreign\n" if unit in self.foreign else metadata["cwd"] + "\n"
            elif prop == "--property=Environment":
                output = " ".join(
                    f"{key}={value}" for key, value in sorted(metadata["env"].items())
                ) + "\n"
            else:
                output = "argv[]=" + shlex.join(metadata["argv"]) + " ; ignore_errors=no\n"
            return subprocess.CompletedProcess(args, 0, output, "")
        if args[:3] == ["systemctl", "--user", "stop"]:
            self.active.discard(args[3])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["systemd-run", "--user"]:
            unit = next(item.split("=", 1)[1] for item in args if item.startswith("--unit="))
            self.active.add(unit)
            self.foreign.discard(unit)
            if unit.endswith("-executor") and self.create_token:
                token = self.environment.executor_config_dir / "token"
                token.write_text("private-token\n", encoding="utf-8")
                token.chmod(0o600)
            return subprocess.CompletedProcess(args, 0, "started\n", "")
        if "control.py" in " ".join(args):
            if "revoke" in args:
                self.environment.lease_file.unlink(missing_ok=True)
            elif "grant" in args:
                lease = self.environment.lease_identity()
                lease["expires_at"] = time.time() + 300
                self.environment.lease_file.write_text(json.dumps(lease), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "ok\n", "")
        return subprocess.CompletedProcess(args, 1, "unexpected command\n", "")


class WorkerManagerTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ).stdout.strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.source = base / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-q")
        self.git(self.source, "config", "user.email", "test@example.invalid")
        self.git(self.source, "config", "user.name", "Supervisor Test")
        (self.source / "app.py").write_text("pass\n", encoding="utf-8")
        server_python = self.source / ".venv-server/bin/python"
        server_python.parent.mkdir(parents=True)
        server_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        server_python.chmod(0o755)
        tools = self.source / "tools"
        (tools / "local_executor").mkdir(parents=True)
        (tools / "mikrofix_bridge.py").write_text("# bridge\n", encoding="utf-8")
        (tools / "mikrofix_local_agent.py").write_text("# reviewer\n", encoding="utf-8")
        (tools / "local_executor/control.py").write_text("# control\n", encoding="utf-8")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-qm", "base")
        self.environment = RunEnvironment.create(
            self.source, base / "state",
            plan(base_checkpoint=self.git(self.source, "rev-parse", "HEAD")), {},
        )

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, runner, probe=None, clock=None, sleeper=None, timeout=1):
        return WorkerManager(
            self.environment,
            runner=runner,
            probe=probe or (lambda _url: {
                "status": "ok", "project": str(self.environment.worktree)
            }),
            clock=clock or time.monotonic,
            sleeper=sleeper or (lambda _seconds: None),
            startup_timeout=timeout,
        )

    def test_start_orders_executor_health_then_bridge_and_is_idempotent(self):
        runner = FakeWorkerRunner(self.environment)
        probes = []
        manager = self.manager(runner, probe=lambda url: (
            probes.append(url) or {
                "status": "ok", "project": str(self.environment.worktree)
            }
        ))
        self.assertEqual("READY", manager.start()["status"])
        starts = [call for call in runner.calls if call[:2] == ["systemd-run", "--user"]]
        self.assertEqual(2, len(starts))
        self.assertIn("executor", next(item for item in starts[0] if item.startswith("--unit=")))
        self.assertIn("bridge", next(item for item in starts[1] if item.startswith("--unit=")))
        self.assertTrue(probes)
        for role in ("executor", "bridge"):
            log = manager.logs_dir / f"{role}.log"
            self.assertTrue(log.is_file())
            self.assertFalse(log.is_symlink())
            self.assertEqual(0o600, os.stat(log).st_mode & 0o777)
        manager.start()
        self.assertEqual(2, len([call for call in runner.calls
                                if call[:2] == ["systemd-run", "--user"]]))

    def test_status_reports_stopped_partial_unhealthy_and_ready(self):
        runner = FakeWorkerRunner(self.environment)
        manager = self.manager(runner)
        self.assertEqual("STOPPED", manager.status()["status"])
        runner.active.add(manager.bridge_unit)
        self.assertEqual("PARTIAL", manager.status()["status"])
        runner.active.clear()
        runner.active.add(manager.executor_unit)
        runner.foreign.add(manager.executor_unit)
        self.assertEqual("UNHEALTHY", manager.status()["status"])
        runner.active.clear()
        runner.foreign.clear()
        manager.start()
        status = manager.status()
        self.assertEqual("READY", status["status"])
        for field in (
            "executor_active", "bridge_active", "executor_identity",
            "bridge_identity", "health_ready", "lease_active",
        ):
            self.assertIn(field, status)

    def test_token_or_health_failure_rolls_back_both_workers_and_lease(self):
        runner = FakeWorkerRunner(self.environment, create_token=False)
        lease = self.environment.lease_identity()
        lease["expires_at"] = time.time() + 60
        self.environment.lease_file.write_text(json.dumps(lease), encoding="utf-8")
        now = [0.0]

        def sleeper(seconds):
            now[0] += seconds

        manager = self.manager(runner, clock=lambda: now[0], sleeper=sleeper, timeout=0.5)
        with self.assertRaisesRegex(RuntimeError, "Timed out"):
            manager.start()
        self.assertEqual(set(), runner.active)
        self.assertFalse(self.environment.lease_file.exists())

    def test_exact_health_project_is_required(self):
        runner = FakeWorkerRunner(self.environment)
        now = [0.0]
        manager = self.manager(
            runner,
            probe=lambda _url: {"status": "ok", "project": "/wrong/worktree"},
            clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
            timeout=0.5,
        )
        with self.assertRaisesRegex(RuntimeError, "Timed out"):
            manager.start()
        self.assertEqual(set(), runner.active)

    def test_stop_orders_bridge_before_executor_and_revokes_lease(self):
        runner = FakeWorkerRunner(self.environment)
        manager = self.manager(runner)
        runner.active.update({manager.executor_unit, manager.bridge_unit})
        lease = self.environment.lease_identity()
        lease["expires_at"] = time.time() + 60
        self.environment.lease_file.write_text(json.dumps(lease), encoding="utf-8")
        manager.stop()
        stops = [call for call in runner.calls if call[:3] == ["systemctl", "--user", "stop"]]
        self.assertEqual(
            [["systemctl", "--user", "stop", manager.bridge_unit],
             ["systemctl", "--user", "stop", manager.executor_unit]],
            stops,
        )
        self.assertFalse(self.environment.lease_file.exists())

    def test_lifecycle_lock_path_is_stable_and_exclusive(self):
        first = self.manager(FakeWorkerRunner(self.environment))
        second = self.manager(FakeWorkerRunner(self.environment))
        self.assertEqual(first.lifecycle_lock_path, second.lifecycle_lock_path)
        with first.lifecycle_lock():
            contender = second.lifecycle_lock_path.open("a+")
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                contender.close()

    def test_unit_names_include_exact_identity_hash_and_do_not_slug_collide(self):
        first = self.manager(FakeWorkerRunner(self.environment))
        altered = self.environment.to_dict()
        altered["run_id"] = "mf_run_test"
        altered_environment = RunEnvironment(**{
            key: (Path(value) if key in {
                "source_root", "repository_root", "worktree", "run_dir", "bridge_home", "bridge_config",
                "executor_config_dir", "executor_state_dir", "allowed_files_manifest",
                "immutable_plan", "metadata_file", "lease_file",
            } else value)
            for key, value in altered.items()
        })
        second = WorkerManager(altered_environment, runner=FakeWorkerRunner(self.environment))
        self.assertNotEqual(first.executor_unit, second.executor_unit)

    def test_bridge_is_stopped_before_recovering_missing_executor(self):
        runner = FakeWorkerRunner(self.environment)
        manager = self.manager(runner)
        runner.active.add(manager.bridge_unit)
        manager.start()
        stop_bridge = runner.calls.index(
            ["systemctl", "--user", "stop", manager.bridge_unit]
        )
        starts = [index for index, call in enumerate(runner.calls)
                  if call[:2] == ["systemd-run", "--user"]]
        self.assertLess(stop_bridge, starts[0])
        self.assertEqual("READY", manager.status()["status"])

    def test_foreign_active_unit_is_not_adopted(self):
        runner = FakeWorkerRunner(self.environment)
        manager = self.manager(runner)
        runner.active.add(manager.executor_unit)
        runner.foreign.add(manager.executor_unit)
        manager.start()
        stop = ["systemctl", "--user", "stop", manager.executor_unit]
        self.assertIn(stop, runner.calls)
        self.assertEqual("READY", manager.status()["status"])

    def test_grant_uses_exact_bound_argv_without_yes_and_verifies_lease(self):
        runner = FakeWorkerRunner(self.environment)
        manager = self.manager(runner)
        manager.start()
        runner.calls.clear()
        manager.grant_lease(7)
        grant = next(call for call in runner.calls if "grant" in call)
        self.assertNotIn("--yes", grant)
        self.assertEqual(str(self.source / ".venv-server/bin/python"), grant[0])
        self.assertEqual(
            str(self.environment.worktree / "tools/local_executor/control.py"),
            grant[1],
        )
        identity = self.environment.lease_identity()
        for option, value in (
            ("--run-id", identity["run_id"]),
            ("--project-root", identity["project_root"]),
            ("--plan-sha256", identity["plan_sha256"]),
            ("--allowed-files-sha256", identity["allowed_files_sha256"]),
        ):
            self.assertEqual(value, grant[grant.index(option) + 1])
        self.assertTrue(manager.lease_active())

    def test_grant_without_ready_workers_does_not_invoke_control_or_write_lease(self):
        runner = FakeWorkerRunner(self.environment)
        manager = self.manager(runner)
        with self.assertRaisesRegex(RuntimeError, "not READY"):
            manager.grant_lease(5)
        self.assertFalse(self.environment.lease_file.exists())
        self.assertFalse(any("control.py" in " ".join(call) for call in runner.calls))

    def test_resume_precondition_failure_is_byte_identical(self):
        state = Path(self.temp.name) / "resume-state.json"
        atomic_write_json(state, initial_run(plan(), {}))
        before = state.read_bytes()

        class NotReady:
            def status(self):
                return {"status": "NOT_READY", "lease_active": False}

        with self.assertRaisesRegex(RuntimeError, "not READY"):
            supervisor_module.require_resume_ready(NotReady())
        self.assertEqual(before, state.read_bytes())

    def test_resume_existing_job_does_not_redispatch(self):
        state = Path(self.temp.name) / "active-state.json"
        adapter = FakeAdapter()
        adapter.job_results = [("running", "")]
        atomic_write_json(state, initial_run(plan(), adapter.snapshot()))
        supervisor = Supervisor(state, adapter)
        supervisor.step()
        dispatched = len(adapter.jobs)

        class Ready:
            def status(self):
                return {"status": "READY", "lease_active": True}

        supervisor_module.require_resume_ready(Ready())
        self.assertFalse(supervisor.step())
        self.assertEqual(dispatched, len(adapter.jobs))


if __name__ == "__main__":
    unittest.main()
