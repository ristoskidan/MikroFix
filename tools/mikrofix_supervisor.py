#!/usr/bin/env python3
"""Fail-closed orchestration for the local MikroFIX coding agents.

The supervisor advances a persisted run by small, explicit transitions.  It
does not merge, push, deploy, alter business rules, or bypass the executor's
short write lease.  External effects are behind ``Adapter`` so the state
machine can be tested without starting Ollama or touching a working tree.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Protocol
import urllib.error
import urllib.request

try:
    from tools.mikrofix_lead_session import EXECUTION_SOURCES, ensure_lead_session
except ModuleNotFoundError:
    from mikrofix_lead_session import (  # type: ignore[no-redef]
        EXECUTION_SOURCES, ensure_lead_session,
    )

try:
    from tools.mikrofix_completion import CompletionOutbox, validate_thread_id
    from tools.mikrofix_workspace_lock import workspace_lock
except ModuleNotFoundError:  # direct execution adds tools/, not the repo root
    from mikrofix_completion import (  # type: ignore[no-redef]
        CompletionOutbox,
        validate_thread_id,
    )
    from mikrofix_workspace_lock import workspace_lock  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "mikrofix-supervisor"
DEFAULT_BRIDGE_CONFIG = (
    Path.home() / ".local" / "share" / "mikrofix-bridge" / "config.json"
)
MAX_RETRIES_LIMIT = 3
DEFAULT_JOB_TIMEOUT_SECONDS = 1800
CONTROL_PLANE_FILES = {
    "AGENTS.md",
    "tools/mikrofix_supervisor.py",
    "tools/mikrofix_bridge.py",
    "tools/mikrofix_completion.py",
    "tools/mikrofix_local_agent.py",
    "tools/mikrofix_agent_workspace.py",
    "tools/mikrofix_local_manager.py",
    "tools/mikrofix_local_manager_ui.py",
    "tools/mikrofix_local_web.py",
    "tools/mikrofix_local_manager_web.html",
    "tools/build_local_agent_rules.py",
    "tests/test_mikrofix_supervisor.py",
    "tests/test_mikrofix_bridge.py",
    "tests/test_mikrofix_completion.py",
    "tests/test_local_executor_server.py",
    "tests/test_mikrofix_local_agent.py",
    "tests/test_mikrofix_agent_workspace.py",
    "tests/test_mikrofix_local_manager.py",
    "tests/test_mikrofix_local_manager_ui.py",
    "tests/test_mikrofix_local_web.py",
    "tests/test_build_local_agent_rules.py",
    "docs/kanon/00_KAZALO.md",
    "docs/kanon/01_POSLOVNA_PRAVILA.md",
    "docs/kanon/02_CILJNA_ARHITEKTURA.md",
    "docs/kanon/03_SPREJEMNI_TESTI.md",
    "docs/local_agents/WORKSPACE.md",
    "docs/local_agents/PRAVILNIK_VERIGE.md",
    "docs/local_agents/roles/PLAN_VALIDATOR_SYSTEM.md",
    "docs/local_agents/supervisor/PLAN_PROPOSAL_SCHEMA.json",
    "desktop/mikrofix-local-manager.desktop",
    "desktop/mikrofix-local-manager-autostart.desktop",
}
CONTROL_PLANE_PREFIXES = (
    "config/local_agents/",
    "docs/archive/",
    "docs/kanon/",
    "docs/local_agents/",
    "desktop/mikrofix-local-",
    "tests/test_mikrofix_",
    "tools/local_executor/",
    "tools/mikrofix_",
)
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
    repository_root: Path
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
        *,
        repository_root: Path | None = None,
    ) -> "RunEnvironment":
        validated = validate_plan(plan)
        run_id = validated["run_id"]
        source_root = source_root.resolve()
        repository_root = (repository_root or source_root).resolve()
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
        trusted_bridge = source_root / "tools" / "mikrofix_bridge.py"
        trusted_reviewer = source_root / "tools" / "mikrofix_local_agent.py"
        trusted_control = source_root / "tools" / "local_executor" / "control.py"
        for label, trusted_path in (
            ("vendored bridge", trusted_bridge),
            ("local reviewer", trusted_reviewer),
            ("lease controller", trusted_control),
        ):
            if not trusted_path.is_file():
                raise RuntimeError(f"Trusted {label} is missing: {trusted_path}")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, mode=0o700)
        result = subprocess.run(
            ["git", "-C", str(repository_root), "worktree", "add", "--detach",
             str(worktree), validated["base_checkpoint"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Unable to create isolated worktree: {result.stdout[-4000:]}")
        worktree_control = worktree / "tools" / "local_executor" / "control.py"
        if not worktree_control.is_file():
            raise RuntimeError(
                f"Trusted worktree lease controller is missing: {worktree_control}"
            )

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
        lead_home = Path(
            bridge_template.get("lead_home", state_dir / "lead-session")
        ).expanduser().resolve()
        lead = ensure_lead_session(
            source_root, lead_home, sources=EXECUTION_SOURCES,
            memory_name="EXECUTOR_CONTEXT.md",
        )
        worker_think = str(bridge_template.get("worker_think", "medium"))
        reviewer_think = str(bridge_template.get("reviewer_think", "medium"))
        if worker_think not in {"low", "medium", "high"}:
            raise ValueError("worker_think must be low, medium or high")
        if reviewer_think not in {"low", "medium", "high"}:
            raise ValueError("reviewer_think must be low, medium or high")
        config = {
            "ollama_url": bridge_template.get("ollama_url", "http://172.17.0.1:11434/api/chat"),
            "model": bridge_template.get(
                "worker_model", bridge_template.get("model", "gpt-oss:20b")
            ),
            "executor_url": executor_url,
            "token_file": str(executor_config_dir / "token"),
            "project": str(worktree),
            "system_prompt": str(
                source_root / "docs/local_agents/roles/EXECUTOR_SYSTEM.md"
            ),
            "project_memory": str(lead["memory_file"]),
            "parent_session_id": str(lead["session_id"]),
            "num_ctx": min(int(bridge_template.get("worker_num_ctx", 32768)), 32768),
            "runtime_options": {
                "num_predict": int(bridge_template.get("worker_num_predict", 8192)),
            },
            "think": worker_think,
            "max_steps": int(bridge_template.get("max_steps", 30)),
            "keep_alive": str(bridge_template.get("worker_keep_alive", "5m")),
            "agent_role": "Lokalni izvajalec (ločena izvedbena seja)",
            "completion_policy": "supervisor_only",
            "reviewer_num_ctx": min(
                int(bridge_template.get("reviewer_num_ctx", 32768)), 32768
            ),
            "reviewer_num_predict": int(
                bridge_template.get("reviewer_num_predict", 3072)
            ),
            "reviewer_think": reviewer_think,
        }
        atomic_write_immutable_json(bridge_config, config)
        metadata = {
            "run_id": run_id,
            "repository_root": str(repository_root),
            "trusted_control_root": str(source_root),
            "worktree": str(worktree),
            "lease_file": str(lease_file),
            "bridge": {
                "argv": [str(server_python), str(trusted_bridge), "monitor"],
                "cwd": str(worktree),
                "env": {
                    "MIKROFIX_BRIDGE_HOME": str(bridge_home),
                    "MIKROFIX_BRIDGE_CONFIG": str(bridge_config),
                    "MIKROFIX_WORKSPACE": str(state_dir),
                },
            },
            "executor": {
                "argv": [str(server_python), "-m", "uvicorn", "tools.local_executor.server:app",
                         "--host", "127.0.0.1", "--port", str(port)],
                # Import the executor implementation from the protected control
                # checkout. MIKROFIX_EXECUTOR_ROOT still confines its data
                # operations to the candidate worktree.
                "cwd": str(source_root),
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
            run_id, source_root, repository_root, worktree, run_dir, bridge_home, bridge_config,
            executor_config_dir, executor_state_dir, allowed_manifest,
            immutable_plan, metadata_file, lease_file, executor_url,
            plan_sha, allowed_sha,
        )

    def to_dict(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in (
            "run_id", "source_root", "repository_root", "worktree", "run_dir", "bridge_home",
            "bridge_config", "executor_config_dir", "executor_state_dir",
            "allowed_files_manifest", "immutable_plan", "metadata_file",
            "lease_file", "executor_url", "plan_sha256", "allowed_sha256",
        )}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunEnvironment":
        value = dict(value)
        # State created before the repository/control-plane split used one root.
        value.setdefault("repository_root", value.get("source_root"))
        path_fields = {
            "source_root", "repository_root", "worktree", "run_dir", "bridge_home", "bridge_config",
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
            "MIKROFIX_WORKSPACE": str(self.run_dir.parents[1]),
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


class CommandRunner(Protocol):
    def run(
        self, args: list[str], *, cwd: Path | None = None,
        env: dict[str, str] | None = None, timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    def run(
        self, args: list[str], *, cwd: Path | None = None,
        env: dict[str, str] | None = None, timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=cwd, env={**os.environ, **(env or {})},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False,
        )


def http_health_probe(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url.rstrip("/") + "/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Executor health probe failed: {exc}") from exc


class WorkerManager:
    """Start and stop one run's executor/bridge as transient user services."""

    EXECUTOR_ENV_KEYS = {
        "MIKROFIX_EXECUTOR_ROOT", "MIKROFIX_EXECUTOR_CONFIG_DIR",
        "MIKROFIX_EXECUTOR_STATE_DIR", "MIKROFIX_EXECUTOR_ALLOWED_FILES",
        "MIKROFIX_EXECUTOR_RUN_ID", "MIKROFIX_EXECUTOR_PLAN_SHA256",
        "MIKROFIX_EXECUTOR_ALLOWED_FILES_SHA256",
    }
    BRIDGE_ENV_KEYS = {
        "MIKROFIX_BRIDGE_HOME", "MIKROFIX_BRIDGE_CONFIG", "MIKROFIX_WORKSPACE",
    }

    def __init__(
        self,
        environment: RunEnvironment,
        *,
        runner: CommandRunner | None = None,
        probe: Callable[[str], dict[str, Any]] = http_health_probe,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        startup_timeout: float = 30.0,
    ):
        self.environment = environment
        self.runner = runner or SubprocessRunner()
        self.probe = probe
        self.clock = clock
        self.sleeper = sleeper
        self.startup_timeout = startup_timeout
        safe_id = environment.run_id.casefold().replace("_", "-")[:32]
        identity_material = "\0".join((
            environment.run_id,
            str(environment.run_dir),
            str(environment.worktree),
            environment.plan_sha256,
            environment.allowed_sha256,
        ))
        identity_suffix = hashlib.sha256(identity_material.encode()).hexdigest()[:12]
        unit_prefix = f"mikrofix-supervisor-{safe_id}-{identity_suffix}"
        self.executor_unit = f"{unit_prefix}-executor"
        self.bridge_unit = f"{unit_prefix}-bridge"
        self.logs_dir = environment.run_dir / "logs"
        self.lifecycle_lock_path = environment.run_dir / "lifecycle.lock"

    @contextmanager
    def lifecycle_lock(self):
        self.lifecycle_lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.lifecycle_lock_path, flags, 0o600)
        handle = os.fdopen(fd, "a+")
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            handle.close()
            raise RuntimeError("Lifecycle lock must be a regular non-symlink file")
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _metadata(self) -> dict[str, Any]:
        data = json.loads(self.environment.metadata_file.read_text(encoding="utf-8"))
        server_python = str(self.environment.source_root / ".venv-server/bin/python")
        expected = {
            "executor": {
                "argv": [server_python, "-m", "uvicorn", "tools.local_executor.server:app",
                         "--host", "127.0.0.1", "--port",
                         self.environment.executor_url.rsplit(":", 1)[-1]],
                "cwd": str(self.environment.source_root),
                "env": self.environment.executor_env(),
            },
            "bridge": {
                "argv": [server_python,
                         str(self.environment.source_root / "tools/mikrofix_bridge.py"),
                         "monitor"],
                "cwd": str(self.environment.worktree),
                "env": self.environment.bridge_env(),
            },
        }
        for role, required in expected.items():
            actual = data.get(role)
            if not isinstance(actual, dict):
                raise RuntimeError(f"Immutable metadata is missing {role}")
            if actual.get("argv") != required["argv"] or actual.get("cwd") != required["cwd"]:
                raise RuntimeError(f"Immutable metadata has untrusted {role} command")
            if actual.get("env") != required["env"]:
                raise RuntimeError(f"Immutable metadata has untrusted {role} environment")
            allowed = self.EXECUTOR_ENV_KEYS if role == "executor" else self.BRIDGE_ENV_KEYS
            if set(actual["env"]) != allowed:
                raise RuntimeError(f"Immutable metadata has non-allowlisted {role} environment")
        return data

    def _run(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return self.runner.run(args, timeout=timeout)

    def _is_active(self, unit: str) -> bool:
        result = self._run(["systemctl", "--user", "is-active", unit])
        return result.returncode == 0 and result.stdout.strip() == "active"

    def _unit_identity_ok(self, unit: str, role: str) -> bool:
        definition = self._metadata()[role]
        cwd = self._run([
            "systemctl", "--user", "show", unit,
            "--property=WorkingDirectory", "--value",
        ])
        environment = self._run([
            "systemctl", "--user", "show", unit,
            "--property=Environment", "--value",
        ])
        command = self._run([
            "systemctl", "--user", "show", unit,
            "--property=ExecStart", "--value",
        ])
        if any(result.returncode != 0 for result in (cwd, environment, command)):
            return False
        try:
            actual_env = set(shlex.split(environment.stdout.strip()))
        except ValueError:
            return False
        expected_env = {f"{key}={value}" for key, value in definition["env"].items()}
        argv_match = re.search(r"(?:^| ; )argv\[\]=(.*?)(?: ;|$)", command.stdout.strip())
        if not argv_match:
            return False
        try:
            actual_argv = shlex.split(argv_match.group(1))
        except ValueError:
            return False
        return (
            cwd.stdout.strip() == definition["cwd"]
            and actual_env == expected_env
            and actual_argv == definition["argv"]
        )

    def _prepare_log(self, role: str) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.logs_dir, 0o700)
        log = self.logs_dir / f"{role}.log"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(log, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"{role} log is not a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        if log.is_symlink():
            raise RuntimeError(f"{role} log must not be a symlink")
        return log

    def _start_unit(self, unit: str, role: str, definition: dict[str, Any]) -> None:
        log = self._prepare_log(role)
        args = [
            "systemd-run", "--user", f"--unit={unit}", "--collect",
            "--property=Restart=on-failure",
            f"--property=StandardOutput=append:{log}",
            f"--property=StandardError=append:{log}",
            f"--working-directory={definition['cwd']}",
        ]
        args.extend(f"--setenv={key}={value}" for key, value in sorted(definition["env"].items()))
        args.extend(["--", *definition["argv"]])
        result = self._run(args)
        if result.returncode != 0:
            raise RuntimeError(f"Unable to start {role}: {result.stdout[-2000:]}")

    def _token(self) -> str:
        token_file = self.environment.executor_config_dir / "token"
        try:
            info = token_file.lstat()
        except OSError as exc:
            raise RuntimeError("Per-run executor token is not ready") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Per-run executor token must be a regular non-symlink file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("Per-run executor token permissions are not private")
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError("Per-run executor token is empty")
        return token

    def _health_ready(self) -> bool:
        self._token()
        health = self.probe(self.environment.executor_url)
        return health.get("status") == "ok" and health.get("project") == str(self.environment.worktree)

    def _wait_until(self, predicate: Callable[[], bool], description: str) -> None:
        deadline = self.clock() + self.startup_timeout
        last_error = ""
        while self.clock() < deadline:
            try:
                if predicate():
                    return
            except (OSError, RuntimeError, ValueError) as exc:
                last_error = str(exc)
            self.sleeper(0.25)
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"Timed out waiting for {description}{detail}")

    def status(self) -> dict[str, Any]:
        executor_active = self._is_active(self.executor_unit)
        bridge_active = self._is_active(self.bridge_unit)
        executor_identity = executor_active and self._unit_identity_ok(
            self.executor_unit, "executor"
        )
        bridge_identity = bridge_active and self._unit_identity_ok(
            self.bridge_unit, "bridge"
        )
        health_ready = False
        if executor_active and executor_identity:
            try:
                health_ready = self._health_ready()
            except (OSError, RuntimeError, ValueError):
                health_ready = False
        ready = (executor_active and bridge_active and executor_identity
                 and bridge_identity and health_ready)
        if ready:
            overall = "READY"
        elif not executor_active and not bridge_active:
            overall = "STOPPED"
        elif ((executor_active and not executor_identity)
              or (bridge_active and not bridge_identity)
              or (executor_active and not health_ready)):
            overall = "UNHEALTHY"
        else:
            overall = "PARTIAL"
        return {
            "status": overall,
            "executor_active": executor_active,
            "bridge_active": bridge_active,
            "executor_identity": executor_identity,
            "bridge_identity": bridge_identity,
            "health_ready": health_ready,
            "lease_active": self.lease_active(),
        }

    def start(self) -> dict[str, Any]:
        with self.lifecycle_lock():
            return self._start_locked()

    def _start_locked(self) -> dict[str, Any]:
        metadata = self._metadata()
        current = self.status()
        if current["status"] == "READY":
            return current
        try:
            if current["bridge_active"] and (
                not current["executor_active"] or not current["executor_identity"]
                or not current["health_ready"]
            ):
                self._run(["systemctl", "--user", "stop", self.bridge_unit])
                current["bridge_active"] = False
            if current["executor_active"] and (
                not current["executor_identity"] or not current["health_ready"]
            ):
                self._run(["systemctl", "--user", "stop", self.executor_unit])
                current["executor_active"] = False
            if not current["executor_active"]:
                self._start_unit(self.executor_unit, "executor", metadata["executor"])
            self._wait_until(self._health_ready, "private token and exact executor health")
            if not current["bridge_active"]:
                self._start_unit(self.bridge_unit, "bridge", metadata["bridge"])
            self._wait_until(lambda: self._is_active(self.bridge_unit), "bridge service")
            result = self.status()
            if result["status"] != "READY":
                raise RuntimeError("Workers did not reach READY")
            return result
        except BaseException:
            self._stop_locked()
            raise

    def _control_command(self, action: str) -> list[str]:
        return [
            str(self.environment.source_root / ".venv-server/bin/python"),
            str(self.environment.worktree / "tools/local_executor/control.py"),
            action, "--state-dir", str(self.environment.executor_state_dir),
        ]

    def lease_active(self) -> bool:
        try:
            lease = json.loads(self.environment.lease_file.read_text(encoding="utf-8"))
            return float(lease.get("expires_at", 0)) > time.time() and all(
                str(lease.get(key) or "") == value
                for key, value in self.environment.lease_identity().items()
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def grant_lease(self, minutes: int) -> None:
        with self.lifecycle_lock():
            if not 1 <= minutes <= 30:
                raise ValueError("minutes must be between 1 and 30")
            if self.status()["status"] != "READY":
                raise RuntimeError("Cannot grant lease: per-run workers are not READY")
            identity = self.environment.lease_identity()
            args = [
                *self._control_command("grant"), "--minutes", str(minutes),
                "--run-id", identity["run_id"],
                "--project-root", identity["project_root"],
                "--plan-sha256", identity["plan_sha256"],
                "--allowed-files-sha256", identity["allowed_files_sha256"],
            ]
            result = self._run(args, timeout=120)
            if result.returncode != 0 or not self.lease_active():
                self.revoke_lease()
                raise RuntimeError("Run-specific write lease was not granted or did not match")

    def revoke_lease(self) -> None:
        result = self._run(self._control_command("revoke"))
        if result.returncode != 0:
            self.environment.lease_file.unlink(missing_ok=True)

    def stop(self) -> dict[str, Any]:
        with self.lifecycle_lock():
            return self._stop_locked()

    def _stop_locked(self) -> dict[str, Any]:
        for unit in (self.bridge_unit, self.executor_unit):
            if self._is_active(unit):
                self._run(["systemctl", "--user", "stop", unit])
        self.revoke_lease()
        return self.status()


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


def is_control_plane_path(path: str) -> bool:
    return path in CONTROL_PLANE_FILES or path.startswith(CONTROL_PLANE_PREFIXES)


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    allowed_keys = {
        "run_id", "objective", "base_checkpoint", "allowed_files", "atoms",
        "max_retries_per_atom", "job_timeout_seconds", "final_gate",
        "constraints", "context_links", "acceptance_criteria",
        "owner_request",
    }
    unknown_keys = sorted(set(plan) - allowed_keys)
    if unknown_keys:
        raise ValueError("unknown plan fields: " + ", ".join(unknown_keys))
    required_strings = ("run_id", "objective", "base_checkpoint")
    for key in required_strings:
        if key not in plan:
            raise ValueError(f"missing required field: {key}")
        if not isinstance(plan[key], str):
            raise ValueError(f"{key} must be a string")
        if not plan[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", plan["run_id"]):
        raise ValueError("run_id may contain only letters, digits, hyphen and underscore")
    if "allowed_files" not in plan:
        raise ValueError("missing required field: allowed_files")
    allowed_raw = plan["allowed_files"]
    if not isinstance(allowed_raw, list):
        raise ValueError("allowed_files must be a list")
    if not allowed_raw:
        raise ValueError("allowed_files must be a non-empty list")
    allowed = [safe_relative_path(item) for item in allowed_raw]
    if len(set(allowed)) != len(allowed):
        raise ValueError("allowed_files entries must be unique")
    protected = sorted(path for path in allowed if is_control_plane_path(path))
    if protected:
        raise ValueError("allowed_files contains protected control-plane paths: "
                         + ", ".join(protected))
    if "atoms" not in plan:
        raise ValueError("missing required field: atoms")
    atoms_raw = plan["atoms"]
    if not isinstance(atoms_raw, list):
        raise ValueError("atoms must be a list")
    if not atoms_raw:
        raise ValueError("atoms must be a non-empty list")
    atoms: list[dict[str, Any]] = []
    atom_ids: set[str] = set()
    for raw in atoms_raw:
        if not isinstance(raw, dict):
            raise ValueError("each atom must be an object")
        atom_unknown = sorted(set(raw) - {"id", "task", "checks", "expected_files"})
        if atom_unknown:
            raise ValueError("unknown atom fields: " + ", ".join(atom_unknown))
        atom_id = raw.get("id")
        task = raw.get("task")
        if not isinstance(atom_id, str):
            raise ValueError("atom id must be a string")
        atom_id = atom_id.strip()
        if not atom_id:
            raise ValueError("atom id must be a non-empty string")
        if atom_id in atom_ids:
            raise ValueError("atom ids must be unique")
        if not isinstance(task, str):
            raise ValueError(f"atom {atom_id} task must be a string")
        if not task.strip():
            raise ValueError(f"atom {atom_id} task must be a non-empty string")
        checks = raw.get("checks", ["syntax_changed_python"])
        if not isinstance(checks, list):
            raise ValueError(f"atom {atom_id} checks must be a list")
        if not checks:
            raise ValueError(f"atom {atom_id} checks must be a non-empty list")
        if any(not isinstance(item, str) for item in checks):
            raise ValueError(f"atom {atom_id} checks entries must be strings")
        if len(set(checks)) != len(checks):
            raise ValueError(f"atom {atom_id} checks must be unique")
        unknown = sorted(set(checks) - set(CHECK_COMMANDS))
        if unknown:
            raise ValueError(f"atom {atom_id} has unknown checks: {', '.join(unknown)}")
        expected_raw = raw.get("expected_files", [])
        if not isinstance(expected_raw, list):
            raise ValueError(f"atom {atom_id} expected_files must be a list")
        expected = [safe_relative_path(item) for item in expected_raw]
        if len(set(expected)) != len(expected):
            raise ValueError(f"atom {atom_id} expected_files must be unique")
        outside = sorted(set(expected) - set(allowed))
        if outside:
            raise ValueError(f"atom {atom_id} expects files outside allowed_files: {outside}")
        atoms.append({"id": atom_id, "task": task.strip(), "checks": list(checks),
                      "expected_files": expected})
        atom_ids.add(atom_id)
    if "max_retries_per_atom" not in plan:
        raise ValueError("missing required field: max_retries_per_atom")
    retries = plan["max_retries_per_atom"]
    if isinstance(retries, bool) or not isinstance(retries, int):
        raise ValueError("max_retries_per_atom must be an integer")
    if not 1 <= retries <= MAX_RETRIES_LIMIT:
        raise ValueError(f"max_retries_per_atom must be between 1 and {MAX_RETRIES_LIMIT}")
    job_timeout = plan.get("job_timeout_seconds", DEFAULT_JOB_TIMEOUT_SECONDS)
    if isinstance(job_timeout, bool) or not isinstance(job_timeout, int):
        raise ValueError("job_timeout_seconds must be an integer")
    if not 30 <= job_timeout <= 7200:
        raise ValueError("job_timeout_seconds must be between 30 and 7200")
    if "final_gate" not in plan:
        raise ValueError("missing required field: final_gate")
    if plan["final_gate"] != Stage.AWAIT_CODEX_REVIEW.value:
        raise ValueError("final_gate must be AWAIT_CODEX_REVIEW")

    def required_string_list(key: str) -> list[str]:
        if key not in plan:
            raise ValueError(f"missing required field: {key}")
        value = plan[key]
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        if not value:
            raise ValueError(f"{key} must be a non-empty list")
        normalized = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"{key}[{index}] must be a string")
            if not item.strip():
                raise ValueError(f"{key}[{index}] must be non-empty")
            normalized.append(item.strip())
        return normalized

    constraints = required_string_list("constraints")
    acceptance = required_string_list("acceptance_criteria")
    owner_request = plan.get("owner_request", plan["objective"])
    if not isinstance(owner_request, str) or not owner_request.strip():
        raise ValueError("owner_request must be a non-empty string")

    if "context_links" not in plan:
        raise ValueError("missing required field: context_links")
    context_raw = plan["context_links"]
    if not isinstance(context_raw, dict):
        raise ValueError("context_links must be an object")
    context_unknown = sorted(set(context_raw) - {"canon", "code", "tests"})
    if context_unknown:
        raise ValueError("unknown context_links fields: " + ", ".join(context_unknown))
    context: dict[str, list[str]] = {}
    for key in ("canon", "code", "tests"):
        if key not in context_raw:
            raise ValueError(f"context_links missing required field: {key}")
        values = context_raw[key]
        if not isinstance(values, list):
            raise ValueError(f"context_links.{key} must be a list")
        if not values:
            raise ValueError(f"context_links.{key} must be a non-empty list")
        normalized = []
        for index, item in enumerate(values):
            try:
                normalized.append(safe_relative_path(item))
            except ValueError as exc:
                raise ValueError(
                    f"unsafe path in context_links.{key}[{index}]: {item!r}"
                ) from exc
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"context_links.{key} paths must be unique")
        context[key] = normalized

    return {
        "run_id": plan["run_id"].strip(),
        "objective": plan["objective"].strip(),
        "owner_request": owner_request.strip(),
        "base_checkpoint": plan["base_checkpoint"].strip(),
        "allowed_files": allowed,
        "atoms": atoms,
        "max_retries_per_atom": retries,
        "job_timeout_seconds": job_timeout,
        "final_gate": Stage.AWAIT_CODEX_REVIEW.value,
        "constraints": constraints,
        "context_links": context,
        "acceptance_criteria": acceptance,
    }


