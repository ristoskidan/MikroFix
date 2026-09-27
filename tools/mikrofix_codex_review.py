#!/usr/bin/env python3
"""Prepare immutable Codex review bundles and record owner-confirmed verdicts."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Iterator

try:
    from tools.mikrofix_supervisor import atomic_write_immutable_json, atomic_write_json
    from tools.mikrofix_task_lifecycle import (
        LifecycleError, TaskState, read_profile_snapshot, read_task_state,
        record_task_event, sha256_text, sha256_value, transition_task,
    )
except ModuleNotFoundError:
    from mikrofix_supervisor import (  # type: ignore[no-redef]
        atomic_write_immutable_json, atomic_write_json,
    )
    from mikrofix_task_lifecycle import (  # type: ignore[no-redef]
        LifecycleError, TaskState, read_profile_snapshot, read_task_state,
        record_task_event, sha256_text, sha256_value, transition_task,
    )


RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class ReviewError(RuntimeError):
    """Fail-closed review preparation or verdict error."""


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"Missing or invalid JSON evidence: {path}") from exc
    if not isinstance(value, dict):
        raise ReviewError(f"JSON evidence must be an object: {path}")
    return value


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReviewError(f"Cannot hash review evidence: {path}") from exc


@contextmanager
def review_lock(root_job: Path) -> Iterator[None]:
    with (root_job / "codex-review.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def resolve_jobs(workspace: Path, run_id: str) -> tuple[Path, Path, dict[str, Any]]:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ReviewError("Unsafe run id")
    workspace = workspace.expanduser().resolve()
    job = workspace / "jobs" / run_id
    request = read_json(job / "request.json")
    parent_id = request.get("parent_run_id")
    if parent_id:
        if not isinstance(parent_id, str) or not RUN_ID_RE.fullmatch(parent_id):
            raise ReviewError("Unsafe parent run id")
        root = workspace / "jobs" / parent_id
        action = job
    else:
        root = job
        conversation = read_json(root / "conversation.json")
        action_id = conversation.get("active_run_id")
        if not isinstance(action_id, str) or not RUN_ID_RE.fullmatch(action_id):
            raise ReviewError("Conversation has no active candidate")
        action = workspace / "jobs" / action_id
    workspace_config = read_json(workspace / "workspace.json")
    repository = Path(str(workspace_config.get("repository") or "")).resolve()
    if not (repository / ".git").exists():
        raise ReviewError("Workspace repository is not a Git checkout")
    return root, action, {"workspace": workspace, "repository": repository}


def _candidate_checkpoint(action: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    manager = read_json(action / "manager.json")
    supervisor_path = manager.get("supervisor_state")
    if not isinstance(supervisor_path, str) or not supervisor_path:
        raise ReviewError("Candidate has no supervisor state")
    supervisor = read_json(Path(supervisor_path))
    if manager.get("state") != "READY_FOR_CODEX_REVIEW" and supervisor.get("stage") != "AWAIT_CODEX_REVIEW":
        raise ReviewError("Candidate is not ready for Codex review")
    checkpoint = manager.get("checkpoint") or supervisor.get("checkpoint")
    if not isinstance(checkpoint, str) or not checkpoint:
        raise ReviewError("Candidate has no checkpoint manifest")
    manifest_path = Path(checkpoint).resolve()
    manifest = read_json(manifest_path)
    patch_path = Path(str(manifest.get("patch") or "")).resolve()
    if not patch_path.is_file():
        raise ReviewError("Candidate patch is missing")
    if manifest.get("patch_sha256") != file_sha256(patch_path):
        raise ReviewError("Candidate patch hash differs from checkpoint manifest")
    if manifest.get("run_id") != action.name:
        raise ReviewError("Checkpoint manifest belongs to another run")
    candidate_files = manifest.get("candidate_files")
    if not isinstance(candidate_files, list) or not candidate_files:
        raise ReviewError("Checkpoint manifest has no candidate file identity")
    expected_identity = hashlib.sha256(json.dumps(
        candidate_files, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if manifest.get("candidate_files_sha256") != expected_identity:
        raise ReviewError("Checkpoint candidate file identity hash differs")
    return manifest_path, manifest, supervisor


def _evidence_sources(
    root: Path, action: Path, manifest_path: Path, manifest: dict[str, Any],
) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    candidates = (
        ("root-request.json", root / "request.json"),
        ("conversation.json", root / "conversation.json"),
        ("task-state.json", root / "task-state.json"),
        ("lead-profile-snapshot.json", root / "lead-profile-snapshot.json"),
        ("action-request.json", action / "request.json"),
        ("plan.json", action / "plan.json"),
        ("supervisor.json", action / "supervisor.json"),
        ("checkpoint-manifest.json", manifest_path),
        ("candidate.patch", Path(str(manifest["patch"])).resolve()),
    )
    for name, path in candidates:
        if not path.is_file():
            raise ReviewError(f"Required review evidence is missing: {path}")
        paths.append((name, path))
    research_input = root / "research-input.json"
    if research_input.is_file():
        paths.append(("research-input.json", research_input))
    conversation = read_json(root / "conversation.json")
    manifests = conversation.get("research_manifests")
    if isinstance(manifests, list):
        for index, pointer in enumerate(manifests, 1):
            if not isinstance(pointer, dict) or not isinstance(pointer.get("path"), str):
                raise ReviewError("Research manifest pointer is invalid")
            source = (root / pointer["path"]).resolve()
            try:
                source.relative_to(root.resolve())
            except ValueError as exc:
                raise ReviewError("Research manifest escapes its task") from exc
            manifest = read_json(source)
            manifest_core = dict(manifest)
            stored_hash = manifest_core.pop("manifest_sha256", None)
            expected_hash = sha256_value(manifest_core)
            if stored_hash != expected_hash or pointer.get("sha256") != expected_hash:
                raise ReviewError("Research manifest hash differs")
            paths.append((f"research-manifest-{index:04d}.json", source))
    latest = conversation.get("latest_plan_candidate")
    if isinstance(latest, dict) and isinstance(latest.get("path"), str):
        candidate = (root / latest["path"]).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ReviewError("Plan candidate escapes its task") from exc
        paths.append(("lead-candidate.json", candidate))
        candidate_value = read_json(candidate)
        expected_candidate_hash = sha256_value({
            "intent_sha256": candidate_value.get("intent_sha256"),
            "decision": candidate_value.get("decision"),
        })
        if (
            candidate_value.get("candidate_sha256") != expected_candidate_hash
            or latest.get("candidate_sha256") != expected_candidate_hash
        ):
            raise ReviewError("Visible plan candidate hash differs")
        intent_hash = candidate_value.get("intent_sha256")
        if not isinstance(intent_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", intent_hash):
            raise ReviewError("Plan candidate has no valid intent hash")
        paths.append(("plan-intent.json", root / "planning" / f"intent-{intent_hash}.json"))
    for index, path in enumerate(sorted((root / "audit" / "events").glob("*.json")), 1):
        paths.append((f"audit-{index:08d}.json", path))
    if not any(name.startswith("audit-") for name, _path in paths):
        raise ReviewError("Task has no append-only audit evidence")
    return paths


def _copy_immutable(source: Path, target: Path) -> str:
    payload = source.read_bytes()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    os.chmod(target, 0o400)
    return hashlib.sha256(payload).hexdigest()


def _load_review_state(root: Path) -> dict[str, Any]:
    path = root / "codex-review.json"
    return read_json(path) if path.is_file() else {}


def _verify_protected_repository(repository: Path, base_checkpoint: str) -> None:
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30, check=False,
    )
    if head.returncode != 0 or head.stdout.strip().lower() != base_checkpoint.lower():
        raise ReviewError("Protected repository is not at the candidate base checkpoint")
    status = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain=v1",
         "--untracked-files=all"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30, check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise ReviewError("Protected repository must be clean before review packaging")


def prepare_review(workspace: Path, run_id: str) -> dict[str, Any]:
    root, action, context = resolve_jobs(workspace, run_id)
    with review_lock(root):
        snapshot = read_profile_snapshot(root)
        lifecycle = read_task_state(root)
        if lifecycle["state"] != TaskState.AWAITING_CODEX.value:
            raise ReviewError(
                f"Task is not awaiting Codex: {lifecycle['state']}"
            )
        current = _load_review_state(root)
        if (
            current.get("status") == "PREPARED"
            and current.get("run_id") == action.name
        ):
            _require_prepared_review(root, action)
            return current
        manifest_path, manifest, _supervisor = _candidate_checkpoint(action)
        _verify_protected_repository(
            context["repository"], str(manifest["base_checkpoint"]),
        )
        sources = _evidence_sources(root, action, manifest_path, manifest)
        research_input = (
            read_json(root / "research-input.json")
            if (root / "research-input.json").is_file() else {}
        )
        if research_input:
            research_core = dict(research_input)
            stored_research_hash = research_core.pop("research_input_sha256", None)
            if stored_research_hash != sha256_value(research_core):
                raise ReviewError("Research input hash differs")
        root_conversation = read_json(root / "conversation.json")
        research_manifest_hashes = [
            str(pointer.get("sha256"))
            for pointer in root_conversation.get("research_manifests", [])
            if isinstance(pointer, dict)
        ]
        latest_candidate = root_conversation.get("latest_plan_candidate")
        plan_candidate_hash = (
            str(latest_candidate.get("candidate_sha256"))
            if isinstance(latest_candidate, dict) else ""
        )
        # Audit and task-state files are evidence, not candidate identity: preparing
        # the review appends an audit event and must not manufacture a new review.
        fingerprint = sha256_value({
            "task_id": root.name,
            "run_id": action.name,
            "profile_snapshot_hash": snapshot["profile_snapshot_hash"],
            "base_checkpoint": manifest["base_checkpoint"],
            "patch_sha256": manifest["patch_sha256"],
            "candidate_files_sha256": manifest["candidate_files_sha256"],
            "research_manifest_sha256s": research_manifest_hashes,
            "plan_candidate_sha256": plan_candidate_hash,
        })
        review_id = f"RV-{action.name}-{fingerprint[:12]}"
        reviews = root / "codex-reviews"
        final_dir = reviews / review_id
        final_package = final_dir / "review-package.json"
        if final_package.is_file():
            package = read_json(final_package)
            if package.get("fingerprint") != fingerprint:
                raise ReviewError("Existing review package fingerprint differs")
            current = _load_review_state(root)
            if (
                current.get("status") == "PREPARED"
                and current.get("review_id") == review_id
                and current.get("package_hash") == package.get("package_hash")
            ):
                return current
        else:
            reviews.mkdir(parents=True, mode=0o700, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f".{review_id}-", dir=reviews))
            try:
                evidence_dir = temporary / "evidence"
                evidence_dir.mkdir(mode=0o700)
                entries = []
                for name, source in sources:
                    target = evidence_dir / name
                    digest = _copy_immutable(source, target)
                    entries.append({
                        "name": name,
                        "path": str(final_dir / "evidence" / name),
                        "sha256": digest,
                    })
                package = {
                    "schema_version": 1,
                    "review_id": review_id,
                    "task_id": root.name,
                    "run_id": action.name,
                    "created_at": utc_timestamp(),
                    "status": "PREPARED",
                    "fingerprint": fingerprint,
                    "profile_snapshot_hash": snapshot["profile_snapshot_hash"],
                    "model": snapshot["model"],
                    "model_digest": snapshot["model_digest"],
                    "owner_request": research_input.get("owner_request", ""),
                    "owner_request_sha256": research_input.get(
                        "owner_request_sha256", "",
                    ),
                    "codex_research_prompt": research_input.get(
                        "codex_research_prompt", "",
                    ),
                    "codex_research_prompt_sha256": research_input.get(
                        "codex_research_prompt_sha256", "",
                    ),
                    "research_manifest_sha256s": research_manifest_hashes,
                    "plan_candidate_sha256": plan_candidate_hash,
                    "base_checkpoint": manifest["base_checkpoint"],
                    "patch_sha256": manifest["patch_sha256"],
                    "candidate_files": manifest["candidate_files"],
                    "candidate_files_sha256": manifest["candidate_files_sha256"],
                    "protected_repository_unchanged": True,
                    "protected_repository_head": manifest["base_checkpoint"],
                    "test_evidence": manifest.get("test_evidence", []),
                    "local_reviewer_verdict": manifest.get(
                        "local_reviewer_verdict", "",
                    ),
                    "evidence": entries,
                    "instructions": (
                        "Review every evidence file and candidate.patch. If rejected, "
                        "return concrete findings. If approved, apply the reviewed "
                        "change in the real source and return the resulting commit SHA."
                    ),
                }
                package["package_hash"] = sha256_value(package)
                atomic_write_immutable_json(
                    temporary / "review-package.json", package,
                )
                os.replace(temporary, final_dir)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        package_hash = str(package["package_hash"])
        record_task_event(
            root, snapshot=snapshot, event_type="CODEX_REVIEW_PREPARED",
            actor_type="owner", actor_id="owner", output_hash=package_hash,
            details={
                "review_id": review_id,
                "package_path": str(final_package),
                "run_id": action.name,
            },
        )
        state = {
            "schema_version": 1,
            "review_id": review_id,
            "task_id": root.name,
            "run_id": action.name,
            "status": "PREPARED",
            "package_path": str(final_package),
            "package_hash": package_hash,
            "updated_at": utc_timestamp(),
        }
        atomic_write_json(root / "codex-review.json", state)
        return state


def _require_prepared_review(root: Path, action: Path) -> dict[str, Any]:
    review = _load_review_state(root)
    if review.get("status") != "PREPARED" or review.get("run_id") != action.name:
        raise ReviewError("No prepared Codex review exists for this candidate")
    package_path = Path(str(review.get("package_path") or ""))
    package = read_json(package_path)
    identity = dict(package)
    expected = identity.pop("package_hash", None)
    if not isinstance(expected, str) or sha256_value(identity) != expected:
        raise ReviewError("Codex review package hash mismatch")
    if expected != review.get("package_hash"):
        raise ReviewError("Review pointer and package hash differ")
    evidence = package.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ReviewError("Codex review package has no evidence")
    for entry in evidence:
        if not isinstance(entry, dict):
            raise ReviewError("Codex review evidence entry is invalid")
        path = Path(str(entry.get("path") or ""))
        digest = entry.get("sha256")
        if not path.is_file() or not isinstance(digest, str):
            raise ReviewError("Codex review evidence is missing")
        if file_sha256(path) != digest:
            raise ReviewError(f"Codex review evidence hash mismatch: {path.name}")
    return review


def record_codex_rejected(workspace: Path, run_id: str, feedback: str) -> dict[str, Any]:
    clean = feedback.strip()
    if not clean:
        raise ReviewError("Codex rejection requires concrete feedback")
    root, action, _context = resolve_jobs(workspace, run_id)
    with review_lock(root):
        review = _require_prepared_review(root, action)
        snapshot = read_profile_snapshot(root)
        rejection_path = Path(review["package_path"]).parent / "codex-rejection.json"
        rejection = {
            "schema_version": 1,
            "review_id": review["review_id"],
            "recorded_at": utc_timestamp(),
            "feedback": clean,
            "feedback_hash": sha256_text(clean),
        }
        atomic_write_immutable_json(rejection_path, rejection)
        transition_task(
            root, TaskState.CODEX_REJECTED, snapshot=snapshot,
            actor_type="owner", actor_id="owner", event_type="CODEX_REJECTED",
            input_hash=rejection["feedback_hash"],
            details={
                "review_id": review["review_id"],
                "feedback_path": str(rejection_path),
                "reported_actor": "codex",
            },
        )
        transition_task(
            root, TaskState.LEAD_REVIEW, snapshot=snapshot,
            actor_type="manager", actor_id="manager",
            event_type="CODEX_FEEDBACK_ATTACHED",
            input_hash=rejection["feedback_hash"],
            details={"review_id": review["review_id"]},
        )
        review.update({"status": "REJECTED", "updated_at": utc_timestamp()})
        atomic_write_json(root / "codex-review.json", review)
        return review


def _resolve_commit(repository: Path, commit: str) -> str:
    clean = commit.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", clean):
        raise ReviewError("Codex approval requires a Git commit SHA")
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", f"{clean}^{{commit}}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30, check=False,
    )
    resolved = result.stdout.strip().lower()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", resolved):
        raise ReviewError("Approved commit does not exist in the repository")
    return resolved


def _commit_candidate_files(
    repository: Path, base_checkpoint: str, commit: str,
) -> list[dict[str, Any]]:
    changed = subprocess.run(
        ["git", "-C", str(repository), "diff", "--no-renames", "--name-only",
         "-z", base_checkpoint, commit, "--"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=False,
    )
    if changed.returncode != 0:
        raise ReviewError("Cannot determine approved commit paths")
    paths = sorted(
        item.decode("utf-8", errors="strict")
        for item in changed.stdout.split(b"\0") if item
    )
    result: list[dict[str, Any]] = []
    for path in paths:
        tree = subprocess.run(
            ["git", "-C", str(repository), "ls-tree", commit, "--", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=30, check=False,
        )
        if tree.returncode != 0:
            raise ReviewError(f"Cannot inspect approved path: {path}")
        line = tree.stdout.strip()
        if not line:
            result.append({
                "path": path, "state": "deleted", "sha256": None, "mode": None,
            })
            continue
        mode = line.split(None, 1)[0]
        blob = subprocess.run(
            ["git", "-C", str(repository), "show", f"{commit}:{path}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False,
        )
        if blob.returncode != 0:
            raise ReviewError(f"Cannot read approved path: {path}")
        result.append({
            "path": path,
            "state": "present",
            "sha256": hashlib.sha256(blob.stdout).hexdigest(),
            "mode": int(mode[-3:], 8),
        })
    return result


def record_codex_approved(workspace: Path, run_id: str, commit: str) -> dict[str, Any]:
    root, action, context = resolve_jobs(workspace, run_id)
    with review_lock(root):
        review = _require_prepared_review(root, action)
        snapshot = read_profile_snapshot(root)
        package = read_json(Path(review["package_path"]))
        resolved = _resolve_commit(context["repository"], commit)
        if resolved == str(package["base_checkpoint"]).lower():
            raise ReviewError("Approved commit must contain a change after the candidate base")
        ancestor = subprocess.run(
            ["git", "-C", str(context["repository"]), "merge-base", "--is-ancestor",
             str(package["base_checkpoint"]), resolved],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
        if ancestor.returncode != 0:
            raise ReviewError("Approved commit does not descend from the candidate base")
        actual_files = _commit_candidate_files(
            context["repository"], str(package["base_checkpoint"]), resolved,
        )
        if actual_files != package.get("candidate_files"):
            raise ReviewError(
                "Approved commit paths or final blob hashes differ from the "
                "reviewed candidate"
            )
        approval_path = Path(review["package_path"]).parent / "codex-approval.json"
        approval = {
            "schema_version": 1,
            "review_id": review["review_id"],
            "recorded_at": utc_timestamp(),
            "commit": resolved,
            "commit_hash": sha256_text(resolved),
            "candidate_files_sha256": package["candidate_files_sha256"],
        }
        atomic_write_immutable_json(approval_path, approval)
        transition_task(
            root, TaskState.CODEX_APPROVED, snapshot=snapshot,
            actor_type="owner", actor_id="owner", event_type="CODEX_APPROVED",
            checkpoint=resolved, input_hash=approval["commit_hash"],
            details={
                "review_id": review["review_id"],
                "approval_path": str(approval_path),
                "reported_actor": "codex",
            },
        )
        transition_task(
            root, TaskState.APPLIED, snapshot=snapshot,
            actor_type="manager", actor_id="manager", event_type="SOURCE_APPLIED",
            checkpoint=resolved, input_hash=approval["commit_hash"],
            details={"review_id": review["review_id"], "commit": resolved},
        )
        review.update({
            "status": "APPLIED", "commit": resolved, "updated_at": utc_timestamp(),
        })
        atomic_write_json(root / "codex-review.json", review)
        return review
