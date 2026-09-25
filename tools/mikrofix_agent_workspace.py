#!/usr/bin/env python3
"""Create and verify the isolated workspace used by MikroFix local agents.

The workspace contains an agent-owned Git clone, immutable project knowledge,
per-run worktrees and candidate artifacts.  It deliberately does not copy
ignored files, secrets, databases or the trusted supervisor control plane.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = Path.home() / "MikroFIX_Agent_Workspace"
WORKSPACE_DIRS = (
    "knowledge", "inbox", "jobs", "worktrees", "runs", "artifacts",
    "reports", "proposals", "archive", "completion-outbox",
)
KNOWLEDGE_PATHS = (
    "AGENTS.md",
    "docs/kanon/00_KAZALO.md",
    "docs/kanon/01_POSLOVNA_PRAVILA.md",
    "docs/kanon/02_CILJNA_ARHITEKTURA.md",
    "docs/kanon/03_SPREJEMNI_TESTI.md",
    "docs/kanon/04_STANJE_IZVEDBE.md",
    "docs/kanon/05_ZGODOVINA_ODLOCITEV.md",
    "docs/local_agents/README.md",
    "docs/local_agents/PRAVILNIK_VERIGE.md",
    "docs/local_agents/pravila",
    "docs/local_agents/roles",
)


class WorkspaceError(RuntimeError):
    pass


def run(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, check=False,
    )
    if result.returncode:
        raise WorkspaceError(f"Command failed ({args[0]}): {result.stdout[-4000:]}")
    return result.stdout.strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def copy_knowledge(source: Path, destination: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in KNOWLEDGE_PATHS:
        origin = source / relative
        if not origin.exists():
            raise WorkspaceError(f"Required project knowledge is missing: {origin}")
        target = destination / relative
        if origin.is_dir():
            shutil.copytree(origin, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            path.chmod(0o400)
            hashes[path.relative_to(destination).as_posix()] = sha256(path)
        elif path.is_dir():
            path.chmod(0o500)
    destination.chmod(0o500)
    return hashes


def policy_text() -> str:
    return """# Varnostna meja lokalne ekipe MikroFix

1. `repository/`, `worktrees/` in ostale mape tega delovnega območja niso
   kanonični original. So prostor za pripravo kandidatov.
2. Lokalni agenti nikoli ne pišejo v zaščiten original ali na varnostni USB.
3. `knowledge/` je samo za branje. Nova spoznanja se zapišejo v `proposals/`.
4. Kandidat brez lokalnih testov, dokazov in pregleda GPT-OSS ni pripravljen.
5. Lokalna oznaka READY ne pomeni odobritve. Merge, push in deploy so prepovedani.
6. Codex kandidat oceni kot APPROVE, REVISE ali REJECT. Samo APPROVE sme postati
   nov zaščiten checkpoint in varnostna kopija.
7. Naloga, odvisna od nepotrjenega kandidata, mora navesti njegov `depends_on`.
   Zavrnitev starša zadrži vse njegove potomce.
8. Zaključek se privzeto sporoči lastniku. Codex se ne kliče samodejno.
"""


def init_workspace(source: Path, workspace: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    if workspace.exists():
        raise WorkspaceError(f"Workspace already exists: {workspace}")
    if not (source / ".git").exists():
        raise WorkspaceError(f"Source is not a Git working tree: {source}")
    head = run(["git", "-C", str(source), "rev-parse", "HEAD"])
    branch = run(["git", "-C", str(source), "branch", "--show-current"])
    dirty = run(["git", "-C", str(source), "status", "--short"]).splitlines()

    workspace.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{workspace.name}.init-", dir=workspace.parent
    ))
    try:
        staging.chmod(0o700)
        for name in WORKSPACE_DIRS:
            (staging / name).mkdir(mode=0o700)
        repository = staging / "repository"
        run(["git", "clone", "--no-hardlinks", "--no-checkout",
             str(source), str(repository)])
        run(["git", "checkout", "--detach", head], cwd=repository)

        knowledge = staging / "knowledge"
        hashes = copy_knowledge(source, knowledge)
        policy = staging / "WORKSPACE_POLICY.md"
        policy.write_text(policy_text(), encoding="utf-8")
        policy.chmod(0o400)

        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        checkpoint = {
            "schema_version": 1,
            "commit": head,
            "branch_at_creation": branch,
            "created_at": created_at,
            "disposition": "LOCAL_AGENT_BASELINE_NOT_CODEX_APPROVED",
        }
        write_json(staging / "CURRENT_CHECKPOINT.json", checkpoint, 0o400)
        config = {
            "schema_version": 1,
            "workspace": str(workspace),
            "repository": str(workspace / "repository"),
            "trusted_control_root": str(source),
            "notification_policy": "owner_only",
            "codex_review_policy": "manual_batch",
            "knowledge_manifest": str(workspace / "KNOWLEDGE_MANIFEST.json"),
        }
        write_json(staging / "workspace.json", config, 0o400)
        write_json(staging / "KNOWLEDGE_MANIFEST.json", {
            "schema_version": 1,
            "created_at": created_at,
            "source_commit": head,
            "files": hashes,
        }, 0o400)
        if dirty:
            (staging / "reports" / "SOURCE_DIRTY_AT_CREATION.txt").write_text(
                "Uncommitted source paths were deliberately excluded:\n"
                + "\n".join(dirty) + "\n",
                encoding="utf-8",
            )
        os.replace(staging, workspace)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return verify_workspace(workspace)


def verify_workspace(workspace: Path) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    config_path = workspace / "workspace.json"
    manifest_path = workspace / "KNOWLEDGE_MANIFEST.json"
    if not config_path.is_file() or not manifest_path.is_file():
        raise WorkspaceError("Workspace config or knowledge manifest is missing")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mismatches = []
    knowledge = workspace / "knowledge"
    expected_files = manifest.get("files", {})
    for relative, expected in expected_files.items():
        path = knowledge / relative
        if not path.is_file() or sha256(path) != expected:
            mismatches.append(relative)
    actual_files = {
        path.relative_to(knowledge).as_posix()
        for path in knowledge.rglob("*") if path.is_file()
    }
    mismatches.extend(
        f"UNEXPECTED:{relative}"
        for relative in sorted(actual_files - set(expected_files))
    )
    if knowledge.stat().st_mode & 0o222:
        mismatches.append("WRITABLE:.")
    mismatches = sorted(set(mismatches))
    repository = Path(config["repository"])
    repository_head = run(["git", "-C", str(repository), "rev-parse", "HEAD"])
    checkpoint = json.loads(
        (workspace / "CURRENT_CHECKPOINT.json").read_text(encoding="utf-8")
    )
    return {
        "status": "OK" if not mismatches else "INVALID",
        "workspace": str(workspace),
        "repository_head": repository_head,
        "baseline_commit": checkpoint["commit"],
        "knowledge_files": len(manifest.get("files", {})),
        "knowledge_mismatches": mismatches,
        "notification_policy": config.get("notification_policy"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MikroFix local-agent workspace")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--source", type=Path, default=PROJECT_ROOT)
    sub.add_parser("verify")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = (
        init_workspace(args.source, args.workspace)
        if args.command == "init" else verify_workspace(args.workspace)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
