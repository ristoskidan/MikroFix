#!/usr/bin/env python3
"""Deterministic task identity, state transitions and append-only audit."""

from __future__ import annotations

from contextlib import contextmanager
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from typing import Any, Iterator
import urllib.parse
import urllib.request
import uuid


SHA256_RE = re.compile(r"[0-9a-f]{64}")
RUNTIME_OPTION_FIELDS = (
    "num_predict", "temperature", "top_k", "top_p", "min_p",
    "repeat_penalty", "seed",
)


class LifecycleError(RuntimeError):
    """Fail-closed lifecycle or identity validation error."""


class TaskState(str, Enum):
    DRAFT = "DRAFT"
    READY_FOR_PROCESSING = "READY_FOR_PROCESSING"
    AWAITING_OWNER_APPROVAL = "AWAITING_OWNER_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    LEAD_REVIEW = "LEAD_REVIEW"
    CANDIDATE_READY = "CANDIDATE_READY"
    AWAITING_CODEX = "AWAITING_CODEX"
    CODEX_REJECTED = "CODEX_REJECTED"
    CODEX_APPROVED = "CODEX_APPROVED"
    APPLIED = "APPLIED"
    BLOCKED = "BLOCKED"


ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DRAFT: frozenset({TaskState.READY_FOR_PROCESSING, TaskState.BLOCKED}),
    TaskState.READY_FOR_PROCESSING: frozenset({TaskState.AWAITING_OWNER_APPROVAL, TaskState.BLOCKED}),
    TaskState.AWAITING_OWNER_APPROVAL: frozenset({
        TaskState.DRAFT, TaskState.APPROVED, TaskState.BLOCKED,
    }),
    TaskState.APPROVED: frozenset({TaskState.EXECUTING, TaskState.BLOCKED}),
    TaskState.EXECUTING: frozenset({TaskState.LEAD_REVIEW, TaskState.BLOCKED}),
    TaskState.LEAD_REVIEW: frozenset({TaskState.CANDIDATE_READY, TaskState.BLOCKED}),
    TaskState.CANDIDATE_READY: frozenset({TaskState.AWAITING_CODEX, TaskState.BLOCKED}),
    TaskState.AWAITING_CODEX: frozenset({
        TaskState.CODEX_REJECTED, TaskState.CODEX_APPROVED,
    }),
    TaskState.CODEX_REJECTED: frozenset({TaskState.LEAD_REVIEW}),
    TaskState.CODEX_APPROVED: frozenset({TaskState.APPLIED}),
    TaskState.APPLIED: frozenset(),
    TaskState.BLOCKED: frozenset(),
}


EVENT_TYPES = frozenset({
    "TASK_CREATED", "LEAD_SELECTED", "LEAD_MESSAGE", "LEAD_PROPOSED_PLAN",
    "OWNER_REPLACED_GOAL", "OWNER_REJECTED_PLAN",
    "CONTEXT_REQUESTED", "CONTEXT_PACK_CREATED", "OWNER_APPROVED_PLAN",
    "ATOM_STARTED", "ATOM_COMPLETED", "LEAD_REQUESTED_REPAIR",
    "LEAD_ACCEPTED_CANDIDATE", "CODEX_REJECTED", "CODEX_FEEDBACK_ATTACHED",
    "CODEX_APPROVED", "SOURCE_APPLIED", "CODEX_REVIEW_PREPARED",
    "TASK_STATE_TRANSITION", "TASK_BLOCKED",
})


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any], *, immutable: bool) -> str:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
    ).encode("utf-8") + b"\n"
    if immutable:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        except FileExistsError as exc:
            raise LifecycleError(f"Immutable artifact already exists: {path}") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        os.chmod(path, 0o400)
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"{field} must be a non-empty string")
    return value.strip()