def initial_run(
    plan: dict[str, Any],
    snapshot: dict[str, str],
    environment: RunEnvironment | None = None,
    codex_thread_id: str | None = None,
) -> dict[str, Any]:
    run = {
        "schema_version": 1,
        "plan": validate_plan(plan),
        "display_name": plan["objective"].strip()[:120],
        "codex_thread_id": validate_thread_id(codex_thread_id),
        "stage": Stage.PLANNED.value,
        "atom_index": 0,
        "attempt": 0,
        "review_attempt": 0,
        "requires_new_change": False,
        "job_id": None,
        "job_dispatched_at": None,
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


def retry_or_block(
    run: dict[str, Any], reason: str, *, require_change: bool = True
) -> None:
    run["feedback"] = reason
    run["requires_new_change"] = require_change
    maximum = run["plan"]["max_retries_per_atom"]
    if run["attempt"] >= maximum:
        record(run, Stage.BLOCKED, f"Retry limit reached: {reason}")
    else:
        run["job_id"] = None
        run["job_dispatched_at"] = None
        record(run, Stage.PLANNED, f"Retry requested: {reason}")


def qwen_prompt(run: dict[str, Any]) -> str:
    plan = run["plan"]
    atom = plan["atoms"][run["atom_index"]]
    feedback = run.get("feedback") or "No earlier attempt."
    return (
        "=== LOCAL EXECUTION REQUIRED ===\n"
        f"RUN {plan['run_id']} / ATOM {atom['id']} / attempt {run['attempt'] + 1}\n"
        f"Objective: {plan['objective']}\n"
        f"Original owner request: {plan.get('owner_request', plan['objective'])}\n"
        f"Task: {atom['task']}\n"
        f"Allowed files (strict): {', '.join(plan['allowed_files'])}\n"
        f"Expected files: {', '.join(atom['expected_files']) or '(not specified)'}\n"
        f"Checks that will run: {', '.join(atom['checks'])}\n"
        f"Earlier gate feedback: {feedback}\n\n"
        f"constraints: {json.dumps(plan['constraints'], ensure_ascii=False)}\n"
        f"acceptance_criteria: {json.dumps(plan['acceptance_criteria'], ensure_ascii=False)}\n"
        f"base_checkpoint: {plan['base_checkpoint']}\n"
        "context_links:\n"
        f"  canon: {json.dumps(plan['context_links']['canon'], ensure_ascii=False)}\n"
        f"  code: {json.dumps(plan['context_links']['code'], ensure_ascii=False)}\n"
        f"  tests: {json.dumps(plan['context_links']['tests'], ensure_ascii=False)}\n"
        "Roles: owner — cilj in odobritev; executor — lokalni izvajalec v "
        "ločeni seji; local_reviewer — neodvisni pregledovalec v novi seji; "
        "Codex — končni tehnični pregled; Bridge — nadzorni most. Follow the "
        "persistent AGENTS.md rules loaded in the bridge system prompt. "
        "Make the smallest complete change. Do not modify any other file. "
        "Do not run git checkout, restore, reset, clean, commit, merge, push, "
        "or deploy. Use the project tools and report exactly what changed."
    )


class Supervisor:
    def __init__(
        self,
        state_file: Path,
        adapter: Adapter,
        *,
        completion_runner: Callable[..., Any] | None = None,
        completion_notify_runner: Callable[..., Any] | None = None,
    ):
        self.state_file = state_file
        self.adapter = adapter
        self.completion_runner = completion_runner
        self.completion_notify_runner = completion_notify_runner
        self.last_completion: dict[str, str] | None = None

    def notify_terminal(self, run: dict[str, Any]) -> dict[str, str] | None:
        if run.get("stage") not in TERMINAL_STAGES:
            return None
        paths = [str(self.state_file.resolve())]
        if run.get("checkpoint"):
            paths.append(str(Path(run["checkpoint"]).resolve()))
        hashes = {
            path: file_sha256(Path(path))
            for path in paths
            if Path(path).is_file()
        }
        paths = list(hashes)
        kwargs: dict[str, Any] = {}
        if self.completion_runner is not None:
            kwargs["runner"] = self.completion_runner
        kwargs["notify_runner"] = self.completion_notify_runner or subprocess.run
        outbox = CompletionOutbox(
            self.state_file.parent / "completion-outbox",
            thread_id=run.get("codex_thread_id"),
            **kwargs,
        )
        blocked = run["stage"] == Stage.BLOCKED.value
        accepted_count = len(run.get("completed_atoms", []))
        self.last_completion = outbox.publish({
            "job_id": run["plan"]["run_id"],
            "display_name": run.get("display_name") or run["plan"]["objective"][:120],
            "state": run["stage"],
            "summary": "Inspect the durable supervisor evidence.",
            "test_result": (
                f"NOT COMPLETE; {accepted_count} earlier atom(s) accepted."
                if blocked else
                f"PASS gates recorded for {accepted_count} accepted atom(s); "
                "exact commands and outputs are in the checkpoint manifest."
            ),
            "risks": (
                "Candidate is not merged, pushed, or deployed."
                if not blocked else "The blocked run may contain an unapproved partial change."
            ),
            "blocked_need": run.get("feedback", "")[:2048] if blocked else "",
            "evidence_paths": paths,
            "evidence_sha256": hashes,
        })
        return self.last_completion

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
            self.notify_terminal(run)
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
            run["job_dispatched_at"] = time.time()
            record(run, Stage.DISPATCHED_TO_QWEN,
                   f"Executor job dispatched: {run['job_id']}")
            self.save(run)
            return True
        if stage is Stage.DISPATCHED_TO_QWEN:
            status, result = self.adapter.qwen_result(str(run["job_id"]))
            if status in {"queued", "running"}:
                dispatched_at = run.get("job_dispatched_at")
                timeout = run["plan"]["job_timeout_seconds"]
                if not isinstance(dispatched_at, (int, float)):
                    run["feedback"] = "Missing dispatch timestamp for active executor job"
                    record(run, Stage.BLOCKED, "Cannot enforce executor job timeout")
                    self.save(run)
                    self.notify_terminal(run)
                    return True
                if time.time() - dispatched_at >= timeout:
                    run["feedback"] = (
                        f"Executor job {run['job_id']} remained {status} for at least "
                        f"{timeout} seconds; worker must be inspected before retry"
                    )
                    record(run, Stage.BLOCKED, "Executor job timeout reached")
                    self.save(run)
                    self.notify_terminal(run)
                    return True
                return False
            if status != "completed":
                retry_or_block(run, f"Executor job ended as {status}: {result}")
            else:
                run["qwen_result"] = result
                record(run, Stage.DIFF_VERIFICATION, "Executor job completed")
            self.save(run)
            if run["stage"] in TERMINAL_STAGES:
                self.notify_terminal(run)
            return True
        if stage is Stage.DIFF_VERIFICATION:
            current = self.adapter.snapshot()
            changed = changed_paths(run["dispatch_snapshot"], current)
            total_changed = changed_paths(run["initial_snapshot"], current)
            allowed = set(run["plan"]["allowed_files"])
            outside = sorted(total_changed - allowed)
            atom = run["plan"]["atoms"][run["atom_index"]]
            expected = set(atom["expected_files"])
            expected_changed = expected & total_changed
            missing = sorted(expected - total_changed)
            review_paths = changed | expected_changed
            if outside:
                run["feedback"] = f"Files outside allowed_files changed: {outside}"
                record(run, Stage.BLOCKED,
                       "Out-of-scope workspace mutation; manual inspection required")
            elif run.get("requires_new_change") and not changed:
                retry_or_block(
                    run,
                    "Executor made no new change after a substantive gate rejection",
                )
            elif not review_paths:
                retry_or_block(run, "No actual workspace change was detected")
            elif missing:
                retry_or_block(run, f"Expected files did not change: {missing}")
            else:
                run["diff"] = self.adapter.diff(sorted(review_paths))
                if not run["diff"].strip():
                    retry_or_block(run, "Changed files produced an empty diff artifact")
                else:
                    if not changed and expected_changed:
                        record(
                            run,
                            Stage.DIFF_VERIFICATION,
                            "Retry reused the existing bounded candidate",
                        )
                    record(run, Stage.LOCAL_TESTS,
                           f"Diff accepted for files: {', '.join(sorted(review_paths))}")
                    run["requires_new_change"] = False
            self.save(run)
            if run["stage"] in TERMINAL_STAGES:
                self.notify_terminal(run)
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
            if run["stage"] in TERMINAL_STAGES:
                self.notify_terminal(run)
            return True
        if stage is Stage.GPT_OSS_REVIEW:
            atom = run["plan"]["atoms"][run["atom_index"]]
            criteria = "\n".join(
                f"{index}. {criterion}"
                for index, criterion in enumerate(
                    run["plan"]["acceptance_criteria"], start=1
                )
            )
            prompt = (
                f"Review atom {atom['id']}: {atom['task']}.\n"
                f"Objective: {run['plan']['objective']}\n"
                f"Original owner request: {run['plan'].get('owner_request', run['plan']['objective'])}\n"
                f"Allowed files: {', '.join(run['plan']['allowed_files'])}\n"
                f"Expected files: {', '.join(atom['expected_files'])}\n"
                "For every numbered acceptance criterion below, emit PASS or FAIL "
                "with concrete evidence from the diff or tests. Reject any defect, "
                "scope drift, missing regression coverage, destructive behavior, "
                "or unsupported completion claim. The original owner request is "
                "binding even when a model-generated objective, atom, or later "
                "criterion is narrower. For its binding criterion, enumerate and "
                "check every explicit requirement separately; do not summarize it "
                "as a single general PASS. For every explicit requirement, include "
                "a verbatim quote from the candidate diff. A paraphrase, a generic "
                "statement about the whole file, or the model's own description is "
                "not candidate evidence; when no exact supporting quote exists, mark "
                "that requirement FAIL. When natural language quality is required, "
                "inspect every sentence for grammar, complete sentence structure, "
                "and unchanged modal meaning. Reject fragments, unnatural wording, "
                "or any sentence that weakens a permission or restriction. In "
                "particular, never treat permission to read or run tests as identical "
                "to permission to modify test files.\n"
                f"ACCEPTANCE CRITERIA:\n{criteria}\n"
                "End with exactly one final line: VERDICT: APPROVE or VERDICT: REJECT."
            )
            material = run["diff"] + "\n\nTEST EVIDENCE:\n" + run["test_evidence"]
            result = self.adapter.review(prompt, material)
            run["review_attempt"] = int(run.get("review_attempt", 0)) + 1
            run["review_evidence"] = result.evidence or result.summary
            if not result.ok:
                format_only = result.summary in {
                    "missing valid verdict", "ambiguous multiple verdicts"
                }
                if format_only and run["review_attempt"] < 2:
                    run["feedback"] = (
                        "Reviewer output format was invalid; repeating review without "
                        "redispatching the executor"
                    )
                    record(run, Stage.GPT_OSS_REVIEW, run["feedback"])
                elif format_only:
                    run["feedback"] = (
                        "Reviewer did not produce one valid verdict after two attempts"
                    )
                    record(run, Stage.BLOCKED, run["feedback"])
                else:
                    run["review_attempt"] = 0
                    retry_or_block(
                        run,
                        f"Local reviewer rejected the atom: {result.summary}",
                        require_change=True,
                    )
                self.save(run)
                if run["stage"] in TERMINAL_STAGES:
                    self.notify_terminal(run)
                return True
            run["review_attempt"] = 0
            run["feedback"] = ""
            run["completed_atoms"].append({
                "id": atom["id"], "attempts": run["attempt"],
                "tests": run["test_evidence"], "review": run["review_evidence"],
            })
            run["accepted_snapshot"] = self.adapter.snapshot()
            run["atom_index"] += 1
            if run["atom_index"] < len(run["plan"]["atoms"]):
                run.update({"attempt": 0, "job_id": None, "feedback": "",
                            "job_dispatched_at": None,
                            "qwen_result": "", "diff": "", "test_evidence": "",
                            "review_evidence": "", "dispatch_snapshot": None,
                            "review_attempt": 0, "requires_new_change": False})
                record(run, Stage.PLANNED, "Atom accepted; next atom planned")
            else:
                record(run, Stage.CHECKPOINT, "All atoms accepted")
            self.save(run)
            if run["stage"] in TERMINAL_STAGES:
                self.notify_terminal(run)
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
            self.notify_terminal(run)
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
        bwrap_path: Path = Path("/usr/bin/bwrap"),
    ):
        self.project_root = project_root.resolve()
        self.state_dir = state_dir.resolve()
        self.lease_file = lease_file.resolve() if lease_file else None
        self._bridge_env = dict(bridge_env or {})
        self.source_root = (source_root or project_root).resolve()
        self.expected_lease = dict(expected_lease or {})
        self.bwrap_path = bwrap_path

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
        return self._run([
            str(self.source_root / ".venv-server" / "bin" / "python"),
            str(self.source_root / "tools" / "mikrofix_bridge.py"),
            "dispatch", "--task", prompt,
        ], timeout=30, env=self._bridge_env).stdout.strip()

    def qwen_result(self, job_id: str) -> tuple[str, str]:
        raw = self._run([
            str(self.source_root / ".venv-server" / "bin" / "python"),
            str(self.source_root / "tools" / "mikrofix_bridge.py"),
            "status", job_id,
        ], timeout=30, env=self._bridge_env).stdout
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
            try:
                result = self._sandbox_check(command, timeout=900)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                outputs.append(f"{check_name}: sandbox failure: {exc}")
                return GateResult(False, f"{check_name} sandbox failed closed",
                                  "\n\n".join(outputs))
            outputs.append(f"$ {' '.join(command)}\n{result.stdout[-60000:]}")
            if result.returncode != 0:
                return GateResult(False, f"{check_name} exited {result.returncode}",
                                  "\n\n".join(outputs))
        return GateResult(True, "All approved checks passed", "\n\n".join(outputs))

    def _sandbox_check(
        self, command: list[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        bwrap = self.bwrap_path.resolve()
        if bwrap != Path("/usr/bin/bwrap") or not bwrap.is_file() or not os.access(bwrap, os.X_OK):
            raise RuntimeError("Required sandbox executable /usr/bin/bwrap is unavailable")
        venv = self.source_root / ".venv"
        server_venv = self.source_root / ".venv-server"
        for label, path in (("desktop venv", venv), ("server venv", server_venv)):
            if not path.is_dir():
                raise RuntimeError(f"Required {label} is unavailable: {path}")

        sandbox_root = self.state_dir / "sandboxes"
        sandbox_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(sandbox_root, 0o700)
        with tempfile.TemporaryDirectory(prefix="check-", dir=sandbox_root) as _temporary:
            translated = list(command)
            interpreter = Path(translated[0]).resolve()
            desktop_python = (venv / "bin" / "python").resolve()
            server_python = (server_venv / "bin" / "python").resolve()
            if interpreter == desktop_python:
                translated[0] = "/opt/venv/bin/python"
            elif interpreter == server_python:
                translated[0] = "/opt/venv-server/bin/python"
            else:
                raise RuntimeError("Check attempted to use an unapproved interpreter")

            args = [
                str(bwrap), "--die-with-parent", "--new-session", "--unshare-all",
                "--clearenv", "--ro-bind", "/usr", "/usr",
            ]
            for system_path in ("/bin", "/lib", "/lib64"):
                if Path(system_path).exists():
                    args.extend(["--ro-bind", system_path, system_path])
            args.extend([
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--dir", "/home", "--dir", "/home/sandbox", "--dir", "/opt",
                "--ro-bind", str(venv), "/opt/venv",
                "--ro-bind", str(server_venv), "/opt/venv-server",
                "--dir", "/work", "--overlay-src", str(self.project_root),
                "--tmp-overlay", "/work", "--chdir", "/work",
                "--setenv", "HOME", "/home/sandbox",
                "--setenv", "TMPDIR", "/tmp",
                "--setenv", "TMP", "/tmp",
                "--setenv", "TEMP", "/tmp",
                "--setenv", "PATH", "/opt/venv/bin:/opt/venv-server/bin:/usr/bin:/bin",
                "--setenv", "LANG", "C.UTF-8",
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--setenv", "PYTHONPYCACHEPREFIX", "/tmp/pycache",
                "--setenv", "PYTHONNOUSERSITE", "1",
                "--", *translated,
            ])
            return subprocess.run(
                args,
                cwd=self.project_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )

    def review(self, prompt: str, material: str) -> GateResult:
        artifacts = self.state_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="review-", suffix=".txt", dir=artifacts)
        path = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(material)
            config_name = self._bridge_env.get("MIKROFIX_BRIDGE_CONFIG")
            if config_name:
                config = json.loads(Path(config_name).read_text(encoding="utf-8"))
            else:
                config = {}
            result = self._run([
                str(self.source_root / ".venv-server" / "bin" / "python"),
                str(self.source_root / "tools" / "mikrofix_local_agent.py"),
                "--agent", "reviewer",
                "--task", prompt, "--material-file", str(path),
                "--num-ctx", str(config.get("reviewer_num_ctx", 32768)),
                "--max-tokens", str(config.get("reviewer_num_predict", 3072)),
                "--thinking", str(config.get("reviewer_think", "medium")),
                "--output", "Specific findings followed by exactly one final verdict line.",
                "--log-dir", str(self.state_dir / "reviews"),
                "--console-log", str(self.state_dir / "agent-console.log"),
            ], timeout=1200, check=False)
        finally:
            path.unlink(missing_ok=True)
        output = result.stdout.strip()
        verdicts = []
        for line in output.splitlines():
            match = re.fullmatch(
                r"\s*VERDICT\s*:\s*(APPROVE|REJECT)\s*",
                line,
                flags=re.IGNORECASE,
            )
            if match:
                verdicts.append(f"VERDICT: {match.group(1).upper()}")
        approved = result.returncode == 0 and verdicts == ["VERDICT: APPROVE"]
        unsupported_evidence = bool(re.search(
            r"\b(?:predpostav\w*|assum(?:e|ed|ing|ption)\w*)\b",
            output,
            flags=re.IGNORECASE,
        ))
        if approved and unsupported_evidence:
            approved = False
            summary = "review approval relied on an unsupported assumption"
            return GateResult(approved, summary, output[-60000:])
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
    parser.add_argument(
        "--repository-root", type=Path, default=PROJECT_ROOT,
        help="Agent-owned Git repository used only as the candidate baseline",
    )
    parser.add_argument(
        "--notify-codex", action="store_true",
        help="Explicitly bind CODEX_THREAD_ID; owner-only local completion is the default",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("plan", type=Path)
    init.add_argument("--state", type=Path)
    for name in ("step", "run", "resume", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--state", type=Path)
        if name in {"run", "resume"}:
            command.add_argument("--interval", type=float, default=5.0)
    workers = subparsers.add_parser("workers")
    workers.add_argument("action", choices=("start", "status", "stop"))
    workers.add_argument("--state", type=Path)
    grant = subparsers.add_parser("grant-lease")
    grant.add_argument("--minutes", type=int, required=True)
    grant.add_argument("--state", type=Path)
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


def load_run_environment(state_file: Path) -> RunEnvironment:
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    if "environment" not in raw:
        raise RuntimeError("Run has no isolated environment metadata")
    return RunEnvironment.from_dict(raw["environment"])


def require_resume_ready(manager: WorkerManager) -> dict[str, Any]:
    status = manager.status()
    if status["status"] != "READY":
        raise RuntimeError("Cannot resume: per-run workers are not READY")
    if not status["lease_active"]:
        raise RuntimeError("Cannot resume: bound run-specific lease is not active")
    return status


def _main(args: argparse.Namespace) -> int:
    state_dir = args.state_dir.expanduser().resolve()
    state_file = (args.state or state_dir / "active-run.json").expanduser().resolve()
    if args.command == "init":
        with state_lock(state_file):
            if state_file.exists():
                raise SystemExit(f"State already exists: {state_file}")
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            validated = validate_plan(plan)
            repository_root = args.repository_root.expanduser().resolve()
            source_adapter = LocalAdapter(repository_root, state_dir)
            source_adapter.verify_base_checkpoint(validated["base_checkpoint"])
            bridge_template = json.loads(DEFAULT_BRIDGE_CONFIG.read_text(encoding="utf-8"))
            environment = RunEnvironment.create(
                PROJECT_ROOT, state_dir, validated, bridge_template,
                repository_root=repository_root,
            )
            adapter = LocalAdapter(
                environment.worktree,
                state_dir,
                lease_file=environment.lease_file,
                bridge_env=environment.bridge_env(),
                source_root=PROJECT_ROOT,
                expected_lease=environment.lease_identity(),
            )
            run = initial_run(
                validated,
                adapter.snapshot(),
                environment,
                codex_thread_id=(
                    os.environ.get("CODEX_THREAD_ID")
                    if args.notify_codex else None
                ),
            )
            atomic_write_json(state_file, run)
            print(f"Initialized {run['plan']['run_id']} at {state_file}")
            print(f"Isolated worktree: {environment.worktree}")
            print(f"Launch metadata: {environment.metadata_file}")
            print(f"Run-specific lease: {environment.lease_file}")
            return 0
    if not state_file.is_file():
        print("No active supervisor run", file=sys.stderr)
        return 2
    environment = load_run_environment(state_file)
    manager = WorkerManager(environment)
    if args.command == "workers":
        if args.action == "start":
            result = manager.start()
        elif args.action == "stop":
            result = manager.stop()
        else:
            result = manager.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "grant-lease":
        manager.grant_lease(args.minutes)
        print("Run-specific write lease granted and verified.")
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
        if supervisor.last_completion and supervisor.last_completion["status"] == "queued_no_thread":
            event_id = supervisor.last_completion["event_id"]
            print(f"Codex thread was not bound; durable event: "
                  f"{state_file.parent / 'completion-outbox' / 'events' / (event_id + '.json')}")
        return 0
    require_resume_ready(manager)
    while True:
        with state_lock(state_file):
            supervisor = Supervisor(state_file, adapter)
            progressed = supervisor.step()
            stage = supervisor.load()["stage"]
        print(f"{utc_timestamp()} {stage}", flush=True)
        if supervisor.last_completion and supervisor.last_completion["status"] == "queued_no_thread":
            event_id = supervisor.last_completion["event_id"]
            print(f"Codex thread was not bound; durable event: "
                  f"{state_file.parent / 'completion-outbox' / 'events' / (event_id + '.json')}")
        if stage in TERMINAL_STAGES or stage == Stage.NEEDS_WRITE_LEASE.value:
            return 0
        if not progressed:
            time.sleep(max(0.2, args.interval))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with workspace_lock(args.state_dir):
        return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
