#!/usr/bin/env python3
"""Sequential, bounded orchestration for delegated read-only research."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable
import uuid

try:
    from tools.mikrofix_research import (
        MAX_CLAIMS, MAX_EVIDENCE_IDS_PER_CLAIM, MAX_REASON_CHARS,
        MAX_WORKER_NUM_PREDICT, MAX_WORKER_SILENCE_SECONDS, MAX_WORKER_STEPS,
        ResearchError, normalize_scope, sha256_value, validate_research_task,
    )
except ModuleNotFoundError:
    from mikrofix_research import (  # type: ignore[no-redef]
        MAX_CLAIMS, MAX_EVIDENCE_IDS_PER_CLAIM, MAX_REASON_CHARS,
        MAX_WORKER_NUM_PREDICT, MAX_WORKER_SILENCE_SECONDS, MAX_WORKER_STEPS,
        ResearchError, normalize_scope, sha256_value, validate_research_task,
    )


MAX_ATTEMPTS = 3


def _atomic_json(path: Path, value: Any, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
    ).encode("utf-8") + b"\n"
    if immutable:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o400)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _bounded_string(value: Any, field: str, *, maximum: int) -> str:
    text = str(value).strip()
    if not 1 <= len(text) <= maximum:
        raise ResearchError(f"{field} is outside the allowed bound")
    return text


def validate_worker_result(
    value: Any, *, task: dict[str, Any], attempt: int,
) -> dict[str, Any]:
    required = {
        "status", "research_task_id", "attempt", "answers", "claims",
        "unconfirmed", "scope_expansion",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ResearchError("worker_result has unknown or missing fields")
    if value["status"] not in {"COMPLETE", "BLOCKED", "SCOPE_EXPANSION_REQUIRED"}:
        raise ResearchError("worker_result status is invalid")
    if value["research_task_id"] != task["research_task_id"] or value["attempt"] != attempt:
        raise ResearchError("worker_result identity does not match its attempt")
    expected_questions = set(task["question_ids"])
    answers = value["answers"]
    if not isinstance(answers, list) or len(answers) > len(expected_questions):
        raise ResearchError("worker_result answers are invalid")
    normalized_answers: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for answer in answers:
        if not isinstance(answer, dict) or set(answer) != {
            "question_id", "answer", "evidence_ids",
        }:
            raise ResearchError("worker answer has unknown or missing fields")
        question_id = str(answer["question_id"])
        if question_id not in expected_questions or question_id in seen_questions:
            raise ResearchError("worker answer question identity is invalid")
        seen_questions.add(question_id)
        evidence_ids = answer["evidence_ids"]
        if not isinstance(evidence_ids, list) or len(evidence_ids) > MAX_EVIDENCE_IDS_PER_CLAIM:
            raise ResearchError("worker answer evidence list is invalid")
        normalized_answers.append({
            "question_id": question_id,
            "answer": _bounded_string(answer["answer"], "answer", maximum=6_000),
            "evidence_ids": [str(item) for item in evidence_ids],
        })
    claims = value["claims"]
    if not isinstance(claims, list) or len(claims) > MAX_CLAIMS:
        raise ResearchError("worker_result claims are invalid")
    normalized_claims: list[dict[str, Any]] = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"claim", "evidence_ids"}:
            raise ResearchError("worker claim has unknown or missing fields")
        evidence_ids = claim["evidence_ids"]
        if not isinstance(evidence_ids, list) or not 1 <= len(evidence_ids) <= MAX_EVIDENCE_IDS_PER_CLAIM:
            raise ResearchError("worker claim must cite bounded evidence")
        normalized_claims.append({
            "claim": _bounded_string(claim["claim"], "claim", maximum=4_000),
            "evidence_ids": [str(item) for item in evidence_ids],
        })
    unconfirmed = value["unconfirmed"]
    if not isinstance(unconfirmed, list) or len(unconfirmed) > MAX_CLAIMS:
        raise ResearchError("worker_result unconfirmed list is invalid")
    expansion = value["scope_expansion"]
    if value["status"] == "SCOPE_EXPANSION_REQUIRED":
        if not isinstance(expansion, dict) or set(expansion) != {
            "path", "symbol", "reason", "evidence_ids",
        }:
            raise ResearchError("scope expansion request is incomplete")
        if not isinstance(expansion["evidence_ids"], list) or not expansion["evidence_ids"]:
            raise ResearchError("scope expansion must cite evidence")
        expansion = {
            "path": _bounded_string(expansion["path"], "scope path", maximum=500),
            "symbol": None if expansion["symbol"] is None else _bounded_string(
                expansion["symbol"], "scope symbol", maximum=500,
            ),
            "reason": _bounded_string(expansion["reason"], "scope reason", maximum=MAX_REASON_CHARS),
            "evidence_ids": [str(item) for item in expansion["evidence_ids"]],
        }
    elif expansion is not None:
        raise ResearchError("scope_expansion is only valid for its matching status")
    if value["status"] == "COMPLETE" and seen_questions != expected_questions:
        raise ResearchError("COMPLETE worker result must answer every assigned question")
    return {
        **value,
        "answers": normalized_answers,
        "claims": normalized_claims,
        "unconfirmed": [
            _bounded_string(item, "unconfirmed", maximum=2_000)
            for item in unconfirmed
        ],
        "scope_expansion": expansion,
    }


def validate_task_set(
    raw_tasks: Any, *, repository: Path, required_question_ids: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 3:
        raise ResearchError("The lead must create 1..3 research tasks")
    tasks = [validate_research_task(item, repository) for item in raw_tasks]
    if len({item["research_task_id"] for item in tasks}) != len(tasks):
        raise ResearchError("Research task ids must be unique")
    covered = {question for task in tasks for question in task["question_ids"]}
    if covered != required_question_ids:
        raise ResearchError("Research tasks must cover every required question exactly in scope")
    for index, task in enumerate(tasks):
        for other in tasks[index + 1:]:
            overlap = set(task["question_ids"]) & set(other["question_ids"])
            if overlap and not (task["reason_for_overlap"] and other["reason_for_overlap"]):
                raise ResearchError("Overlapping tasks require an explicit reason on both tasks")
    return tasks


def run_sequential_workers(
    *, root: Path, repository: Path, tasks: list[dict[str, Any]],
    worker_snapshot: dict[str, Any],
    worker_runner: Callable[[dict[str, Any]], dict[str, Any]],
    review_result: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    release_model: Callable[[str], None],
    prepare_result: Callable[
        [dict[str, Any], dict[str, Any], Any], tuple[dict[str, Any], list[dict[str, Any]]]
    ] | None = None,
    expand_scope: Callable[
        [dict[str, Any], dict[str, Any], list[dict[str, Any]]], dict[str, Any]
    ] | None = None,
    attempt_offsets: dict[str, int] | None = None,
    initial_corrections: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run one worker at a time, in fresh sessions, with at most three attempts."""
    results: list[dict[str, Any]] = []
    worker_root = root / "research-workers"
    for original_task in tasks:
        task = original_task
        correction = (initial_corrections or {}).get(task["research_task_id"])
        accepted: dict[str, Any] | None = None
        first_attempt = (attempt_offsets or {}).get(task["research_task_id"], 0) + 1
        for attempt in range(first_attempt, MAX_ATTEMPTS + 1):
            attempt_id = f"{task['research_task_id']}-A{attempt}-{uuid.uuid4().hex[:12]}"
            attempt_dir = worker_root / task["research_task_id"] / f"attempt-{attempt}"
            request = {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "research_task": task,
                "attempt": attempt,
                "correction": correction,
                "worker_profile_snapshot_hash": worker_snapshot["profile_snapshot_hash"],
                "limits": {
                    "max_steps": MAX_WORKER_STEPS,
                    "max_num_predict": MAX_WORKER_NUM_PREDICT,
                    "silence_timeout_seconds": MAX_WORKER_SILENCE_SECONDS,
                },
            }
            _atomic_json(attempt_dir / "request.json", request, immutable=True)
            started = time.monotonic()
            try:
                runner_output = worker_runner(request)
                evidence_cards: list[dict[str, Any]] = []
                evidence_payload: Any = None
                if (
                    isinstance(runner_output, tuple)
                    and len(runner_output) == 2
                    and isinstance(runner_output[0], dict)
                ):
                    raw_result, evidence_payload = runner_output
                else:
                    raw_result = runner_output
                if not isinstance(raw_result, dict):
                    raise ResearchError("Worker runner returned an invalid result")
                if prepare_result is not None:
                    raw_result, evidence_cards = prepare_result(
                        request, raw_result, evidence_payload,
                    )
                result = validate_worker_result(raw_result, task=task, attempt=attempt)
                _atomic_json(attempt_dir / "result.json", result, immutable=True)
                if result["status"] == "SCOPE_EXPANSION_REQUIRED":
                    if expand_scope is None:
                        raise ResearchError("No scope-expansion handler is configured")
                    decision = review_result(task, result)
                    if (
                        not isinstance(decision, dict)
                        or set(decision) != {"decision", "reason", "missing_evidence"}
                        or decision.get("decision") not in {"REVISE", "BLOCKED"}
                        or not isinstance(decision.get("reason"), str)
                        or not isinstance(decision.get("missing_evidence"), list)
                    ):
                        raise ResearchError(
                            "Lead must explicitly REVISE or BLOCK a scope expansion"
                        )
                    _atomic_json(
                        attempt_dir / "lead-decision.json", decision, immutable=True,
                    )
                    if decision["decision"] == "BLOCKED":
                        break
                    task = expand_scope(task, result, evidence_cards)
                    _atomic_json(
                        attempt_dir / "scope-revision.json", task, immutable=True,
                    )
                    correction = {
                        "previous_worker_result": result,
                        "missing_evidence": decision["missing_evidence"],
                        "reason": decision["reason"],
                    }
                    continue
                decision = review_result(task, result)
                if (
                    not isinstance(decision, dict)
                    or set(decision) != {"decision", "reason", "missing_evidence"}
                    or decision.get("decision") not in {"ACCEPT", "REVISE", "BLOCKED"}
                    or not isinstance(decision.get("reason"), str)
                    or not isinstance(decision.get("missing_evidence"), list)
                ):
                    raise ResearchError("Lead review returned an invalid decision")
                _atomic_json(attempt_dir / "lead-decision.json", decision, immutable=True)
                if decision["decision"] == "ACCEPT":
                    accepted = {
                        "task": task, "attempt": attempt, "attempt_id": attempt_id,
                        "result": result, "lead_decision": decision,
                        "evidence_cards": evidence_cards,
                    }
                    break
                if decision["decision"] == "BLOCKED":
                    break
                correction = {
                    "previous_worker_result": result,
                    "missing_evidence": decision.get("missing_evidence") or [],
                    "reason": _bounded_string(
                        decision.get("reason"), "revision reason", maximum=MAX_REASON_CHARS,
                    ),
                }
            finally:
                release_model(str(worker_snapshot["model_tag"]))
                _atomic_json(attempt_dir / "session.json", {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "fresh_session": True,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "model_released": True,
                })
        if accepted is None:
            raise ResearchError(
                f"Research task {task['research_task_id']} is BLOCKED after at most three attempts"
            )
        results.append(accepted)
    return results
