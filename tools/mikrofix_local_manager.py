#!/usr/bin/env python3
"""Owner-facing intake and GPT-OSS planning for supervised local work."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.request

try:
    from tools.mikrofix_supervisor import (
        PROJECT_ROOT, atomic_write_immutable_json, atomic_write_json,
        validate_plan,
    )
except ModuleNotFoundError:
    from mikrofix_supervisor import (  # type: ignore[no-redef]
        PROJECT_ROOT, atomic_write_immutable_json, atomic_write_json,
        validate_plan,
    )


DEFAULT_WORKSPACE = Path.home() / "MikroFIX_Agent_Workspace"
BRIDGE_TEMPLATE = Path.home() / ".local/share/mikrofix-bridge/config.json"
MODEL_PLAN_FIELDS = {
    "objective", "allowed_files", "atoms", "constraints", "context_links",
    "acceptance_criteria",
}
MAX_PLANNER_ATTEMPTS = 3


class ManagerError(RuntimeError):
    pass


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
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManagerError(f"Planner did not return valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ManagerError("Planner result must be one JSON object")
    unknown = sorted(set(value) - MODEL_PLAN_FIELDS)
    missing = sorted(MODEL_PLAN_FIELDS - set(value))
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
    atoms = value.get("atoms")
    if isinstance(atoms, list):
        for index, atom in enumerate(atoms):
            if isinstance(atom, dict) and not atom.get("checks"):
                errors.append(f"atoms[{index}].checks must be a non-empty list")
    context = value.get("context_links")
    if not isinstance(context, dict):
        errors.append("context_links must be an object with canon, code and tests lists")
    else:
        for section in ("canon", "code", "tests"):
            paths = context.get(section)
            if not isinstance(paths, list) or not paths:
                errors.append(f"context_links.{section} must be a non-empty list")
                continue
            for relative in paths:
                if not isinstance(relative, str) or not (repository / relative).is_file():
                    errors.append(f"context_links.{section} missing file: {relative}")
    return errors


def normalize_plan(raw: str, *, run_id: str, repository: Path) -> dict[str, Any]:
    model_plan = parse_model_json(raw)
    preliminary_errors = model_plan_errors(model_plan, repository)
    if preliminary_errors:
        raise ManagerError("; ".join(preliminary_errors))
    head = command(["git", "rev-parse", "HEAD"], cwd=repository, timeout=30)
    plan = {
        **model_plan,
        "run_id": run_id,
        "base_checkpoint": head,
        "max_retries_per_atom": 3,
        "job_timeout_seconds": 1800,
        "final_gate": "AWAIT_CODEX_REVIEW",
    }
    validated = validate_plan(plan)
    missing_context = sorted({
        relative
        for values in validated["context_links"].values()
        for relative in values
        if not (repository / relative).is_file()
    })
    if missing_context:
        raise ManagerError("Planner referenced missing context files: " + ", ".join(missing_context))
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
        "Pripravi izvedbeni načrt za naslednjo zahtevo lastnika. Pred odgovorom "
        "z orodji preglej dejanska pravila, povezano kodo in teste. Končni odgovor "
        "naj bo samo zahtevani JSON.\n\nZAHTEVA:\n" + task + correction
    )


def run_bridge_monitor(
    bridge: list[str], bridge_env: dict[str, str], bridge_home: Path,
    job_id: str,
) -> None:
    """Run the planner worker directly; the owner UI reads its durable log."""
    del bridge_home, job_id
    command(
        bridge + ["monitor", "--once"],
        cwd=PROJECT_ROOT,
        env=bridge_env,
        timeout=1800,
    )


def run_planner(task: str, *, job_dir: Path, repository: Path) -> str:
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    runtime = job_dir / "planner-runtime"
    bridge_home = runtime / "bridge"
    executor_config = runtime / "executor-config"
    executor_state = runtime / "executor-state"
    for path in (bridge_home, executor_config, executor_state):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    port = free_local_port()
    token_file = executor_config / "token"
    executor_url = f"http://127.0.0.1:{port}"
    bridge_config = bridge_home / "config.json"
    atomic_write_immutable_json(bridge_config, {
        "ollama_url": template["ollama_url"],
        "model": template.get("model", "gpt-oss:20b"),
        "executor_url": executor_url,
        "token_file": str(token_file),
        "project": str(repository),
        "system_prompt": str(PROJECT_ROOT / "docs/local_agents/supervisor/PLANNER_SYSTEM.md"),
        "num_ctx": int(template.get("num_ctx", 131072)),
        "max_steps": 60,
        "keep_alive": "0",
        # Planner jobs are internal stages. Only the final supervised run may
        # tell the owner that the task itself has finished.
        "completion_policy": "supervisor_only",
        "agent_role": "GPT-OSS — lokalni vodja načrtovanja",
    })
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
    bridge_env = {
        "MIKROFIX_BRIDGE_HOME": str(bridge_home),
        "MIKROFIX_BRIDGE_CONFIG": str(bridge_config),
        "CODEX_THREAD_ID": "",
    }
    bridge = [str(PROJECT_ROOT / ".venv-server/bin/python"),
              str(PROJECT_ROOT / "tools/mikrofix_bridge.py")]
    try:
        wait_for_executor(executor_url, token_file, process)
        last_error = ""
        for attempt in range(MAX_PLANNER_ATTEMPTS):
            job_id = command(
                bridge + ["dispatch", "--name", "Načrt lokalnega vodje",
                          "--task", planner_prompt(task, last_error)],
                cwd=PROJECT_ROOT, env=bridge_env, timeout=30,
            ).splitlines()[-1]
            run_bridge_monitor(bridge, bridge_env, bridge_home, job_id)
            raw = command(bridge + ["result", job_id], cwd=PROJECT_ROOT,
                          env=bridge_env, timeout=30)
            try:
                # Validate schema, paths and all semantic gates while the
                # planner still has its one bounded correction opportunity.
                normalize_plan(raw, run_id=job_dir.name, repository=repository)
                return raw
            except (ManagerError, ValueError) as exc:
                last_error = str(exc)
        raise ManagerError(
            f"Planner remained invalid after {MAX_PLANNER_ATTEMPTS} attempts: "
            + last_error
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


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
           parent_run_id: str | None = None) -> dict[str, Any]:
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
    })
    state = {"schema_version": 1, "run_id": run_id, "state": "PLANNING"}
    atomic_write_json(job_dir / "manager.json", state)
    try:
        raw = generator(task, job_dir=job_dir, repository=repository)
        plan = normalize_plan(raw, run_id=run_id, repository=repository)
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
    except Exception as exc:
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
    config = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    repository = Path(config["repository"]).resolve()
    supervisor_state = Path(state["supervisor_state"]).resolve()
    base = [
        sys.executable, str(PROJECT_ROOT / "tools/mikrofix_supervisor.py"),
        "--state-dir", str(workspace), "--repository-root", str(repository),
    ]
    state["state"] = "WAITING_FOR_OWNER_LEASE"
    atomic_write_json(manager_file, state)
    workers_started = False
    try:
        command(base + ["workers", "start", "--state", str(supervisor_state)],
                cwd=PROJECT_ROOT, timeout=120)
        workers_started = True
        command(base + ["grant-lease", "--minutes", str(lease_minutes),
                        "--state", str(supervisor_state)],
                cwd=PROJECT_ROOT, timeout=360)
        state["state"] = "RUNNING_LOCAL"
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
    except Exception as exc:
        state["state"] = "BLOCKED"
        state["exact_reason"] = str(exc)
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
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("run_id")
    execute_parser.add_argument("--lease-minutes", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command_name == "submit":
            result = submit(
                args.task, workspace=args.workspace,
                parent_run_id=args.parent,
            )
        else:
            result = execute_run(
                args.run_id, workspace=args.workspace,
                lease_minutes=args.lease_minutes,
            )
    except Exception as exc:
        print(json.dumps({"state": "BLOCKED", "exact_reason": str(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
