import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.mikrofix_codex_review import (
    ReviewError, prepare_review, record_codex_approved, record_codex_rejected,
)
from tools.mikrofix_supervisor import atomic_write_json
from tools.mikrofix_task_lifecycle import (
    TaskState, build_profile_snapshot, initialize_task_lifecycle,
    load_profile_registry, read_task_state, sha256_text, sha256_value,
    transition_task,
    write_profile_snapshot, write_system_prompt_snapshot,
)


class CodexReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repository = self.base / "repository"
        self.workspace = self.base / "workspace"
        self.root = self.workspace / "jobs" / "MF-ROOT"
        self.action = self.workspace / "jobs" / "MF-ACTION"
        self.repository.mkdir()
        self.root.mkdir(parents=True)
        self.action.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repository / "app.py").write_text("before\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-qm", "base")
        self.base_checkpoint = self.git("rev-parse", "HEAD")
        (self.workspace / "workspace.json").write_text(json.dumps({
            "repository": str(self.repository),
        }), encoding="utf-8")

        prompt = self.repository / "docs" / "lead.md"
        prompt.parent.mkdir()
        prompt.write_text("lead prompt\n", encoding="utf-8")
        registry = self.repository / "profiles.json"
        registry.write_text(json.dumps({
            "schema_version": 1,
            "profiles": {"lead": {
                "version": 1, "model": "model:test", "role": "lead",
                "display_name": "Test lead", "num_ctx": 4096, "think": True,
                "system_prompt": "docs/lead.md", "keep_alive": "5m",
                "enabled": True,
            }},
        }), encoding="utf-8")
        self.git("add", "docs/lead.md", "profiles.json")
        self.git("commit", "-qm", "test review fixtures")
        self.base_checkpoint = self.git("rev-parse", "HEAD")
        profile = load_profile_registry(registry, self.repository)["lead"]
        self.snapshot = build_profile_snapshot(
            profile, project_root=self.repository, model_digest="a" * 64,
        )
        write_profile_snapshot(self.root, self.snapshot)
        write_system_prompt_snapshot(
            self.root, project_root=self.repository, snapshot=self.snapshot,
        )
        initialize_task_lifecycle(
            self.root, task_id="MF-ROOT", conversation_id="MF-ROOT",
            snapshot=self.snapshot, input_text="Owner request",
            checkpoint=self.base_checkpoint,
        )
        for state in (
            TaskState.READY_FOR_PROCESSING,
            TaskState.AWAITING_OWNER_APPROVAL,
            TaskState.APPROVED,
            TaskState.EXECUTING,
            TaskState.LEAD_REVIEW,
            TaskState.CANDIDATE_READY,
            TaskState.AWAITING_CODEX,
        ):
            transition_task(
                self.root, state, snapshot=self.snapshot,
                actor_type="test", actor_id="test",
            )
        (self.root / "request.json").write_text(json.dumps({
            "run_id": "MF-ROOT", "task": "Owner request",
        }), encoding="utf-8")
        research_core = {
            "schema_version": 1,
            "owner_request": "Owner request",
            "owner_request_sha256": sha256_text("Owner request"),
            "codex_research_prompt": "Inspect app.py",
            "codex_research_prompt_sha256": sha256_text("Inspect app.py"),
        }
        research_input = {
            **research_core, "research_input_sha256": sha256_value(research_core),
        }
        (self.root / "research-input.json").write_text(
            json.dumps(research_input), encoding="utf-8",
        )
        research_dir = self.root / "research"
        research_dir.mkdir()
        manifest_core = {
            "schema_version": 1, "report": "Verified report",
            "report_sha256": sha256_text("Verified report"),
        }
        research_manifest = {
            **manifest_core, "manifest_sha256": sha256_value(manifest_core),
        }
        (research_dir / "manifest-0001.json").write_text(
            json.dumps(research_manifest), encoding="utf-8",
        )
        planning = self.root / "planning"
        planning.mkdir()
        intent_hash = "b" * 64
        decision = {"objective": "Apply verified candidate"}
        candidate_hash = sha256_value({
            "intent_sha256": intent_hash, "decision": decision,
        })
        (planning / f"intent-{intent_hash}.json").write_text(
            json.dumps({"intent_sha256": intent_hash}), encoding="utf-8",
        )
        (planning / f"lead-candidate-{intent_hash}.json").write_text(json.dumps({
            "intent_sha256": intent_hash, "decision": decision,
            "candidate_sha256": candidate_hash,
        }), encoding="utf-8")
        (self.root / "conversation.json").write_text(json.dumps({
            "active_run_id": "MF-ACTION", "turns": [],
            "research_manifests": [{
                "path": "research/manifest-0001.json",
                "sha256": research_manifest["manifest_sha256"],
            }],
            "latest_plan_candidate": {
                "path": f"planning/lead-candidate-{intent_hash}.json",
                "candidate_sha256": candidate_hash,
            },
        }), encoding="utf-8")
        (self.action / "request.json").write_text(json.dumps({
            "run_id": "MF-ACTION", "parent_run_id": "MF-ROOT",
        }), encoding="utf-8")
        (self.action / "plan.json").write_text(json.dumps({
            "run_id": "MF-ACTION", "base_checkpoint": self.base_checkpoint,
            "allowed_files": ["app.py"],
        }), encoding="utf-8")
        artifacts = self.workspace / "artifacts" / "MF-ACTION"
        artifacts.mkdir(parents=True)
        self.patch = artifacts / "candidate.patch"
        self.patch.write_text("candidate patch\n", encoding="utf-8")
        import hashlib
        patch_hash = hashlib.sha256(self.patch.read_bytes()).hexdigest()
        candidate_files = [{
            "path": "app.py", "state": "present",
            "sha256": hashlib.sha256(b"after\n").hexdigest(), "mode": 0o644,
        }]
        candidate_files_hash = hashlib.sha256(json.dumps(
            candidate_files, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.manifest = artifacts / "manifest.json"
        self.manifest.write_text(json.dumps({
            "run_id": "MF-ACTION", "base_checkpoint": self.base_checkpoint,
            "patch_sha256": patch_hash, "patch": str(self.patch),
            "candidate_files": candidate_files,
            "candidate_files_sha256": candidate_files_hash,
            "test_evidence": [{"command": "tests", "exit_code": 0}],
            "local_reviewer_verdict": "APPROVE",
        }), encoding="utf-8")
        supervisor = {
            "stage": "AWAIT_CODEX_REVIEW", "checkpoint": str(self.manifest),
            "test_evidence": "PASS", "review_evidence": "APPROVE",
        }
        (self.action / "supervisor.json").write_text(
            json.dumps(supervisor), encoding="utf-8",
        )
        (self.action / "manager.json").write_text(json.dumps({
            "run_id": "MF-ACTION", "state": "READY_FOR_CODEX_REVIEW",
            "checkpoint": str(self.manifest),
            "supervisor_state": str(self.action / "supervisor.json"),
        }), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()

    def test_prepare_review_is_immutable_complete_and_idempotent(self):
        first = prepare_review(self.workspace, "MF-ACTION")
        second = prepare_review(self.workspace, "MF-ROOT")
        self.assertEqual(first, second)
        package_path = Path(first["package_path"])
        package = json.loads(package_path.read_text(encoding="utf-8"))
        names = {entry["name"] for entry in package["evidence"]}
        for required in (
            "root-request.json", "conversation.json", "task-state.json",
            "lead-profile-snapshot.json", "plan.json", "supervisor.json",
            "checkpoint-manifest.json", "candidate.patch",
        ):
            self.assertIn(required, names)
        self.assertEqual(0, package_path.stat().st_mode & 0o222)
        prepared_events = [
            json.loads(path.read_text())["event_type"]
            for path in sorted((self.root / "audit" / "events").glob("*.json"))
        ].count("CODEX_REVIEW_PREPARED")
        self.assertEqual(1, prepared_events)

    def test_prepared_evidence_tampering_fails_closed(self):
        prepared = prepare_review(self.workspace, "MF-ACTION")
        package = json.loads(
            Path(prepared["package_path"]).read_text(encoding="utf-8")
        )
        patch_entry = next(
            entry for entry in package["evidence"]
            if entry["name"] == "candidate.patch"
        )
        copied_patch = Path(patch_entry["path"])
        copied_patch.chmod(0o600)
        copied_patch.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ReviewError, "evidence hash mismatch"):
            prepare_review(self.workspace, "MF-ROOT")

    def test_verdict_without_prepared_review_fails_closed(self):
        with self.assertRaisesRegex(ReviewError, "No prepared Codex review"):
            record_codex_rejected(self.workspace, "MF-ACTION", "finding")
        with self.assertRaisesRegex(ReviewError, "No prepared Codex review"):
            record_codex_approved(
                self.workspace, "MF-ACTION", self.base_checkpoint,
            )

    def test_rejection_is_immutable_and_returns_to_lead_review(self):
        prepare_review(self.workspace, "MF-ACTION")
        result = record_codex_rejected(
            self.workspace, "MF-ROOT", "Missing concurrency regression test",
        )
        self.assertEqual("REJECTED", result["status"])
        self.assertEqual(TaskState.LEAD_REVIEW.value, read_task_state(self.root)["state"])
        rejection = Path(result["package_path"]).parent / "codex-rejection.json"
        self.assertIn("concurrency", rejection.read_text(encoding="utf-8"))
        self.assertEqual(0, rejection.stat().st_mode & 0o222)

    def test_approval_requires_real_descendant_commit_and_marks_applied(self):
        prepare_review(self.workspace, "MF-ACTION")
        with self.assertRaisesRegex(ReviewError, "must contain a change"):
            record_codex_approved(self.workspace, "MF-ACTION", self.base_checkpoint)
        (self.repository / "app.py").write_text("after\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-qm", "apply reviewed candidate")
        commit = self.git("rev-parse", "HEAD")
        result = record_codex_approved(self.workspace, "MF-ACTION", commit)
        self.assertEqual("APPLIED", result["status"])
        self.assertEqual(commit, result["commit"])
        self.assertEqual(TaskState.APPLIED.value, read_task_state(self.root)["state"])

    def test_approval_rejects_unrelated_descendant_commit(self):
        prepare_review(self.workspace, "MF-ACTION")
        (self.repository / "other.py").write_text("unrelated\n", encoding="utf-8")
        self.git("add", "other.py")
        self.git("commit", "-qm", "unrelated descendant")
        commit = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(
            ReviewError, "paths or final blob hashes differ",
        ):
            record_codex_approved(self.workspace, "MF-ACTION", commit)

    def test_approval_rejects_matching_commit_that_is_not_checked_out(self):
        prepare_review(self.workspace, "MF-ACTION")
        (self.repository / "app.py").write_text("after\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-qm", "candidate exists but is not applied")
        candidate = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.base_checkpoint)
        with self.assertRaisesRegex(ReviewError, "not at the required checkpoint"):
            record_codex_approved(self.workspace, "MF-ACTION", candidate)

    def test_prepare_review_requires_clean_repository_at_exact_base(self):
        (self.repository / "app.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ReviewError, "must be clean"):
            prepare_review(self.workspace, "MF-ACTION")

    def test_prepare_review_rejects_tampered_research_manifest(self):
        path = self.root / "research" / "manifest-0001.json"
        value = json.loads(path.read_text())
        value["report"] = "tampered"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ReviewError, "Research manifest hash differs"):
            prepare_review(self.workspace, "MF-ACTION")


if __name__ == "__main__":
    unittest.main()