def load_profile_registry(path: Path, project_root: Path) -> dict[str, dict[str, Any]]:
    """Load and strictly validate the versioned lead profile registry."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"Cannot load lead profile registry: {path}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "profiles"}:
        raise LifecycleError("Lead profile registry has unknown or missing fields")
    schema_version = raw["schema_version"]
    if schema_version not in {1, 2} or not isinstance(raw["profiles"], dict):
        raise LifecycleError("Unsupported lead profile registry schema")
    result: dict[str, dict[str, Any]] = {}
    expected = {
        "version", "model", "role", "display_name", "num_ctx", "think",
        "system_prompt", "keep_alive", "enabled",
    }
    if schema_version == 2:
        expected |= {"conversation_num_ctx", "conversation_keep_alive"}
    for profile_id, profile in raw["profiles"].items():
        if not isinstance(profile_id, str) or not re.fullmatch(r"[a-z0-9_]+", profile_id):
            raise LifecycleError(f"Unsafe lead profile id: {profile_id!r}")
        if not isinstance(profile, dict) or set(profile) != expected:
            raise LifecycleError(f"Profile {profile_id} has unknown or missing fields")
        version = profile["version"]
        num_ctx = profile["num_ctx"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise LifecycleError(f"Profile {profile_id} version must be positive")
        if isinstance(num_ctx, bool) or not isinstance(num_ctx, int) or num_ctx < 1024:
            raise LifecycleError(f"Profile {profile_id} num_ctx is invalid")
        conversation_num_ctx = (
            profile["conversation_num_ctx"]
            if schema_version == 2 else min(num_ctx, 16384)
        )
        if (
            isinstance(conversation_num_ctx, bool)
            or not isinstance(conversation_num_ctx, int)
            or conversation_num_ctx < 1024
            or conversation_num_ctx > num_ctx
        ):
            raise LifecycleError(
                f"Profile {profile_id} conversation_num_ctx is invalid"
            )
        if not isinstance(profile["enabled"], bool):
            raise LifecycleError(f"Profile {profile_id} enabled must be boolean")
        think = profile["think"]
        if not isinstance(think, (bool, str)) or isinstance(think, str) and not think.strip():
            raise LifecycleError(f"Profile {profile_id} think is invalid")
        relative_prompt = _required_string(profile["system_prompt"], "system_prompt")
        prompt_path = PurePosixPath(relative_prompt)
        if prompt_path.is_absolute() or ".." in prompt_path.parts:
            raise LifecycleError(f"Profile {profile_id} system_prompt is unsafe")
        resolved_prompt = (project_root / relative_prompt).resolve()
        try:
            resolved_prompt.relative_to(project_root.resolve())
        except ValueError as exc:
            raise LifecycleError(f"Profile {profile_id} system_prompt escapes project") from exc
        if not resolved_prompt.is_file():
            raise LifecycleError(f"Profile {profile_id} system_prompt does not exist")
        result[profile_id] = {
            "profile_id": profile_id,
            "version": version,
            "model": _required_string(profile["model"], "model"),
            "role": _required_string(profile["role"], "role"),
            "display_name": _required_string(profile["display_name"], "display_name"),
            "num_ctx": num_ctx,
            "conversation_num_ctx": conversation_num_ctx,
            "think": think,
            "system_prompt": relative_prompt,
            "keep_alive": _required_string(profile["keep_alive"], "keep_alive"),
            "conversation_keep_alive": _required_string(
                profile.get("conversation_keep_alive", "5m"),
                "conversation_keep_alive",
            ),
            "enabled": profile["enabled"],
        }
    if not result:
        raise LifecycleError("Lead profile registry must not be empty")
    return result


def resolve_ollama_digest(ollama_url: str, model: str) -> str:
    """Resolve the exact locally installed digest for a model tag."""
    parsed = urllib.parse.urlsplit(_required_string(ollama_url, "ollama_url"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LifecycleError("Invalid Ollama URL")
    tags_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/api/tags", "", ""))
    try:
        with urllib.request.urlopen(tags_url, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise LifecycleError("Cannot resolve local Ollama model digest") from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise LifecycleError("Ollama tags response has an invalid shape")
    for item in models:
        if isinstance(item, dict) and model in {item.get("name"), item.get("model")}:
            digest = str(item.get("digest") or "").lower()
            if not SHA256_RE.fullmatch(digest):
                raise LifecycleError(f"Ollama returned an invalid digest for {model}")
            return digest
    raise LifecycleError(f"Configured Ollama model is not installed: {model}")


def build_profile_snapshot(
    profile: dict[str, Any], *, project_root: Path, model_digest: str,
) -> dict[str, Any]:
    digest = model_digest.lower()
    if not SHA256_RE.fullmatch(digest):
        raise LifecycleError("model_digest must be a SHA-256 digest")
    prompt_path = (project_root / str(profile["system_prompt"])).resolve()
    runtime_options = {
        field: (profile.get("runtime_options") or {}).get(field)
        for field in RUNTIME_OPTION_FIELDS
    }
    _validate_runtime_options(runtime_options)
    snapshot = {
        "schema_version": 3,
        "profile_id": profile["profile_id"],
        "profile_version": profile["version"],
        "model": profile["model"],
        "model_tag": profile["model"],
        "model_digest": digest,
        "role": profile["role"],
        "display_name": profile["display_name"],
        "num_ctx": profile["num_ctx"],
        "conversation_num_ctx": profile["conversation_num_ctx"],
        "think": profile["think"],
        "system_prompt": profile["system_prompt"],
        "system_prompt_hash": hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        "keep_alive": profile["keep_alive"],
        "conversation_keep_alive": profile["conversation_keep_alive"],
        "runtime_options": runtime_options,
        "created_at": utc_timestamp(),
    }
    snapshot["profile_snapshot_hash"] = sha256_value(snapshot)
    return snapshot


def validate_profile_snapshot(snapshot: object) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise LifecycleError("Lead profile snapshot must be an object")
    legacy_required = {
        "schema_version", "profile_id", "profile_version", "model", "model_tag",
        "model_digest", "role", "display_name", "num_ctx", "think",
        "system_prompt", "system_prompt_hash", "keep_alive", "created_at",
        "profile_snapshot_hash",
    }
    v2_required = legacy_required | {
        "conversation_num_ctx", "conversation_keep_alive",
    }
    current_required = v2_required | {"runtime_options"}
    schema_version = snapshot.get("schema_version")
    required = (
        legacy_required if schema_version == 1
        else v2_required if schema_version == 2
        else current_required
    )
    if schema_version not in {1, 2, 3} or set(snapshot) != required:
        raise LifecycleError("Lead profile snapshot has unknown or missing fields")
    if snapshot["model"] != snapshot["model_tag"]:
        raise LifecycleError("Lead profile model and tag differ")
    for field in (
        "profile_id", "model", "model_tag", "role", "display_name",
        "system_prompt", "keep_alive", "created_at",
    ):
        _required_string(snapshot.get(field), field)
    if (isinstance(snapshot["profile_version"], bool)
            or not isinstance(snapshot["profile_version"], int)
            or snapshot["profile_version"] < 1):
        raise LifecycleError("profile_version must be a positive integer")
    if (isinstance(snapshot["num_ctx"], bool)
            or not isinstance(snapshot["num_ctx"], int)
            or snapshot["num_ctx"] < 1024):
        raise LifecycleError("num_ctx is invalid")
    if schema_version in {2, 3}:
        _required_string(
            snapshot.get("conversation_keep_alive"),
            "conversation_keep_alive",
        )
        if (
            isinstance(snapshot["conversation_num_ctx"], bool)
            or not isinstance(snapshot["conversation_num_ctx"], int)
            or snapshot["conversation_num_ctx"] < 1024
            or snapshot["conversation_num_ctx"] > snapshot["num_ctx"]
        ):
            raise LifecycleError("conversation_num_ctx is invalid")
    if schema_version == 3:
        _validate_runtime_options(snapshot["runtime_options"])
    if not isinstance(snapshot["think"], (bool, str)):
        raise LifecycleError("think is invalid")
    for field in ("model_digest", "system_prompt_hash", "profile_snapshot_hash"):
        if not isinstance(snapshot[field], str) or not SHA256_RE.fullmatch(snapshot[field].lower()):
            raise LifecycleError(f"{field} must be a SHA-256 digest")
    identity = dict(snapshot)
    expected = identity.pop("profile_snapshot_hash").lower()
    if sha256_value(identity) != expected:
        raise LifecycleError("Lead profile snapshot hash mismatch")
    return dict(snapshot)


def _validate_runtime_options(value: object) -> None:
    if not isinstance(value, dict) or set(value) != set(RUNTIME_OPTION_FIELDS):
        raise LifecycleError("runtime_options has unknown or missing fields")
    integer_fields = {"num_predict", "top_k", "seed"}
    number_fields = {"temperature", "top_p", "min_p", "repeat_penalty"}
    for field in integer_fields:
        item = value[field]
        if item is not None and (isinstance(item, bool) or not isinstance(item, int)):
            raise LifecycleError(f"runtime_options.{field} must be integer or null")
    for field in number_fields:
        item = value[field]
        if item is not None and (isinstance(item, bool) or not isinstance(item, (int, float))):
            raise LifecycleError(f"runtime_options.{field} must be number or null")
    if value["num_predict"] is not None and value["num_predict"] < 1:
        raise LifecycleError("runtime_options.num_predict is out of range")
    if value["top_k"] is not None and not 0 <= value["top_k"] <= 1000:
        raise LifecycleError("runtime_options.top_k is out of range")
    if value["seed"] is not None and value["seed"] < -1:
        raise LifecycleError("runtime_options.seed is out of range")
    for field in ("top_p", "min_p"):
        if value[field] is not None and not 0 <= value[field] <= 1:
            raise LifecycleError(f"runtime_options.{field} is out of range")
    if value["temperature"] is not None and not 0 <= value["temperature"] <= 2:
        raise LifecycleError("runtime_options.temperature is out of range")
    if value["repeat_penalty"] is not None and not 0 <= value["repeat_penalty"] <= 2:
        raise LifecycleError("runtime_options.repeat_penalty is out of range")


def write_profile_snapshot(job_dir: Path, snapshot: dict[str, Any]) -> Path:
    validate_profile_snapshot(snapshot)
    path = job_dir / "lead-profile-snapshot.json"
    _atomic_json(path, snapshot, immutable=True)
    return path


def write_system_prompt_snapshot(
    job_dir: Path, *, project_root: Path, snapshot: dict[str, Any],
) -> Path:
    """Persist the exact prompt bytes selected by the immutable profile."""
    locked = validate_profile_snapshot(snapshot)
    source = (project_root / locked["system_prompt"]).resolve()
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != locked["system_prompt_hash"]:
        raise LifecycleError("System prompt changed before snapshot persistence")
    path = job_dir / "lead-system-prompt.md"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError as exc:
        raise LifecycleError(f"Immutable artifact already exists: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o400)
    return path


def read_system_prompt_snapshot(job_dir: Path, snapshot: dict[str, Any]) -> Path:
    locked = validate_profile_snapshot(snapshot)
    path = job_dir / "lead-system-prompt.md"
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise LifecycleError("Missing immutable lead system prompt") from exc
    if digest != locked["system_prompt_hash"]:
        raise LifecycleError("Immutable lead system prompt hash mismatch")
    return path


def read_profile_snapshot(job_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((job_dir / "lead-profile-snapshot.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("Missing or invalid lead profile snapshot") from exc
    return validate_profile_snapshot(value)


@contextmanager
def _task_lock(job_dir: Path) -> Iterator[None]:
    job_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (job_dir / "task-lifecycle.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _optional_hash(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value.lower()):
        raise LifecycleError(f"{field} must be null or a SHA-256 digest")
    return value.lower()


def _last_event(job_dir: Path) -> dict[str, Any] | None:
    paths = sorted((job_dir / "audit" / "events").glob("*.json"))
    if not paths:
        return None
    previous_id: str | None = None
    last: dict[str, Any] | None = None
    for sequence, path in enumerate(paths, start=1):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LifecycleError("Cannot read previous audit event") from exc
        if not isinstance(event, dict) or event.get("sequence") != sequence:
            raise LifecycleError("Audit event sequence is invalid")
        if event.get("parent_event_id") != previous_id:
            raise LifecycleError("Audit parent chain is invalid")
        identity = dict(event)
        event_hash = identity.pop("event_hash", None)
        if not isinstance(event_hash, str) or sha256_value(identity) != event_hash:
            raise LifecycleError("Audit event hash mismatch")
        previous_id = _required_string(event.get("event_id"), "event_id")
        last = event
    return last


def _append_event(
    job_dir: Path, *, event_type: str, task_id: str, conversation_id: str,
    actor_type: str, actor_id: str, snapshot: dict[str, Any],
    prompt_hash: str | None = None, context_pack_hash: str | None = None,
    checkpoint: str | None = None, input_hash: str | None = None,
    output_hash: str | None = None, details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise LifecycleError(f"Unknown audit event type: {event_type}")
    locked = validate_profile_snapshot(snapshot)
    previous = _last_event(job_dir)
    sequence = int(previous["sequence"]) + 1 if previous else 1
    event = {
        "schema_version": 1,
        "sequence": sequence,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "task_id": _required_string(task_id, "task_id"),
        "conversation_id": _required_string(conversation_id, "conversation_id"),
        "timestamp": utc_timestamp(),
        "actor_type": _required_string(actor_type, "actor_type"),
        "actor_id": _required_string(actor_id, "actor_id"),
        "model": locked["model"],
        "model_tag": locked["model_tag"],
        "model_digest": locked["model_digest"],
        "profile_snapshot_hash": locked["profile_snapshot_hash"],
        "prompt_hash": _optional_hash(prompt_hash, "prompt_hash"),
        "context_pack_hash": _optional_hash(context_pack_hash, "context_pack_hash"),
        "checkpoint": checkpoint,
        "input_hash": _optional_hash(input_hash, "input_hash"),
        "output_hash": _optional_hash(output_hash, "output_hash"),
        "parent_event_id": previous.get("event_id") if previous else None,
        "details": details or {},
    }
    if checkpoint is not None and (not isinstance(checkpoint, str) or not checkpoint.strip()):
        raise LifecycleError("checkpoint must be null or non-empty")
    if not isinstance(event["details"], dict):
        raise LifecycleError("audit details must be an object")
    event["event_hash"] = sha256_value(event)
    path = job_dir / "audit" / "events" / f"{sequence:08d}-{event['event_id']}.json"
    _atomic_json(path, event, immutable=True)
    return event


def initialize_task_lifecycle(
    job_dir: Path, *, task_id: str, conversation_id: str,
    snapshot: dict[str, Any], input_text: str, checkpoint: str | None = None,
) -> dict[str, Any]:
    with _task_lock(job_dir):
        if (job_dir / "task-state.json").exists():
            raise LifecycleError("Task lifecycle already exists")
        first = _append_event(
            job_dir, event_type="TASK_CREATED", task_id=task_id,
            conversation_id=conversation_id, actor_type="owner", actor_id="owner",
            snapshot=snapshot, input_hash=sha256_text(input_text),
            checkpoint=checkpoint,
            details={"state": TaskState.DRAFT.value},
        )
        selected = _append_event(
            job_dir, event_type="LEAD_SELECTED", task_id=task_id,
            conversation_id=conversation_id, actor_type="manager", actor_id="manager",
            snapshot=snapshot, checkpoint=checkpoint, details={
                "state": TaskState.DRAFT.value,
                "selected_after_event": first["event_id"],
            },
        )
        state = {
            "schema_version": 1, "task_id": task_id,
            "conversation_id": conversation_id, "state": TaskState.DRAFT.value,
            "profile_snapshot_hash": snapshot["profile_snapshot_hash"],
            "checkpoint": checkpoint,
            "last_event_id": selected["event_id"],
            "updated_at": utc_timestamp(),
        }
        _atomic_json(job_dir / "task-state.json", state, immutable=False)
        return state


def read_task_state(job_dir: Path) -> dict[str, Any]:
    try:
        state = json.loads((job_dir / "task-state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("Missing or invalid task lifecycle state") from exc
    required = {
        "schema_version", "task_id", "conversation_id", "state",
        "profile_snapshot_hash", "checkpoint", "last_event_id", "updated_at",
    }
    if not isinstance(state, dict) or set(state) != required or state["schema_version"] != 1:
        raise LifecycleError("Unsupported task lifecycle state")
    try:
        TaskState(state["state"])
    except (TypeError, ValueError) as exc:
        raise LifecycleError("Unknown task state") from exc
    last = _last_event(job_dir)
    if last is None or state["last_event_id"] != last.get("event_id"):
        raise LifecycleError("Task state does not match the audit tail")
    return state


def transition_task(
    job_dir: Path, target: TaskState, *, snapshot: dict[str, Any],
    actor_type: str, actor_id: str, event_type: str = "TASK_STATE_TRANSITION",
    prompt_hash: str | None = None, context_pack_hash: str | None = None,
    checkpoint: str | None = None, input_hash: str | None = None,
    output_hash: str | None = None, details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _task_lock(job_dir):
        state = read_task_state(job_dir)
        locked = validate_profile_snapshot(snapshot)
        if state["profile_snapshot_hash"] != locked["profile_snapshot_hash"]:
            raise LifecycleError("Task state and profile snapshot differ")
        current = TaskState(state["state"])
        if target not in ALLOWED_TRANSITIONS[current]:
            raise LifecycleError(
                f"Forbidden task transition: {current.value} -> {target.value}"
            )
        if (
            current is TaskState.AWAITING_OWNER_APPROVAL
            and target is TaskState.DRAFT
            and event_type != "OWNER_REJECTED_PLAN"
        ):
            raise LifecycleError(
                "AWAITING_OWNER_APPROVAL -> DRAFT requires OWNER_REJECTED_PLAN"
            )
        event_details = {"from_state": current.value, "to_state": target.value}
        if details:
            event_details.update(details)
        event = _append_event(
            job_dir, event_type=event_type, task_id=state["task_id"],
            conversation_id=state["conversation_id"], actor_type=actor_type,
            actor_id=actor_id, snapshot=locked, prompt_hash=prompt_hash,
            context_pack_hash=context_pack_hash,
            checkpoint=checkpoint if checkpoint is not None else state["checkpoint"],
            input_hash=input_hash, output_hash=output_hash, details=event_details,
        )
        state["state"] = target.value
        if checkpoint is not None:
            state["checkpoint"] = checkpoint
        state["last_event_id"] = event["event_id"]
        state["updated_at"] = utc_timestamp()
        _atomic_json(job_dir / "task-state.json", state, immutable=False)
        return state


def record_task_event(
    job_dir: Path, *, snapshot: dict[str, Any], event_type: str,
    actor_type: str, actor_id: str, prompt_hash: str | None = None,
    context_pack_hash: str | None = None, checkpoint: str | None = None,
    input_hash: str | None = None, output_hash: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _task_lock(job_dir):
        state = read_task_state(job_dir)
        event = _append_event(
            job_dir, event_type=event_type, task_id=state["task_id"],
            conversation_id=state["conversation_id"], actor_type=actor_type,
            actor_id=actor_id, snapshot=snapshot, prompt_hash=prompt_hash,
            context_pack_hash=context_pack_hash,
            checkpoint=checkpoint if checkpoint is not None else state["checkpoint"],
            input_hash=input_hash, output_hash=output_hash, details=details,
        )
        state["last_event_id"] = event["event_id"]
        state["updated_at"] = utc_timestamp()
        _atomic_json(job_dir / "task-state.json", state, immutable=False)
        return event
