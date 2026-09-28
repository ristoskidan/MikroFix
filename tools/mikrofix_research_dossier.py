#!/usr/bin/env python3
"""Deterministic evidence cards, structural gate and versioned dossiers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

try:
    from tools.mikrofix_research import (
        MAX_EXCERPT_CHARS, ResearchError, assert_repository_identity,
        canonical_json, sha256_value,
    )
except ModuleNotFoundError:
    from mikrofix_research import (  # type: ignore[no-redef]
        MAX_EXCERPT_CHARS, ResearchError, assert_repository_identity,
        canonical_json, sha256_value,
    )


def _immutable_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
    ).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o400)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
    ).encode("utf-8") + b"\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def evidence_cards_from_trace(
    events: list[dict[str, Any]], *, task: dict[str, Any], attempt: int,
) -> list[dict[str, Any]]:
    """Issue cards only for successful structured project_read results."""
    pending: dict[int, dict[str, Any]] = {}
    cards: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            continue
        step = event.get("step")
        if event.get("type") == "tool_call" and isinstance(step, int):
            pending[step] = event
            continue
        if (
            event.get("type") != "tool_result" or not isinstance(step, int)
            or not event.get("ok") or event.get("tool") != "project_read"
        ):
            continue
        call = pending.get(step)
        result = event.get("result")
        if not call or not isinstance(result, dict):
            continue
        content = result.get("raw_content")
        path = result.get("path")
        if not isinstance(content, str) or not content or not isinstance(path, str) or not path:
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        event_id = f"tool-result-{index:06d}"
        number = len(cards) + 1
        content_bytes = content.encode("utf-8")
        cards.append({
            "evidence_id": f"E-{task['research_task_id']}-A{attempt}-{number:04d}",
            "research_task_id": task["research_task_id"],
            "original_task_sha256": task["original_task_sha256"],
            "attempt": attempt,
            "tool_result_event_id": event_id,
            "raw_tool_result_sha256": hashlib.sha256(canonical_json(event)).hexdigest(),
            "tool": "project_read",
            "normalized_arguments": {
                key: arguments[key] for key in sorted(arguments)
                if key in {"path", "start_line", "end_line"}
            },
            "canonical_path": path,
            "actual_start_line": result.get("start_line"),
            "actual_end_line": result.get("end_line"),
            "content_bytes_sha256": hashlib.sha256(content_bytes).hexdigest(),
            "excerpt": content[:MAX_EXCERPT_CHARS],
            "symbol": result.get("symbol") if isinstance(result.get("symbol"), str) else None,
        })
    return cards


def check_scope_expansion(
    request: dict[str, Any], *, evidence_by_id: dict[str, dict[str, Any]],
) -> None:
    """Check only cited identity/path linkage, never semantic justification."""
    cited = request.get("evidence_ids")
    path = str(request.get("path") or "")
    symbol = request.get("symbol")
    if not isinstance(cited, list) or not cited or not path:
        raise ResearchError("Scope expansion request is incomplete")
    cards = []
    for evidence_id in cited:
        card = evidence_by_id.get(str(evidence_id))
        if card is None:
            raise ResearchError("Scope expansion cites unknown evidence")
        cards.append(card)
    needles = [path]
    if isinstance(symbol, str) and symbol:
        needles.append(symbol)
    haystack = "\n".join(
        str(card.get("excerpt") or "") + "\n" + str(card.get("canonical_path") or "")
        for card in cards
    )
    if not any(needle in haystack for needle in needles):
        raise ResearchError("Scope expansion evidence does not contain its path or symbol")


def evidence_gate(
    *, identity: dict[str, Any], repository: Path, project_map: Path,
    canon_paths: list[Path], tasks: list[dict[str, Any]],
    accepted_results: list[dict[str, Any]], evidence_cards: list[dict[str, Any]],
    required_question_ids: set[str],
) -> dict[str, Any]:
    assert_repository_identity(
        identity, repository, project_map=project_map, canon_paths=canon_paths,
    )
    if len(tasks) != len(accepted_results):
        raise ResearchError("Every research task requires one accepted result")
    evidence_by_id = {card["evidence_id"]: card for card in evidence_cards}
    if len(evidence_by_id) != len(evidence_cards):
        raise ResearchError("Evidence ids must be unique")
    answered: set[str] = set()
    for accepted in accepted_results:
        result = accepted["result"]
        if result.get("status") != "COMPLETE":
            raise ResearchError("Only COMPLETE worker results can enter a dossier")
        for answer in result["answers"]:
            answered.add(answer["question_id"])
            for evidence_id in answer["evidence_ids"]:
                if evidence_id not in evidence_by_id:
                    raise ResearchError("Worker answer cites unknown evidence")
        for claim in result["claims"]:
            if any(item not in evidence_by_id for item in claim["evidence_ids"]):
                raise ResearchError("Worker claim cites unknown evidence")
    if answered != required_question_ids:
        raise ResearchError("Evidence gate requires an answer for every question")
    return {
        "status": "PASSED",
        "question_ids": sorted(answered),
        "evidence_ids": sorted(evidence_by_id),
        "gate_sha256": sha256_value({
            "identity": identity,
            "tasks": tasks,
            "results": accepted_results,
            "evidence_cards": evidence_cards,
        }),
    }


def create_dossier_revision(
    *, root: Path, identity: dict[str, Any], required_questions: list[dict[str, str]],
    accepted_results: list[dict[str, Any]], evidence_cards: list[dict[str, Any]],
    synthesis: list[dict[str, Any]], unresolved: list[str],
) -> dict[str, Any]:
    dossier_root = root / "research-dossiers"
    pointer = dossier_root / "current.json"
    previous_hash = None
    revision = 1
    if pointer.is_file():
        current = json.loads(pointer.read_text(encoding="utf-8"))
        revision = int(current["dossier_revision"]) + 1
        previous_hash = str(current["dossier_sha256"])
    evidence_ids = {item["evidence_id"] for item in evidence_cards}
    normalized_synthesis = []
    for claim in synthesis:
        if not isinstance(claim, dict) or set(claim) != {"claim", "evidence_ids"}:
            raise ResearchError("Synthesis claim has unknown or missing fields")
        if not claim["evidence_ids"] or any(item not in evidence_ids for item in claim["evidence_ids"]):
            raise ResearchError("Every synthesis claim must cite existing evidence")
        normalized_synthesis.append(claim)
    core = {
        "schema_version": 1,
        "dossier_revision": revision,
        "previous_dossier_sha256": previous_hash,
        "research_identity": identity,
        "required_questions": required_questions,
        "accepted_tasks": [item["task"] for item in accepted_results],
        "evidence_cards": evidence_cards,
        "lead_decisions": [item["lead_decision"] for item in accepted_results],
        "worker_results": [item["result"] for item in accepted_results],
        "synthesis": normalized_synthesis,
        "unresolved": unresolved,
    }
    dossier = {**core, "dossier_sha256": sha256_value(core)}
    relative = f"revision-{revision:04d}-{dossier['dossier_sha256'][:12]}.json"
    _immutable_json(dossier_root / relative, dossier)
    _atomic_json(pointer, {
        "schema_version": 1, "dossier_revision": revision,
        "dossier_sha256": dossier["dossier_sha256"], "path": relative,
    })
    return dossier


def record_codex_research_verdict(
    *, root: Path, dossier_sha256: str, verdict: str,
    research_task_id: str | None = None, reason: str = "",
) -> dict[str, Any]:
    """Bind an immutable Codex verdict to the current exact dossier revision."""
    if verdict not in {"APPROVE", "REVISE", "REJECT"}:
        raise ResearchError("Unknown Codex research verdict")
    pointer_path = root / "research-dossiers/current.json"
    if not pointer_path.is_file():
        raise ResearchError("No current research dossier exists")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if pointer.get("dossier_sha256") != dossier_sha256:
        raise ResearchError("Codex verdict does not match the current dossier")
    if verdict == "REVISE" and (
        research_task_id not in {"R1", "R2", "R3"} or not reason.strip()
    ):
        raise ResearchError("REVISE requires a task id and reason")
    if verdict == "REJECT" and not reason.strip():
        raise ResearchError("REJECT requires a reason")
    if verdict == "APPROVE" and (research_task_id is not None or reason.strip()):
        raise ResearchError("APPROVE accepts only the dossier hash")
    core = {
        "schema_version": 1, "dossier_sha256": dossier_sha256,
        "verdict": verdict, "research_task_id": research_task_id,
        "reason": reason.strip(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    artifact = {**core, "verdict_sha256": sha256_value(core)}
    verdict_path = (
        root / "research-dossiers/verdicts"
        / f"{dossier_sha256[:12]}-{artifact['verdict_sha256'][:12]}.json"
    )
    _immutable_json(verdict_path, artifact)
    _atomic_json(root / "research-dossiers/current-verdict.json", {
        "schema_version": 1,
        "dossier_sha256": dossier_sha256,
        "verdict": verdict,
        "verdict_sha256": artifact["verdict_sha256"],
        "path": str(verdict_path.relative_to(root / "research-dossiers")),
    })
    return artifact


def require_current_codex_approval(root: Path) -> dict[str, Any]:
    dossier_pointer = root / "research-dossiers/current.json"
    verdict_pointer = root / "research-dossiers/current-verdict.json"
    if not dossier_pointer.is_file() or not verdict_pointer.is_file():
        raise ResearchError("/prepare-plan requires a Codex-approved research dossier")
    pointer = json.loads(dossier_pointer.read_text(encoding="utf-8"))
    dossier_path = (root / "research-dossiers" / str(pointer.get("path") or "")).resolve()
    try:
        dossier_path.relative_to((root / "research-dossiers").resolve())
    except ValueError as exc:
        raise ResearchError("Research dossier pointer escapes its root") from exc
    dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
    core = dict(dossier)
    stored_hash = core.pop("dossier_sha256", None)
    if stored_hash != sha256_value(core) or stored_hash != pointer.get("dossier_sha256"):
        raise ResearchError("Current research dossier hash is invalid")
    verdict = json.loads(verdict_pointer.read_text(encoding="utf-8"))
    if (
        verdict.get("verdict") != "APPROVE"
        or verdict.get("dossier_sha256") != dossier.get("dossier_sha256")
    ):
        raise ResearchError("The current research dossier is not Codex-approved")
    return dossier
