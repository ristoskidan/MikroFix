#!/usr/bin/env python3
"""Owner-facing intake and role-based planning for supervised local work."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Callable
import urllib.parse
import urllib.request

try:
    from tools.mikrofix_codex_review import (
        prepare_review, record_codex_approved, record_codex_rejected,
    )
    from tools.mikrofix_lead_session import (
        ensure_context_ready, ensure_conversation_session, select_planning_sources,
    )
    from tools.mikrofix_supervisor import (
        PROJECT_ROOT, atomic_write_immutable_json, atomic_write_json,
        validate_plan,
    )
    from tools.mikrofix_task_lifecycle import (
        RUNTIME_OPTION_FIELDS, TaskState, build_profile_snapshot, initialize_task_lifecycle,
        load_profile_registry, read_profile_snapshot, read_task_state,
        read_system_prompt_snapshot, record_task_event, resolve_ollama_digest,
        sha256_text, transition_task, write_profile_snapshot,
        write_system_prompt_snapshot,
    )
    from tools.mikrofix_workspace_lock import workspace_lock
    from tools.mikrofix_research import (
        MAX_OWNER_REQUEST_CHARS, MAX_RESEARCH_PROMPT_CHARS,
        ResearchError, assert_repository_identity, build_scope_catalog,
        repository_identity, normalize_scope, scope_correction_details,
        validate_questions,
    )
    from tools.mikrofix_research_dossier import (
        check_scope_expansion, create_dossier_revision,
        evidence_cards_from_trace, evidence_gate, load_current_dossier,
        record_codex_research_verdict, require_current_codex_approval,
    )
    from tools.mikrofix_research_orchestrator import (
        run_sequential_workers, validate_task_set,
    )
except ModuleNotFoundError:
    from mikrofix_codex_review import (  # type: ignore[no-redef]
        prepare_review, record_codex_approved, record_codex_rejected,
    )
    from mikrofix_lead_session import (  # type: ignore[no-redef]
        ensure_context_ready, ensure_conversation_session, select_planning_sources,
    )
    from mikrofix_supervisor import (  # type: ignore[no-redef]
        PROJECT_ROOT, atomic_write_immutable_json, atomic_write_json,
        validate_plan,
    )
    from mikrofix_task_lifecycle import (  # type: ignore[no-redef]
        RUNTIME_OPTION_FIELDS, TaskState, build_profile_snapshot, initialize_task_lifecycle,
        load_profile_registry, read_profile_snapshot, read_task_state,
        read_system_prompt_snapshot, record_task_event, resolve_ollama_digest,
        sha256_text, transition_task, write_profile_snapshot,
        write_system_prompt_snapshot,
    )
    from mikrofix_workspace_lock import workspace_lock  # type: ignore[no-redef]
    from mikrofix_research import (  # type: ignore[no-redef]
        MAX_OWNER_REQUEST_CHARS, MAX_RESEARCH_PROMPT_CHARS,
        ResearchError, assert_repository_identity, build_scope_catalog,
        repository_identity, normalize_scope, scope_correction_details,
        validate_questions,
    )
    from mikrofix_research_dossier import (  # type: ignore[no-redef]
        check_scope_expansion, create_dossier_revision,
        evidence_cards_from_trace, evidence_gate, load_current_dossier,
        record_codex_research_verdict, require_current_codex_approval,
    )
    from mikrofix_research_orchestrator import (  # type: ignore[no-redef]
        run_sequential_workers, validate_task_set,
    )


DEFAULT_WORKSPACE = Path.home() / "MikroFIX_Agent_Workspace"
BRIDGE_TEMPLATE = Path.home() / ".local/share/mikrofix-bridge/config.json"
PROFILE_REGISTRY = PROJECT_ROOT / "config/local_agents/lead_profiles.json"
ROLE_REGISTRY = PROJECT_ROOT / "config/local_agents/role_profiles.json"
MODEL_RUNTIME = PROJECT_ROOT / "config/local_agents/model_runtime.json"
PROPOSAL_SCHEMA = (
    PROJECT_ROOT / "docs/local_agents/supervisor/PLAN_PROPOSAL_SCHEMA.json"
)
MODEL_PLAN_FIELDS = {
    "objective", "allowed_files", "atoms", "constraints", "acceptance_criteria",
    # Accepted during migration but ignored; the manager computes trusted context.
    "context_links",
}
REQUIRED_MODEL_PLAN_FIELDS = MODEL_PLAN_FIELDS - {"context_links"}
PLAN_SOURCE_FIELDS = {
    "objective_source_turns", "decision_source_turns", "discussion_only_turns",
}
MAX_PLANNER_ATTEMPTS = 3
BRIDGE_TOOL_NAMES = {
    "project_status", "project_read", "project_list", "project_search",
    "project_diff", "project_apply_patch", "project_create", "project_replace",
    "project_run_check", "system_command", "desktop_inspect", "desktop_action",
}
PLANNING_DRAFTER_MODEL = "qwen3:4b-thinking-2507-q8_0"
PLAN_DRAFTER_SYSTEM = (
    PROJECT_ROOT / "docs/local_agents/roles/PLAN_DRAFTER_SYSTEM.md"
)
PLAN_PREPARER_SYSTEM = (
    PROJECT_ROOT / "docs/local_agents/roles/PLAN_PREPARER_SYSTEM.md"
)
GPT_VALIDATOR_SYSTEM = (
    PROJECT_ROOT / "docs/local_agents/roles/PLAN_VALIDATOR_SYSTEM.md"
)
LEAD_CONVERSATION_SYSTEM = (
    PROJECT_ROOT / "docs/local_agents/roles/CONVERSATION_LEAD_SYSTEM.md"
)
MAX_CONVERSATION_TURNS = 40


def research_decomposition_schema() -> dict[str, Any]:
    task = json.loads(
        (PROJECT_ROOT / "docs/local_agents/supervisor/RESEARCH_TASK_SCHEMA.json")
        .read_text(encoding="utf-8")
    )
    # The manager adds immutable identity fields after validating model output.
    properties = dict(task["properties"])
    properties.pop("schema_version", None)
    properties.pop("original_task_sha256", None)
    required = [
        item for item in task["required"]
        if item not in {"schema_version", "original_task_sha256"}
    ]
    return {
        "type": "object", "additionalProperties": False,
        "required": ["tasks"],
        "properties": {"tasks": {
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": required, "properties": properties,
            },
        }},
    }


def compact_scope_catalog(
    catalog: dict[str, Any], *, context: str, max_files: int = 80,
    max_directories: int = 40,
) -> dict[str, Any]:
    """Select a compact hint set; the complete catalog remains manager-only."""
    tokens = {
        token.lower() for token in re.findall(r"[A-Za-z0-9_]{4,}", context)
    }
    files = list(catalog.get("files") or [])
    scored = sorted(
        files,
        key=lambda path: (
            -sum(token in path.lower() for token in tokens),
            len(path), path,
        ),
    )
    relevant = [
        path for path in scored
        if any(token in path.lower() for token in tokens)
    ][:max_files]
    directories = list(catalog.get("directories") or [])
    top_level = [path for path in directories if "/" not in path]
    related_directories = sorted({
        parent
        for path in relevant
        for parent in [str(Path(path).parent).replace(os.sep, "/")]
        if parent != "." and parent in directories
    })
    selected_directories = list(dict.fromkeys(
        [*top_level, *related_directories]
    ))[:max_directories]
    return {
        "repository_checkpoint": catalog["repository_checkpoint"],
        "allowed_directories": selected_directories,
        "relevant_existing_files": relevant,
        "instruction": (
            "Use only an exact listed path. If the exact file is unknown, "
            "choose an existing allowed directory; never invent a filename."
        ),
    }


def worker_result_schema() -> dict[str, Any]:
    return json.loads(
        (PROJECT_ROOT / "docs/local_agents/supervisor/WORKER_RESULT_SCHEMA.json")
        .read_text(encoding="utf-8")
    )


def lead_review_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["decision", "reason", "missing_evidence"],
        "properties": {
            "decision": {"enum": ["ACCEPT", "REVISE", "BLOCKED"]},
            "reason": {"type": "string", "maxLength": 4000},
            "missing_evidence": {
                "type": "array", "maxItems": 12,
                "items": {"type": "string", "maxLength": 500},
            },
        },
    }


def lead_synthesis_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["synthesis", "unresolved"],
        "properties": {
            "synthesis": {
                "type": "array", "maxItems": 64,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["claim", "evidence_ids"],
                    "properties": {
                        "claim": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "evidence_ids": {
                            "type": "array", "minItems": 1, "maxItems": 12,
                            "uniqueItems": True,
                            "items": {"type": "string", "maxLength": 100},
                        },
                    },
                },
            },
            "unresolved": {
                "type": "array", "maxItems": 64,
                "items": {"type": "string", "maxLength": 4000},
            },
        },
    }


def conversation_response_schema(*, planning_requested: bool = True) -> dict[str, Any]:
    if not planning_requested:
        return {
            "type": "object", "additionalProperties": False,
            "required": ["reply"],
            "properties": {"reply": {"type": "string", "minLength": 1}},
        }
    return {
        "type": "object", "additionalProperties": False,
        "required": [
            "reply", "routing", "objective", "allowed_files",
            "atoms", "constraints", "acceptance_criteria",
            "objective_source_turns", "decision_source_turns",
            "discussion_only_turns",
        ],
        "properties": {
            "reply": {"type": "string", "minLength": 1},
            "routing": {
                "type": "string",
                "enum": ["NEEDS_INFO", "READY_FOR_PROCESSING"],
            },
            "objective": {"type": "string"},
            "allowed_files": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "atoms": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "task", "expected_files"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "task": {"type": "string", "minLength": 1},
                        "expected_files": {
                            "type": "array", "minItems": 1, "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
            "constraints": {
                "type": "array", "items": {"type": "string", "minLength": 1},
            },
            "acceptance_criteria": {
                "type": "array", "items": {"type": "string", "minLength": 1},
            },
            "objective_source_turns": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "decision_source_turns": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "discussion_only_turns": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }


RESEARCH_ENVELOPE_RE = re.compile(
    r"\A\s*worker_model=(?P<worker>[^\s]+)\s*\n+"
    r"\[OWNER_REQUEST\]\s*\n(?P<owner>.*?)\n\s*\[/OWNER_REQUEST\]"
    r"\s*\n+\s*\[CODEX_RESEARCH_PROMPT\]\s*\n(?P<prompt>.*?)\n\s*"
    r"\[/CODEX_RESEARCH_PROMPT\]\s*\n+\s*"
    r"\[REQUIRED_QUESTIONS\]\s*\n(?P<questions>.*?)\n\s*"
    r"\[/REQUIRED_QUESTIONS\]\s*\Z",
    flags=re.DOTALL,
)
EVIDENCE_BEARING_TOOLS = frozenset({"project_read", "project_search"})


def parse_research_envelope(value: str) -> dict[str, Any]:
    """Parse the explicit owner/Codex boundary for one new research task."""
    match = RESEARCH_ENVELOPE_RE.fullmatch(value.strip())
    if not match:
        raise ManagerError(
            "Ukaz /research zahteva worker_model=<tag> ter natanko razdelke "
            "[OWNER_REQUEST], [CODEX_RESEARCH_PROMPT] in [REQUIRED_QUESTIONS]."
        )
    worker_model = match.group("worker").strip()
    owner_request = match.group("owner").strip()
    codex_prompt = match.group("prompt").strip()
    if not worker_model or not owner_request or not codex_prompt:
        raise ManagerError("Model in vsi razdelki /research morajo biti neprazni")
    if len(owner_request) > MAX_OWNER_REQUEST_CHARS:
        raise ManagerError("OWNER_REQUEST je predolg")
    if len(codex_prompt) > MAX_RESEARCH_PROMPT_CHARS:
        raise ManagerError("CODEX_RESEARCH_PROMPT je predolg")
    questions = []
    for raw_line in match.group("questions").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        question_match = re.fullmatch(r"(Q[1-9][0-9]{0,2}):\s*(.+)", line)
        if not question_match:
            raise ManagerError("Vsako obvezno vprašanje mora imeti obliko Q1: besedilo")
        questions.append({
            "id": question_match.group(1), "question": question_match.group(2),
        })
    try:
        questions = validate_questions(questions)
    except ResearchError as exc:
        raise ManagerError(str(exc)) from exc
    core = {
        "schema_version": 1,
        "worker_model": worker_model,
        "owner_request": owner_request,
        "owner_request_sha256": sha256_text(owner_request),
        "codex_research_prompt": codex_prompt,
        "codex_research_prompt_sha256": sha256_text(codex_prompt),
        "required_questions": questions,
        "required_questions_sha256": canonical_json_sha256(questions),
    }
    return {**core, "research_input_sha256": canonical_json_sha256(core)}


def binding_owner_criterion(owner_request: str) -> str:
    """Keep every owner requirement inside the evidence-scored review gate."""
    request = owner_request.strip()
    return (
        "Treat the original owner request as binding in full. Enumerate every "
        "explicit requirement and restriction from the quoted request, cite "
        "concrete candidate evidence for each one, and FAIL this criterion if "
        "any item is omitted, weakened, contradicted, or unsupported. Original "
        f"owner request: {request}"
    )


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return sha256_text(payload)


def _relative_evidence_paths(tool: str, arguments: dict[str, Any]) -> list[str]:
    values: list[str] = []
    if tool in {"project_read", "project_list"}:
        raw = arguments.get("path")
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    if tool == "project_search":
        for field in ("path", "file_glob"):
            raw = arguments.get(field)
            if isinstance(raw, str) and raw.strip():
                values.append(raw.strip())
    return sorted(dict.fromkeys(values))


def build_research_evidence_manifest(
    *, trace_path: Path, bridge_job_id: str, report: str,
    research_input: dict[str, Any], evidence: dict[str, str],
) -> dict[str, Any]:
    """Build a bounded manifest from real structured tool/result trace pairs."""
    try:
        lines = trace_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ManagerError("Research trace is missing") from exc
    pending: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ManagerError("Research trace contains invalid JSON") from exc
        if not isinstance(event, dict):
            raise ManagerError("Research trace event must be an object")
        if event.get("type") == "tool_call":
            tool = str(event.get("tool") or "")
            arguments = event.get("arguments")
            pending.append({
                "tool": tool,
                "step": int(event.get("step") or 0),
                "arguments": arguments if isinstance(arguments, dict) else {},
            })
        elif event.get("type") == "tool_result":
            tool = str(event.get("tool") or "")
            match_index = next(
                (index for index, call in enumerate(pending)
                 if call["tool"] == tool),
                None,
            )
            if match_index is None:
                raise ManagerError("Research trace has an unmatched tool result")
            call = pending.pop(match_index)
            if not event.get("ok") or event.get("counts_as_evidence") is False:
                continue
            result = event.get("result")
            if tool == "project_search" and isinstance(result, dict):
                if int(result.get("count") or 0) <= 0:
                    continue
            calls.append({
                "tool": tool,
                "step": call["step"],
                "arguments": call["arguments"],
                "arguments_sha256": canonical_json_sha256(call["arguments"]),
                "relative_paths": _relative_evidence_paths(
                    tool, call["arguments"],
                ),
                "result_sha256": canonical_json_sha256(result),
            })
    if not any(call["tool"] in EVIDENCE_BEARING_TOOLS for call in calls):
        raise ManagerError(
            "Research requires a successful project_read or non-empty "
            "project_search; project_status alone is not evidence."
        )
    core = {
        "schema_version": 1,
        "bridge_job_id": bridge_job_id,
        "research_input_sha256": research_input["research_input_sha256"],
        "owner_request_sha256": research_input["owner_request_sha256"],
        "codex_research_prompt_sha256": (
            research_input["codex_research_prompt_sha256"]
        ),
        "report": report,
        "report_sha256": sha256_text(report),
        "prompt_sha256": evidence.get("prompt_hash", ""),
        "model": evidence.get("model", ""),
        "model_digest": evidence.get("model_digest", ""),
        "profile_snapshot_hash": evidence.get("profile_snapshot_hash", ""),
        "trace_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
        "successful_evidence_calls": calls,
    }
    return {**core, "manifest_sha256": canonical_json_sha256(core)}


def load_research_input(job_dir: Path) -> dict[str, Any]:
    path = job_dir / "research-input.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagerError("Immutable research input is missing or invalid") from exc
    if not isinstance(value, dict):
        raise ManagerError("Immutable research input must be an object")
    core = dict(value)
    stored = core.pop("research_input_sha256", None)
    if stored != canonical_json_sha256(core):
        raise ManagerError("Immutable research input hash mismatch")
    return value


def load_research_manifests(
    job_dir: Path, conversation: dict[str, Any],
) -> list[dict[str, Any]]:
    pointers = conversation.get("research_manifests")
    if not isinstance(pointers, list) or not pointers:
        raise ManagerError("Planning requires immutable research evidence")
    manifests: list[dict[str, Any]] = []
    for pointer in pointers:
        if not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}:
            raise ManagerError("Research manifest pointer is invalid")
        path = (job_dir / str(pointer["path"])).resolve()
        try:
            path.relative_to(job_dir.resolve())
        except ValueError as exc:
            raise ManagerError("Research manifest escapes its job") from exc
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagerError("Research manifest is missing or invalid") from exc
        core = dict(manifest)
        stored = core.pop("manifest_sha256", None)
        expected = canonical_json_sha256(core)
        if stored != expected or pointer["sha256"] != expected:
            raise ManagerError("Research manifest hash mismatch")
        manifests.append(manifest)
    return manifests


def build_plan_intent(
    conversation: dict[str, Any], *, repository: Path,
    job_dir: Path | None = None,
) -> dict[str, Any]:
    """Freeze the exact conversation evidence used by one planning request."""
    raw_turns = conversation.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ManagerError("Planning requires an existing owner conversation")
    turns: list[dict[str, str]] = []
    for index, raw in enumerate(raw_turns, start=1):
        if not isinstance(raw, dict) or raw.get("role") not in {"owner", "lead"}:
            raise ManagerError(f"Conversation turn {index} is invalid")
        if raw.get("kind") in {"plan_command", "plan_candidate"}:
            continue
        role = str(raw["role"])
        content = str(raw.get("content") or "").strip()
        if not content:
            raise ManagerError(f"Conversation turn {index} is empty")
        turn_id = f"turn-{index:04d}-{role}"
        kind = (
            "goal_replacement" if raw.get("kind") == "goal_replacement"
            else "conversation"
        )
        turns.append({
            "turn_id": turn_id,
            "role": role,
            "kind": kind,
            "content": content,
            "content_sha256": sha256_text(content),
        })
    owner_turns = [turn for turn in turns if turn["role"] == "owner"]
    if not owner_turns:
        raise ManagerError("Planning requires an owner root request")
    checkpoint = command(
        ["git", "rev-parse", "HEAD"], cwd=repository, timeout=30,
    ).strip()
    replacements = [
        turn for turn in owner_turns if turn["kind"] == "goal_replacement"
    ]
    root_turn = replacements[-1] if replacements else owner_turns[0]
    research_binding: dict[str, Any] = {}
    if job_dir is not None:
        research_input = load_research_input(job_dir)
        try:
            dossier = require_current_codex_approval(job_dir)
        except ResearchError as exc:
            raise ManagerError(str(exc)) from exc
        if (
            dossier.get("research_identity", {}).get("repository_checkpoint")
            != checkpoint
        ):
            raise ManagerError("STALE: research dossier checkpoint differs from repository")
        research_binding = {
            "research_input_sha256": research_input["research_input_sha256"],
            "research_dossier_sha256": dossier["dossier_sha256"],
        }
    core = {
        "schema_version": 1,
        "root_turn_id": root_turn["turn_id"],
        "root_request": root_turn["content"],
        "checkpoint": checkpoint,
        "turns": turns,
        **research_binding,
    }
    return {**core, "intent_sha256": canonical_json_sha256(core)}


def validate_plan_sources(
    decision: dict[str, Any], plan_intent: dict[str, Any],
) -> dict[str, list[str]]:
    """Fail closed when a plan cannot be traced to frozen owner turns."""
    turns = plan_intent.get("turns")
    if not isinstance(turns, list):
        raise ManagerError("Frozen plan intent has no turns")
    known = {
        str(turn["turn_id"]): str(turn["role"])
        for turn in turns
        if isinstance(turn, dict) and "turn_id" in turn and "role" in turn
    }
    root_turn_id = str(plan_intent.get("root_turn_id") or "")
    sources: dict[str, list[str]] = {}
    errors: list[str] = []
    for field in sorted(PLAN_SOURCE_FIELDS):
        raw = decision.get(field)
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            errors.append(f"{field} must be a list of turn ids")
            sources[field] = []
            continue
        values = list(dict.fromkeys(raw))
        if len(values) != len(raw):
            errors.append(f"{field} contains duplicate turn ids")
        unknown = sorted(set(values) - set(known))
        if unknown:
            errors.append(f"{field} contains unknown turn ids: {', '.join(unknown)}")
        sources[field] = values
    objective = sources.get("objective_source_turns", [])
    decisions = sources.get("decision_source_turns", [])
    discussion = sources.get("discussion_only_turns", [])
    if root_turn_id not in objective:
        errors.append("objective_source_turns must include the root owner request")
    ordered_ids = list(known)
    if root_turn_id in ordered_ids:
        root_index = ordered_ids.index(root_turn_id)
        superseded = {
            turn_id for turn_id in ordered_ids[:root_index]
            if known[turn_id] == "owner"
        }
        reused = sorted(superseded & (set(objective) | set(decisions)))
        if reused:
            errors.append(
                "owner turns before an explicit replacement goal are superseded: "
                + ", ".join(reused)
            )
    for field, values in (
        ("objective_source_turns", objective),
        ("decision_source_turns", decisions),
    ):
        non_owner = sorted(turn_id for turn_id in values if known.get(turn_id) != "owner")
        if non_owner:
            errors.append(f"{field} may reference only owner turns: {', '.join(non_owner)}")
    overlaps = sorted(
        (set(objective) | set(decisions)) & set(discussion)
    )
    if overlaps:
        errors.append(
            "planning and discussion-only sources overlap: " + ", ".join(overlaps)
        )
    owner_turn_ids = {turn_id for turn_id, role in known.items() if role == "owner"}
    missing_owner_turns = sorted(
        owner_turn_ids - set(objective) - set(decisions) - set(discussion)
    )
    if missing_owner_turns:
        errors.append(
            "every owner turn must be classified; missing: "
            + ", ".join(missing_owner_turns)
        )
    if errors:
        raise ManagerError("Plan source trace is invalid: " + "; ".join(errors))
    return sources


def normalize_plan_sources(
    decision: dict[str, Any], plan_intent: dict[str, Any],
) -> dict[str, Any]:
    """Conservatively repair bookkeeping without changing plan substance."""
    normalized = dict(decision)
    turns = plan_intent.get("turns")
    if not isinstance(turns, list):
        raise ManagerError("Frozen plan intent has no turns")
    known = {
        str(turn["turn_id"]): str(turn["role"])
        for turn in turns
        if isinstance(turn, dict) and "turn_id" in turn and "role" in turn
    }
    ordered = list(known)
    owner_ids = [turn_id for turn_id in ordered if known[turn_id] == "owner"]
    root = str(plan_intent.get("root_turn_id") or "")
    if root not in owner_ids:
        raise ManagerError("Frozen plan intent has no valid owner root")
    root_index = ordered.index(root)
    superseded = {
        turn_id for turn_id in ordered[:root_index]
        if known[turn_id] == "owner"
    }

    def listed(field: str) -> list[str]:
        raw = decision.get(field)
        if not isinstance(raw, list):
            return []
        return list(dict.fromkeys(
            item for item in raw if isinstance(item, str) and item in known
        ))

    raw_objective = listed("objective_source_turns")
    raw_decisions = listed("decision_source_turns")
    objective = [
        turn_id for turn_id in raw_objective
        if known[turn_id] == "owner" and turn_id not in superseded
    ]
    if root not in objective:
        objective.insert(0, root)
    decisions = [
        turn_id for turn_id in raw_decisions
        if known[turn_id] == "owner"
        and turn_id not in superseded
        and turn_id not in objective
    ]
    discussion = [
        turn_id for turn_id in listed("discussion_only_turns")
        if turn_id not in objective and turn_id not in decisions
    ]
    # A lead turn can never authorize execution. Preserve such mistaken
    # references only as discussion evidence instead of rejecting the plan.
    for turn_id in raw_objective + raw_decisions:
        if known[turn_id] != "owner" and turn_id not in discussion:
            discussion.append(turn_id)
    for turn_id in owner_ids:
        if (
            turn_id not in objective
            and turn_id not in decisions
            and turn_id not in discussion
        ):
            discussion.append(turn_id)
    normalized["objective_source_turns"] = objective
    normalized["decision_source_turns"] = decisions
    normalized["discussion_only_turns"] = discussion
    return normalized


def trusted_plan_task(
    plan_intent: dict[str, Any], sources: dict[str, list[str]],
) -> str:
    """Build the validator request from cited owner turns, never model prose."""
    by_id = {
        str(turn["turn_id"]): str(turn["content"])
        for turn in plan_intent["turns"]
    }
    selected = list(dict.fromkeys(
        sources["objective_source_turns"] + sources["decision_source_turns"]
    ))
    sections = [
        "POTRJENA IZVIRNA ZAHTEVA:\n" + by_id[str(plan_intent["root_turn_id"])]
    ]
    additions = [
        f"[{turn_id}] {by_id[turn_id]}"
        for turn_id in selected
        if turn_id != plan_intent["root_turn_id"]
    ]
    if additions:
        sections.append("POTRJENE DOPOLNITVE:\n" + "\n".join(additions))
    return "\n\n".join(sections)


def render_plan_candidate(decision: dict[str, Any], candidate_sha256: str) -> str:
    """Render the exact structured candidate the owner may later dispatch."""
    atoms = "\n".join(
        f"{index}. [{atom['id']}] {atom['task']}\n"
        f"   Datoteke: {', '.join(atom['expected_files'])}"
        for index, atom in enumerate(decision["atoms"], start=1)
    )
    allowed = "\n".join(f"- `{path}`" for path in decision["allowed_files"])
    constraints = "\n".join(f"- {item}" for item in decision["constraints"])
    criteria = "\n".join(
        f"- {item}" for item in decision["acceptance_criteria"]
    )
    return (
        f"IZVEDBENI NAČRT · revizija `{candidate_sha256[:12]}`\n\n"
        f"Cilj:\n{decision['objective']}\n\n"
        f"Dovoljene datoteke:\n{allowed}\n\n"
        f"Koraki:\n{atoms}\n\n"
        f"Omejitve:\n{constraints}\n\n"
        f"Sprejemna merila:\n{criteria}\n\n"
        "Ta revizija še ni poslana naprej. Popravke povej v običajnem "
        "pogovoru, nato ponovno uporabi `/prepare-plan`. Ko je prikazana "
        "revizija pravilna, jo pošlji z `/plan-dispatch`."
    )


class ManagerError(RuntimeError):
    pass


def enabled_lead_profiles() -> dict[str, dict[str, Any]]:
    return {
        profile_id: profile
        for profile_id, profile in load_profile_registry(
            PROFILE_REGISTRY, PROJECT_ROOT
        ).items()
        if profile["enabled"]
    }


def role_profiles(*, include_disabled: bool = False) -> dict[str, dict[str, Any]]:
    """Load strictly validated model-independent roles."""
    try:
        raw = json.loads(ROLE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagerError(f"Cannot load role registry: {ROLE_REGISTRY}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "roles"}:
        raise ManagerError("Role registry has unknown or missing fields")
    if raw["schema_version"] != 1 or not isinstance(raw["roles"], dict):
        raise ManagerError("Unsupported role registry schema")
    roles: dict[str, dict[str, Any]] = {}
    expected = {"version", "role", "display_name", "system_prompt", "enabled"}
    for role_id, role in raw["roles"].items():
        if not isinstance(role_id, str) or not re.fullmatch(r"[a-z0-9_]+", role_id):
            raise ManagerError(f"Unsafe role id: {role_id!r}")
        if not isinstance(role, dict) or set(role) != expected:
            raise ManagerError(f"Role {role_id} has unknown or missing fields")
        if role.get("role") != role_id:
            raise ManagerError(f"Role {role_id} identity mismatch")
        if not isinstance(role.get("version"), int) or role["version"] < 1:
            raise ManagerError(f"Role {role_id} version must be positive")
        if not isinstance(role.get("enabled"), bool):
            raise ManagerError(f"Role {role_id} enabled must be boolean")
        for field in ("display_name", "system_prompt"):
            if not isinstance(role.get(field), str) or not role[field].strip():
                raise ManagerError(f"Role {role_id} {field} is invalid")
        prompt = (PROJECT_ROOT / role["system_prompt"]).resolve()
        try:
            prompt.relative_to(PROJECT_ROOT.resolve())
        except ValueError as exc:
            raise ManagerError(f"Role {role_id} prompt escapes project") from exc
        if not prompt.is_file():
            raise ManagerError(f"Role {role_id} prompt does not exist")
        if include_disabled or role["enabled"]:
            roles[role_id] = {"profile_id": role_id, **role}
    if not roles:
        raise ManagerError("Role registry has no enabled roles")
    return roles


def enabled_role_profiles() -> dict[str, dict[str, Any]]:
    """Load owner-selectable roles only."""
    return role_profiles()


def _model_runtime_registry() -> dict[str, Any]:
    try:
        raw = json.loads(MODEL_RUNTIME.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagerError(f"Cannot load model runtime registry: {MODEL_RUNTIME}") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version", "defaults", "overrides", "presets",
    } or raw["schema_version"] != 2:
        raise ManagerError("Invalid model runtime registry")
    defaults = raw["defaults"]
    overrides = raw["overrides"]
    presets = raw["presets"]
    if not all(isinstance(value, dict) for value in (defaults, overrides, presets)):
        raise ManagerError("Invalid model runtime settings")
    return raw


def model_runtime_settings(model_tag: str) -> dict[str, Any]:
    """Resolve safe technical settings independently from the selected role."""
    raw = _model_runtime_registry()
    defaults = raw["defaults"]
    overrides = raw["overrides"]
    settings = {**defaults, **(overrides.get(model_tag) or {})}
    expected = {
        "num_ctx", "conversation_num_ctx", "think", "keep_alive",
        "conversation_keep_alive",
    }
    if set(settings) != expected:
        raise ManagerError(f"Model settings for {model_tag} are incomplete")
    if (
        isinstance(settings["num_ctx"], bool)
        or not isinstance(settings["num_ctx"], int)
        or settings["num_ctx"] < 1024
        or isinstance(settings["conversation_num_ctx"], bool)
        or not isinstance(settings["conversation_num_ctx"], int)
        or settings["conversation_num_ctx"] < 1024
        or settings["conversation_num_ctx"] > settings["num_ctx"]
    ):
        raise ManagerError(f"Model context settings for {model_tag} are invalid")
    if not isinstance(settings["think"], (bool, str)):
        raise ManagerError(f"Model think setting for {model_tag} is invalid")
    for field in ("keep_alive", "conversation_keep_alive"):
        if not isinstance(settings[field], str) or not settings[field].strip():
            raise ManagerError(f"Model {field} for {model_tag} is invalid")
    return settings


OWNER_RUNTIME_FIELDS = frozenset({"num_ctx", "think", *RUNTIME_OPTION_FIELDS})


def model_runtime_presets(model_tag: str) -> dict[str, dict[str, Any]]:
    values = _model_runtime_registry()["presets"].get(model_tag, {})
    if not isinstance(values, dict):
        raise ManagerError(f"Model presets for {model_tag} are invalid")
    result: dict[str, dict[str, Any]] = {}
    for name, preset in values.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise ManagerError(f"Unsafe runtime preset name: {name!r}")
        if not isinstance(preset, dict):
            raise ManagerError(f"Runtime preset {name} must be an object")
        result[name] = normalize_runtime_selection(model_tag, preset)
    return result


def normalize_runtime_selection(
    model_tag: str, values: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate owner-selectable runtime values against the model safety cap."""
    selected = dict(values or {})
    unknown = sorted(set(selected) - OWNER_RUNTIME_FIELDS)
    if unknown:
        raise ManagerError("Unsupported runtime setting(s): " + ", ".join(unknown))
    base = model_runtime_settings(model_tag)

    def integer(field: str, minimum: int, maximum: int) -> None:
        if field not in selected:
            return
        value = selected[field]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ManagerError(f"{field} must be an integer from {minimum} to {maximum}")

    def number(field: str, minimum: float, maximum: float) -> None:
        if field not in selected:
            return
        value = selected[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise ManagerError(f"{field} must be a number from {minimum} to {maximum}")
        selected[field] = float(value)

    integer("num_ctx", 1024, int(base["num_ctx"]))
    effective_ctx = int(selected.get("num_ctx", base["conversation_num_ctx"]))
    integer("num_predict", 1, min(effective_ctx, 32768))
    integer("top_k", 0, 1000)
    integer("seed", -1, 2**31 - 1)
    number("temperature", 0, 2)
    number("top_p", 0, 1)
    number("min_p", 0, 1)
    number("repeat_penalty", 0, 2)
    if "think" in selected:
        think = selected["think"]
        if isinstance(base["think"], bool):
            if not isinstance(think, bool):
                raise ManagerError("think must be true or false for this model")
        elif think not in {"low", "medium", "high", "max"}:
            raise ManagerError("think must be low, medium, high or max for this model")
    return {field: selected[field] for field in sorted(selected)}


def resolve_runtime_selection(
    model_tag: str, *, preset: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    presets = model_runtime_presets(model_tag)
    if preset is not None and preset not in presets:
        choices = ", ".join(sorted(presets)) or "(none)"
        raise ManagerError(f"Unknown preset {preset!r} for {model_tag}; available: {choices}")
    merged = {**(presets.get(preset, {})), **(overrides or {})}
    return normalize_runtime_selection(model_tag, merged)


def installed_ollama_models() -> list[str]:
    """Read the live local model inventory used by the Open WebUI selector."""
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    parsed = urllib.parse.urlsplit(str(template["ollama_url"]))
    tags_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/api/tags", "", ""))
    try:
        with urllib.request.urlopen(tags_url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ManagerError("Cannot read installed Ollama models") from exc
    values = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise ManagerError("Ollama model inventory has an invalid shape")
    names = {
        str(item.get("name") or item.get("model") or "").strip()
        for item in values if isinstance(item, dict)
    }
    return sorted(name for name in names if name)


def snapshot_for_new_task(
    lead_profile: str, *, model_tag: str | None = None,
    runtime_overrides: dict[str, Any] | None = None,
    digest_resolver: Callable[[str, str], str] = resolve_ollama_digest,
    allow_disabled: bool = False,
) -> dict[str, Any]:
    roles = role_profiles(include_disabled=allow_disabled)
    if lead_profile not in roles:
        raise ManagerError(f"Unknown or disabled role profile: {lead_profile}")
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    selected_model = (model_tag or str(template["model"])).strip()
    if not selected_model:
        raise ManagerError("A local model must be selected")
    role = roles[lead_profile]
    runtime = model_runtime_settings(selected_model)
    selected_runtime = normalize_runtime_selection(selected_model, runtime_overrides)
    if "num_ctx" in selected_runtime:
        runtime["num_ctx"] = selected_runtime["num_ctx"]
        runtime["conversation_num_ctx"] = selected_runtime["num_ctx"]
    if "think" in selected_runtime:
        runtime["think"] = selected_runtime["think"]
    profile = {
        "profile_id": lead_profile,
        "version": role["version"],
        "model": selected_model,
        "role": role["role"],
        "display_name": role["display_name"],
        "system_prompt": role["system_prompt"],
        "enabled": role["enabled"],
        "runtime_options": {
            field: selected_runtime.get(field) for field in RUNTIME_OPTION_FIELDS
        },
        **runtime,
    }
    digest = digest_resolver(str(template["ollama_url"]), selected_model)
    return build_profile_snapshot(
        profile, project_root=PROJECT_ROOT, model_digest=digest,
    )


def command(args: list[str], *, cwd: Path, env: dict[str, str] | None = None,
            timeout: int = 1800) -> str:
    result = subprocess.run(
        args, cwd=cwd, env={**os.environ, **(env or {})},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False,
    )
    if result.returncode:
        raise ManagerError(f"Command failed ({args[0]}): {result.stdout[-4000:]}")
    return result.stdout.strip()


def parse_model_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    decoder = json.JSONDecoder()
    values: list[Any] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and REQUIRED_MODEL_PLAN_FIELDS <= set(value)
            and value not in values
        ):
            values.append(value)
    if len(values) != 1:
        raise ManagerError(
            f"Planner must return exactly one JSON object; found {len(values)}"
        )
    value = values[0]
    if not isinstance(value, dict):
        raise ManagerError("Planner result must be one JSON object")
    unknown = sorted(set(value) - MODEL_PLAN_FIELDS)
    missing = sorted(REQUIRED_MODEL_PLAN_FIELDS - set(value))
    if unknown or missing:
        raise ManagerError(f"Planner fields invalid; missing={missing}, unknown={unknown}")
    return value


def model_plan_errors(value: dict[str, Any], repository: Path) -> list[str]:
    """Return all cheap semantic errors so one retry can fix them together."""
    errors: list[str] = []
    for key in ("constraints", "acceptance_criteria"):
        items = value.get(key)
        if not isinstance(items, list) or not items:
            errors.append(f"{key} must be a non-empty list")
        elif any(not isinstance(item, str) or not item.strip() for item in items):
            errors.append(f"{key} entries must be non-empty strings")
    allowed = value.get("allowed_files")
    if not isinstance(allowed, list) or not allowed:
        errors.append("allowed_files must be a non-empty list")
        allowed_set: set[str] = set()
    else:
        allowed_set = {item for item in allowed if isinstance(item, str)}
        mistaken_tools = sorted(allowed_set & BRIDGE_TOOL_NAMES)
        if mistaken_tools:
            errors.append(
                "allowed_files contains tool names instead of repository paths: "
                + ", ".join(mistaken_tools)
            )
    atoms = value.get("atoms")
    if not isinstance(atoms, list) or not atoms:
        errors.append("atoms must be a non-empty list")
    else:
        for index, atom in enumerate(atoms):
            if not isinstance(atom, dict):
                errors.append(f"atoms[{index}] must be an object")
                continue
            expected = atom.get("expected_files")
            if not isinstance(expected, list) or not expected:
                errors.append(f"atoms[{index}].expected_files must be a non-empty list")
            elif not set(expected) <= allowed_set:
                errors.append(f"atoms[{index}].expected_files must be inside allowed_files")
    return errors


def trusted_checks(paths: list[str]) -> list[str]:
    """Select bounded checks from file types; the model never chooses commands."""
    checks = ["syntax_changed_python"]
    if any(path == "test_v32_central_api.py" or "central_" in path for path in paths):
        checks.append("central_api_tests")
    elif any(path.endswith(".py") for path in paths):
        checks.append("desktop_unit_tests")
    return list(dict.fromkeys(checks))


def first_existing(repository: Path, candidates: list[str]) -> str:
    for relative in candidates:
        if (repository / relative).is_file():
            return relative
    raise ManagerError("Trusted context is missing from the agent repository")


def trusted_context(repository: Path, allowed_files: list[str]) -> dict[str, list[str]]:
    """Build context exclusively from verified repository paths."""
    existing_allowed = [path for path in allowed_files if (repository / path).is_file()]
    code = existing_allowed[:4] or [first_existing(repository, [
        "docs/local_agents/README.md", "README_v30.txt", "AGENTS.md",
    ])]
    central = any("central" in path for path in allowed_files)
    test_candidates = (
        ["test_v32_central_api.py"] if central else
        ["tests/test_mikrofix_supervisor.py", "test_v32_application_entrypoints.py"]
    )
    verified_test = next(
        (candidate for candidate in test_candidates if (repository / candidate).is_file()),
        None,
    )
    if verified_test is None:
        fallback_tests = sorted(
            path.relative_to(repository).as_posix()
            for path in repository.rglob("test*.py")
            if path.is_file() and ".git" not in path.parts
        )
        if not fallback_tests:
            raise ManagerError("Trusted test context is missing from the agent repository")
        verified_test = fallback_tests[0]
    return {
        "canon": [first_existing(repository, [
            "AGENTS.md", "docs/local_agents/README.md",
        ])],
        "code": code,
        "tests": [verified_test],
    }


def normalize_plan(
    raw: str, *, run_id: str, repository: Path, owner_request: str = ""
) -> dict[str, Any]:
    model_plan = parse_model_json(raw)
    preliminary_errors = model_plan_errors(model_plan, repository)
    if preliminary_errors:
        raise ManagerError("; ".join(preliminary_errors))
    head = command(["git", "rev-parse", "HEAD"], cwd=repository, timeout=30)
    allowed_files = list(model_plan["allowed_files"])
    atoms = [{
        "id": atom["id"],
        "task": atom["task"],
        "expected_files": list(atom["expected_files"]),
        "checks": trusted_checks(list(atom["expected_files"])),
    } for atom in model_plan["atoms"]]
    trusted_owner_request = owner_request.strip() or model_plan["objective"]
    acceptance_criteria = [binding_owner_criterion(trusted_owner_request)]
    acceptance_criteria.extend(model_plan["acceptance_criteria"])
    plan = {
        "objective": model_plan["objective"],
        "owner_request": trusted_owner_request,
        "allowed_files": allowed_files,
        "atoms": atoms,
        "constraints": list(model_plan["constraints"]),
        "acceptance_criteria": acceptance_criteria,
        "context_links": trusted_context(repository, allowed_files),
        "run_id": run_id,
        "base_checkpoint": head,
        "max_retries_per_atom": 3,
        "job_timeout_seconds": 1800,
        "final_gate": "AWAIT_CODEX_REVIEW",
    }
    validated = validate_plan(plan)
    if validated["objective"].startswith("BLOCKED:"):
        raise ManagerError(validated["objective"])
    return validated


def free_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_executor(url: str, token_file: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ManagerError("Read-only planner executor stopped during startup")
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
            request = urllib.request.Request(
                url + "/health", headers={"Authorization": f"Bearer {token}"}
            )
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
        time.sleep(0.2)
    raise ManagerError("Read-only planner executor did not become healthy")


def planner_prompt(task: str, previous_error: str = "") -> str:
    correction = (
        "\nPREJŠNJI IZHOD JE BIL ZAVRNJEN: " + previous_error
        if previous_error else ""
    )
    return (
        "Pripravi izvedbeni načrt za naslednjo zahtevo lastnika. Jedrna pravila, "
        "specifikacija, stanje in lekcije so že v trajnem kontekstu glavne seje; "
        "ne beri jih znova. Z orodji preveri samo konkretno kodo, teste in poti. "
        "Končni odgovor naj bo samo zahtevani JSON.\n\nZAHTEVA:\n"
        + task + correction
    )


def qwen_planner_prompt(task: str, previous_error: str = "") -> str:
    correction = (
        "\nPREJŠNJI PREDLOG JE BIL ZAVRNJEN: " + previous_error
        if previous_error else ""
    )
    return (
        "Razčleni naslednjo zahtevo lastnika v najmanjši varen izvedbeni načrt. "
        "Projektni spomin je že potrjen; preveri samo konkretne poti in simbole. "
        "Ne spreminjaj datotek. V allowed_files in expected_files zapiši samo "
        "prave projektno relativne poti datotek; imen orodij, funkcij ali ukazov "
        "nikoli ne zapiši kot datotečne poti. Vrni samo zahtevani JSON.\n\nZAHTEVA:\n"
        + task + correction
    )


def gpt_validation_prompt(
    task: str, qwen_proposal: str, memory_hash: str, previous_error: str = "",
    *, plan_intent: dict[str, Any] | None = None,
    source_trace: dict[str, list[str]] | None = None,
) -> str:
    correction = (
        "\nPREJŠNJA POTRDITEV JE BILA ZAVRNJENA: " + previous_error
        if previous_error else ""
    )
    provenance = ""
    if plan_intent is not None and source_trace is not None:
        provenance = (
            "\n\nZAMRZNJENI POGOVORNI VIR:\n"
            + json.dumps(plan_intent, ensure_ascii=False, indent=2)
            + "\n\nSLED VIROV SVETOVALCA:\n"
            + json.dumps(source_trace, ensure_ascii=False, indent=2)
            + "\nPreveri, da cilj izhaja iz root_turn_id, poznejše odločitve "
            "izhajajo samo iz citiranih lastnikovih turnov in da "
            "discussion_only_turns niso postali izvedbene zahteve."
        )
    return (
        "Kot konfigurirani neodvisni validator preveri predlog pogovornega "
        "svetovalca proti lastnikovi zahtevi in projektnemu spominu. "
        "Svetovalec ne odloča. "
        "Odpravi vse napake in vrni samo končni JSON načrt.\n\n"
        f"SKUPNI MEMORY SHA-256: {memory_hash}\n\n"
        f"ZAHTEVA LASTNIKA:\n{task}\n\nPREDLOG SVETOVALCA:\n{qwen_proposal}"
        + provenance + correction
    )


def run_bridge_monitor(
    bridge: list[str], bridge_env: dict[str, str], bridge_home: Path,
    job_id: str, *, idle_timeout_seconds: int = 900,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Run one worker with an inactivity limit but no total duration limit."""
    if idle_timeout_seconds <= 0:
        raise ValueError("idle_timeout_seconds must be positive")
    callback = progress_callback or (lambda _event: None)

    trace_path = bridge_home / "jobs" / job_id / "trace.jsonl"
    output_path = bridge_home / "monitor-progress.log"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    offset = 0
    last_activity = time.monotonic()
    with output_path.open("w+", encoding="utf-8") as output:
        process = subprocess.Popen(
            bridge + ["monitor", "--once"], cwd=PROJECT_ROOT,
            env=bridge_env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, text=True,
        )
        try:
            while process.poll() is None:
                new_offset = forward_trace_events(trace_path, offset, callback)
                current_time = time.monotonic()
                if new_offset != offset:
                    offset = new_offset
                    last_activity = current_time
                if current_time - last_activity >= idle_timeout_seconds:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    reason = (
                        "Bridge monitor timed out after "
                        f"{idle_timeout_seconds} seconds without progress"
                    )
                    mark_bridge_job_interrupted(
                        bridge_home, job_id, reason,
                        error_code="BRIDGE_MONITOR_IDLE_TIMEOUT",
                    )
                    raise ManagerError(reason)
                time.sleep(0.1)
            offset = forward_trace_events(trace_path, offset, callback)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            mark_bridge_job_interrupted(
                bridge_home, job_id,
                "Bridge monitoring was interrupted before worker completion.",
                error_code="BRIDGE_MONITOR_INTERRUPTED",
            )
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if process.returncode:
            output.seek(0)
            reason = "Bridge monitor failed: " + output.read()[-4000:]
            mark_bridge_job_interrupted(
                bridge_home, job_id, reason,
                error_code="BRIDGE_MONITOR_FAILED",
            )
            raise ManagerError(reason)


def mark_bridge_job_interrupted(
    bridge_home: Path, job_id: str, reason: str, *, error_code: str,
) -> None:
    """Close a worker record after the manager has terminated its process."""
    job_dir = bridge_home / "jobs" / job_id
    job_path = job_dir / "job.json"
    if not job_path.is_file():
        return
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if job.get("status") not in {"queued", "running"}:
        return
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    job.update({
        "status": "failed",
        "finished_at": timestamp,
        "error": reason,
        "error_code": error_code,
    })
    atomic_write_json(job_path, job)
    with (job_dir / "log.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] Naloga je bila prekinjena: {reason}\n")
    with (job_dir / "trace.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "at": timestamp,
            "type": "error",
            "role": "manager",
            "content": reason,
            "error_code": error_code,
        }, ensure_ascii=False) + "\n")


def forward_trace_events(
    trace_path: Path, offset: int,
    progress_callback: Callable[[dict[str, Any]], None],
) -> int:
    """Forward only complete append-only JSONL events from one bridge trace."""
    if not trace_path.is_file():
        return offset
    with trace_path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        while True:
            line_start = handle.tell()
            line = handle.readline()
            if not line:
                return handle.tell()
            if not line.endswith("\n"):
                return line_start
            offset = handle.tell()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                try:
                    progress_callback(event)
                except Exception:
                    # A disconnected UI must never stop the supervised job.
                    continue


def run_planner(
    task: str, *, job_dir: Path, repository: Path,
    advisor_proposal: dict[str, Any] | None = None,
    advisor_snapshot: dict[str, Any] | None = None,
    plan_intent: dict[str, Any] | None = None,
    source_trace: dict[str, list[str]] | None = None,
) -> str:
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    lead_home = Path.home() / ".local/share/mikrofix-bridge"
    gpt_model = str(template.get("model", "gpt-oss:20b"))
    gpt_num_ctx = min(
        int(template.get("validator_num_ctx", template.get("num_ctx", 32768))),
        32768,
    )
    gpt_num_predict = int(template.get("validator_num_predict", 4096))
    gpt_think = str(template.get("validator_think", "low"))
    if gpt_think not in {"low", "medium", "high"}:
        raise ManagerError("validator_think must be low, medium or high")
    gpt_keep_alive = str(template.get("lead_keep_alive", "30m"))
    qwen_model = str(template.get("planning_advisor_model", PLANNING_DRAFTER_MODEL))
    qwen_num_ctx = int(template.get("planning_advisor_num_ctx", 32768))
    qwen_num_predict = int(template.get("planning_advisor_num_predict", 6144))
    qwen_keep_alive = str(template.get("planning_advisor_keep_alive", "15m"))
    planning_home = lead_home / "planning-team"
    planning_sources = select_planning_sources(PROJECT_ROOT, task)
    gpt_planner_lead = ensure_context_ready(
        PROJECT_ROOT, planning_home / "gpt-oss",
        ollama_url=str(template["ollama_url"]), model=gpt_model,
        num_ctx=min(gpt_num_ctx, 32768), keep_alive=gpt_keep_alive,
        sources=planning_sources, memory_name="PLAN_VALIDATOR_CONTEXT.md",
    )
    advisor_memory: dict[str, object] | None = None
    if advisor_proposal is None:
        advisor_memory = ensure_context_ready(
            PROJECT_ROOT, planning_home / "advisor",
            ollama_url=str(template["ollama_url"]), model=qwen_model,
            num_ctx=qwen_num_ctx, keep_alive=qwen_keep_alive,
            sources=planning_sources, memory_name="PLAN_DRAFTER_CONTEXT.md",
        )
        if advisor_memory["memory_sha256"] != gpt_planner_lead["memory_sha256"]:
            raise ManagerError(
                "Planning advisor and validator did not acknowledge the same memory"
            )
    runtime = job_dir / "planner-runtime"
    bridge_home = runtime / "bridge"
    qwen_bridge_home = runtime / "qwen-bridge"
    executor_config = runtime / "executor-config"
    executor_state = runtime / "executor-state"
    for path in (bridge_home, qwen_bridge_home, executor_config, executor_state):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    port = free_local_port()
    token_file = executor_config / "token"
    executor_url = f"http://127.0.0.1:{port}"
    schema = json.loads(PROPOSAL_SCHEMA.read_text(encoding="utf-8"))

    def write_stage_config(
        home: Path, *, model: str, num_ctx: int, keep_alive: str,
        system_prompt: Path, memory: dict[str, object], role: str,
        think: bool | str,
        num_predict: int,
        require_tool_use: bool,
    ) -> Path:
        config_path = home / "config.json"
        atomic_write_immutable_json(config_path, {
            "ollama_url": template["ollama_url"],
            "model": model,
            "executor_url": executor_url,
            "token_file": str(token_file),
            "project": str(repository),
            "system_prompt": str(system_prompt),
            "project_memory": str(memory["memory_file"]),
            "parent_session_id": str(memory["session_id"]),
            "num_ctx": num_ctx,
            "runtime_options": {"num_predict": num_predict},
            "max_steps": 60,
            "keep_alive": keep_alive,
            "think": think,
            "require_tool_use": require_tool_use,
            "completion_policy": "supervisor_only",
            "response_format": schema,
            "agent_role": role,
        })
        return config_path

    advisor_config = None
    if advisor_memory is not None:
        advisor_config = write_stage_config(
            qwen_bridge_home, model=qwen_model, num_ctx=qwen_num_ctx,
            keep_alive=qwen_keep_alive, system_prompt=PLAN_DRAFTER_SYSTEM,
            memory=advisor_memory, role="Rezervni pripravljalec načrta", think=True,
            num_predict=qwen_num_predict,
            require_tool_use=False,
        )
    bridge_config = write_stage_config(
        bridge_home, model=gpt_model, num_ctx=gpt_num_ctx,
        keep_alive=gpt_keep_alive, system_prompt=GPT_VALIDATOR_SYSTEM,
        memory=gpt_planner_lead,
        role="Konfigurirani neodvisni potrjevalec načrta", think=gpt_think,
        num_predict=gpt_num_predict,
        require_tool_use=True,
    )
    executor_env = {
        "MIKROFIX_EXECUTOR_ROOT": str(repository),
        "MIKROFIX_EXECUTOR_CONFIG_DIR": str(executor_config),
        "MIKROFIX_EXECUTOR_STATE_DIR": str(executor_state),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    log_path = runtime / "executor.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(PROJECT_ROOT / ".venv-server/bin/python"), "-m", "uvicorn",
         "tools.local_executor.server:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT, env={**os.environ, **executor_env}, stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    bridge = [str(PROJECT_ROOT / ".venv-server/bin/python"),
              str(PROJECT_ROOT / "tools/mikrofix_bridge.py")]

    def stage_environment(home: Path, config: Path) -> dict[str, str]:
        return {
            "MIKROFIX_BRIDGE_HOME": str(home),
            "MIKROFIX_BRIDGE_CONFIG": str(config),
            "CODEX_THREAD_ID": "",
            "MIKROFIX_WORKSPACE": str(job_dir.parent.parent),
        }

    def ask_stage(
        *, home: Path, config: Path, display_name: str,
        prompt_factory: Callable[[str], str],
    ) -> str:
        environment = stage_environment(home, config)
        last_error = ""
        for _attempt in range(MAX_PLANNER_ATTEMPTS):
            job_id = command(
                bridge + ["dispatch", "--name", display_name,
                          "--task", prompt_factory(last_error)],
                cwd=PROJECT_ROOT, env=environment, timeout=30,
            ).splitlines()[-1]
            run_bridge_monitor(bridge, environment, home, job_id)
            raw = command(bridge + ["result", job_id], cwd=PROJECT_ROOT,
                          env=environment, timeout=30)
            try:
                normalize_plan(
                    raw, run_id=job_dir.name, repository=repository,
                    owner_request=task,
                )
                return raw
            except (ManagerError, ValueError) as exc:
                last_error = str(exc)
        raise ManagerError(
            f"{display_name} remained invalid after {MAX_PLANNER_ATTEMPTS} "
            f"attempts: {last_error}"
        )

    try:
        wait_for_executor(executor_url, token_file, process)
        if plan_intent is not None:
            atomic_write_immutable_json(
                job_dir / "planning-intent.json", plan_intent,
            )
        if source_trace is not None:
            atomic_write_immutable_json(
                job_dir / "planning-source-trace.json", {
                    "schema_version": 1,
                    "intent_sha256": (
                        plan_intent.get("intent_sha256") if plan_intent else None
                    ),
                    **source_trace,
                },
            )
        if advisor_proposal is None:
            assert advisor_config is not None
            advisor_raw = ask_stage(
                home=qwen_bridge_home, config=advisor_config,
                display_name="Predlog svetovalca za razčlenitev",
                prompt_factory=lambda error: qwen_planner_prompt(task, error),
            )
            normalized_advisor = parse_model_json(advisor_raw)
        else:
            normalized_advisor = advisor_proposal
        atomic_write_immutable_json(
            job_dir / "advisor-proposal.json", normalized_advisor,
        )
        final_raw = ask_stage(
            home=bridge_home, config=bridge_config,
            display_name="Neodvisna potrditev načrta",
            prompt_factory=lambda error: gpt_validation_prompt(
                task, json.dumps(normalized_advisor, ensure_ascii=False, indent=2),
                str(gpt_planner_lead["memory_sha256"]), error,
                plan_intent=plan_intent, source_trace=source_trace,
            ),
        )
        final_proposal = parse_model_json(final_raw)
        atomic_write_immutable_json(
            job_dir / "validator-approved-plan.json", final_proposal,
        )
        atomic_write_immutable_json(job_dir / "planning-handoff.json", {
            "schema_version": 1,
            "shared_memory_sha256": gpt_planner_lead["memory_sha256"],
            "plan_intent_sha256": (
                plan_intent.get("intent_sha256") if plan_intent else None
            ),
            "advisor": {
                "role": "planning_advisor",
                "profile_id": (
                    advisor_snapshot.get("profile_id") if advisor_snapshot else None
                ),
                "model": (
                    advisor_snapshot.get("model") if advisor_snapshot else qwen_model
                ),
                "model_digest": (
                    advisor_snapshot.get("model_digest") if advisor_snapshot else None
                ),
                "session_id": (
                    advisor_memory.get("session_id") if advisor_memory else None
                ),
                "proposal_sha256": hashlib.sha256(
                    json.dumps(
                        normalized_advisor, ensure_ascii=False, sort_keys=True,
                    ).encode()
                ).hexdigest(),
            },
            "validator": {
                "role": "plan_validator",
                "model": gpt_model,
                "session_id": gpt_planner_lead["session_id"],
                "execution_parent_session_id": lead["session_id"],
                "approved_plan_sha256": hashlib.sha256(
                    json.dumps(final_proposal, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
            },
        })
        return final_raw
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


def repository_orientation(repository: Path) -> str:
    """Return only the current revision and a bounded dirty-worktree summary."""
    head = command(["git", "rev-parse", "HEAD"], cwd=repository, timeout=30).strip()
    status_lines = command(
        ["git", "status", "--short"], cwd=repository, timeout=30,
    ).splitlines()
    if not status_lines:
        return f"commit: {head}\ndelovno drevo: clean"
    rendered = "\n".join(status_lines[:20])
    if len(status_lines) > 20:
        rendered += f"\n... {len(status_lines) - 20} dodatnih poti ni prikazanih"
    return f"commit: {head}\nspremenjene poti ({len(status_lines)}):\n{rendered}"


def lead_conversation_prompt(
    message: str, *, turns: list[dict[str, str]],
    planning_requested: bool = False, repository_snapshot: str = "",
    plan_intent: dict[str, Any] | None = None,
) -> str:
    """Build one bounded continuation turn for one isolated persistent lead."""
    history = turns[-MAX_CONVERSATION_TURNS:]
    rendered_history = "\n\n".join(
        ("LASTNIK" if turn.get("role") == "owner" else "VODJA")
        + ":\n" + str(turn.get("content") or "").strip()
        for turn in history
        if str(turn.get("content") or "").strip()
    )
    if planning_requested and plan_intent is None:
        raise ManagerError("Planning requires a frozen plan intent")
    mode_instruction = (
        "Lastnik je izrecno sprožil /prepare-plan. Formalni izvedbeni načrt "
        "pripravi samo, če so vzrok oziroma relevantne ugotovitve preverjene, "
        "možnosti obravnavane in lastnikova izbira dovolj jasna. Če odločitev "
        "ali dokaz manjka, vrni NEEDS_INFO in zahtevaj najmanjši manjkajoči "
        "podatek. Za objective_source_turns navedi začetno lastnikovo zahtevo "
        "in druge lastnikove zapise, ki dejansko določajo cilj. V "
        "decision_source_turns navedi samo poznejše lastnikove odločitve ali "
        "omejitve. Pojasnjevalna vprašanja, kot je zahteva za snippet, uvrsti "
        "v discussion_only_turns in jih ne spremeni v cilj. To še ni dovoljenje "
        "za izvedbo. "
        if planning_requested else
        "To je raziskovalni in svetovalni pogovor. Ostani strogo osredotočen "
        "na prijavljeni simptom. Z bralnimi orodji preveri samo neposredno "
        "relevantno kodo, teste in pravila; ne pregleduj nepovezanih delov "
        "projekta. Če za usmerjen pregled manjka podatek o reprodukciji, "
        "najprej postavi kratko konkretno vprašanje. Nato poročaj, kaj je "
        "preverjeno, kaj je samo domneva, katere možnosti "
        "obstajajo, njihove posledice in svoje priporočilo. Z lastnikom razčisti "
        "izbiro. Odgovori naravno in samo o tem problemu. Načrt sme sprožiti "
        "samo /prepare-plan. "
    )
    sections = [
        "Neposredno odgovori lastniku. " + mode_instruction +
        "To ni nova naloga. Drugi profil lokalnega vodje v tem pogovoru ne "
        "sodeluje, ne vidi zgodovine in ga ne smeš klicati ali prositi za "
        "pregled. Za dejstva o projektu uporabi samo bralna projektna orodja. "
        "Vrni odločitev v točno zahtevani strukturirani obliki; polje reply je "
        "besedilo, ki ga vidi lastnik. Ne določi si vnaprej števila orodnih "
        "klicev, ne preštevaj jih kot cilj in ne razpravljaj o izbiri med "
        "tool_calls ter JSON odgovorom. Dokler manjka konkreten dokaz, uporabi "
        "naslednje smiselno dovoljeno orodje; ko je dokazov dovolj, odgovori."
    ]
    if repository_snapshot.strip():
        sections.append(
            "TRENUTNO STANJE REPOZITORIJA:\n" + repository_snapshot.strip()
        )
    if planning_requested and plan_intent is not None:
        frozen_turns = "\n\n".join(
            f"[{turn['turn_id']}] "
            + ("LASTNIK" if turn["role"] == "owner" else "VODJA")
            + (
                " (NOV POTRJENI CILJ)"
                if turn.get("kind") == "goal_replacement" else ""
            )
            + ":\n" + str(turn["content"])
            for turn in plan_intent["turns"]
        )
        sections.append(
            "ZAMRZNJENI PLAN INTENT:\n"
            f"intent_sha256: {plan_intent['intent_sha256']}\n"
            f"root_turn_id: {plan_intent['root_turn_id']}\n\n"
            + frozen_turns
        )
    elif rendered_history:
        sections.append("DOSEDANJI POGOVOR:\n" + rendered_history)
    sections.append(
        "UKAZ LASTNIKA:\n/prepare-plan"
        if planning_requested else
        "SPOROČILO LASTNIKA:\n" + message.strip()
    )
    return "\n\n".join(sections)


def conversation_runtime_settings(snapshot: dict[str, Any]) -> tuple[int, str]:
    """Resolve snapshotted conversation limits, with a safe legacy fallback."""
    return (
        int(snapshot.get(
            "conversation_num_ctx", min(snapshot["num_ctx"], 16384),
        )),
        str(snapshot.get("conversation_keep_alive", "5m")),
    )


def release_ollama_model(model_tag: str) -> None:
    """Unload one exact model tag after a bounded worker session."""
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    url = urllib.parse.urljoin(str(template["ollama_url"]).rstrip("/") + "/", "api/generate")
    request = urllib.request.Request(
        url,
        data=json.dumps({"model": model_tag, "keep_alive": 0}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                raise ManagerError(f"Ollama model release failed: HTTP {response.status}")
            response.read()
    except OSError as exc:
        raise ManagerError(f"Ollama model release failed for {model_tag}") from exc


def assert_frozen_research_models(
    lead_snapshot: dict[str, Any], worker_snapshot: dict[str, Any],
) -> None:
    """Fail if either immutable model tag now resolves to another digest."""
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    ollama_url = str(template["ollama_url"])
    for label, snapshot in (("lead", lead_snapshot), ("worker", worker_snapshot)):
        current = resolve_ollama_digest(ollama_url, str(snapshot["model_tag"]))
        if current != snapshot.get("model_digest"):
            raise ResearchError(f"RESEARCH_{label.upper()}_MODEL_CHANGED")


def expand_research_scope(
    task: dict[str, Any], result: dict[str, Any],
    evidence_cards: list[dict[str, Any]], *, repository: Path,
) -> dict[str, Any]:
    """Apply one evidence-linked, exact-path scope revision deterministically."""
    request = result.get("scope_expansion")
    if not isinstance(request, dict):
        raise ResearchError("Scope expansion result has no request")
    evidence_by_id = {card["evidence_id"]: card for card in evidence_cards}
    check_scope_expansion(request, evidence_by_id=evidence_by_id)
    relative = str(request["path"])
    target = repository / relative
    kind = "file" if target.is_file() else "directory" if target.is_dir() else ""
    if not kind:
        raise ResearchError("Requested scope expansion path does not exist")
    revised_scope = normalize_scope(
        [*task["allowed_scope"], {"kind": kind, "path": relative}], repository,
    )
    revised = dict(task)
    revised["allowed_scope"] = revised_scope
    return revised


def run_structured_research_session(
    prompt: str, *, job_dir: Path, repository: Path, snapshot: dict[str, Any],
    session_kind: str, response_format: dict[str, Any],
    allowed_tools: list[str], max_steps: int,
    allowed_scope: list[dict[str, str]] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one fresh bounded session and return its JSON plus durable trace."""
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    session_dir = (
        job_dir / "delegated-research-runtime" / session_kind
        / f"session-{time.time_ns()}"
    )
    bridge_home = session_dir / "bridge"
    executor_config = session_dir / "executor-config"
    executor_state = session_dir / "executor-state"
    for path in (bridge_home, executor_config, executor_state):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    scope_path = session_dir / "read-scope.json"
    if allowed_scope is not None:
        atomic_write_immutable_json(scope_path, {
            "schema_version": 1, "allowed_scope": allowed_scope,
        })
    port = free_local_port()
    token_file = executor_config / "token"
    executor_url = f"http://127.0.0.1:{port}"
    num_ctx, keep_alive = conversation_runtime_settings(snapshot)
    config_path = bridge_home / "config.json"
    bridge_config = {
        "ollama_url": template["ollama_url"], "model": snapshot["model_tag"],
        "executor_url": executor_url, "token_file": str(token_file),
        "project": str(repository),
        "system_prompt": str(PROJECT_ROOT / snapshot["system_prompt"]),
        "num_ctx": num_ctx, "max_steps": max_steps, "keep_alive": keep_alive,
        "think": snapshot["think"], "require_tool_use": bool(allowed_tools),
        "model_silence_timeout_seconds": 900,
        "minimum_successful_tool_calls": 1 if allowed_tools else 0,
        "allowed_tools": allowed_tools, "max_toolless_responses": 2,
        "deterministic_reply_wrapper": False,
        "completion_policy": "supervisor_only", "response_format": response_format,
        "agent_role": session_kind,
    }
    runtime_options = snapshot.get("runtime_options") or {}
    for field in RUNTIME_OPTION_FIELDS:
        if runtime_options.get(field) is not None:
            bridge_config[field] = runtime_options[field]
    atomic_write_immutable_json(config_path, bridge_config)
    executor_env = {
        "MIKROFIX_EXECUTOR_ROOT": str(repository),
        "MIKROFIX_EXECUTOR_CONFIG_DIR": str(executor_config),
        "MIKROFIX_EXECUTOR_STATE_DIR": str(executor_state),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if allowed_scope is not None:
        executor_env["MIKROFIX_EXECUTOR_READ_SCOPE"] = str(scope_path)
    log = (session_dir / "executor.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(PROJECT_ROOT / ".venv-server/bin/python"), "-m", "uvicorn",
         "tools.local_executor.server:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT, env={**os.environ, **executor_env},
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    bridge = [str(PROJECT_ROOT / ".venv-server/bin/python"),
              str(PROJECT_ROOT / "tools/mikrofix_bridge.py")]
    bridge_env = {
        "MIKROFIX_BRIDGE_HOME": str(bridge_home),
        "MIKROFIX_BRIDGE_CONFIG": str(config_path), "CODEX_THREAD_ID": "",
        "MIKROFIX_WORKSPACE": str(job_dir.parent.parent),
    }
    try:
        wait_for_executor(executor_url, token_file, process)
        bridge_job_id = command(
            bridge + ["dispatch", "--name", session_kind, "--task", prompt],
            cwd=PROJECT_ROOT, env=bridge_env, timeout=30,
        ).splitlines()[-1]
        run_bridge_monitor(
            bridge, bridge_env, bridge_home, bridge_job_id,
            idle_timeout_seconds=1200, progress_callback=progress_callback,
        )
        raw = command(
            bridge + ["result", bridge_job_id], cwd=PROJECT_ROOT,
            env=bridge_env, timeout=30,
        ).strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManagerError(f"{session_kind} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ManagerError(f"{session_kind} returned a non-object result")
        trace_path = bridge_home / "jobs" / bridge_job_id / "trace.jsonl"
        events = [
            json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return result, events
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


def run_lead_conversation(
    message: str, *, job_dir: Path, repository: Path,
    turns: list[dict[str, str]], lead_profile: str = "conversation_lead",
    lead_snapshot: dict[str, Any] | None = None,
    planning_requested: bool = False,
    plan_intent: dict[str, Any] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Ask only the selected persistent lead through a read-only executor."""
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    snapshot = lead_snapshot or read_profile_snapshot(job_dir)
    if snapshot["profile_id"] != lead_profile:
        raise ManagerError("Conversation profile differs from its immutable snapshot")
    base_home = Path.home() / ".local/share/mikrofix-bridge/lead-profiles"
    lead_home = base_home / lead_profile / job_dir.name
    model = str(snapshot["model"])
    num_ctx, keep_alive = conversation_runtime_settings(snapshot)
    think: bool | str = snapshot["think"]
    role = f"{snapshot['display_name']} — glavni lokalni vodja v pogovoru"
    system_prompt = read_system_prompt_snapshot(job_dir, snapshot)
    lead = ensure_conversation_session(
        lead_home, model=model,
        profile_snapshot_hash=str(snapshot["profile_snapshot_hash"]),
        model_digest=str(snapshot["model_digest"]),
    )

    turn_dir = job_dir / "conversation-runtime" / "turns" / f"turn-{time.time_ns()}"
    bridge_home = turn_dir / "bridge"
    executor_config = turn_dir / "executor-config"
    executor_state = turn_dir / "executor-state"
    for path in (bridge_home, executor_config, executor_state):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)

    port = free_local_port()
    token_file = executor_config / "token"
    executor_url = f"http://127.0.0.1:{port}"
    config_path = bridge_home / "config.json"
    bridge_config = {
        "ollama_url": template["ollama_url"],
        "model": model,
        "executor_url": executor_url,
        "token_file": str(token_file),
        "project": str(repository),
        "system_prompt": str(system_prompt),
        "project_memory": str(lead["memory_file"]),
        "parent_session_id": str(lead["session_id"]),
        "num_ctx": num_ctx,
        # Read-only research may legitimately traverse several project paths.
        # Duplicate/misused-call guards stop loops before this final host bound.
        "max_steps": 40,
        "keep_alive": keep_alive,
        "think": think,
        "require_tool_use": True,
        # Ollama is streamed; this is an inactivity window, not a total limit.
        "model_silence_timeout_seconds": 900,
        "minimum_successful_tool_calls": 1,
        "allowed_tools": [
            "project_status", "project_read", "project_list",
            "project_search", "project_diff", "project_run_check",
        ],
        "max_toolless_responses": 2,
        "deterministic_reply_wrapper": not planning_requested,
        "completion_policy": "supervisor_only",
        "response_format": conversation_response_schema(
            planning_requested=planning_requested,
        ),
        "agent_role": role,
    }
    runtime_options = snapshot.get("runtime_options") or {}
    for field in RUNTIME_OPTION_FIELDS:
        if runtime_options.get(field) is not None:
            bridge_config[field] = runtime_options[field]
    atomic_write_immutable_json(config_path, bridge_config)
    executor_env = {
        "MIKROFIX_EXECUTOR_ROOT": str(repository),
        "MIKROFIX_EXECUTOR_CONFIG_DIR": str(executor_config),
        "MIKROFIX_EXECUTOR_STATE_DIR": str(executor_state),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MIKROFIX_EXECUTOR_READ_ONLY_CHECKS": "decimal_value_edges",
    }
    log_path = turn_dir / "executor.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(PROJECT_ROOT / ".venv-server/bin/python"), "-m", "uvicorn",
         "tools.local_executor.server:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT, env={**os.environ, **executor_env},
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    bridge = [str(PROJECT_ROOT / ".venv-server/bin/python"),
              str(PROJECT_ROOT / "tools/mikrofix_bridge.py")]
    bridge_env = {
        "MIKROFIX_BRIDGE_HOME": str(bridge_home),
        "MIKROFIX_BRIDGE_CONFIG": str(config_path),
        "CODEX_THREAD_ID": "",
        "MIKROFIX_WORKSPACE": str(job_dir.parent.parent),
    }
    try:
        wait_for_executor(executor_url, token_file, process)
        prompt = lead_conversation_prompt(
            message, turns=turns,
            planning_requested=planning_requested,
            repository_snapshot=repository_orientation(repository),
            plan_intent=plan_intent,
        )
        bridge_job_id = command(
            bridge + ["dispatch", "--name", f"Neposredni pogovor: {role}",
                      "--task", prompt],
            cwd=PROJECT_ROOT, env=bridge_env, timeout=30,
        ).splitlines()[-1]
        run_bridge_monitor(
            bridge, bridge_env, bridge_home, bridge_job_id,
            # Keep the outer watchdog longer than the bridge's own silence
            # window so the durable inner error remains the primary evidence.
            idle_timeout_seconds=1200, progress_callback=progress_callback,
        )
        raw_response = command(
            bridge + ["result", bridge_job_id], cwd=PROJECT_ROOT,
            env=bridge_env, timeout=30,
        ).strip()
        if not raw_response:
            raise ManagerError("Izbrani lokalni vodja je vrnil prazen odgovor")
        try:
            decision = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise ManagerError("Izbrani lokalni vodja ni vrnil veljavne odločitve") from exc
        if not isinstance(decision, dict):
            raise ManagerError("Odločitev lokalnega vodje mora biti JSON objekt")
        required = set(conversation_response_schema(
            planning_requested=planning_requested,
        )["required"])
        if set(decision) != required:
            raise ManagerError("Odločitev lokalnega vodje nima natančno zahtevanih polj")
        routing = decision.get("routing", "NEEDS_INFO")
        if planning_requested and routing not in {
            "NEEDS_INFO", "READY_FOR_PROCESSING",
        }:
            raise ManagerError("Odločitev lokalnega vodje ima neveljaven routing")
        if not isinstance(decision.get("reply"), str) or not decision["reply"].strip():
            raise ManagerError("Odločitev lokalnega vodje nima odgovora za lastnika")
        return decision, {
            "bridge_job_id": bridge_job_id,
            "session_id": str(lead["session_id"]),
            "memory_sha256": str(lead["memory_sha256"]),
            "trace_root": str(bridge_home / "jobs"),
            "lead_profile": lead_profile,
            "model": model,
            "model_digest": str(snapshot["model_digest"]),
            "profile_snapshot_hash": str(snapshot["profile_snapshot_hash"]),
            "prompt_hash": sha256_text(prompt),
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


def plan_preparer_snapshot(research_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Derive a separate immutable role identity while keeping owner runtime."""
    roles = json.loads(ROLE_REGISTRY.read_text(encoding="utf-8"))["roles"]
    role = roles["plan_preparer"]
    profile = {
        "profile_id": "plan_preparer",
        "version": role["version"],
        "model": research_snapshot["model"],
        "role": "plan_preparer",
        "display_name": role["display_name"],
        "num_ctx": research_snapshot["num_ctx"],
        "conversation_num_ctx": research_snapshot.get(
            "conversation_num_ctx", research_snapshot["num_ctx"],
        ),
        "think": research_snapshot["think"],
        "system_prompt": role["system_prompt"],
        "keep_alive": research_snapshot["keep_alive"],
        "conversation_keep_alive": research_snapshot.get(
            "conversation_keep_alive", research_snapshot["keep_alive"],
        ),
        "runtime_options": research_snapshot.get("runtime_options") or {},
    }
    return build_profile_snapshot(
        profile, project_root=PROJECT_ROOT,
        model_digest=str(research_snapshot["model_digest"]),
    )


def plan_preparer_prompt(
    *, plan_intent: dict[str, Any], research_input: dict[str, Any],
    dossier: dict[str, Any], repository_snapshot: str,
) -> str:
    return (
        "Pripravi samo prikazni kandidat izvedbenega načrta iz spodnjih "
        "eksplicitnih artefaktov. Nimaš dostopa do spomina raziskovalca in ne "
        "smeš klicati orodij.\n\n"
        "TRENUTNI REPOZITORIJ:\n" + repository_snapshot + "\n\n"
        "IZVIRNA ZAHTEVA LASTNIKA:\n" + research_input["owner_request"]
        + "\n\nCODEXOV RAZISKOVALNI PROMPT:\n"
        + research_input["codex_research_prompt"]
        + "\n\nZAMRZNJENI PLAN INTENT:\n"
        + json.dumps(plan_intent, ensure_ascii=False, sort_keys=True)
        + "\n\nCODEXOVO POTRJENI RAZISKOVALNI DOSJE:\n"
        + json.dumps(dossier, ensure_ascii=False, sort_keys=True)
        + "\n\nVrni samo objekt po zahtevani strukturirani shemi."
    )


def run_plan_preparer(
    _message: str, *, job_dir: Path, repository: Path,
    turns: list[dict[str, str]], lead_profile: str = "conversation_lead",
    lead_snapshot: dict[str, Any] | None = None,
    planning_requested: bool = True,
    plan_intent: dict[str, Any] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run a tool-less planner in a fresh role/session over immutable evidence."""
    del turns, lead_profile
    if not planning_requested or plan_intent is None:
        raise ManagerError("plan_preparer requires a frozen planning request")
    research_snapshot = lead_snapshot or read_profile_snapshot(job_dir)
    snapshot = plan_preparer_snapshot(research_snapshot)
    research_input = load_research_input(job_dir)
    dossier = require_current_codex_approval(job_dir)
    if plan_intent.get("research_input_sha256") != research_input["research_input_sha256"]:
        raise ManagerError("Plan intent is not bound to the research input")
    if plan_intent.get("research_dossier_sha256") != dossier["dossier_sha256"]:
        raise ManagerError("Plan intent is not bound to the approved research dossier")

    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    intent_hash = str(plan_intent["intent_sha256"])
    turn_dir = (
        job_dir / "planning-runtime" / intent_hash / f"attempt-{time.time_ns()}"
    )
    bridge_home = turn_dir / "bridge"
    executor_config = turn_dir / "executor-config"
    executor_state = turn_dir / "executor-state"
    for path in (bridge_home, executor_config, executor_state):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    snapshot_path = turn_dir / "plan-preparer-profile-snapshot.json"
    if not snapshot_path.exists():
        atomic_write_immutable_json(snapshot_path, snapshot)
    elif json.loads(snapshot_path.read_text(encoding="utf-8")) != snapshot:
        raise ManagerError("Stored plan_preparer snapshot differs")

    port = free_local_port()
    token_file = executor_config / "token"
    executor_url = f"http://127.0.0.1:{port}"
    config_path = bridge_home / "config.json"
    num_ctx, keep_alive = conversation_runtime_settings(snapshot)
    bridge_config = {
        "ollama_url": template["ollama_url"],
        "model": snapshot["model"],
        "executor_url": executor_url,
        "token_file": str(token_file),
        "project": str(repository),
        "system_prompt": str(PLAN_PREPARER_SYSTEM),
        "num_ctx": num_ctx,
        "max_steps": 4,
        "keep_alive": keep_alive,
        "think": snapshot["think"],
        "require_tool_use": False,
        "model_silence_timeout_seconds": 900,
        "minimum_successful_tool_calls": 1,
        "allowed_tools": [],
        "max_toolless_responses": 2,
        "deterministic_reply_wrapper": False,
        "completion_policy": "supervisor_only",
        "response_format": conversation_response_schema(planning_requested=True),
        "agent_role": "Pripravljavec izvedbenega načrta",
    }
    for field in RUNTIME_OPTION_FIELDS:
        value = (snapshot.get("runtime_options") or {}).get(field)
        if value is not None:
            bridge_config[field] = value
    if not config_path.exists():
        atomic_write_immutable_json(config_path, bridge_config)
    elif json.loads(config_path.read_text(encoding="utf-8")) != bridge_config:
        raise ManagerError("Stored plan_preparer runtime differs")
    executor_env = {
        "MIKROFIX_EXECUTOR_ROOT": str(repository),
        "MIKROFIX_EXECUTOR_CONFIG_DIR": str(executor_config),
        "MIKROFIX_EXECUTOR_STATE_DIR": str(executor_state),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    log = (turn_dir / "executor.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(PROJECT_ROOT / ".venv-server/bin/python"), "-m", "uvicorn",
         "tools.local_executor.server:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT, env={**os.environ, **executor_env},
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    bridge = [str(PROJECT_ROOT / ".venv-server/bin/python"),
              str(PROJECT_ROOT / "tools/mikrofix_bridge.py")]
    bridge_env = {
        "MIKROFIX_BRIDGE_HOME": str(bridge_home),
        "MIKROFIX_BRIDGE_CONFIG": str(config_path),
        "CODEX_THREAD_ID": "",
        "MIKROFIX_WORKSPACE": str(job_dir.parent.parent),
    }
    prompt = plan_preparer_prompt(
        plan_intent=plan_intent, research_input=research_input,
        dossier=dossier, repository_snapshot=repository_orientation(repository),
    )
    try:
        wait_for_executor(executor_url, token_file, process)
        bridge_job_id = command(
            bridge + ["dispatch", "--name", "Ločeni pripravljavec načrta",
                      "--task", prompt],
            cwd=PROJECT_ROOT, env=bridge_env, timeout=30,
        ).splitlines()[-1]
        run_bridge_monitor(
            bridge, bridge_env, bridge_home, bridge_job_id,
            idle_timeout_seconds=1200, progress_callback=progress_callback,
        )
        raw = command(
            bridge + ["result", bridge_job_id], cwd=PROJECT_ROOT,
            env=bridge_env, timeout=30,
        ).strip()
        decision = json.loads(raw)
        required = set(conversation_response_schema(planning_requested=True)["required"])
        if not isinstance(decision, dict) or set(decision) != required:
            raise ManagerError("plan_preparer did not return the exact planning schema")
        return decision, {
            "bridge_job_id": bridge_job_id,
            "session_id": f"PLAN-{intent_hash[:16]}",
            "memory_sha256": "",
            "trace_root": str(bridge_home / "jobs"),
            "lead_profile": "plan_preparer",
            "model": str(snapshot["model"]),
            "model_digest": str(snapshot["model_digest"]),
            "profile_snapshot_hash": str(snapshot["profile_snapshot_hash"]),
            "prompt_hash": sha256_text(prompt),
        }
    except (json.JSONDecodeError, OSError) as exc:
        raise ManagerError("plan_preparer returned invalid JSON") from exc
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


def talk(
    message: str, *, parent_run_id: str,
    workspace: Path = DEFAULT_WORKSPACE,
    lead_profile: str = "conversation_lead",
    responder: Callable[..., tuple[dict[str, Any], dict[str, str]]] = run_lead_conversation,
    planning_requested: bool = False,
    plan_intent: dict[str, Any] | None = None,
    plan_candidate_path: Path | None = None,
    owner_content: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Append one direct owner/lead turn without creating a new manager job."""
    workspace = workspace.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_run_id):
        raise ManagerError("Unsafe parent run id")
    job_dir = workspace / "jobs" / parent_run_id
    if not (job_dir / "request.json").is_file():
        raise ManagerError(f"Parent run does not exist: {parent_run_id}")
    clean_message = message.strip()
    if not clean_message:
        raise ManagerError("Conversation message must not be empty")
    config = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    repository = Path(config["repository"]).resolve()
    conversation_file = job_dir / "conversation.json"
    lock_path = job_dir / "conversation.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        snapshot = read_profile_snapshot(job_dir)
        conversation: dict[str, Any] = {}
        if conversation_file.is_file():
            try:
                value = json.loads(conversation_file.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    conversation = value
            except (OSError, json.JSONDecodeError):
                conversation = {}
        locked_profile = str(conversation.get("lead_profile") or "")
        if snapshot["profile_id"] != lead_profile:
            raise ManagerError(
                "Ta pogovor je zaklenjen na drug immutable profil vodje."
            )
        if locked_profile and locked_profile != lead_profile:
            raise ManagerError(
                "Ta pogovor je zaklenjen na drug profil vodje. Za drugi profil "
                "odpri novo nalogo."
            )
        turns = conversation.get("turns")
        if not isinstance(turns, list):
            turns = []
        responder_kwargs = {
            "job_dir": job_dir, "repository": repository, "turns": turns,
            "lead_profile": lead_profile, "lead_snapshot": snapshot,
            "planning_requested": planning_requested,
        }
        if planning_requested:
            if plan_intent is None:
                raise ManagerError("Planning requires a frozen plan intent")
            responder_kwargs["plan_intent"] = plan_intent
        if progress_callback is not None:
            responder_kwargs["progress_callback"] = progress_callback
        decision, evidence = responder(clean_message, **responder_kwargs)
        if (
            planning_requested
            and decision.get("routing") == "READY_FOR_PROCESSING"
        ):
            assert plan_intent is not None
            decision = normalize_plan_sources(decision, plan_intent)
        reported_routing = str(decision.get("routing", "NEEDS_INFO"))
        routing = reported_routing if planning_requested else "NEEDS_INFO"
        reply = str(decision["reply"]).strip()
        research_manifests = conversation.get("research_manifests")
        if not isinstance(research_manifests, list):
            research_manifests = []
        if (
            not planning_requested
            and (job_dir / "research-input.json").is_file()
            and not conversation.get("research_invalidated_by_goal_replacement")
        ):
            research_input = load_research_input(job_dir)
            bridge_job_id = str(evidence.get("bridge_job_id") or "")
            trace_root = Path(str(evidence.get("trace_root") or ""))
            if not bridge_job_id or not trace_root:
                raise ManagerError("Research response has no durable bridge trace")
            manifest = build_research_evidence_manifest(
                trace_path=trace_root / bridge_job_id / "trace.jsonl",
                bridge_job_id=bridge_job_id,
                report=reply,
                research_input=research_input,
                evidence=evidence,
            )
            research_dir = job_dir / "research"
            manifest_path = research_dir / f"manifest-{len(research_manifests) + 1:04d}.json"
            atomic_write_immutable_json(manifest_path, manifest)
            research_manifests.append({
                "path": str(manifest_path.relative_to(job_dir)),
                "sha256": manifest["manifest_sha256"],
            })
        if not planning_requested and reported_routing == "READY_FOR_PROCESSING":
            reply += (
                "\n\nIzvedbeni načrt ni bil ustvarjen. Ko želiš zaključiti pogovor "
                "in pripraviti načrt, uporabi /prepare-plan."
            )
        routed_run_id = ""
        latest_plan_candidate = conversation.get("latest_plan_candidate")
        event_hashes = {
            "prompt_hash": evidence.get("prompt_hash") or None,
            "context_pack_hash": evidence.get("memory_sha256") or None,
            "input_hash": sha256_text(clean_message),
            "output_hash": sha256_text(reply),
        }
        if routing == "READY_FOR_PROCESSING":
            assert plan_intent is not None
            source_trace = validate_plan_sources(decision, plan_intent)
            proposal = {
                key: decision[key]
                for key in (
                    "objective", "allowed_files", "atoms", "constraints",
                    "acceptance_criteria",
                )
            }
            errors = model_plan_errors(proposal, repository)
            if errors:
                raise ManagerError(
                    "READY_FOR_PROCESSING plan is incomplete: " + "; ".join(errors)
                )
            candidate_record = {
                    "schema_version": 1,
                    "intent_sha256": plan_intent["intent_sha256"],
                    "decision": decision,
                    "evidence": {
                        key: evidence.get(key, "")
                        for key in (
                            "bridge_job_id", "session_id", "memory_sha256",
                            "lead_profile", "model", "model_digest",
                            "profile_snapshot_hash", "prompt_hash",
                        )
                    },
                    "candidate_sha256": canonical_json_sha256({
                        "intent_sha256": plan_intent["intent_sha256"],
                        "decision": decision,
                    }),
                }
            if plan_candidate_path is None:
                raise ManagerError("Planning candidate path is required")
            if not plan_candidate_path.exists():
                atomic_write_immutable_json(plan_candidate_path, candidate_record)
            else:
                stored_candidate = json.loads(
                    plan_candidate_path.read_text(encoding="utf-8")
                )
                if stored_candidate != candidate_record:
                    raise ManagerError("Stored plan candidate does not match its intent")
            latest_plan_candidate = {
                "intent_sha256": plan_intent["intent_sha256"],
                "candidate_sha256": candidate_record["candidate_sha256"],
                "path": str(plan_candidate_path.relative_to(job_dir)),
            }
            reply = reply + "\n\n" + render_plan_candidate(
                decision, str(candidate_record["candidate_sha256"]),
            )
            record_task_event(
                job_dir, snapshot=snapshot, event_type="LEAD_PROPOSED_PLAN",
                actor_type="lead", actor_id=str(
                    evidence.get("lead_profile") or "plan_preparer"
                ), **event_hashes,
                details={
                    "intent_sha256": plan_intent["intent_sha256"],
                    "candidate_sha256": candidate_record["candidate_sha256"],
                    "dispatched": False,
                },
            )
        else:
            record_task_event(
                job_dir, snapshot=snapshot, event_type="LEAD_MESSAGE",
                actor_type="lead", actor_id=lead_profile, **event_hashes,
                details={
                    "routing": routing,
                    "reported_routing": reported_routing,
                    "planning_requested": planning_requested,
                },
            )
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        owner_turn = {
            "role": "owner", "content": (owner_content or clean_message).strip(),
            "at": stamp,
        }
        lead_turn = {"role": "lead", "content": reply, "at": stamp,
             "lead_profile": str(evidence.get("lead_profile") or lead_profile),
             "routing": routing, "routed_run_id": routed_run_id,
             "bridge_job_id": evidence.get("bridge_job_id", "")}
        if planning_requested:
            owner_turn["kind"] = "plan_command"
            lead_turn["kind"] = "plan_candidate"
        turns.extend([
            owner_turn,
            lead_turn,
        ])
        conversation = {
            "schema_version": 1,
            "parent_run_id": parent_run_id,
            "session_id": evidence.get("session_id", ""),
            "memory_sha256": evidence.get("memory_sha256", ""),
            "lead_profile": lead_profile,
            "model_tag": conversation.get("model_tag", snapshot["model_tag"]),
            "turns": turns,
            "active_run_id": routed_run_id or conversation.get("active_run_id", ""),
            "latest_plan_candidate": latest_plan_candidate,
            "rejected_run_ids": conversation.get("rejected_run_ids", []),
            "research_input_sha256": conversation.get(
                "research_input_sha256", "",
            ),
            "research_manifests": research_manifests,
            "updated_at": stamp,
        }
        atomic_write_json(conversation_file, conversation)
        return conversation


def replace_conversation_goal(
    parent_run_id: str, new_goal: str, *, workspace: Path = DEFAULT_WORKSPACE,
) -> dict[str, Any]:
    """Record an explicit owner-authorized goal replacement without an LLM."""
    workspace = workspace.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_run_id):
        raise ManagerError("Unsafe parent run id")
    clean_goal = new_goal.strip()
    if not clean_goal:
        raise ManagerError("Replacement goal must not be empty")
    job_dir = workspace / "jobs" / parent_run_id
    snapshot = read_profile_snapshot(job_dir)
    lifecycle = read_task_state(job_dir)
    if lifecycle["state"] != TaskState.DRAFT.value:
        raise ManagerError(
            f"Goal can only be replaced in DRAFT, not {lifecycle['state']}"
        )
    conversation_file = job_dir / "conversation.json"
    lock_path = job_dir / "conversation.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        conversation = json.loads(conversation_file.read_text(encoding="utf-8"))
        if conversation.get("active_run_id"):
            raise ManagerError("Conversation already has an active execution plan")
        turns = conversation.get("turns")
        if not isinstance(turns, list):
            raise ManagerError("Conversation turns are invalid")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        turns.append({
            "role": "owner", "kind": "goal_replacement",
            "content": clean_goal, "at": stamp,
        })
        conversation["turns"] = turns
        if (job_dir / "research-input.json").is_file():
            conversation["research_manifests"] = []
            conversation["research_input_sha256"] = ""
            conversation["latest_plan_candidate"] = None
            conversation["research_invalidated_by_goal_replacement"] = {
                "at": stamp,
                "turn_index": len(turns),
            }
        conversation["updated_at"] = stamp
        atomic_write_json(conversation_file, conversation)
    record_task_event(
        job_dir, snapshot=snapshot, event_type="OWNER_REPLACED_GOAL",
        actor_type="owner", actor_id="owner",
        input_hash=sha256_text(clean_goal),
        details={"replaced_by_turn_index": len(turns)},
    )
    return conversation


def prepare_plan(
    parent_run_id: str, *, workspace: Path = DEFAULT_WORKSPACE,
    responder: Callable[..., tuple[dict[str, Any], dict[str, str]]] = run_plan_preparer,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Prepare and display one reviewable plan revision without dispatching it."""
    workspace = workspace.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_run_id):
        raise ManagerError("Unsafe parent run id")
    job_dir = workspace / "jobs" / parent_run_id
    snapshot = read_profile_snapshot(job_dir)
    conversation = json.loads(
        (job_dir / "conversation.json").read_text(encoding="utf-8")
    )
    if conversation.get("active_run_id"):
        return conversation
    lifecycle = read_task_state(job_dir)
    if lifecycle["state"] != TaskState.DRAFT.value:
        raise ManagerError(
            f"Plan can only be prepared from DRAFT, not {lifecycle['state']}"
        )
    if conversation.get("research_invalidated_by_goal_replacement"):
        raise ManagerError(
            "Zamenjava cilja je razveljavila stare raziskovalne dokaze. "
            "Za novi cilj začni novo nalogo z /research."
        )
    config = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    repository = Path(config["repository"]).resolve()
    has_research_input = (job_dir / "research-input.json").is_file()
    if responder is run_plan_preparer and not has_research_input:
        raise ManagerError(
            "Ta pogovor nima nespremenljivega /research vhoda. Začni novo "
            "nalogo z /research."
        )
    if has_research_input:
        try:
            approved_dossier = require_current_codex_approval(job_dir)
        except ResearchError as exc:
            raise ManagerError(str(exc)) from exc
        try:
            assert_repository_identity(
                approved_dossier["research_identity"], repository,
                project_map=repository / "docs/local_agents/MIKROFIX_PROJECT_MAP.md",
                canon_paths=sorted((repository / "docs/kanon").glob("*.md")),
            )
        except (KeyError, ResearchError) as exc:
            raise ManagerError("Approved research dossier is stale") from exc
        conversation["approved_research_dossier_sha256"] = approved_dossier[
            "dossier_sha256"
        ]
    plan_intent = build_plan_intent(
        conversation, repository=repository,
        job_dir=job_dir if (job_dir / "research-input.json").is_file() else None,
    )
    planning_dir = job_dir / "planning"
    intent_hash = str(plan_intent["intent_sha256"])
    intent_path = planning_dir / f"intent-{intent_hash}.json"
    if intent_path.exists():
        existing_intent = json.loads(intent_path.read_text(encoding="utf-8"))
        if existing_intent != plan_intent:
            raise ManagerError("Stored plan intent does not match the conversation")
    else:
        atomic_write_immutable_json(intent_path, plan_intent)
    candidate_path = planning_dir / f"lead-candidate-{intent_hash}.json"
    selected_responder = responder
    if candidate_path.is_file():
        cached = json.loads(candidate_path.read_text(encoding="utf-8"))
        if set(cached) != {
            "schema_version", "intent_sha256", "decision", "evidence",
            "candidate_sha256",
        } or cached.get("schema_version") != 1:
            raise ManagerError("Cached lead candidate has unknown or missing fields")
        if cached.get("intent_sha256") != intent_hash:
            raise ManagerError("Cached lead candidate belongs to another plan intent")
        cached_decision = cached.get("decision")
        cached_evidence = cached.get("evidence")
        if not isinstance(cached_decision, dict) or not isinstance(cached_evidence, dict):
            raise ManagerError("Cached lead candidate is invalid")
        expected_candidate_hash = canonical_json_sha256({
            "intent_sha256": intent_hash,
            "decision": cached_decision,
        })
        if cached.get("candidate_sha256") != expected_candidate_hash:
            raise ManagerError("Cached lead candidate hash is invalid")

        def cached_responder(
            _message: str, **_kwargs: Any,
        ) -> tuple[dict[str, Any], dict[str, str]]:
            return dict(cached_decision), {
                str(key): str(value) for key, value in cached_evidence.items()
            }

        selected_responder = cached_responder
    return talk(
        "/prepare-plan", parent_run_id=parent_run_id, workspace=workspace,
        lead_profile=str(snapshot["profile_id"]), responder=selected_responder,
        planning_requested=True, plan_intent=plan_intent,
        plan_candidate_path=candidate_path,
        progress_callback=progress_callback,
    )


def continue_research_revision(
    parent_run_id: str, *, research_task_id: str, reason: str,
    workspace: Path = DEFAULT_WORKSPACE,
    worker_runner: Callable[..., tuple[dict[str, Any], Any]] | None = None,
    review_runner: Callable[..., dict[str, Any]] | None = None,
    synthesis_runner: Callable[..., dict[str, Any]] | None = None,
    model_releaser: Callable[[str], None] = release_ollama_model,
    model_identity_checker: Callable[[dict[str, Any], dict[str, Any]], None] = (
        assert_frozen_research_models
    ),
) -> dict[str, Any]:
    """Run one Codex-requested worker revision and create dossier revision N+1."""
    workspace = workspace.expanduser().resolve()
    job_dir = workspace / "jobs" / parent_run_id
    config = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    repository = Path(config["repository"]).resolve()
    project_map = repository / "docs/local_agents/MIKROFIX_PROJECT_MAP.md"
    canon_paths = sorted((repository / "docs/kanon").glob("*.md"))
    lead_snapshot = read_profile_snapshot(job_dir)
    worker_snapshot = json.loads(
        (job_dir / "worker-profile-snapshot.json").read_text(encoding="utf-8")
    )
    identity = json.loads((job_dir / "research-identity.json").read_text(encoding="utf-8"))
    previous = load_current_dossier(job_dir)

    def assert_identity() -> None:
        assert_repository_identity(
            identity, repository, project_map=project_map, canon_paths=canon_paths,
        )
        model_identity_checker(lead_snapshot, worker_snapshot)

    tasks = previous["accepted_tasks"]
    selected = next(
        (task for task in tasks if task["research_task_id"] == research_task_id), None,
    )
    if selected is None:
        raise ResearchError("Codex revision references an unknown research task")
    prior_result = next(
        result for result in previous["worker_results"]
        if result["research_task_id"] == research_task_id
    )
    prior_attempt = int(prior_result["attempt"])

    def execute_worker(request: dict[str, Any]):
        assert_identity()
        task = request["research_task"]
        prompt = (
            "Ponovi samo ciljno read-only podnalogo po Codexovi pripombi. "
            "Dokazna branja označi READ-1, READ-2 ... in vrni samo JSON.\n\n"
            + json.dumps(request, ensure_ascii=False, sort_keys=True)
        )
        if worker_runner is not None:
            return worker_runner(
                request, prompt=prompt, job_dir=job_dir,
                repository=repository, snapshot=worker_snapshot,
            )
        return run_structured_research_session(
            prompt, job_dir=job_dir, repository=repository, snapshot=worker_snapshot,
            session_kind=f"RESEARCH_WORKER_{research_task_id}_A{request['attempt']}",
            response_format=worker_result_schema(),
            allowed_tools=["project_status", "project_read", "project_list", "project_search"],
            max_steps=30, allowed_scope=task["allowed_scope"],
        )

    def prepare_result(request: dict[str, Any], result: dict[str, Any], events: Any):
        if not isinstance(events, list):
            raise ResearchError("Worker session has no durable tool-result trace")
        cards = evidence_cards_from_trace(
            events, task=request["research_task"], attempt=request["attempt"],
        )
        if not cards:
            raise ResearchError("Worker produced no evidence cards")
        mapping = {
            f"READ-{index}": card["evidence_id"]
            for index, card in enumerate(cards, start=1)
        }
        normalized = json.loads(json.dumps(result))
        for field in ("answers", "claims"):
            for item in normalized.get(field, []):
                item["evidence_ids"] = [
                    mapping.get(value, value) for value in item.get("evidence_ids", [])
                ]
        expansion = normalized.get("scope_expansion")
        if isinstance(expansion, dict):
            expansion["evidence_ids"] = [
                mapping.get(value, value) for value in expansion.get("evidence_ids", [])
            ]
        attempt_dir = (
            job_dir / "research-workers" / research_task_id
            / f"attempt-{request['attempt']}"
        )
        atomic_write_immutable_json(attempt_dir / "issued-evidence.json", {
            "schema_version": 1, "evidence_cards": cards,
        })
        return normalized, cards

    def review(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        assert_identity()
        attempt_dir = (
            job_dir / "research-workers" / research_task_id
            / f"attempt-{result['attempt']}"
        )
        cards = json.loads((attempt_dir / "issued-evidence.json").read_text())["evidence_cards"]
        prompt = (
            "Preveri popravljeni rezultat proti podnalogi in Codexovi pripombi. "
            "Vrni samo zahtevani JSON.\n\n"
            + json.dumps({"task": task, "result": result, "evidence_cards": cards,
                          "codex_reason": reason}, ensure_ascii=False, sort_keys=True)
        )
        if review_runner is not None:
            return review_runner(
                task, result, prompt=prompt, job_dir=job_dir, snapshot=lead_snapshot,
            )
        decision, _ = run_structured_research_session(
            prompt, job_dir=job_dir, repository=repository, snapshot=lead_snapshot,
            session_kind="LEAD_WORKER_REVISION_REVIEW",
            response_format=lead_review_schema(), allowed_tools=[], max_steps=4,
        )
        return decision

    manager_file = job_dir / "manager.json"
    manager_state = json.loads(manager_file.read_text(encoding="utf-8"))
    manager_state["research_state"] = "WORKERS_RUNNING"
    atomic_write_json(manager_file, manager_state)
    revised = run_sequential_workers(
        root=job_dir, repository=repository, tasks=[selected],
        worker_snapshot=worker_snapshot, worker_runner=execute_worker,
        review_result=review, release_model=model_releaser,
        prepare_result=prepare_result,
        expand_scope=lambda task, result, issued: expand_research_scope(
            task, result, issued, repository=repository,
        ),
        attempt_offsets={research_task_id: prior_attempt},
        initial_corrections={research_task_id: {
            "previous_worker_result": prior_result,
            "missing_evidence": [reason], "reason": reason,
        }},
    )[0]
    accepted: list[dict[str, Any]] = []
    for task, result, decision in zip(
        previous["accepted_tasks"], previous["worker_results"],
        previous["lead_decisions"], strict=True,
    ):
        if task["research_task_id"] == research_task_id:
            accepted.append(revised)
            continue
        attempt = int(result["attempt"])
        accepted.append({
            "task": task, "attempt": attempt, "attempt_id": "preserved",
            "result": result, "lead_decision": decision,
            "evidence_cards": [
                card for card in previous["evidence_cards"]
                if card["research_task_id"] == task["research_task_id"]
                and card["attempt"] == attempt
            ],
        })
    cards = [card for item in accepted for card in item["evidence_cards"]]
    assert_identity()
    research_input = load_research_input(job_dir)
    gate = evidence_gate(
        identity=identity, repository=repository, project_map=project_map,
        canon_paths=canon_paths, tasks=[item["task"] for item in accepted],
        accepted_results=accepted, evidence_cards=cards,
        required_question_ids={item["id"] for item in research_input["required_questions"]},
    )
    next_revision = int(previous["dossier_revision"]) + 1
    atomic_write_immutable_json(
        job_dir / f"evidence-gate-revision-{next_revision:04d}.json", gate,
    )
    synthesis_payload = {"accepted_results": accepted, "evidence_cards": cards}
    if synthesis_runner is None:
        synthesis, _ = run_structured_research_session(
            "Sestavi strukturirano sintezo. Vrni samo JSON.\n\n"
            + json.dumps(synthesis_payload, ensure_ascii=False, sort_keys=True),
            job_dir=job_dir, repository=repository, snapshot=lead_snapshot,
            session_kind="LEAD_REVISION_SYNTHESIS",
            response_format=lead_synthesis_schema(), allowed_tools=[], max_steps=4,
        )
    else:
        synthesis = synthesis_runner(
            accepted, cards, prompt=json.dumps(synthesis_payload, ensure_ascii=False),
            job_dir=job_dir, snapshot=lead_snapshot,
        )
    if not isinstance(synthesis, dict) or set(synthesis) != {"synthesis", "unresolved"}:
        raise ResearchError("LEAD_SYNTHESIS returned an invalid schema")
    dossier = create_dossier_revision(
        root=job_dir, identity=identity,
        required_questions=research_input["required_questions"],
        accepted_results=accepted, evidence_cards=cards,
        synthesis=synthesis["synthesis"], unresolved=synthesis["unresolved"],
    )
    manager_state["research_state"] = "AWAITING_CODEX_RESEARCH_REVIEW"
    manager_state["research_dossier_sha256"] = dossier["dossier_sha256"]
    manager_state["research_verdict_sha256"] = ""
    atomic_write_json(manager_file, manager_state)
    conversation_file = job_dir / "conversation.json"
    conversation = json.loads(conversation_file.read_text(encoding="utf-8"))
    conversation["research_dossier_sha256"] = dossier["dossier_sha256"]
    conversation["updated_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
    )
    conversation["turns"].append({
        "role": "lead", "kind": "research_dossier",
        "content": json.dumps({
            "status": "DOSSIER_READY", "dossier_revision": dossier["dossier_revision"],
            "dossier_sha256": dossier["dossier_sha256"],
            "synthesis": dossier["synthesis"], "unresolved": dossier["unresolved"],
        }, ensure_ascii=False, indent=2),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    atomic_write_json(conversation_file, conversation)
    return dossier


def _record_research_review_locked(
    parent_run_id: str, *, dossier_sha256: str, verdict: str,
    research_task_id: str | None = None, reason: str = "",
    workspace: Path = DEFAULT_WORKSPACE,
    revision_worker_runner: Callable[..., tuple[dict[str, Any], Any]] | None = None,
    revision_review_runner: Callable[..., dict[str, Any]] | None = None,
    revision_synthesis_runner: Callable[..., dict[str, Any]] | None = None,
    model_releaser: Callable[[str], None] = release_ollama_model,
    model_identity_checker: Callable[[dict[str, Any], dict[str, Any]], None] = (
        assert_frozen_research_models
    ),
) -> dict[str, Any]:
    """Record a verdict while the caller holds research-review.lock."""
    workspace = workspace.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_run_id):
        raise ManagerError("Unsafe parent run id")
    job_dir = workspace / "jobs" / parent_run_id
    try:
        dossier = load_current_dossier(job_dir)
        config = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
        repository = Path(config["repository"]).resolve()
        assert_repository_identity(
            dossier["research_identity"], repository,
            project_map=repository / "docs/local_agents/MIKROFIX_PROJECT_MAP.md",
            canon_paths=sorted((repository / "docs/kanon").glob("*.md")),
        )
        verdict_record = record_codex_research_verdict(
            root=job_dir, dossier_sha256=dossier_sha256, verdict=verdict,
            research_task_id=research_task_id, reason=reason,
        )
    except ResearchError as exc:
        raise ManagerError(str(exc)) from exc
    manager_file = job_dir / "manager.json"
    manager = json.loads(manager_file.read_text(encoding="utf-8"))
    manager["research_state"] = {
        "APPROVE": "CODEX_RESEARCH_APPROVED",
        "REVISE": "RESEARCH_REVISION_REQUIRED",
        "REJECT": "BLOCKED",
    }[verdict]
    manager["research_dossier_sha256"] = dossier_sha256
    manager["research_verdict_sha256"] = verdict_record["verdict_sha256"]
    atomic_write_json(manager_file, manager)
    if verdict == "REVISE":
        try:
            dossier = continue_research_revision(
                parent_run_id, research_task_id=str(research_task_id), reason=reason,
                workspace=workspace, worker_runner=revision_worker_runner,
                review_runner=revision_review_runner,
                synthesis_runner=revision_synthesis_runner,
                model_releaser=model_releaser,
                model_identity_checker=model_identity_checker,
            )
        except BaseException as exc:
            manager = json.loads(manager_file.read_text(encoding="utf-8"))
            manager["research_state"] = "BLOCKED"
            manager["exact_reason"] = str(exc)
            atomic_write_json(manager_file, manager)
            raise
        verdict_record = {**verdict_record, "new_dossier": dossier}
    return verdict_record


def record_research_review(
    parent_run_id: str, *, dossier_sha256: str, verdict: str,
    research_task_id: str | None = None, reason: str = "",
    workspace: Path = DEFAULT_WORKSPACE,
    revision_worker_runner: Callable[..., tuple[dict[str, Any], Any]] | None = None,
    revision_review_runner: Callable[..., dict[str, Any]] | None = None,
    revision_synthesis_runner: Callable[..., dict[str, Any]] | None = None,
    model_releaser: Callable[[str], None] = release_ollama_model,
    model_identity_checker: Callable[[dict[str, Any], dict[str, Any]], None] = (
        assert_frozen_research_models
    ),
) -> dict[str, Any]:
    """Atomically bind and execute a verdict for one exact dossier revision."""
    workspace = workspace.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_run_id):
        raise ManagerError("Unsafe parent run id")
    job_dir = workspace / "jobs" / parent_run_id
    lock_path = job_dir / "research-review.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _record_research_review_locked(
            parent_run_id, dossier_sha256=dossier_sha256, verdict=verdict,
            research_task_id=research_task_id, reason=reason,
            workspace=workspace, revision_worker_runner=revision_worker_runner,
            revision_review_runner=revision_review_runner,
            revision_synthesis_runner=revision_synthesis_runner,
            model_releaser=model_releaser,
            model_identity_checker=model_identity_checker,
        )


def dispatch_plan(
    parent_run_id: str, *, workspace: Path = DEFAULT_WORKSPACE,
) -> dict[str, Any]:
    """Dispatch exactly the latest owner-visible plan revision to validation."""
    workspace = workspace.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_run_id):
        raise ManagerError("Unsafe parent run id")
    job_dir = workspace / "jobs" / parent_run_id
    snapshot = read_profile_snapshot(job_dir)
    conversation_file = job_dir / "conversation.json"
    lock_path = job_dir / "conversation.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        conversation = json.loads(conversation_file.read_text(encoding="utf-8"))
        if conversation.get("active_run_id"):
            raise ManagerError("Conversation already has a dispatched plan")
        lifecycle = read_task_state(job_dir)
        if lifecycle["state"] != TaskState.DRAFT.value:
            raise ManagerError(
                f"Plan can only be dispatched from DRAFT, not {lifecycle['state']}"
            )
        config = json.loads(
            (workspace / "workspace.json").read_text(encoding="utf-8")
        )
        repository = Path(config["repository"]).resolve()
        plan_intent = build_plan_intent(
            conversation, repository=repository,
            job_dir=job_dir if (job_dir / "research-input.json").is_file() else None,
        )
        pointer = conversation.get("latest_plan_candidate")
        if not isinstance(pointer, dict) or set(pointer) != {
            "intent_sha256", "candidate_sha256", "path",
        }:
            raise ManagerError("Najprej pripravi načrt z /prepare-plan")
        intent_hash = str(plan_intent["intent_sha256"])
        if pointer.get("intent_sha256") != intent_hash:
            raise ManagerError(
                "Pogovor se je po zadnjem načrtu spremenil. "
                "Ponovno uporabi /prepare-plan."
            )
        candidate_path = (
            job_dir / "planning" / f"lead-candidate-{intent_hash}.json"
        )
        if pointer.get("path") != str(candidate_path.relative_to(job_dir)):
            raise ManagerError("Latest plan candidate path is invalid")
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        if set(candidate) != {
            "schema_version", "intent_sha256", "decision", "evidence",
            "candidate_sha256",
        } or candidate.get("schema_version") != 1:
            raise ManagerError("Stored plan candidate has unknown or missing fields")
        expected_hash = canonical_json_sha256({
            "intent_sha256": intent_hash,
            "decision": candidate.get("decision"),
        })
        if (
            candidate.get("intent_sha256") != intent_hash
            or candidate.get("candidate_sha256") != expected_hash
            or pointer.get("candidate_sha256") != expected_hash
        ):
            raise ManagerError("Stored plan candidate hash is invalid")
        decision = candidate.get("decision")
        if not isinstance(decision, dict):
            raise ManagerError("Stored plan decision is invalid")
        candidate_evidence = candidate.get("evidence")
        if not isinstance(candidate_evidence, dict):
            raise ManagerError("Stored plan evidence is invalid")
        if (
            (job_dir / "research-input.json").is_file()
            and candidate_evidence.get("lead_profile") != "plan_preparer"
        ):
            raise ManagerError(
                "Research plan candidate was not produced by plan_preparer"
            )
        source_trace = validate_plan_sources(decision, plan_intent)
        proposal = {
            key: decision[key]
            for key in (
                "objective", "allowed_files", "atoms", "constraints",
                "acceptance_criteria",
            )
        }
        errors = model_plan_errors(proposal, repository)
        if errors:
            raise ManagerError(
                "Stored plan candidate is incomplete: " + "; ".join(errors)
            )
        ready_task = trusted_plan_task(plan_intent, source_trace)
        routed = submit(
            ready_task, workspace=workspace,
            generator=lambda task, **kwargs: run_planner(
                task, advisor_proposal=proposal,
                advisor_snapshot=snapshot, plan_intent=plan_intent,
                source_trace=source_trace, **kwargs,
            ),
            initialize=True, parent_run_id=parent_run_id,
            lead_snapshot=snapshot,
        )
        routed_run_id = str(routed["run_id"])
        transition_task(
            job_dir, TaskState.READY_FOR_PROCESSING,
            snapshot=snapshot, actor_type="owner", actor_id="owner",
            output_hash=expected_hash,
            details={
                "command": "/plan-dispatch",
                "candidate_sha256": expected_hash,
                "routed_run_id": routed_run_id,
            },
        )
        transition_task(
            job_dir, TaskState.AWAITING_OWNER_APPROVAL,
            snapshot=snapshot, actor_type="manager", actor_id="manager",
            output_hash=expected_hash,
            details={"routed_run_id": routed_run_id},
        )
        conversation["active_run_id"] = routed_run_id
        conversation["updated_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        atomic_write_json(conversation_file, conversation)
        return conversation


def reject_plan(
    parent_run_id: str, reason: str = "", *, workspace: Path = DEFAULT_WORKSPACE,
) -> dict[str, Any]:
    """Reject an unapproved dispatched plan and reopen its parent conversation."""
    workspace = workspace.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_run_id):
        raise ManagerError("Unsafe parent run id")
    parent_dir = workspace / "jobs" / parent_run_id
    conversation_file = parent_dir / "conversation.json"
    lock_path = parent_dir / "conversation.lock"
    clean_reason = reason.strip() or (
        "Načrt ni odobren. Pred novo revizijo upoštevaj nadaljnja navodila lastnika."
    )
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        conversation = json.loads(conversation_file.read_text(encoding="utf-8"))
        child_run_id = str(conversation.get("active_run_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", child_run_id):
            raise ManagerError("Conversation has no dispatched plan to reject")
        child_dir = workspace / "jobs" / child_run_id
        child_request = json.loads(
            (child_dir / "request.json").read_text(encoding="utf-8")
        )
        child_manager_file = child_dir / "manager.json"
        child_manager = json.loads(child_manager_file.read_text(encoding="utf-8"))
        if child_request.get("parent_run_id") != parent_run_id:
            raise ManagerError("Dispatched plan does not belong to this conversation")
        if child_manager.get("state") != "READY_FOR_OWNER_LEASE":
            raise ManagerError(
                "Plan can only be rejected before execution, not "
                f"{child_manager.get('state')}"
            )
        lifecycle = read_task_state(parent_dir)
        if lifecycle["state"] != TaskState.AWAITING_OWNER_APPROVAL.value:
            raise ManagerError(
                "Plan rejection requires AWAITING_OWNER_APPROVAL, not "
                f"{lifecycle['state']}"
            )
        turns = conversation.get("turns")
        if not isinstance(turns, list):
            raise ManagerError("Conversation turns are invalid")
        pointer = conversation.get("latest_plan_candidate")
        candidate_hash = (
            str(pointer.get("candidate_sha256") or "")
            if isinstance(pointer, dict) else ""
        )
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_hash):
            raise ManagerError("Dispatched plan has no valid candidate hash")
        snapshot = read_profile_snapshot(parent_dir)
        if lifecycle["profile_snapshot_hash"] != snapshot["profile_snapshot_hash"]:
            raise ManagerError("Task state and profile snapshot differ")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rejection = {
            "schema_version": 1,
            "parent_run_id": parent_run_id,
            "rejected_run_id": child_run_id,
            "candidate_sha256": candidate_hash,
            "reason": clean_reason,
            "rejected_at": stamp,
        }
        rejection_path = child_dir / "owner-plan-rejection.json"
        atomic_write_immutable_json(rejection_path, rejection)
        transition_task(
            parent_dir, TaskState.DRAFT, snapshot=snapshot,
            actor_type="owner", actor_id="owner",
            event_type="OWNER_REJECTED_PLAN",
            input_hash=sha256_text(clean_reason),
            output_hash=candidate_hash,
            details={
                "rejected_run_id": child_run_id,
                "candidate_sha256": candidate_hash,
                "rejection_artifact": str(rejection_path.relative_to(child_dir)),
            },
        )
        child_manager.update({
            "state": "OWNER_REJECTED_PLAN",
            "owner_rejection": str(rejection_path),
            "exact_reason": clean_reason,
        })
        atomic_write_json(child_manager_file, child_manager)
        turns.append({
            "role": "owner", "kind": "plan_rejection",
            "content": clean_reason, "at": stamp,
            "rejected_run_id": child_run_id,
        })
        rejected_runs = conversation.get("rejected_run_ids")
        if not isinstance(rejected_runs, list):
            rejected_runs = []
        if child_run_id not in rejected_runs:
            rejected_runs.append(child_run_id)
        conversation["turns"] = turns
        conversation["active_run_id"] = ""
        conversation["latest_plan_candidate"] = None
        conversation["rejected_run_ids"] = rejected_runs
        conversation["updated_at"] = stamp
        atomic_write_json(conversation_file, conversation)
        return conversation


def start_conversation(
    message: str, *, workspace: Path = DEFAULT_WORKSPACE,
    lead_profile: str = "conversation_lead", model_tag: str | None = None,
    runtime_overrides: dict[str, Any] | None = None,
    responder: Callable[..., tuple[dict[str, Any], dict[str, str]]] = run_lead_conversation,
    snapshot_factory: Callable[..., dict[str, Any]] = snapshot_for_new_task,
    worker_snapshot_factory: Callable[..., dict[str, Any]] = snapshot_for_new_task,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    research_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one owner-visible conversation root, then ask only its chosen lead."""
    workspace = workspace.expanduser().resolve()
    clean_message = message.strip()
    if not clean_message:
        raise ManagerError("Conversation message must not be empty")
    canonical_research_input = None
    if research_input is not None:
        canonical_research_input = parse_research_envelope(
            "worker_model=" + str(research_input.get("worker_model") or "")
            + "\n[OWNER_REQUEST]\n"
            + str(research_input.get("owner_request") or "")
            + "\n[/OWNER_REQUEST]\n\n[CODEX_RESEARCH_PROMPT]\n"
            + str(research_input.get("codex_research_prompt") or "")
            + "\n[/CODEX_RESEARCH_PROMPT]\n\n[REQUIRED_QUESTIONS]\n"
            + "\n".join(
                f"{item.get('id')}: {item.get('question')}"
                for item in (research_input.get("required_questions") or [])
            )
            + "\n[/REQUIRED_QUESTIONS]"
        )
        if research_input != canonical_research_input:
            raise ManagerError("Research input is not canonical")
    # Validate the workspace before creating a durable root.
    workspace_config = json.loads(
        (workspace / "workspace.json").read_text(encoding="utf-8")
    )
    repository = Path(workspace_config["repository"]).resolve()
    checkpoint = command(
        ["git", "rev-parse", "HEAD"], cwd=repository, timeout=30,
    ).strip()
    snapshot_kwargs: dict[str, Any] = {"model_tag": model_tag}
    if runtime_overrides:
        snapshot_kwargs["runtime_overrides"] = runtime_overrides
    snapshot = snapshot_factory(lead_profile, **snapshot_kwargs)
    if snapshot.get("profile_id") != lead_profile:
        raise ManagerError("Snapshot factory returned a different lead profile")
    worker_snapshot = None
    if canonical_research_input is not None:
        worker_snapshot = worker_snapshot_factory(
            "research_worker",
            model_tag=canonical_research_input["worker_model"],
            allow_disabled=True,
        )
        if worker_snapshot.get("profile_id") != "research_worker":
            raise ManagerError("Worker snapshot factory returned a different role")
    run_id = new_run_id(workspace)
    job_dir = workspace / "jobs" / run_id
    job_dir.mkdir(parents=True, mode=0o700)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    atomic_write_immutable_json(job_dir / "request.json", {
        "schema_version": 1, "run_id": run_id, "task": clean_message,
        "parent_run_id": None, "created_at": stamp,
        "lead_profile": lead_profile, "kind": "lead_conversation",
        "model": snapshot["model"], "model_tag": snapshot["model_tag"],
        "model_digest": snapshot["model_digest"],
        "profile_snapshot_hash": snapshot["profile_snapshot_hash"],
    })
    if canonical_research_input is not None:
        atomic_write_immutable_json(
            job_dir / "research-input.json", canonical_research_input,
        )
        assert worker_snapshot is not None
        atomic_write_immutable_json(
            job_dir / "worker-profile-snapshot.json", worker_snapshot,
        )
    write_profile_snapshot(job_dir, snapshot)
    write_system_prompt_snapshot(
        job_dir, project_root=PROJECT_ROOT, snapshot=snapshot,
    )
    initialize_task_lifecycle(
        job_dir, task_id=run_id, conversation_id=run_id,
        snapshot=snapshot, input_text=clean_message, checkpoint=checkpoint,
    )
    atomic_write_json(job_dir / "manager.json", {
        "schema_version": 1, "run_id": run_id, "state": "CONVERSATION",
        "lead_profile": lead_profile,
    })
    atomic_write_json(job_dir / "conversation.json", {
        "schema_version": 1, "parent_run_id": run_id,
        "lead_profile": lead_profile, "model_tag": snapshot["model_tag"],
        "turns": [], "active_run_id": "",
        "research_input_sha256": (
            canonical_research_input["research_input_sha256"]
            if canonical_research_input else ""
        ),
        "research_manifests": [],
        "updated_at": stamp,
    })
    try:
        research_message = clean_message
        owner_content = None
        if canonical_research_input is not None:
            research_message = (
                "IZVIRNA ZAHTEVA LASTNIKA:\n"
                + canonical_research_input["owner_request"]
                + "\n\nRAZISKOVALNI PROMPT CODEXA:\n"
                + canonical_research_input["codex_research_prompt"]
            )
            owner_content = canonical_research_input["owner_request"]
        return talk(
            research_message, parent_run_id=run_id, workspace=workspace,
            lead_profile=lead_profile, responder=responder,
            owner_content=owner_content,
            progress_callback=progress_callback,
        )
    except BaseException as exc:
        try:
            if read_task_state(job_dir)["state"] != TaskState.BLOCKED.value:
                transition_task(
                    job_dir, TaskState.BLOCKED, snapshot=snapshot,
                    actor_type="manager", actor_id="manager",
                    event_type="TASK_BLOCKED", details={"reason": str(exc)},
                )
        except BaseException:
            pass
        atomic_write_json(job_dir / "manager.json", {
            "schema_version": 1, "run_id": run_id, "state": "BLOCKED",
            "lead_profile": lead_profile, "exact_reason": str(exc),
        })
        raise


def start_research(
    envelope: str, *, workspace: Path = DEFAULT_WORKSPACE,
    lead_profile: str = "conversation_lead", model_tag: str | None = None,
    runtime_overrides: dict[str, Any] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    snapshot_factory: Callable[..., dict[str, Any]] = snapshot_for_new_task,
    decomposition_runner: Callable[..., dict[str, Any]] | None = None,
    worker_runner: Callable[..., tuple[dict[str, Any], Any]] | None = None,
    review_runner: Callable[..., dict[str, Any]] | None = None,
    synthesis_runner: Callable[..., dict[str, Any]] | None = None,
    model_releaser: Callable[[str], None] = release_ollama_model,
    model_identity_checker: Callable[[dict[str, Any], dict[str, Any]], None] = (
        assert_frozen_research_models
    ),
) -> dict[str, Any]:
    """Run the delegated research entrypoint through an immutable dossier."""
    research_input = parse_research_envelope(envelope)
    workspace = workspace.expanduser().resolve()
    config = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    repository = Path(config["repository"]).resolve()
    lead_kwargs: dict[str, Any] = {"model_tag": model_tag}
    if runtime_overrides:
        lead_kwargs["runtime_overrides"] = runtime_overrides
    lead_snapshot = snapshot_factory(lead_profile, **lead_kwargs)
    worker_snapshot = snapshot_factory(
        "research_worker", model_tag=research_input["worker_model"],
        allow_disabled=True,
    )
    if lead_snapshot.get("profile_id") != "conversation_lead":
        raise ManagerError("Delegated research requires conversation_lead")
    if worker_snapshot.get("profile_id") != "research_worker":
        raise ManagerError("Delegated research requires research_worker")
    run_id = new_run_id(workspace)
    job_dir = workspace / "jobs" / run_id
    job_dir.mkdir(parents=True, mode=0o700)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    project_map = repository / "docs/local_agents/MIKROFIX_PROJECT_MAP.md"
    canon_paths = sorted((repository / "docs/kanon").glob("*.md"))
    try:
        identity = repository_identity(
            repository, project_map=project_map, canon_paths=canon_paths,
        )
        identity.update({
            "lead_profile_snapshot_hash": lead_snapshot["profile_snapshot_hash"],
            "worker_profile_snapshot_hash": worker_snapshot["profile_snapshot_hash"],
        })
        identity["research_identity_sha256"] = canonical_json_sha256(identity)
        scope_catalog = build_scope_catalog(
            repository, identity["repository_checkpoint"],
        )
        atomic_write_immutable_json(job_dir / "request.json", {
            "schema_version": 1, "run_id": run_id,
            "task": research_input["owner_request"], "parent_run_id": None,
            "created_at": stamp, "lead_profile": lead_profile,
            "kind": "delegated_research", "model": lead_snapshot["model"],
            "model_tag": lead_snapshot["model_tag"],
            "model_digest": lead_snapshot["model_digest"],
            "profile_snapshot_hash": lead_snapshot["profile_snapshot_hash"],
        })
        atomic_write_immutable_json(job_dir / "research-input.json", research_input)
        atomic_write_immutable_json(job_dir / "worker-profile-snapshot.json", worker_snapshot)
        atomic_write_immutable_json(job_dir / "research-identity.json", identity)
        atomic_write_immutable_json(
            job_dir / "research-scope-catalog.json", scope_catalog,
        )
        write_profile_snapshot(job_dir, lead_snapshot)
        write_system_prompt_snapshot(
            job_dir, project_root=PROJECT_ROOT, snapshot=lead_snapshot,
        )
        initialize_task_lifecycle(
            job_dir, task_id=run_id, conversation_id=run_id,
            snapshot=lead_snapshot, input_text=research_input["owner_request"],
            checkpoint=identity["repository_checkpoint"],
        )
        atomic_write_json(job_dir / "manager.json", {
            "schema_version": 1, "run_id": run_id, "state": "CONVERSATION",
            "research_state": "LEAD_DECOMPOSITION", "lead_profile": lead_profile,
        })

        def assert_identity() -> None:
            assert_repository_identity(
                identity, repository, project_map=project_map,
                canon_paths=canon_paths,
            )
            model_identity_checker(lead_snapshot, worker_snapshot)

        project_map_text = project_map.read_text(encoding="utf-8")[:20_000]
        canon_digest = "\n".join(
            f"{path.relative_to(repository)} sha256={sha256_text(path.read_text(encoding='utf-8'))}"
            for path in canon_paths
        )
        compact_catalog = compact_scope_catalog(
            scope_catalog,
            context=(
                research_input["owner_request"] + "\n"
                + research_input["codex_research_prompt"] + "\n"
                + json.dumps(
                    research_input["required_questions"], ensure_ascii=False,
                )
            ),
        )
        decomposition_prompt = (
            "Razdeli raziskavo na 1 do največ 3 zaporedne podnaloge. Sam ne "
            "raziskuj kode. Vsaka podnaloga mora pokriti navedena vprašanja, "
            "imeti izrecni file/directory obseg in zahtevane dokaze. Vrni samo "
            "zahtevani JSON.\n\nZAHTEVA:\n" + research_input["owner_request"]
            + "\n\nCODEX PROMPT:\n" + research_input["codex_research_prompt"]
            + "\n\nVPRAŠANJA:\n" + json.dumps(
                research_input["required_questions"], ensure_ascii=False,
            )
            + "\n\nZEMLJEVID:\n" + project_map_text
            + "\n\nPREVERJEN KOMPAKTNI KATALOG POTI:\n"
            + json.dumps(compact_catalog, ensure_ascii=False, sort_keys=True)
            + "\n\nKANON HASHI:\n" + canon_digest
        )

        def run_decomposition(prompt: str, *, correction: bool) -> dict[str, Any]:
            assert_identity()
            if decomposition_runner is None:
                result, _ = run_structured_research_session(
                    prompt, job_dir=job_dir, repository=repository,
                    snapshot=lead_snapshot,
                    session_kind=(
                        "LEAD_DECOMPOSITION_CORRECTION" if correction else
                        "LEAD_DECOMPOSITION"
                    ),
                    response_format=research_decomposition_schema(),
                    allowed_tools=[], max_steps=4,
                    progress_callback=progress_callback,
                )
                return result
            return decomposition_runner(
                prompt, job_dir=job_dir, repository=repository,
                snapshot=lead_snapshot, correction=correction,
            )

        required_question_ids = {
            item["id"] for item in research_input["required_questions"]
        }
        raw_decomposition = run_decomposition(
            decomposition_prompt, correction=False,
        )
        for decomposition_attempt in (1, 2):
            if not isinstance(raw_decomposition, dict) or set(raw_decomposition) != {"tasks"}:
                raise ResearchError("LEAD_DECOMPOSITION returned an invalid schema")
            assert_identity()
            try:
                tasks = validate_task_set(
                    raw_decomposition["tasks"], repository=repository,
                    required_question_ids=required_question_ids,
                    scope_catalog=scope_catalog,
                    checkpoint=identity["repository_checkpoint"],
                )
                break
            except ResearchError:
                corrections = [
                    detail
                    for task in raw_decomposition.get("tasks", [])
                    if isinstance(task, dict)
                    for detail in scope_correction_details(
                        task.get("allowed_scope"), scope_catalog,
                        checkpoint=identity["repository_checkpoint"],
                    )
                ]
                if not corrections or decomposition_attempt == 2:
                    raise
                correction_payload = {
                    "error": "INVALID_DECOMPOSITION_SCOPE",
                    "attempt": decomposition_attempt,
                    "invalid_scopes": corrections,
                    "compact_catalog": compact_catalog,
                }
                correction_prompt = (
                    decomposition_prompt
                    + "\n\nPOPRAVEK MANAGERJA:\n"
                    + json.dumps(
                        correction_payload, ensure_ascii=False, sort_keys=True,
                    )
                    + "\nVrni celoten popravljen tasks JSON. To je edini "
                    "dovoljeni popravljalni poskus."
                )
                raw_decomposition = run_decomposition(
                    correction_prompt, correction=True,
                )
        else:
            raise ResearchError("LEAD_DECOMPOSITION scope validation failed")
        atomic_write_immutable_json(job_dir / "research-tasks.json", {
            "schema_version": 1, "tasks": tasks,
        })
        manager_state = json.loads((job_dir / "manager.json").read_text(encoding="utf-8"))
        manager_state["research_state"] = "WORKERS_RUNNING"
        atomic_write_json(job_dir / "manager.json", manager_state)

        def execute_worker(request: dict[str, Any]):
            assert_identity()
            task = request["research_task"]
            prompt = (
                "Opravi samo spodnjo read-only podnalogo z dovoljenimi orodji. "
                "Vsako dokazno branje označi po vrstnem redu READ-1, READ-2 ...; "
                "v evidence_ids citiraj te oznake. Če potrebuješ pot zunaj "
                "obsega, vrni SCOPE_EXPANSION_REQUIRED. Vrni samo zahtevani JSON.\n\n"
                + json.dumps(request, ensure_ascii=False, sort_keys=True)
            )
            if worker_runner is not None:
                return worker_runner(
                    request, prompt=prompt, job_dir=job_dir,
                    repository=repository, snapshot=worker_snapshot,
                )
            return run_structured_research_session(
                prompt, job_dir=job_dir, repository=repository,
                snapshot=worker_snapshot,
                session_kind=f"RESEARCH_WORKER_{task['research_task_id']}_A{request['attempt']}",
                response_format=worker_result_schema(),
                allowed_tools=["project_status", "project_read", "project_list", "project_search"],
                max_steps=30, allowed_scope=task["allowed_scope"],
                progress_callback=progress_callback,
            )

        def prepare_worker_result(
            request: dict[str, Any], result: dict[str, Any], events: Any,
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            if not isinstance(events, list):
                raise ResearchError("Worker session has no durable tool-result trace")
            cards = evidence_cards_from_trace(
                events, task=request["research_task"], attempt=request["attempt"],
            )
            if not cards:
                raise ResearchError("Worker produced no evidence cards")
            mapping = {
                f"READ-{index}": card["evidence_id"]
                for index, card in enumerate(cards, start=1)
            }
            normalized = json.loads(json.dumps(result))
            for answer in normalized.get("answers", []):
                answer["evidence_ids"] = [
                    mapping.get(item, item) for item in answer.get("evidence_ids", [])
                ]
            for claim in normalized.get("claims", []):
                claim["evidence_ids"] = [
                    mapping.get(item, item) for item in claim.get("evidence_ids", [])
                ]
            expansion = normalized.get("scope_expansion")
            if isinstance(expansion, dict):
                expansion["evidence_ids"] = [
                    mapping.get(item, item) for item in expansion.get("evidence_ids", [])
                ]
            return normalized, cards

        def review_worker(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
            assert_identity()
            attempt_dir = (
                job_dir / "research-workers" / task["research_task_id"]
                / f"attempt-{result['attempt']}"
            )
            cards = json.loads((attempt_dir / "issued-evidence.json").read_text())[
                "evidence_cards"
            ] if (attempt_dir / "issued-evidence.json").is_file() else []
            prompt = (
                "Presodi samo popolnost worker rezultata proti podnalogi. "
                "ACCEPT je dovoljen le, če so odgovorjena vsa vprašanja in vsaka "
                "trditev citira obstoječi evidence_id. Semantične dvome označi "
                "REVISE. Zahtevo SCOPE_EXPANSION_REQUIRED izrecno označi REVISE "
                "ali BLOCKED; manager bo preveril in omejil pot. Vrni samo JSON.\n\n"
                + json.dumps({"task": task, "result": result, "evidence_cards": cards},
                             ensure_ascii=False, sort_keys=True)
            )
            if review_runner is not None:
                return review_runner(
                    task, result, prompt=prompt, job_dir=job_dir,
                    snapshot=lead_snapshot,
                )
            decision, _ = run_structured_research_session(
                prompt, job_dir=job_dir, repository=repository,
                snapshot=lead_snapshot, session_kind="LEAD_WORKER_REVIEW",
                response_format=lead_review_schema(), allowed_tools=[], max_steps=4,
                progress_callback=progress_callback,
            )
            return decision

        def prepare_and_persist(
            request: dict[str, Any], result: dict[str, Any], events: Any,
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            normalized, cards = prepare_worker_result(request, result, events)
            attempt_dir = (
                job_dir / "research-workers" / request["research_task"]["research_task_id"]
                / f"attempt-{request['attempt']}"
            )
            atomic_write_immutable_json(attempt_dir / "issued-evidence.json", {
                "schema_version": 1, "evidence_cards": cards,
            })
            return normalized, cards

        accepted = run_sequential_workers(
            root=job_dir, repository=repository, tasks=tasks,
            worker_snapshot=worker_snapshot, worker_runner=execute_worker,
            review_result=review_worker, release_model=model_releaser,
            prepare_result=prepare_and_persist,
            expand_scope=lambda task, result, issued: expand_research_scope(
                task, result, issued, repository=repository,
            ),
        )
        cards = [card for item in accepted for card in item["evidence_cards"]]
        assert_identity()
        gate = evidence_gate(
            identity=identity, repository=repository, project_map=project_map,
            canon_paths=canon_paths, tasks=tasks, accepted_results=accepted,
            evidence_cards=cards,
            required_question_ids={
                item["id"] for item in research_input["required_questions"]
            },
        )
        atomic_write_immutable_json(job_dir / "evidence-gate.json", gate)
        synthesis_prompt = (
            "Sestavi samo strukturirano sintezo sprejetih rezultatov. Vsaka "
            "trditev mora citirati obstoječi evidence_id. Vrni samo JSON.\n\n"
            + json.dumps({"accepted_results": accepted, "evidence_cards": cards},
                         ensure_ascii=False, sort_keys=True)
        )
        if synthesis_runner is None:
            synthesis, _ = run_structured_research_session(
                synthesis_prompt, job_dir=job_dir, repository=repository,
                snapshot=lead_snapshot, session_kind="LEAD_SYNTHESIS",
                response_format=lead_synthesis_schema(), allowed_tools=[], max_steps=4,
                progress_callback=progress_callback,
            )
        else:
            synthesis = synthesis_runner(
                accepted, cards, prompt=synthesis_prompt, job_dir=job_dir,
                snapshot=lead_snapshot,
            )
        if not isinstance(synthesis, dict) or set(synthesis) != {"synthesis", "unresolved"}:
            raise ResearchError("LEAD_SYNTHESIS returned an invalid schema")
        dossier = create_dossier_revision(
            root=job_dir, identity=identity,
            required_questions=research_input["required_questions"],
            accepted_results=accepted, evidence_cards=cards,
            synthesis=synthesis["synthesis"], unresolved=synthesis["unresolved"],
        )
        manager_state["research_state"] = "AWAITING_CODEX_RESEARCH_REVIEW"
        manager_state["research_dossier_sha256"] = dossier["dossier_sha256"]
        atomic_write_json(job_dir / "manager.json", manager_state)
        reply = json.dumps({
            "status": "DOSSIER_READY",
            "dossier_revision": dossier["dossier_revision"],
            "dossier_sha256": dossier["dossier_sha256"],
            "synthesis": dossier["synthesis"], "unresolved": dossier["unresolved"],
            "next_command": f"/research-approved {dossier['dossier_sha256']}",
        }, ensure_ascii=False, indent=2)
        conversation = {
            "schema_version": 1, "parent_run_id": run_id,
            "lead_profile": lead_profile, "model_tag": lead_snapshot["model_tag"],
            "turns": [
                {"role": "owner", "content": research_input["owner_request"], "at": stamp},
                {"role": "lead", "content": reply, "at": stamp,
                 "kind": "research_dossier"},
            ],
            "active_run_id": "", "latest_plan_candidate": None,
            "research_input_sha256": research_input["research_input_sha256"],
            "research_manifests": [], "research_dossier_sha256": dossier["dossier_sha256"],
            "updated_at": stamp,
        }
        atomic_write_json(job_dir / "conversation.json", conversation)
        return conversation
    except BaseException as exc:
        if (job_dir / "lead-profile-snapshot.json").is_file():
            try:
                if read_task_state(job_dir)["state"] != TaskState.BLOCKED.value:
                    transition_task(
                        job_dir, TaskState.BLOCKED, snapshot=lead_snapshot,
                        actor_type="manager", actor_id="manager",
                        event_type="TASK_BLOCKED", details={"reason": str(exc)},
                    )
            except BaseException:
                pass
        atomic_write_json(job_dir / "manager.json", {
            "schema_version": 1, "run_id": run_id, "state": "BLOCKED",
            "research_state": "BLOCKED", "lead_profile": lead_profile,
            "exact_reason": str(exc),
        })
        raise


def new_run_id(workspace: Path) -> str:
    prefix = time.strftime("MF-LOCAL-%Y%m%d-%H%M%S", time.localtime())
    candidate = prefix
    counter = 1
    while (workspace / "jobs" / candidate).exists():
        counter += 1
        candidate = f"{prefix}-{counter}"
    return candidate


def submit(task: str, *, workspace: Path = DEFAULT_WORKSPACE,
           generator: Callable[..., str] = run_planner,
           initialize: bool = True,
           parent_run_id: str | None = None,
           lead_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    config = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    repository = Path(config["repository"]).resolve()
    if parent_run_id is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_run_id):
            raise ManagerError("Unsafe parent run id")
        if not (workspace / "jobs" / parent_run_id / "request.json").is_file():
            raise ManagerError(f"Parent run does not exist: {parent_run_id}")
    run_id = new_run_id(workspace)
    job_dir = workspace / "jobs" / run_id
    job_dir.mkdir(parents=True, mode=0o700)
    atomic_write_immutable_json(job_dir / "request.json", {
        "schema_version": 1, "run_id": run_id, "task": task,
        "parent_run_id": parent_run_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile_snapshot_hash": (
            lead_snapshot["profile_snapshot_hash"] if lead_snapshot else None
        ),
    })
    if lead_snapshot is not None:
        write_profile_snapshot(job_dir, lead_snapshot)
    state = {"schema_version": 1, "run_id": run_id, "state": "PLANNING"}
    atomic_write_json(job_dir / "manager.json", state)
    try:
        raw = generator(task, job_dir=job_dir, repository=repository)
        plan = normalize_plan(
            raw, run_id=run_id, repository=repository, owner_request=task
        )
        atomic_write_immutable_json(job_dir / "plan.json", plan)
        if initialize:
            command([
                sys.executable, str(PROJECT_ROOT / "tools/mikrofix_supervisor.py"),
                "--state-dir", str(workspace), "--repository-root", str(repository),
                "init", str(job_dir / "plan.json"),
                "--state", str(job_dir / "supervisor.json"),
            ], cwd=PROJECT_ROOT, timeout=120)
        state.update({
            "state": "READY_FOR_OWNER_LEASE" if initialize else "PLAN_READY",
            "plan": str(job_dir / "plan.json"),
            "supervisor_state": str(job_dir / "supervisor.json") if initialize else None,
        })
    except BaseException as exc:
        state.update({"state": "BLOCKED", "exact_reason": str(exc)})
        atomic_write_json(job_dir / "manager.json", state)
        raise
    atomic_write_json(job_dir / "manager.json", state)
    return state


def execute_run(run_id: str, *, workspace: Path = DEFAULT_WORKSPACE,
                lease_minutes: int = 30) -> dict[str, Any]:
    """Execute one planned run locally through the trusted supervisor."""
    workspace = workspace.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ManagerError("Unsafe run id")
    job_dir = workspace / "jobs" / run_id
    manager_file = job_dir / "manager.json"
    state = json.loads(manager_file.read_text(encoding="utf-8"))
    if state.get("state") != "READY_FOR_OWNER_LEASE":
        raise ManagerError(
            f"Run {run_id} is not ready for execution: {state.get('state')}"
        )
    request_file = job_dir / "request.json"
    request = (
        json.loads(request_file.read_text(encoding="utf-8"))
        if request_file.is_file() else {}
    )
    parent_run_id = request.get("parent_run_id")
    lifecycle_dir = (
        workspace / "jobs" / str(parent_run_id)
        if parent_run_id else job_dir
    )
    lifecycle_snapshot = (
        read_profile_snapshot(lifecycle_dir)
        if (lifecycle_dir / "task-state.json").is_file() else None
    )
    config = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    repository = Path(config["repository"]).resolve()
    supervisor_state = Path(state["supervisor_state"]).resolve()
    base = [
        sys.executable, str(PROJECT_ROOT / "tools/mikrofix_supervisor.py"),
        "--state-dir", str(workspace), "--repository-root", str(repository),
    ]
    state["state"] = "WAITING_FOR_OWNER_LEASE"
    state.pop("exact_reason", None)
    atomic_write_json(manager_file, state)
    workers_started = False
    lease_granted = False
    try:
        command(base + ["workers", "start", "--state", str(supervisor_state)],
                cwd=PROJECT_ROOT, timeout=120)
        workers_started = True
        command(base + ["grant-lease", "--minutes", str(lease_minutes),
                        "--state", str(supervisor_state)],
                cwd=PROJECT_ROOT, timeout=360)
        lease_granted = True
        if lifecycle_snapshot is not None:
            transition_task(
                lifecycle_dir, TaskState.APPROVED,
                snapshot=lifecycle_snapshot, actor_type="owner", actor_id="owner",
                event_type="OWNER_APPROVED_PLAN",
                details={"run_id": run_id, "lease_minutes": lease_minutes},
            )
            transition_task(
                lifecycle_dir, TaskState.EXECUTING,
                snapshot=lifecycle_snapshot, actor_type="manager", actor_id="manager",
                details={"run_id": run_id},
            )
        state["state"] = "RUNNING_LOCAL"
        state.pop("exact_reason", None)
        atomic_write_json(manager_file, state)
        command(base + ["resume", "--state", str(supervisor_state)],
                cwd=PROJECT_ROOT, timeout=7200)
        supervisor = json.loads(supervisor_state.read_text(encoding="utf-8"))
        stage = supervisor.get("stage")
        state["state"] = (
            "READY_FOR_CODEX_REVIEW"
            if stage == "AWAIT_CODEX_REVIEW" else "BLOCKED"
        )
        state["local_stage"] = stage
        state["checkpoint"] = supervisor.get("checkpoint")
        state["exact_reason"] = supervisor.get("feedback", "")
        if lifecycle_snapshot is not None:
            if stage == "AWAIT_CODEX_REVIEW":
                transition_task(
                    lifecycle_dir, TaskState.LEAD_REVIEW,
                    snapshot=lifecycle_snapshot, actor_type="supervisor",
                    actor_id="mikrofix_supervisor", details={"run_id": run_id},
                )
                transition_task(
                    lifecycle_dir, TaskState.CANDIDATE_READY,
                    snapshot=lifecycle_snapshot, actor_type="lead",
                    actor_id=str(lifecycle_snapshot["profile_id"]),
                    event_type="LEAD_ACCEPTED_CANDIDATE",
                    checkpoint=supervisor.get("checkpoint"),
                    details={"run_id": run_id},
                )
                transition_task(
                    lifecycle_dir, TaskState.AWAITING_CODEX,
                    snapshot=lifecycle_snapshot, actor_type="manager",
                    actor_id="manager", checkpoint=supervisor.get("checkpoint"),
                    details={"run_id": run_id},
                )
            else:
                transition_task(
                    lifecycle_dir, TaskState.BLOCKED,
                    snapshot=lifecycle_snapshot, actor_type="supervisor",
                    actor_id="mikrofix_supervisor", event_type="TASK_BLOCKED",
                    details={"run_id": run_id, "reason": state["exact_reason"]},
                )
    except Exception as exc:
        # A dismissed or timed-out owner prompt changes no candidate state.
        # Keep the validated plan reusable instead of forcing replanning.
        state["state"] = (
            "READY_FOR_OWNER_LEASE" if not lease_granted
            else "BLOCKED"
        )
        state["exact_reason"] = str(exc)
        if lease_granted and lifecycle_snapshot is not None:
            try:
                if read_task_state(lifecycle_dir)["state"] != TaskState.BLOCKED.value:
                    transition_task(
                        lifecycle_dir, TaskState.BLOCKED,
                        snapshot=lifecycle_snapshot, actor_type="manager",
                        actor_id="manager", event_type="TASK_BLOCKED",
                        details={"run_id": run_id, "reason": str(exc)},
                    )
            except BaseException:
                pass
        raise
    finally:
        if workers_started:
            try:
                command(base + ["workers", "stop", "--state", str(supervisor_state)],
                        cwd=PROJECT_ROOT, timeout=120)
            except Exception as stop_error:
                state["stop_warning"] = str(stop_error)
        atomic_write_json(manager_file, state)
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MikroFix local work manager")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    sub = parser.add_subparsers(dest="command_name", required=True)
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--task", required=True)
    submit_parser.add_argument("--parent")
    start_parser = sub.add_parser("start")
    start_parser.add_argument("--message", required=True)
    start_parser.add_argument(
        "--profile", choices=tuple(enabled_role_profiles()),
        default="conversation_lead",
    )
    start_parser.add_argument("--model")
    research_parser = sub.add_parser("research")
    research_parser.add_argument("--envelope", required=True)
    research_parser.add_argument(
        "--profile", choices=tuple(enabled_role_profiles()),
        default="conversation_lead",
    )
    research_parser.add_argument("--model", required=True)
    revise_research_parser = sub.add_parser("research-revise")
    revise_research_parser.add_argument("run_id")
    revise_research_parser.add_argument("--dossier", required=True)
    revise_research_parser.add_argument("--task", choices=("R1", "R2", "R3"), required=True)
    revise_research_parser.add_argument("--reason", required=True)
    talk_parser = sub.add_parser("talk")
    talk_parser.add_argument("--message", required=True)
    talk_parser.add_argument("--parent", required=True)
    talk_parser.add_argument(
        "--profile", choices=tuple(enabled_role_profiles()),
        default="conversation_lead",
    )
    plan_parser = sub.add_parser("prepare-plan")
    plan_parser.add_argument("run_id")
    dispatch_parser = sub.add_parser("plan-dispatch")
    dispatch_parser.add_argument("run_id")
    reject_plan_parser = sub.add_parser("reject-plan")
    reject_plan_parser.add_argument("run_id")
    reject_plan_parser.add_argument("--reason", default="")
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("run_id")
    execute_parser.add_argument("--lease-minutes", type=int, default=30)
    prepare_parser = sub.add_parser("prepare-review")
    prepare_parser.add_argument("run_id")
    rejected_parser = sub.add_parser("codex-rejected")
    rejected_parser.add_argument("run_id")
    rejected_parser.add_argument("--feedback", required=True)
    approved_parser = sub.add_parser("codex-approved")
    approved_parser.add_argument("run_id")
    approved_parser.add_argument("--commit", required=True)
    return parser.parse_args()


def _main(args: argparse.Namespace) -> int:
    try:
        if args.command_name == "submit":
            result = submit(
                args.task, workspace=args.workspace,
                parent_run_id=args.parent,
            )
        elif args.command_name == "start":
            result = start_conversation(
                args.message, workspace=args.workspace, lead_profile=args.profile,
                model_tag=args.model,
            )
        elif args.command_name == "talk":
            result = talk(
                args.message, parent_run_id=args.parent,
                workspace=args.workspace, lead_profile=args.profile,
            )
        elif args.command_name == "research":
            result = start_research(
                args.envelope, workspace=args.workspace,
                lead_profile=args.profile, model_tag=args.model,
            )
        elif args.command_name == "research-revise":
            result = record_research_review(
                args.run_id, workspace=args.workspace,
                dossier_sha256=args.dossier, verdict="REVISE",
                research_task_id=args.task, reason=args.reason,
            )
        elif args.command_name == "prepare-plan":
            result = prepare_plan(args.run_id, workspace=args.workspace)
        elif args.command_name == "plan-dispatch":
            result = dispatch_plan(args.run_id, workspace=args.workspace)
        elif args.command_name == "reject-plan":
            result = reject_plan(
                args.run_id, args.reason, workspace=args.workspace,
            )
        elif args.command_name == "execute":
            result = execute_run(
                args.run_id, workspace=args.workspace,
                lease_minutes=args.lease_minutes,
            )
        elif args.command_name == "prepare-review":
            result = prepare_review(args.workspace, args.run_id)
        elif args.command_name == "codex-rejected":
            result = record_codex_rejected(
                args.workspace, args.run_id, args.feedback,
            )
        else:
            result = record_codex_approved(
                args.workspace, args.run_id, args.commit,
            )
    except Exception as exc:
        print(json.dumps({"state": "BLOCKED", "exact_reason": str(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    args = parse_args()
    with workspace_lock(args.workspace):
        return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
