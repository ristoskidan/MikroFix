#!/usr/bin/env python3
"""Deterministic contracts and identities for delegated read-only research."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, Iterable


class ResearchError(RuntimeError):
    """A delegated-research contract or identity is invalid."""


SHA256_RE = re.compile(r"[0-9a-f]{64}")
QUESTION_ID_RE = re.compile(r"Q[1-9][0-9]{0,2}")
TASK_ID_RE = re.compile(r"R[1-3]")
MAX_OWNER_REQUEST_CHARS = 20_000
MAX_RESEARCH_PROMPT_CHARS = 30_000
MAX_QUESTIONS = 20
MAX_QUESTION_CHARS = 1_000
MAX_TASKS = 3
MAX_SCOPE_ENTRIES = 32
MAX_CLAIMS = 64
MAX_EVIDENCE_IDS_PER_CLAIM = 12
MAX_EXCERPT_CHARS = 2_000
MAX_REASON_CHARS = 4_000
MAX_WORKER_STEPS = 30
MAX_WORKER_NUM_PREDICT = 12_288
MAX_WORKER_SILENCE_SECONDS = 900
ALLOWED_TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".md", ".txt", ".json", ".toml", ".yaml", ".yml",
    ".sql", ".html", ".css", ".js", ".ts", ".tsx", ".jsx", ".ini",
    ".cfg", ".sh", ".service", ".xml", ".csv",
})
ALLOWED_EXTENSIONLESS = frozenset({"Makefile", "Dockerfile", "LICENSE"})
DENIED_NAMES = frozenset({".env", "id_rsa", "id_ed25519", "credentials", "token"})


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _run_git(repository: Path, arguments: list[str], *, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60, check=False,
    )
    if result.returncode:
        raise ResearchError(
            "Git identity command failed: "
            + result.stderr.decode("utf-8", errors="replace")[-1000:]
        )
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def repository_identity(
    repository: Path, *, project_map: Path, canon_paths: Iterable[Path],
) -> dict[str, Any]:
    """Bind research to one clean commit and Git-tracked content inventory."""
    repository = repository.resolve()
    status = str(_run_git(
        repository, ["status", "--porcelain=v1", "--untracked-files=all"],
    ))
    if status:
        raise ResearchError("Research requires a clean protected repository")
    checkpoint = str(_run_git(repository, ["rev-parse", "HEAD"])).lower()
    tree_oid = str(_run_git(repository, ["rev-parse", "HEAD^{tree}"])).lower()
    raw_tree = _run_git(repository, ["ls-tree", "-r", "-z", "HEAD"], binary=True)
    assert isinstance(raw_tree, bytes)
    entries: list[dict[str, Any]] = []
    for record in raw_tree.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, object_oid = metadata.decode("ascii").split()
        content = _run_git(
            repository, ["cat-file", object_type, object_oid], binary=True,
        )
        assert isinstance(content, bytes)
        entries.append({
            "path": raw_path.decode("utf-8", errors="strict"),
            "type": object_type,
            "mode": mode,
            "content_sha256": hashlib.sha256(content).hexdigest(),
        })
    entries.sort(key=lambda item: item["path"])
    map_bytes = project_map.read_bytes()
    canon = []
    for path in sorted((item.resolve() for item in canon_paths), key=str):
        canon.append({
            "path": str(path.relative_to(repository)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return {
        "schema_version": 1,
        "repository_checkpoint": checkpoint,
        "repository_tree_oid": tree_oid,
        "repository_inventory_sha256": sha256_value(entries),
        "tracked_entry_count": len(entries),
        "project_map_sha256": hashlib.sha256(map_bytes).hexdigest(),
        "canon_bundle_sha256": sha256_value(canon),
        "canon_files": canon,
    }


def assert_repository_identity(
    expected: dict[str, Any], repository: Path, *, project_map: Path,
    canon_paths: Iterable[Path],
) -> None:
    current = repository_identity(
        repository, project_map=project_map, canon_paths=canon_paths,
    )
    fields = (
        "repository_checkpoint", "repository_tree_oid",
        "repository_inventory_sha256", "project_map_sha256",
        "canon_bundle_sha256",
    )
    if any(current.get(field) != expected.get(field) for field in fields):
        raise ResearchError("RESEARCH_BASE_CHANGED")


def _canonical_relative_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ResearchError("Scope path must be a non-empty relative POSIX path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ResearchError(f"Unsafe scope path: {raw}")
    normalized = path.as_posix()
    if normalized == "." or normalized.startswith(".git/") or normalized == ".git":
        raise ResearchError(f"Forbidden scope path: {raw}")
    lowered = {part.lower() for part in path.parts}
    if lowered & DENIED_NAMES or any(
        part.endswith((".pem", ".key", ".p12", ".pfx")) for part in lowered
    ):
        raise ResearchError(f"Secret-bearing scope path is forbidden: {raw}")
    return normalized


def _assert_no_symlink(repository: Path, relative: str) -> Path:
    current = repository.resolve()
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ResearchError(f"Symlink scope is forbidden: {relative}")
    try:
        current.resolve().relative_to(repository.resolve())
    except ValueError as exc:
        raise ResearchError(f"Scope escapes repository: {relative}") from exc
    return current


def normalize_scope(entries: Any, repository: Path) -> list[dict[str, str]]:
    """Validate explicit file/directory scope without following symlinks."""
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SCOPE_ENTRIES:
        raise ResearchError("allowed_scope must contain 1..32 entries")
    normalized: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"kind", "path"}:
            raise ResearchError("Each scope entry requires exactly kind and path")
        kind = entry["kind"]
        if kind not in {"file", "directory"}:
            raise ResearchError("Scope kind must be file or directory")
        relative = _canonical_relative_path(entry["path"])
        target = _assert_no_symlink(repository, relative)
        if kind == "file":
            if not target.is_file():
                raise ResearchError(f"Scoped file does not exist: {relative}")
            if target.suffix.lower() not in ALLOWED_TEXT_SUFFIXES and target.name not in ALLOWED_EXTENSIONLESS:
                raise ResearchError(f"Scoped file type is not readable text: {relative}")
        elif not target.is_dir():
            raise ResearchError(f"Scoped directory does not exist: {relative}")
        normalized.append({"kind": kind, "path": relative})
    normalized.sort(key=lambda item: (item["path"], item["kind"]))
    if len({(item["kind"], item["path"]) for item in normalized}) != len(normalized):
        raise ResearchError("Duplicate scope entries are forbidden")
    for item in normalized:
        for other in normalized:
            if item is other or other["kind"] != "directory":
                continue
            prefix = other["path"].rstrip("/") + "/"
            if item["path"].startswith(prefix):
                raise ResearchError("Redundant scope entries are forbidden")
    return normalized


def validate_questions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_QUESTIONS:
        raise ResearchError("required_questions must contain 1..20 questions")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "question"}:
            raise ResearchError("Each question requires exactly id and question")
        question_id = str(item["id"])
        question = str(item["question"]).strip()
        if not QUESTION_ID_RE.fullmatch(question_id):
            raise ResearchError(f"Invalid question id: {question_id}")
        if not 1 <= len(question) <= MAX_QUESTION_CHARS:
            raise ResearchError("Question length is outside the allowed bound")
        result.append({"id": question_id, "question": question})
    if len({item["id"] for item in result}) != len(result):
        raise ResearchError("Question ids must be unique")
    return result


def validate_research_task(value: Any, repository: Path) -> dict[str, Any]:
    required = {
        "research_task_id", "objective", "question_ids", "allowed_scope",
        "required_evidence", "reason_for_overlap",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ResearchError("research_task has unknown or missing fields")
    task_id = str(value["research_task_id"])
    if not TASK_ID_RE.fullmatch(task_id):
        raise ResearchError("research_task_id must be R1, R2 or R3")
    objective = str(value["objective"]).strip()
    if not 1 <= len(objective) <= 2_000:
        raise ResearchError("Research task objective is outside the allowed bound")
    question_ids = value["question_ids"]
    if not isinstance(question_ids, list) or not 1 <= len(question_ids) <= MAX_QUESTIONS:
        raise ResearchError("research_task question_ids are invalid")
    if any(not QUESTION_ID_RE.fullmatch(str(item)) for item in question_ids):
        raise ResearchError("research_task has an invalid question id")
    evidence = value["required_evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12:
        raise ResearchError("required_evidence must contain 1..12 items")
    reason = value["reason_for_overlap"]
    if reason is not None and not 1 <= len(str(reason).strip()) <= MAX_REASON_CHARS:
        raise ResearchError("Overlap reason is outside the allowed bound")
    core = {
        "schema_version": 1,
        "research_task_id": task_id,
        "objective": objective,
        "question_ids": [str(item) for item in question_ids],
        "allowed_scope": normalize_scope(value["allowed_scope"], repository),
        "required_evidence": [str(item).strip() for item in evidence],
        "reason_for_overlap": None if reason is None else str(reason).strip(),
    }
    return {**core, "original_task_sha256": sha256_value(core)}
