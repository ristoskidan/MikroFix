#!/usr/bin/env python3
"""Fail-closed orchestration for the local MikroFIX coding agents.

The supervisor advances a persisted run by small, explicit transitions.  It
does not merge, push, deploy, alter business rules, or bypass the executor's
short write lease.  External effects are behind ``Adapter`` so the state
machine can be tested without starting Ollama or touching a working tree.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import tempfile
import time
from typing import Any, Protocol


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "mikrofix-supervisor"
DEFAULT_BRIDGE_CONFIG = (
    Path.home() / ".local" / "share" / "mikrofix-bridge" / "config.json"
)
MAX_RETRIES_LIMIT = 3
CHECK_COMMANDS: dict[str, tuple[str, ...]] = {
    "syntax_changed_python": (".venv/bin/python", "-m", "py_compile"),
    "local_agent_tests": (
        ".venv/bin/python", "-m", "unittest", "-v",
        "tests/test_mikrofix_local_agent.py",
    ),
    "desktop_unit_tests": (
        ".venv/bin/python", "-m", "unittest", "discover", "-v",
    ),
    "central_api_tests": (
        ".venv-server/bin/python", "-m", "unittest", "-v",
        "test_v32_central_api.py",
    ),
}


class Stage(str, Enum):
    PLANNED = "PLANNED"
    DISPATCHED_TO_QWEN = "DISPATCHED_TO_QWEN"
    DIFF_VERIFICATION = "DIFF_VERIFICATION"
    LOCAL_TESTS = "LOCAL_TESTS"
    GPT_OSS_REVIEW = "GPT_OSS_REVIEW"
    CHECKPOINT = "CHECKPOINT"
    AWAIT_CODEX_REVIEW = "AWAIT_CODEX_REVIEW"
    NEEDS_WRITE_LEASE = "NEEDS_WRITE_LEASE"
    BLOCKED = "BLOCKED"


TERMINAL_STAGES = {Stage.AWAIT_CODEX_REVIEW.value, Stage.BLOCKED.value}


@dataclass(frozen=True)
class GateResult:
    ok: bool
    summary: str
    evidence: str = ""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write_immutable_json(path: Path, value: dict[str, Any]) -> str:
    """Create a read-only JSON artifact without replacing an existing file."""
    if path.exists():
        raise RuntimeError(f"Immutable artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o400)
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class RunEnvironment:
    """Paths and launch metadata for one isolated supervisor run.

    Creating this object prepares but deliberately does not launch the executor,
    bridge monitor, Ollama, or a write lease.
    """

    run_id: str
    source_root: Path
    worktree: Path
    run_dir: Path
    bridge_home: Path
    bridge_config: Path
    executor_config_dir: Path
    executor_state_dir: Path
    allowed_files_manifest: Path
    immutable_plan: Path
    metadata_file: Path
    lease_file: Path
    executor_url: str
    plan_sha256: str
    allowed_sha256: str

    @classmethod
    def create(
        cls,
        source_root: Path,
        state_dir: Path,
        plan: dict[str, Any],
        bridge_template: dict[str, Any],
    ) -> "RunEnvironment":
        validated = validate_plan(plan)
        run_id = validated["run_id"]
        source_root = source_root.resolve()
        state_dir = state_dir.resolve()
        worktree = state_dir / "worktrees" / run_id
        run_dir = state_dir / "runs" / run_id
        if worktree.exists() or run_dir.exists():
            raise RuntimeError(f"Run environment already exists: {run_id}")
        server_python = source_root / ".venv-server" / "bin" / "python"
        if not server_python.is_file() or not os.access(server_python, os.X_OK):
            raise RuntimeError(
                f"Server virtual-environment interpreter is missing or not executable: "
                f"{server_python}"
            )
        worktree.parent.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, mode=0o700)
        result = subprocess.run(
            ["git", "-C", str(source_root), "worktree", "add", "--detach",
             str(worktree), validated["base_checkpoint"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Unable to create isolated worktree: {result.stdout[-4000:]}")

        bridge_home = run_dir / "bridge"
        bridge_config = bridge_home / "config.json"
        executor_config_dir = run_dir / "executor" / "config"
        executor_state_dir = run_dir / "executor" / "state"
        allowed_manifest = run_dir / "allowed-files.json"
        immutable_plan = run_dir / "plan.json"
        metadata_file = run_dir / "environment.json"
        lease_file = executor_state_dir / "executor-lease.json"
        bridge_home.mkdir(parents=True, mode=0o700)
        executor_config_dir.mkdir(parents=True, mode=0o700)
        executor_state_dir.mkdir(parents=True, mode=0o700)

        plan_sha = atomic_write_immutable_json(immutable_plan, validated)
        allowed_sha = atomic_write_immutable_json(allowed_manifest, {
            "run_id": run_id, "allowed_files": validated["allowed_files"]
        })
        port = 20000 + int(hashlib.sha256(run_id.encode()).hexdigest()[:6], 16) % 20000
        executor_url = f"http://127.0.0.1:{port}"
        config = {
            "ollama_url": bridge_template.get("ollama_url", "http://172.17.0.1:11434/api/chat"),
            "model": bridge_template.get("model", "qwen3.5:9b"),
            "executor_url": executor_url,
            "token_file": str(executor_config_dir / "token"),
            "project": str(worktree),
            "system_prompt": str(worktree / "docs/local_agents/izvajalec/QWEN_IZVAJALEC_SYSTEM.md"),
            "num_ctx": int(bridge_template.get("num_ctx", 65536)),
            "max_steps": int(bridge_template.get("max_steps", 30)),
        }
        atomic_write_immutable_json(bridge_config, config)
        metadata = {
            "run_id": run_id,
            "worktree": str(worktree),
            "lease_file": str(lease_file),
            "bridge": {
                "argv": ["bridge", "monitor"],
                "cwd": str(worktree),
                "env": {
                    "MIKROFIX_BRIDGE_HOME": str(bridge_home),
                    "MIKROFIX_BRIDGE_CONFIG": str(bridge_config),
                },
            },
            "executor": {
                "argv": [str(server_python), "-m", "uvicorn", "tools.local_executor.server:app",
                         "--host", "127.0.0.1", "--port", str(port)],
                "cwd": str(worktree),
                "env": {
                    "MIKROFIX_EXECUTOR_ROOT": str(worktree),
                    "MIKROFIX_EXECUTOR_CONFIG_DIR": str(executor_config_dir),
                    "MIKROFIX_EXECUTOR_STATE_DIR": str(executor_state_dir),
                    "MIKROFIX_EXECUTOR_ALLOWED_FILES": str(allowed_manifest),
                    "MIKROFIX_EXECUTOR_RUN_ID": run_id,
                    "MIKROFIX_EXECUTOR_PLAN_SHA256": plan_sha,
                    "MIKROFIX_EXECUTOR_ALLOWED_FILES_SHA256": allowed_sha,
                },
            },
            "safety": {
                "executor_started": False,
                "bridge_started": False,
                "lease_created": False,
                "instruction": "Launch from reviewed metadata and grant a run-specific short lease.",
            },
        }
        atomic_write_immutable_json(metadata_file, metadata)
        return cls(
            run_id, source_root, worktree, run_dir, bridge_home, bridge_config,
            executor_config_dir, executor_state_dir, allowed_manifest,
            immutable_plan, metadata_file, lease_file, executor_url,
            plan_sha, allowed_sha,
        )

    def to_dict(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in (
            "run_id", "source_root", "worktree", "run_dir", "bridge_home",
            "bridge_config", "executor_config_dir", "executor_state_dir",
            "allowed_files_manifest", "immutable_plan", "metadata_file",
            "lease_file", "executor_url", "plan_sha256", "allowed_sha256",
        )}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunEnvironment":
        path_fields = {
            "source_root", "worktree", "run_dir", "bridge_home", "bridge_config",
            "executor_config_dir", "executor_state_dir", "allowed_files_manifest",
            "immutable_plan", "metadata_file", "lease_file",
        }
        kwargs = {key: (Path(raw).resolve() if key in path_fields else str(raw))
                  for key, raw in value.items()}
        environment = cls(**kwargs)
        environment.verify_immutable_artifacts()
        return environment

    def verify_immutable_artifacts(self) -> None:
        if file_sha256(self.immutable_plan) != self.plan_sha256:
            raise RuntimeError("Immutable run plan changed; refusing to continue")
        if file_sha256(self.allowed_files_manifest) != self.allowed_sha256:
            raise RuntimeError("Allowed-files manifest changed; refusing to continue")
        if not self.worktree.is_dir():
            raise RuntimeError("Isolated run worktree is missing")

    def bridge_env(self) -> dict[str, str]:
        return {
            "MIKROFIX_BRIDGE_HOME": str(self.bridge_home),
            "MIKROFIX_BRIDGE_CONFIG": str(self.bridge_config),
        }

    def executor_env(self) -> dict[str, str]:
        return {
            "MIKROFIX_EXECUTOR_ROOT": str(self.worktree),
            "MIKROFIX_EXECUTOR_CONFIG_DIR": str(self.executor_config_dir),
            "MIKROFIX_EXECUTOR_STATE_DIR": str(self.executor_state_dir),
            "MIKROFIX_EXECUTOR_ALLOWED_FILES": str(self.allowed_files_manifest),
            "MIKROFIX_EXECUTOR_RUN_ID": self.run_id,
            "MIKROFIX_EXECUTOR_PLAN_SHA256": self.plan_sha256,
            "MIKROFIX_EXECUTOR_ALLOWED_FILES_SHA256": self.allowed_sha256,
        }

    def lease_identity(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "project_root": str(self.worktree),
            "plan_sha256": self.plan_sha256,
            "allowed_files_sha256": self.allowed_sha256,
        }


class Adapter(Protocol):
    def lease_active(self) -> bool: ...
    def snapshot(self) -> dict[str, str]: ...
    def dispatch_qwen(self, prompt: str) -> str: ...
    def qwen_result(self, job_id: str) -> tuple[str, str]: ...
    def diff(self, paths: list[str]) -> str: ...
    def run_tests(self, checks: list[str]) -> GateResult: ...
    def review(self, prompt: str, material: str) -> GateResult: ...
    def checkpoint(self, run: dict[str, Any], diff: str) -> str: ...


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_relative_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("allowed_files entries must be non-empty strings")
    if "\\" in raw:
        raise ValueError(f"non-portable path is forbidden: {raw!r}")
    path = PurePosixPath(raw)
    if (path.is_absolute() or ".." in path.parts or ".git" in path.parts
            or path.as_posix() in {"", "."}):
        raise ValueError(f"unsafe project-relative path: {raw!r}")
    return path.as_posix()


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    required_strings = ("run_id", "objective", "base_checkpoint")
    for key in required_strings:
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", plan["run_id"]):
        raise ValueError("run_id may contain only letters, digits, hyphen and underscore")
    allowed_raw = plan.get("allowed_files")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise ValueError("allowed_files must be a non-empty list")
    allowed = list(dict.fromkeys(safe_relative_path(item) for item in allowed_raw))
    atoms_raw = plan.get("atoms")
    if not isinstance(atoms_raw, list) or not atoms_raw:
        raise ValueError("atoms must be a non-empty list")
    atoms: list[dict[str, Any]] = []
    atom_ids: set[str] = set()
    for raw in atoms_raw:
        if not isinstance(raw, dict):
            raise ValueError("each atom must be an object")
        atom_id = raw.get("id")
        task = raw.get("task")
        if not isinstance(atom_id, str) or not atom_id.strip() or atom_id in atom_ids:
            raise ValueError("atom ids must be unique non-empty strings")
        if not isinstance(task, str) or not task.strip():
            raise ValueError(f"atom {atom_id} task must be a non-empty string")
        checks = raw.get("checks", ["syntax_changed_python"])
        if not isinstance(checks, list) or not checks:
            raise ValueError(f"atom {atom_id} checks must be a non-empty list")
        unknown = sorted(set(checks) - set(CHECK_COMMANDS))
        if unknown:
            raise ValueError(f"atom {atom_id} has unknown checks: {', '.join(unknown)}")
        expected = [safe_relative_path(item) for item in raw.get("expected_files", [])]
        outside = sorted(set(expected) - set(allowed))
        if outside:
            raise ValueError(f"atom {atom_id} expects files outside allowed_files: {outside}")
        atoms.append({"id": atom_id, "task": task.strip(), "checks": checks,
                      "expected_files": expected})
        atom_ids.add(atom_id)
    retries = plan.get("max_retries_per_atom", MAX_RETRIES_LIMIT)
    if not isinstance(retries, int) or not 1 <= retries <= MAX_RETRIES_LIMIT:
        raise ValueError(f"max_retries_per_atom must be between 1 and {MAX_RETRIES_LIMIT}")
    if plan.get("final_gate", Stage.AWAIT_CODEX_REVIEW.value) != Stage.AWAIT_CODEX_REVIEW.value:
        raise ValueError("final_gate must be AWAIT_CODEX_REVIEW")
    return {
        "run_id": plan["run_id"].strip(),
        "objective": plan["objective"].strip(),
        "base_checkpoint": plan["base_checkpoint"].strip(),
        "allowed_files": allowed,
        "atoms": atoms,
        "max_retries_per_atom": retries,
        "final_gate": Stage.AWAIT_CODEX_REVIEW.value,
    }


def initial_run(
    plan: dict[str, Any],
    snapshot: dict[str, str],
    environment: RunEnvironment | None = None,
) -> dict[str, Any]:
    run = {
        "schema_version": 1,
        "plan": validate_plan(plan),
        "stage": Stage.PLANNED.value,
        "atom_index": 0,
        "attempt": 0,
        "job_id": None,
        "feedback": "",
        "qwen_result": "",
        "diff": "",
        "test_evidence": "",
        "review_evidence": "",
        "initial_snapshot": snapshot,
        "accepted_snapshot": snapshot,
        "dispatch_snapshot": None,
        "completed_atoms": [],
        "events": [{"at": utc_timestamp(), "stage": Stage.PLANNED.value,
                    "message": "Run initialized"}],
        "checkpoint": None,
        "updated_at": utc_timestamp(),
    }
    if environment is not None:
        run["environment"] = environment.to_dict()
    return run


def changed_paths(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {path for path in set(before) | set(after) if before.get(path) != after.get(path)}


def record(run: dict[str, Any], stage: Stage, message: str) -> None:
    run["stage"] = stage.value
    run["updated_at"] = utc_timestamp()
    run["events"].append({"at": run["updated_at"], "stage": stage.value,
                          "message": message})


def retry_or_block(run: dict[str, Any], reason: str) -> None:
    run["feedback"] = reason
    maximum = run["plan"]["max_retries_per_atom"]
    if run["attempt"] >= maximum:
        record(run, Stage.BLOCKED, f"Retry limit reached: {reason}")
    else:
        run["job_id"] = None
        record(run, Stage.PLANNED, f"Retry requested: {reason}")


def qwen_prompt(run: dict[str, Any]) -> str:
    plan = run["plan"]
    atom = plan["atoms"][run["atom_index"]]
    feedback = run.get("feedback") or "No earlier attempt."
    return (
        f"RUN {plan['run_id']} / ATOM {atom['id']} / attempt {run['attempt'] + 1}\n"
        f"Objective: {plan['objective']}\n"
        f"Task: {atom['task']}\n"
        f"Allowed files (strict): {', '.join(plan['allowed_files'])}\n"
        f"Expected files: {', '.join(atom['expected_files']) or '(not specified)'}\n"
        f"Checks that will run: {', '.join(atom['checks'])}\n"
        f"Earlier gate feedback: {feedback}\n\n"
        "Make the smallest complete change. Do not modify any other file. "
        "Do not run git checkout, restore, reset, clean, commit, merge, push, "
        "or deploy. Use the project tools and report exactly what changed."
    )


class Supervisor:
    def __init__(self, state_file: Path, adapter: Adapter):
        self.state_file = state_file
        self.adapter = adapter

    def load(self) -> dict[str, Any]:
        value = json.loads(self.state_file.read_text(encoding="utf-8"))
        if value.get("schema_version") != 1:
            raise RuntimeError("Unsupported or missing supervisor state schema")
        validated = validate_plan(value.get("plan", {}))
        if "environment" in value:
            environment = RunEnvironment.from_dict(value["environment"])
            immutable_plan = json.loads(environment.immutable_plan.read_text(encoding="utf-8"))
            if validated != immutable_plan or environment.run_id != validated["run_id"]:
                raise RuntimeError("Persisted state differs from immutable run plan")
        if value.get("stage") not in {stage.value for stage in Stage}:
            raise RuntimeError("Unknown supervisor stage; refusing to continue")
        return value

    def save(self, run: dict[str, Any]) -> None:
        atomic_write_json(self.state_file, run)

    def step(self) -> bool:
        run = self.load()
        stage = Stage(run["stage"])
        if stage.value in TERMINAL_STAGES:
            return False
        if stage is Stage.NEEDS_WRITE_LEASE:
            if not self.adapter.lease_active():
                return False
            record(run, Stage.PLANNED, "Write lease is active; resuming")
            self.save(run)
            return True
        if stage is Stage.PLANNED:
            if not self.adapter.lease_active():
                record(run, Stage.NEEDS_WRITE_LEASE,
                       "Executor write lease is absent or expired")
                self.save(run)
                return True
            run["attempt"] += 1
            run["dispatch_snapshot"] = self.adapter.snapshot()
            run["job_id"] = self.adapter.dispatch_qwen(qwen_prompt(run))
            record(run, Stage.DISPATCHED_TO_QWEN,
                   f"Qwen job dispatched: {run['job_id']}")
            self.save(run)
            return True
        if stage is Stage.DISPATCHED_TO_QWEN:
            status, result = self.adapter.qwen_result(str(run["job_id"]))
            if status in {"queued", "running"}:
                return False
            if status != "completed":
                retry_or_block(run, f"Qwen job ended as {status}: {result}")
            else:
                run["qwen_result"] = result
                record(run, Stage.DIFF_VERIFICATION, "Qwen job completed")
            self.save(run)
            return True
        if stage is Stage.DIFF_VERIFICATION:
            current = self.adapter.snapshot()
            changed = changed_paths(run["dispatch_snapshot"], current)
            allowed = set(run["plan"]["allowed_files"])
            outside = sorted(changed - allowed)
            atom = run["plan"]["atoms"][run["atom_index"]]
            missing = sorted(set(atom["expected_files"]) - changed)
            if outside:
                run["feedback"] = f"Files outside allowed_files changed: {outside}"
                record(run, Stage.BLOCKED,
                       "Out-of-scope workspace mutation; manual inspection required")
            elif not changed:
                retry_or_block(run, "No actual workspace change was detected")
            elif missing:
                retry_or_block(run, f"Expected files did not change: {missing}")
            else:
                run["diff"] = self.adapter.diff(sorted(changed))
                if not run["diff"].strip():
                    retry_or_block(run, "Changed files produced an empty diff artifact")
                else:
                    record(run, Stage.LOCAL_TESTS,
                           f"Diff accepted for files: {', '.join(sorted(changed))}")
            self.save(run)
            return True
        if stage is Stage.LOCAL_TESTS:
            atom = run["plan"]["atoms"][run["atom_index"]]
            result = self.adapter.run_tests(atom["checks"])
            run["test_evidence"] = result.evidence or result.summary
            if result.ok:
                record(run, Stage.GPT_OSS_REVIEW, "Local checks passed")
            else:
                retry_or_block(run, f"Local checks failed: {result.summary}")
            self.save(run)
            return True
        if stage is Stage.GPT_OSS_REVIEW:
            atom = run["plan"]["atoms"][run["atom_index"]]
            prompt = (
                f"Review atom {atom['id']}: {atom['task']}. Reject defects, scope "
                "drift, missing regression coverage, destructive behavior, and "
                "unsupported completion claims. End with exactly VERDICT: APPROVE "
                "or VERDICT: REJECT."
            )
            material = run["diff"] + "\n\nTEST EVIDENCE:\n" + run["test_evidence"]
            result = self.adapter.review(prompt, material)
            run["review_evidence"] = result.evidence or result.summary
            if not result.ok:
                retry_or_block(run, f"GPT-OSS rejected the atom: {result.summary}")
                self.save(run)
                return True
            run["completed_atoms"].append({
                "id": atom["id"], "attempts": run["attempt"],
                "tests": run["test_evidence"], "review": run["review_evidence"],
            })
            run["accepted_snapshot"] = self.adapter.snapshot()
            run["atom_index"] += 1
            if run["atom_index"] < len(run["plan"]["atoms"]):
                run.update({"attempt": 0, "job_id": None, "feedback": "",
                            "qwen_result": "", "diff": "", "test_evidence": "",
                            "review_evidence": "", "dispatch_snapshot": None})
                record(run, Stage.PLANNED, "Atom accepted; next atom planned")
            else:
                record(run, Stage.CHECKPOINT, "All atoms accepted")
            self.save(run)
            return True
        if stage is Stage.CHECKPOINT:
            current = self.adapter.snapshot()
            outside = sorted(changed_paths(run["initial_snapshot"], current)
                             - set(run["plan"]["allowed_files"]))
            if outside:
                run["feedback"] = f"Final scope check found outside files: {outside}"
                record(run, Stage.BLOCKED, "Final scope check failed")
            else:
                final_diff = self.adapter.diff(run["plan"]["allowed_files"])
                run["checkpoint"] = self.adapter.checkpoint(run, final_diff)
                record(run, Stage.AWAIT_CODEX_REVIEW,
                       "Checkpoint artifact created; no merge, push, or deployment performed")
            self.save(run)
            return True
        raise RuntimeError(f"Unhandled stage: {stage.value}")


class LocalAdapter:
    """Concrete adapter for the existing bridge, Git and local reviewer."""

    def __init__(
        self,
        project_root: Path,
        state_dir: Path,
        *,
        lease_file: Path | None = None,
        bridge_env: dict[str, str] | None = None,
        source_root: Path | None = None,
        expected_lease: dict[str, str] | None = None,
    ):
        self.project_root = project_root.resolve()
        self.state_dir = state_dir.resolve()
        self.lease_file = lease_file.resolve() if lease_file else None
        self._bridge_env = dict(bridge_env or {})
        self.source_root = (source_root or project_root).resolve()
        self.expected_lease = dict(expected_lease or {})

    def _run(self, args: list[str], timeout: int = 900, check: bool = True,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(args, cwd=self.project_root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=timeout, check=False,
                                env={**os.environ, **(env or {})})
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed ({args[0]}): {result.stdout[-4000:]}")
        return result

    def lease_active(self) -> bool:
        if self.lease_file is None:
            return False
        try:
            lease = json.loads(self.lease_file.read_text(encoding="utf-8"))
            return float(lease.get("expires_at", 0)) > time.time() and all(
                str(lease.get(key) or "") == value
                for key, value in self.expected_lease.items()
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def verify_base_checkpoint(self, expected: str) -> None:
        resolved = self._run(
            ["git", "rev-parse", "--verify", f"{expected}^{{commit}}"], timeout=30
        ).stdout.strip()
        head = self._run(["git", "rev-parse", "HEAD"], timeout=30).stdout.strip()
        if resolved != head:
            raise RuntimeError(
                f"Plan base checkpoint {resolved} is not current HEAD {head}; "
                "refusing to initialize on an unexpected revision"
            )

    def snapshot(self) -> dict[str, str]:
        result = self._run([
            "git", "status", "--porcelain=v1", "-z", "--untracked-files=all"
        ])
        entries = result.stdout.split("\0")
        snapshot: dict[str, str] = {}
        for entry in entries:
            if not entry:
                continue
            if len(entry) < 4:
                raise RuntimeError("Unparseable Git status; refusing to continue")
            status, raw_path = entry[:2], entry[3:]
            if " -> " in raw_path or status[0] in "RC" or status[1] in "RC":
                raise RuntimeError("Rename/copy status is unsupported; refusing to continue")
            path = safe_relative_path(raw_path)
            full_path = self.project_root / path
            if full_path.is_file():
                digest = hashlib.sha256(full_path.read_bytes()).hexdigest()
            elif full_path.exists():
                digest = "non-file"
            else:
                digest = "missing"
            snapshot[path] = f"{status}:{digest}"
        return snapshot

    def dispatch_qwen(self, prompt: str) -> str:
        return self._run(["bridge", "dispatch", "--task", prompt], timeout=30,
                         env=self._bridge_env).stdout.strip()

    def qwen_result(self, job_id: str) -> tuple[str, str]:
        raw = self._run(["bridge", "status", job_id], timeout=30,
                        env=self._bridge_env).stdout
        try:
            job = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Bridge returned invalid status JSON") from exc
        return str(job.get("status", "unknown")), str(job.get("result") or job.get("error") or "")

    def diff(self, paths: list[str]) -> str:
        if not paths:
            return ""
        safe_paths = [safe_relative_path(path) for path in paths]
        tracked = self._run(["git", "diff", "--", *safe_paths], check=False).stdout
        staged = self._run(["git", "diff", "--cached", "--", *safe_paths], check=False).stdout
        chunks = [tracked, staged]
        for path in safe_paths:
            full_path = self.project_root / path
            probe = self._run(["git", "ls-files", "--error-unmatch", "--", path],
                              timeout=30, check=False)
            if probe.returncode != 0 and full_path.is_file():
                rendered = self._run(["git", "diff", "--no-index", "--", "/dev/null", path],
                                     timeout=30, check=False).stdout
                chunks.append(rendered)
        return "\n".join(chunk for chunk in chunks if chunk.strip())

    def run_tests(self, checks: list[str]) -> GateResult:
        outputs: list[str] = []
        for check_name in checks:
            if check_name not in CHECK_COMMANDS:
                return GateResult(False, f"Unknown test profile: {check_name}")
            command = list(CHECK_COMMANDS[check_name])
            if command[0].startswith(".venv"):
                command[0] = str(self.source_root / command[0])
            if check_name == "syntax_changed_python":
                queries = (
                    ["git", "diff", "--name-only", "--diff-filter=ACM", "--", "*.py"],
                    ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM",
                     "--", "*.py"],
                    ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
                )
                changed = sorted({
                    safe_relative_path(path)
                    for query in queries
                    for path in self._run(query).stdout.splitlines()
                    if path.endswith(".py")
                })
                command.extend(changed)
                if not changed:
                    outputs.append(f"{check_name}: no changed Python files")
                    continue
            result = self._run(command, timeout=900, check=False)
            outputs.append(f"$ {' '.join(command)}\n{result.stdout[-60000:]}")
            if result.returncode != 0:
                return GateResult(False, f"{check_name} exited {result.returncode}",
                                  "\n\n".join(outputs))
        return GateResult(True, "All approved checks passed", "\n\n".join(outputs))

    def review(self, prompt: str, material: str) -> GateResult:
        artifacts = self.state_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="review-", suffix=".txt", dir=artifacts)
        path = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(material)
            result = self._run([
                "python3", "tools/mikrofix_local_agent.py", "--agent", "reviewer",
                "--task", prompt, "--material-file", str(path), "--max-tokens", "2200",
                "--output", "Specific findings followed by exactly one final verdict line.",
                "--log-dir", str(self.state_dir / "reviews"),
                "--console-log", str(self.state_dir / "agent-console.log"),
            ], timeout=1200, check=False)
        finally:
            path.unlink(missing_ok=True)
        output = result.stdout.strip()
        verdicts = [line.strip() for line in output.splitlines()
                    if line.strip().startswith("VERDICT:")]
        approved = result.returncode == 0 and verdicts == ["VERDICT: APPROVE"]
        if approved:
            summary = "approved"
        elif len(verdicts) > 1:
            summary = "ambiguous multiple verdicts"
        else:
            summary = verdicts[0] if verdicts else "missing valid verdict"
        return GateResult(approved, summary, output[-60000:])

    def checkpoint(self, run: dict[str, Any], diff: str) -> str:
        run_id = run["plan"]["run_id"]
        safe_id = "".join(char for char in run_id if char.isalnum() or char in "-_")
        if not safe_id or safe_id != run_id:
            raise RuntimeError("Unsafe run_id for checkpoint path")
        artifact_dir = self.state_dir / "artifacts" / safe_id
        patch_path = artifact_dir / "candidate.patch"
        manifest_path = artifact_dir / "manifest.json"
        atomic_write_text(patch_path, diff)
        digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        manifest = {
            "run_id": run_id, "base_checkpoint": run["plan"]["base_checkpoint"],
            "patch_sha256": digest, "patch": str(patch_path),
            "completed_atoms": run["completed_atoms"], "created_at": utc_timestamp(),
            "disposition": "AWAIT_CODEX_REVIEW",
            "not_performed": ["merge", "push", "deploy"],
        }
        atomic_write_json(manifest_path, manifest)
        return str(manifest_path)


def state_lock(state_file: Path):
    lock_path = state_file.with_suffix(state_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MikroFIX local agent supervisor")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("plan", type=Path)
    init.add_argument("--state", type=Path)
    for name in ("step", "run", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--state", type=Path)
        if name == "run":
            command.add_argument("--interval", type=float, default=5.0)
    return parser.parse_args(argv)


def isolated_adapter(state_file: Path, state_dir: Path) -> LocalAdapter:
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    if "environment" not in raw:
        raise RuntimeError("Run has no isolated environment metadata")
    environment = RunEnvironment.from_dict(raw["environment"])
    return LocalAdapter(
        environment.worktree,
        state_dir,
        lease_file=environment.lease_file,
        bridge_env=environment.bridge_env(),
        source_root=environment.source_root,
        expected_lease=environment.lease_identity(),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_dir = args.state_dir.expanduser().resolve()
    state_file = (args.state or state_dir / "active-run.json").expanduser().resolve()
    if args.command == "init":
        with state_lock(state_file):
            if state_file.exists():
                raise SystemExit(f"State already exists: {state_file}")
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            validated = validate_plan(plan)
            source_adapter = LocalAdapter(PROJECT_ROOT, state_dir)
            source_adapter.verify_base_checkpoint(validated["base_checkpoint"])
            bridge_template = json.loads(DEFAULT_BRIDGE_CONFIG.read_text(encoding="utf-8"))
            environment = RunEnvironment.create(
                PROJECT_ROOT, state_dir, validated, bridge_template
            )
            adapter = LocalAdapter(
                environment.worktree,
                state_dir,
                lease_file=environment.lease_file,
                bridge_env=environment.bridge_env(),
                source_root=PROJECT_ROOT,
                expected_lease=environment.lease_identity(),
            )
            run = initial_run(validated, adapter.snapshot(), environment)
            atomic_write_json(state_file, run)
            print(f"Initialized {run['plan']['run_id']} at {state_file}")
            print(f"Isolated worktree: {environment.worktree}")
            print(f"Launch metadata: {environment.metadata_file}")
            print(f"Run-specific lease: {environment.lease_file}")
            return 0
    adapter = isolated_adapter(state_file, state_dir)
    if args.command == "status":
        with state_lock(state_file):
            supervisor = Supervisor(state_file, adapter)
            run = supervisor.load()
        print(json.dumps({key: run.get(key) for key in (
            "stage", "atom_index", "attempt", "job_id", "feedback", "checkpoint"
        )}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "step":
        with state_lock(state_file):
            supervisor = Supervisor(state_file, adapter)
            supervisor.step()
            stage = supervisor.load()["stage"]
        print(stage)
        return 0
    while True:
        with state_lock(state_file):
            supervisor = Supervisor(state_file, adapter)
            progressed = supervisor.step()
            stage = supervisor.load()["stage"]
        print(f"{utc_timestamp()} {stage}", flush=True)
        if stage in TERMINAL_STAGES or stage == Stage.NEEDS_WRITE_LEASE.value:
            return 0
        if not progressed:
            time.sleep(max(0.2, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
