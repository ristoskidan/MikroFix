import importlib.util
import inspect
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

    def profile_snapshot(self, profile_id, *, model_tag=None):
        return manager.snapshot_for_new_task(
            profile_id, model_tag=model_tag or "gpt-oss:20b",
            digest_resolver=lambda _url, _model: "a" * 64,
        )

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

    def test_trace_forwarder_waits_for_complete_jsonl_records(self):
        trace = self.root / "trace.jsonl"
        trace.write_text(
            '{"type":"model","thinking":"first"}\n'
            '{"type":"tool_call","tool":"project_status"}\n'
            '{"type":"model","thinking":"part',
            encoding="utf-8",
        )
        received = []
        offset = manager.forward_trace_events(trace, 0, received.append)
        self.assertEqual(["model", "tool_call"], [item["type"] for item in received])

        with trace.open("a", encoding="utf-8") as handle:
            handle.write('ial"}\n')
        offset = manager.forward_trace_events(trace, offset, received.append)
        self.assertEqual("partial", received[-1]["thinking"])
        self.assertEqual(trace.stat().st_size, offset)
        self.assertEqual(3, len(received))

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
        self.assertIn("Svetovalec ne odloča", gpt)
        self.assertIn("abc123", gpt)
        self.assertIn(proposal, gpt)
        self.assertIn("missing criterion", gpt)

    def test_lead_profiles_are_isolated_and_prompt_is_natural_structured(self):
        prompt = manager.lead_conversation_prompt(
            "Kaj še manjka?",
            turns=[
                {"role": "owner", "content": "Office button is wrong"},
                {"role": "lead", "content": "Povej cilj."},
            ],
            repository_snapshot="commit: abc\nstatus: (clean)",
        )
        self.assertIn("Drugi profil", prompt)
        self.assertIn("strukturirani obliki", prompt)
        self.assertIn("bralnimi orodji", prompt)
        self.assertIn("/prepare-plan", prompt)
        self.assertIn("strogo osredotočen", prompt)
        self.assertIn("Office button is wrong", prompt)
        self.assertIn("Kaj še manjka?", prompt)
        self.assertNotIn("MF-ONE", prompt)
        self.assertNotIn("IZBRANI IN ZAKLENJENI PROFIL", prompt)
        self.assertNotIn("IZVIRNA ZAHTEVA", prompt)
        self.assertNotIn("ne v JSON-u", prompt)
        first = manager.lead_conversation_prompt(
            "Enkratno prvo vprašanje", turns=[],
            repository_snapshot="commit: abc\ndelovno drevo: clean",
        )
        self.assertEqual(1, first.count("Enkratno prvo vprašanje"))
        self.assertNotIn("DOSEDANJI POGOVOR", first)
        planning = manager.lead_conversation_prompt(
            "/prepare-plan", turns=[], planning_requested=True,
            plan_intent={
                "intent_sha256": "a" * 64,
                "root_turn_id": "turn-0001-owner",
                "turns": [{
                    "turn_id": "turn-0001-owner", "role": "owner",
                    "content": "Implement the bounded behavior",
                }],
            },
        )
        self.assertIn("izrecno sprožil /prepare-plan", planning)
        self.assertIn("lastnikova izbira", planning)
        self.assertIn("turn-0001-owner", planning)
        self.assertIn("discussion_only_turns", planning)
        self.assertEqual(
            ["reply"],
            manager.conversation_response_schema(
                planning_requested=False,
            )["required"],
        )
        self.assertIn(
            "routing", manager.conversation_response_schema()["required"],
        )

    def test_conversation_runtime_skips_context_acknowledgement(self):
        source = inspect.getsource(manager.run_lead_conversation)
        self.assertIn("ensure_conversation_session", source)
        self.assertNotIn("ensure_context_ready", source)
        current = self.profile_snapshot("conversation_lead")
        self.assertEqual(
            (16384, "5m"), manager.conversation_runtime_settings(current),
        )
        legacy = dict(current)
        legacy.pop("conversation_num_ctx")
        legacy.pop("conversation_keep_alive")
        self.assertEqual(
            (16384, "5m"), manager.conversation_runtime_settings(legacy),
        )
        self.assertIn('"request_timeout_seconds": 360', source)
        self.assertIn('"minimum_successful_tool_calls": 1', source)
        self.assertIn('"project_run_check"', source)
        self.assertNotIn('"project_apply_patch"', source)
        self.assertIn("MIKROFIX_EXECUTOR_READ_ONLY_CHECKS", source)
        self.assertIn('"deterministic_reply_wrapper": not planning_requested', source)
        self.assertIn("timeout_seconds=900", source)

    def test_plan_intent_keeps_root_request_and_excludes_discussion_from_task(self):
        conversation = {"turns": [
            {"role": "owner", "content": "Fix decimal_value", "at": "first"},
            {"role": "lead", "content": "I found the parser", "at": "second"},
            {"role": "owner", "content": "Show me the snippet", "at": "third"},
        ]}
        intent = manager.build_plan_intent(
            conversation, repository=self.repository,
        )
        self.assertEqual("turn-0001-owner", intent["root_turn_id"])
        self.assertEqual("Fix decimal_value", intent["root_request"])
        sources = manager.validate_plan_sources({
            "objective_source_turns": ["turn-0001-owner"],
            "decision_source_turns": [],
            "discussion_only_turns": ["turn-0003-owner"],
        }, intent)
        task = manager.trusted_plan_task(intent, sources)
        self.assertIn("Fix decimal_value", task)
        self.assertNotIn("Show me the snippet", task)
        with self.assertRaisesRegex(manager.ManagerError, "root owner request"):
            manager.validate_plan_sources({
                "objective_source_turns": ["turn-0003-owner"],
                "decision_source_turns": [],
                "discussion_only_turns": ["turn-0001-owner"],
            }, intent)

    def test_explicit_replace_goal_becomes_new_root_and_is_audited(self):
        def responder(_message, **_kwargs):
            return ({"reply": "Initial investigation complete."}, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-BRIDGE", "lead_profile": "conversation_lead",
                "prompt_hash": "c" * 64,
            })

        conversation = manager.start_conversation(
            "Inspect the old goal", workspace=self.workspace,
            lead_profile="conversation_lead", model_tag="gpt-oss:20b",
            responder=responder, snapshot_factory=self.profile_snapshot,
        )
        run_id = conversation["parent_run_id"]
        replaced = manager.replace_conversation_goal(
            run_id, "Fix only decimal_value", workspace=self.workspace,
        )
        intent = manager.build_plan_intent(
            replaced, repository=self.repository,
        )
        self.assertEqual("turn-0003-owner", intent["root_turn_id"])
        self.assertEqual("Fix only decimal_value", intent["root_request"])
        self.assertEqual("goal_replacement", intent["turns"][-1]["kind"])
        events = [
            json.loads(path.read_text())
            for path in sorted(
                (self.workspace / "jobs" / run_id / "audit" / "events").glob("*.json")
            )
        ]
        self.assertEqual("OWNER_REPLACED_GOAL", events[-1]["event_type"])
        with self.assertRaisesRegex(manager.ManagerError, "superseded"):
            manager.validate_plan_sources({
                "objective_source_turns": [
                    "turn-0001-owner", "turn-0003-owner",
                ],
                "decision_source_turns": [],
                "discussion_only_turns": [],
            }, intent)

    def test_prepare_plan_does_not_promote_latest_explanatory_question(self):
        def discussion_responder(_message, **_kwargs):
            return ({"reply": "Verified explanation."}, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-DISCUSS", "lead_profile": "conversation_lead",
                "prompt_hash": "c" * 64,
            })

        conversation = manager.start_conversation(
            "Fix decimal_value finite-number validation", workspace=self.workspace,
            lead_profile="conversation_lead", model_tag="gpt-oss:20b",
            responder=discussion_responder, snapshot_factory=self.profile_snapshot,
        )
        run_id = conversation["parent_run_id"]
        manager.talk(
            "Show me the snippet", parent_run_id=run_id,
            workspace=self.workspace, responder=discussion_responder,
        )

        def planning_responder(_message, **kwargs):
            self.assertEqual(
                "Fix decimal_value finite-number validation",
                kwargs["plan_intent"]["root_request"],
            )
            return ({
                "reply": "Bounded plan ready.",
                "routing": "READY_FOR_PROCESSING",
                "ready_task": "Show me the snippet",
                "objective": "Fix decimal_value finite-number validation",
                "allowed_files": ["app.py", "tests/test_app.py"],
                "atoms": [{
                    "id": "A1", "task": "Fix and test",
                    "expected_files": ["app.py", "tests/test_app.py"],
                }],
                "constraints": ["Preserve valid values"],
                "acceptance_criteria": ["Regression test passes"],
                "objective_source_turns": ["turn-0001-owner"],
                "decision_source_turns": [],
                "discussion_only_turns": ["turn-0003-owner"],
            }, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-PLAN", "lead_profile": "conversation_lead",
                "prompt_hash": "d" * 64,
            })

        captured = {}

        def submit_stub(task, **_kwargs):
            captured["task"] = task
            return {"run_id": "MF-CHILD"}

        with mock.patch.object(manager, "submit", side_effect=submit_stub):
            result = manager.prepare_plan(
                run_id, workspace=self.workspace, responder=planning_responder,
            )
        self.assertNotIn("task", captured)
        self.assertEqual("", result["active_run_id"])
        self.assertIn("IZVEDBENI NAČRT", result["turns"][-1]["content"])
        with mock.patch.object(manager, "submit", side_effect=submit_stub):
            result = manager.dispatch_plan(run_id, workspace=self.workspace)
        self.assertIn("Fix decimal_value", captured["task"])
        self.assertNotIn("Show me the snippet", captured["task"])
        self.assertEqual("MF-CHILD", result["active_run_id"])
    def test_role_identity_is_stable_while_model_selection_changes(self):
        resolver = lambda _url, model: (
            "a" * 64 if model == "gpt-oss:20b" else "b" * 64
        )
        first = manager.snapshot_for_new_task(
            "conversation_lead", model_tag="gpt-oss:20b",
            digest_resolver=resolver,
        )
        second = manager.snapshot_for_new_task(
            "conversation_lead", model_tag="qwen3:14b-q4_K_M",
            digest_resolver=resolver,
        )
        self.assertEqual(first["profile_id"], second["profile_id"])
        self.assertEqual(3, first["profile_version"])
        self.assertEqual(first["role"], second["role"])
        self.assertEqual(first["system_prompt_hash"], second["system_prompt_hash"])
        self.assertNotEqual(first["model_tag"], second["model_tag"])
        self.assertNotEqual(first["model_digest"], second["model_digest"])
        self.assertNotEqual(
            first["profile_snapshot_hash"], second["profile_snapshot_hash"],
        )

    def test_runtime_preset_and_overrides_are_validated_and_snapshotted(self):
        selected = manager.resolve_runtime_selection(
            "qwen3:4b-thinking-2507-q8_0", preset="deep",
            overrides={"temperature": 0.5},
        )
        self.assertEqual(32768, selected["num_ctx"])
        self.assertEqual(12288, selected["num_predict"])
        snapshot = manager.snapshot_for_new_task(
            "conversation_lead",
            model_tag="qwen3:4b-thinking-2507-q8_0",
            runtime_overrides=selected,
            digest_resolver=lambda _url, _model: "a" * 64,
        )
        self.assertEqual(3, snapshot["schema_version"])
        self.assertEqual(32768, snapshot["conversation_num_ctx"])
        self.assertEqual(12288, snapshot["runtime_options"]["num_predict"])
        self.assertEqual(0.5, snapshot["runtime_options"]["temperature"])
        with self.assertRaisesRegex(manager.ManagerError, "num_ctx"):
            manager.resolve_runtime_selection(
                "qwen3:4b-thinking-2507-q8_0",
                overrides={"num_ctx": 65536},
            )

    def test_start_conversation_passes_runtime_into_snapshot_factory(self):
        captured = {}

        def snapshot_factory(profile_id, **kwargs):
            captured.update(kwargs)
            return self.profile_snapshot(profile_id, model_tag=kwargs["model_tag"])

        def responder(_message, **_kwargs):
            return ({"reply": "Ugotovitev"}, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-BRIDGE", "lead_profile": "conversation_lead",
            })

        manager.start_conversation(
            "Preveri", workspace=self.workspace,
            lead_profile="conversation_lead", model_tag="gpt-oss:20b",
            runtime_overrides={"num_predict": 4096}, responder=responder,
            snapshot_factory=snapshot_factory,
        )
        self.assertEqual({"num_predict": 4096}, captured["runtime_overrides"])

    def test_start_conversation_keeps_one_root_and_locks_lead_profile(self):
        def responder(message, **kwargs):
            self.assertEqual("Build the intake", message)
            self.assertEqual("conversation_lead", kwargs["lead_profile"])
            self.assertEqual(
                "gpt-oss:20b",
                kwargs["lead_snapshot"]["model"],
            )
            return ({
                "reply": "Kateri uporabnik sme potrditi prevzem?",
                "routing": "NEEDS_INFO", "ready_task": "", "objective": "",
                "allowed_files": [], "atoms": [], "constraints": [],
                "acceptance_criteria": [],
            }, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-BRIDGE", "lead_profile": "conversation_lead",
            })

        conversation = manager.start_conversation(
            "Build the intake", workspace=self.workspace,
            lead_profile="conversation_lead", model_tag="gpt-oss:20b",
            responder=responder,
            snapshot_factory=self.profile_snapshot,
        )
        run_id = conversation["parent_run_id"]
        self.assertEqual("conversation_lead", conversation["lead_profile"])
        self.assertEqual(2, len(conversation["turns"]))
        self.assertEqual(1, len(list((self.workspace / "jobs").iterdir())))
        request = json.loads(
            (self.workspace / "jobs" / run_id / "request.json").read_text()
        )
        self.assertEqual("lead_conversation", request["kind"])
        self.assertEqual("a" * 64, request["model_digest"])
        snapshot = json.loads(
            (self.workspace / "jobs" / run_id / "lead-profile-snapshot.json").read_text()
        )
        self.assertEqual(request["profile_snapshot_hash"], snapshot["profile_snapshot_hash"])
        with self.assertRaisesRegex(manager.ManagerError, "zaklenjen"):
            manager.talk(
                "Switch", parent_run_id=run_id, workspace=self.workspace,
                lead_profile="different_role", responder=responder,
            )

    def test_only_explicit_prepare_plan_moves_to_owner_approval_with_audit(self):
        def responder(_message, **kwargs):
            if not kwargs.get("planning_requested"):
                return ({"reply": "Preveril sem relevantni del kode."}, {
                    "session_id": "lead-session", "memory_sha256": "b" * 64,
                    "bridge_job_id": "MF-BRIDGE", "lead_profile": "conversation_lead",
                    "prompt_hash": "c" * 64,
                })
            return ({
                "reply": "Predlog je pripravljen.",
                "routing": "READY_FOR_PROCESSING",
                "ready_task": "Implement the bounded behavior",
                "objective": "Implement one bounded behavior",
                "allowed_files": ["app.py", "tests/test_app.py"],
                "atoms": [{
                    "id": "A1", "task": "Implement and test",
                    "expected_files": ["app.py", "tests/test_app.py"],
                }],
                "constraints": ["No unrelated changes"],
                "acceptance_criteria": ["Regression test passes"],
                "objective_source_turns": ["turn-0001-owner"],
                "decision_source_turns": [],
                "discussion_only_turns": ["turn-0002-lead"],
            }, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-BRIDGE", "lead_profile": "conversation_lead",
                "prompt_hash": "c" * 64,
            })

        with mock.patch.object(manager, "submit") as submit:
            conversation = manager.start_conversation(
                "Implement it", workspace=self.workspace,
                lead_profile="conversation_lead", model_tag="gpt-oss:20b",
                responder=responder, snapshot_factory=self.profile_snapshot,
            )
        submit.assert_not_called()
        job_dir = self.workspace / "jobs" / conversation["parent_run_id"]
        lifecycle = json.loads((job_dir / "task-state.json").read_text())
        self.assertEqual("DRAFT", lifecycle["state"])
        self.assertEqual("", conversation["active_run_id"])
        self.assertEqual("NEEDS_INFO", conversation["turns"][-1]["routing"])
        self.assertEqual(
            "Preveril sem relevantni del kode.",
            conversation["turns"][-1]["content"],
        )

        def submit_stub(task, **kwargs):
            kwargs["generator"](
                task, job_dir=self.root / "generated",
                repository=self.repository,
            )
            return {"run_id": "MF-CHILD"}

        with mock.patch.object(
            manager, "run_planner", return_value="{}",
        ) as run_planner, mock.patch.object(
            manager, "submit", side_effect=submit_stub,
        ):
            conversation = manager.prepare_plan(
                conversation["parent_run_id"], workspace=self.workspace,
                responder=responder,
            )
            self.assertEqual("", conversation["active_run_id"])
            self.assertIn("IZVEDBENI NAČRT", conversation["turns"][-1]["content"])
            run_planner.assert_not_called()
            conversation = manager.dispatch_plan(
                conversation["parent_run_id"], workspace=self.workspace,
            )
        planner_kwargs = run_planner.call_args.kwargs
        self.assertEqual(
            "Implement one bounded behavior",
            planner_kwargs["advisor_proposal"]["objective"],
        )
        self.assertEqual(
            "conversation_lead", planner_kwargs["advisor_snapshot"]["profile_id"],
        )
        self.assertEqual(
            "turn-0001-owner", planner_kwargs["plan_intent"]["root_turn_id"],
        )
        self.assertEqual(
            ["turn-0001-owner"],
            planner_kwargs["source_trace"]["objective_source_turns"],
        )
        self.assertIn("POTRJENA IZVIRNA ZAHTEVA", run_planner.call_args.args[0])
        self.assertIn("Implement it", run_planner.call_args.args[0])
        self.assertNotIn(
            "Implement the bounded behavior", run_planner.call_args.args[0],
        )
        lifecycle = json.loads((job_dir / "task-state.json").read_text())
        self.assertEqual("AWAITING_OWNER_APPROVAL", lifecycle["state"])
        events = [
            json.loads(path.read_text())
            for path in sorted((job_dir / "audit" / "events").glob("*.json"))
        ]
        self.assertEqual(
            ["TASK_CREATED", "LEAD_SELECTED", "LEAD_MESSAGE",
             "LEAD_PROPOSED_PLAN",
             "TASK_STATE_TRANSITION", "TASK_STATE_TRANSITION"],
            [event["event_type"] for event in events],
        )
        self.assertEqual("MF-CHILD", conversation["active_run_id"])
        cached = manager.prepare_plan(
            conversation["parent_run_id"], workspace=self.workspace,
            responder=mock.Mock(side_effect=AssertionError("must not rerun lead")),
        )
        self.assertEqual("MF-CHILD", cached["active_run_id"])

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

    def test_prepare_plan_reuses_cached_lead_candidate_after_validator_failure(self):
        def conversation_responder(_message, **_kwargs):
            return ({"reply": "Relevant code is verified."}, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-CHAT", "lead_profile": "conversation_lead",
                "prompt_hash": "c" * 64,
            })

        conversation = manager.start_conversation(
            "Fix decimal_value", workspace=self.workspace,
            lead_profile="conversation_lead", model_tag="gpt-oss:20b",
            responder=conversation_responder,
            snapshot_factory=self.profile_snapshot,
        )
        planning_calls = []

        def planning_responder(_message, **_kwargs):
            planning_calls.append(True)
            return ({
                "reply": "Plan candidate ready.",
                "routing": "READY_FOR_PROCESSING",
                "ready_task": "untrusted model wording",
                "objective": "Fix decimal_value",
                "allowed_files": ["app.py", "tests/test_app.py"],
                "atoms": [{
                    "id": "A1", "task": "Fix and test",
                    "expected_files": ["app.py", "tests/test_app.py"],
                }],
                "constraints": ["Preserve valid inputs"],
                "acceptance_criteria": ["Regression test passes"],
                "objective_source_turns": ["turn-0001-owner"],
                "decision_source_turns": [],
                "discussion_only_turns": ["turn-0002-lead"],
            }, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-PLAN", "lead_profile": "conversation_lead",
                "prompt_hash": "d" * 64,
            })

        manager.prepare_plan(
            conversation["parent_run_id"], workspace=self.workspace,
            responder=planning_responder,
        )
        with mock.patch.object(
            manager, "submit", side_effect=manager.ManagerError("validator failed"),
        ):
            with self.assertRaisesRegex(manager.ManagerError, "validator failed"):
                manager.dispatch_plan(
                    conversation["parent_run_id"], workspace=self.workspace,
                )
        job_dir = self.workspace / "jobs" / conversation["parent_run_id"]
        self.assertEqual(
            "DRAFT", manager.read_task_state(job_dir)["state"],
        )
        with mock.patch.object(
            manager, "submit", return_value={"run_id": "MF-CHILD"},
        ):
            result = manager.dispatch_plan(
                conversation["parent_run_id"], workspace=self.workspace,
            )
        self.assertEqual([True], planning_calls)
        self.assertEqual("MF-CHILD", result["active_run_id"])

    def test_plan_dispatch_rejects_conversation_change_after_visible_revision(self):
        def conversation_responder(_message, **_kwargs):
            return ({"reply": "Relevant code is verified."}, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-CHAT", "lead_profile": "conversation_lead",
                "prompt_hash": "c" * 64,
            })

        conversation = manager.start_conversation(
            "Fix decimal_value", workspace=self.workspace,
            lead_profile="conversation_lead", model_tag="gpt-oss:20b",
            responder=conversation_responder,
            snapshot_factory=self.profile_snapshot,
        )

        def planning_responder(_message, **_kwargs):
            return ({
                "reply": "Plan candidate ready.",
                "routing": "READY_FOR_PROCESSING",
                "ready_task": "untrusted wording",
                "objective": "Fix decimal_value",
                "allowed_files": ["app.py", "tests/test_app.py"],
                "atoms": [{
                    "id": "A1", "task": "Fix and test",
                    "expected_files": ["app.py", "tests/test_app.py"],
                }],
                "constraints": ["Preserve valid inputs"],
                "acceptance_criteria": ["Regression test passes"],
                "objective_source_turns": ["turn-0001-owner"],
                "decision_source_turns": [],
                "discussion_only_turns": [],
            }, {
                "session_id": "lead-session", "memory_sha256": "b" * 64,
                "bridge_job_id": "MF-PLAN", "lead_profile": "conversation_lead",
                "prompt_hash": "d" * 64,
            })

        run_id = conversation["parent_run_id"]
        manager.prepare_plan(
            run_id, workspace=self.workspace, responder=planning_responder,
        )
        manager.talk(
            "Ohrani tudi prazne vrednosti.", parent_run_id=run_id,
            workspace=self.workspace, responder=conversation_responder,
        )
        with mock.patch.object(manager, "submit") as submit:
            with self.assertRaisesRegex(
                manager.ManagerError, "Ponovno uporabi /prepare-plan",
            ):
                manager.dispatch_plan(run_id, workspace=self.workspace)
        submit.assert_not_called()

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

    def test_execute_run_advances_parent_task_to_codex_gate(self):
        parent_id = "MF-PARENT-LIFECYCLE"
        parent = self.workspace / "jobs" / parent_id
        parent.mkdir()
        snapshot = self.profile_snapshot("conversation_lead")
        manager.write_profile_snapshot(parent, snapshot)
        manager.write_system_prompt_snapshot(
            parent, project_root=manager.PROJECT_ROOT, snapshot=snapshot,
        )
        manager.initialize_task_lifecycle(
            parent, task_id=parent_id, conversation_id=parent_id,
            snapshot=snapshot, input_text="Owner request",
        )
        manager.transition_task(
            parent, manager.TaskState.READY_FOR_PROCESSING,
            snapshot=snapshot, actor_type="lead", actor_id="conversation_lead",
            event_type="LEAD_PROPOSED_PLAN",
        )
        manager.transition_task(
            parent, manager.TaskState.AWAITING_OWNER_APPROVAL,
            snapshot=snapshot, actor_type="manager", actor_id="manager",
        )

        run_id = "MF-CHILD-LIFECYCLE"
        job_dir = self.workspace / "jobs" / run_id
        job_dir.mkdir()
        supervisor_state = job_dir / "supervisor.json"
        supervisor_state.write_text(json.dumps({
            "stage": "PLANNED", "checkpoint": None, "feedback": "",
        }), encoding="utf-8")
        (job_dir / "request.json").write_text(json.dumps({
            "run_id": run_id, "parent_run_id": parent_id,
        }), encoding="utf-8")
        (job_dir / "manager.json").write_text(json.dumps({
            "schema_version": 1, "run_id": run_id,
            "state": "READY_FOR_OWNER_LEASE",
            "supervisor_state": str(supervisor_state),
        }), encoding="utf-8")

        def fake_command(args, **_kwargs):
            if "resume" in args:
                supervisor_state.write_text(json.dumps({
                    "stage": "AWAIT_CODEX_REVIEW",
                    "checkpoint": "/candidate/manifest.json", "feedback": "",
                }), encoding="utf-8")
            return "ok"

        with mock.patch.object(manager, "command", side_effect=fake_command):
            manager.execute_run(run_id, workspace=self.workspace)
        lifecycle = manager.read_task_state(parent)
        self.assertEqual("AWAITING_CODEX", lifecycle["state"])
        event_types = [
            json.loads(path.read_text())["event_type"]
            for path in sorted((parent / "audit" / "events").glob("*.json"))
        ]
        self.assertIn("OWNER_APPROVED_PLAN", event_types)
        self.assertIn("LEAD_ACCEPTED_CANDIDATE", event_types)

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
