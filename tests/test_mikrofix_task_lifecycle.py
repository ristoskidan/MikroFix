import json
from pathlib import Path
import tempfile
import unittest

from tools.mikrofix_task_lifecycle import (
    LifecycleError,
    TaskState,
    build_profile_snapshot,
    initialize_task_lifecycle,
    load_profile_registry,
    read_profile_snapshot,
    read_task_state,
    sha256_value,
    transition_task,
    write_profile_snapshot,
    write_system_prompt_snapshot,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.job = self.root / "job"
        prompt = self.project / "docs" / "lead.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("lead prompt\n", encoding="utf-8")
        self.registry_path = self.project / "profiles.json"
        self.write_registry("model:first")

    def tearDown(self):
        self.temporary.cleanup()

    def write_registry(self, model):
        self.registry_path.write_text(json.dumps({
            "schema_version": 1,
            "profiles": {
                "test_lead": {
                    "version": 1,
                    "model": model,
                    "role": "lead",
                    "display_name": "Test lead",
                    "num_ctx": 4096,
                    "think": True,
                    "system_prompt": "docs/lead.md",
                    "keep_alive": "5m",
                    "enabled": True,
                }
            },
        }), encoding="utf-8")

    def snapshot(self, digest=DIGEST_A):
        profile = load_profile_registry(self.registry_path, self.project)["test_lead"]
        return build_profile_snapshot(
            profile, project_root=self.project, model_digest=digest,
        )

    def initialize(self):
        snapshot = self.snapshot()
        self.job.mkdir()
        write_profile_snapshot(self.job, snapshot)
        write_system_prompt_snapshot(
            self.job, project_root=self.project, snapshot=snapshot,
        )
        initialize_task_lifecycle(
            self.job, task_id="MF-1", conversation_id="MF-1",
            snapshot=snapshot, input_text="owner request", checkpoint="base-checkpoint",
        )
        return snapshot

    def events(self):
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.job / "audit" / "events").glob("*.json"))
        ]

    def test_registry_change_does_not_change_existing_snapshot(self):
        first = self.snapshot(DIGEST_A)
        self.job.mkdir()
        write_profile_snapshot(self.job, first)
        self.write_registry("model:second")
        second = self.snapshot(DIGEST_B)

        persisted = read_profile_snapshot(self.job)
        self.assertEqual("model:first", persisted["model"])
        self.assertEqual(DIGEST_A, persisted["model_digest"])
        self.assertEqual(4096, persisted["conversation_num_ctx"])
        self.assertEqual("5m", persisted["conversation_keep_alive"])
        self.assertNotEqual(first["profile_snapshot_hash"], second["profile_snapshot_hash"])

    def test_legacy_v1_snapshot_remains_readable(self):
        snapshot = self.snapshot(DIGEST_A)
        snapshot.pop("conversation_num_ctx")
        snapshot.pop("conversation_keep_alive")
        snapshot.pop("runtime_options")
        snapshot["schema_version"] = 1
        snapshot.pop("profile_snapshot_hash")
        snapshot["profile_snapshot_hash"] = sha256_value(snapshot)
        self.job.mkdir()
        write_profile_snapshot(self.job, snapshot)
        persisted = read_profile_snapshot(self.job)
        self.assertEqual(1, persisted["schema_version"])
        self.assertNotIn("conversation_num_ctx", persisted)

    def test_runtime_options_are_part_of_snapshot_identity(self):
        first = self.snapshot(DIGEST_A)
        profile = load_profile_registry(self.registry_path, self.project)["test_lead"]
        profile["runtime_options"] = {
            "num_predict": 4096, "temperature": 0.5, "top_k": 20,
            "top_p": 0.95, "min_p": 0.0, "repeat_penalty": 1.0,
            "seed": -1,
        }
        second = build_profile_snapshot(
            profile, project_root=self.project, model_digest=DIGEST_A,
        )
        self.assertNotEqual(first["profile_snapshot_hash"], second["profile_snapshot_hash"])
        self.assertEqual(4096, second["runtime_options"]["num_predict"])

    def test_v2_registry_rejects_invalid_conversation_context(self):
        raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        raw["schema_version"] = 2
        profile = raw["profiles"]["test_lead"]
        profile["conversation_num_ctx"] = 8192
        profile["conversation_keep_alive"] = "5m"
        self.registry_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(LifecycleError, "conversation_num_ctx"):
            load_profile_registry(self.registry_path, self.project)

    def test_initial_events_are_immutable_and_parent_linked(self):
        snapshot = self.initialize()
        events = self.events()
        self.assertEqual(["TASK_CREATED", "LEAD_SELECTED"], [e["event_type"] for e in events])
        self.assertIsNone(events[0]["parent_event_id"])
        self.assertEqual(events[0]["event_id"], events[1]["parent_event_id"])
        self.assertEqual(snapshot["model_digest"], events[1]["model_digest"])
        self.assertEqual("base-checkpoint", events[0]["checkpoint"])
        self.assertEqual("base-checkpoint", events[1]["checkpoint"])
        self.assertEqual(0, events[0]["input_hash"].count(" "))
        for path in (self.job / "audit" / "events").glob("*.json"):
            self.assertEqual(0, path.stat().st_mode & 0o222)

    def test_forbidden_transition_is_fail_closed(self):
        snapshot = self.initialize()
        before_state = read_task_state(self.job)
        before_events = self.events()
        with self.assertRaisesRegex(LifecycleError, "Forbidden"):
            transition_task(
                self.job, TaskState.EXECUTING, snapshot=snapshot,
                actor_type="manager", actor_id="manager",
            )
        self.assertEqual(before_state, read_task_state(self.job))
        self.assertEqual(before_events, self.events())

    def test_allowed_transitions_each_append_an_audit_event(self):
        snapshot = self.initialize()
        targets = [
            TaskState.READY_FOR_PROCESSING,
            TaskState.AWAITING_OWNER_APPROVAL,
            TaskState.APPROVED,
            TaskState.EXECUTING,
            TaskState.LEAD_REVIEW,
            TaskState.CANDIDATE_READY,
            TaskState.AWAITING_CODEX,
            TaskState.CODEX_REJECTED,
            TaskState.LEAD_REVIEW,
        ]
        for target in targets:
            transition_task(
                self.job, target, snapshot=snapshot,
                actor_type="manager", actor_id="manager",
            )
        events = self.events()
        self.assertEqual(2 + len(targets), len(events))
        self.assertEqual(TaskState.LEAD_REVIEW.value, read_task_state(self.job)["state"])
        for previous, current in zip(events, events[1:]):
            self.assertEqual(previous["event_id"], current["parent_event_id"])

    def test_owner_can_reject_unapproved_plan_back_to_draft(self):
        snapshot = self.initialize()
        transition_task(
            self.job, TaskState.READY_FOR_PROCESSING, snapshot=snapshot,
            actor_type="owner", actor_id="owner",
        )
        transition_task(
            self.job, TaskState.AWAITING_OWNER_APPROVAL, snapshot=snapshot,
            actor_type="manager", actor_id="manager",
        )
        with self.assertRaisesRegex(LifecycleError, "OWNER_REJECTED_PLAN"):
            transition_task(
                self.job, TaskState.DRAFT, snapshot=snapshot,
                actor_type="owner", actor_id="owner",
            )
        transition_task(
            self.job, TaskState.DRAFT, snapshot=snapshot,
            actor_type="owner", actor_id="owner",
            event_type="OWNER_REJECTED_PLAN",
        )
        self.assertEqual(TaskState.DRAFT.value, read_task_state(self.job)["state"])
        self.assertEqual("OWNER_REJECTED_PLAN", self.events()[-1]["event_type"])

    def test_tampered_snapshot_fails_closed(self):
        snapshot = self.initialize()
        snapshot["model"] = "other:model"
        with self.assertRaisesRegex(LifecycleError, "model and tag differ"):
            transition_task(
                self.job, TaskState.READY_FOR_PROCESSING, snapshot=snapshot,
                actor_type="manager", actor_id="manager",
            )
        self.assertEqual(TaskState.DRAFT.value, read_task_state(self.job)["state"])
        self.assertEqual(2, len(self.events()))

    def test_tampered_audit_chain_fails_closed(self):
        self.initialize()
        last = sorted((self.job / "audit" / "events").glob("*.json"))[-1]
        last.chmod(0o600)
        event = json.loads(last.read_text(encoding="utf-8"))
        event["actor_id"] = "tampered"
        last.write_text(json.dumps(event), encoding="utf-8")
        with self.assertRaisesRegex(LifecycleError, "Audit event hash mismatch"):
            read_task_state(self.job)


if __name__ == "__main__":
    unittest.main()
