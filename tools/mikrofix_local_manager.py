#!/usr/bin/env python3
"""Owner-facing intake and GPT-OSS planning for supervised local work."""

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
from typing import Any, Callable
import urllib.request

try:
    from tools.mikrofix_codex_review import (
        prepare_review, record_codex_approved, record_codex_rejected,
    )
    from tools.mikrofix_lead_session import (
        PLANNING_SOURCES, ensure_context_ready, ensure_conversation_session,
    )
    from tools.mikrofix_supervisor import (
        PROJECT_ROOT, atomic_write_immutable_json, atomic_write_json,
        validate_plan,
    )
    from tools.mikrofix_task_lifecycle import (
        TaskState, build_profile_snapshot, initialize_task_lifecycle,
        load_profile_registry, read_profile_snapshot, read_task_state,
        read_system_prompt_snapshot, record_task_event, resolve_ollama_digest,
        sha256_text, transition_task, write_profile_snapshot,
        write_system_prompt_snapshot,
    )
except ModuleNotFoundError:
    from mikrofix_codex_review import (  # type: ignore[no-redef]
        prepare_review, record_codex_approved, record_codex_rejected,
    )
    from mikrofix_lead_session import (  # type: ignore[no-redef]
        PLANNING_SOURCES, ensure_context_ready, ensure_conversation_session,
    )
    from mikrofix_supervisor import (  # type: ignore[no-redef]
        PROJECT_ROOT, atomic_write_immutable_json, atomic_write_json,
        validate_plan,
    )
    from mikrofix_task_lifecycle import (  # type: ignore[no-redef]
        TaskState, build_profile_snapshot, initialize_task_lifecycle,
        load_profile_registry, read_profile_snapshot, read_task_state,
        read_system_prompt_snapshot, record_task_event, resolve_ollama_digest,
        sha256_text, transition_task, write_profile_snapshot,
        write_system_prompt_snapshot,
    )


DEFAULT_WORKSPACE = Path.home() / "MikroFIX_Agent_Workspace"
BRIDGE_TEMPLATE = Path.home() / ".local/share/mikrofix-bridge/config.json"
PROFILE_REGISTRY = PROJECT_ROOT / "config/local_agents/lead_profiles.json"
PROPOSAL_SCHEMA = (
    PROJECT_ROOT / "docs/local_agents/supervisor/PLAN_PROPOSAL_SCHEMA.json"
)
MODEL_PLAN_FIELDS = {
    "objective", "allowed_files", "atoms", "constraints", "acceptance_criteria",
    # Accepted during migration but ignored; the manager computes trusted context.
    "context_links",
}
REQUIRED_MODEL_PLAN_FIELDS = MODEL_PLAN_FIELDS - {"context_links"}
MAX_PLANNER_ATTEMPTS = 3
BRIDGE_TOOL_NAMES = {
    "project_status", "project_read", "project_list", "project_search",
    "project_diff", "project_apply_patch", "project_create", "project_replace",
    "project_run_check", "system_command", "desktop_inspect", "desktop_action",
}
QWEN_PLANNER_MODEL = "qwen3:4b-thinking-2507-q8_0"
QWEN_PLANNER_SYSTEM = (
    PROJECT_ROOT / "docs/local_agents/supervisor/QWEN_PLANNER_SYSTEM.md"
)
GPT_VALIDATOR_SYSTEM = (
    PROJECT_ROOT / "docs/local_agents/supervisor/PLANNER_SYSTEM.md"
)
LEAD_CONVERSATION_SYSTEM = (
    PROJECT_ROOT / "docs/local_agents/supervisor/LEAD_CONVERSATION_SYSTEM.md"
)
MAX_CONVERSATION_TURNS = 40


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
            "reply", "routing", "ready_task", "objective", "allowed_files",
            "atoms", "constraints", "acceptance_criteria",
        ],
        "properties": {
            "reply": {"type": "string", "minLength": 1},
            "routing": {
                "type": "string",
                "enum": ["NEEDS_INFO", "READY_FOR_PROCESSING"],
            },
            "ready_task": {"type": "string"},
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
        },
    }


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


def snapshot_for_new_task(
    lead_profile: str, *,
    digest_resolver: Callable[[str, str], str] = resolve_ollama_digest,
) -> dict[str, Any]:
    profiles = enabled_lead_profiles()
    if lead_profile not in profiles:
        raise ManagerError(f"Unknown or disabled lead profile: {lead_profile}")
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    profile = profiles[lead_profile]
    digest = digest_resolver(str(template["ollama_url"]), str(profile["model"]))
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
    task: str, qwen_proposal: str, memory_hash: str, previous_error: str = ""
) -> str:
    correction = (
        "\nPREJŠNJA POTRDITEV JE BILA ZAVRNJENA: " + previous_error
        if previous_error else ""
    )
    return (
        "Kot konfigurirani neodvisni validator preveri predlog pogovornega "
        "svetovalca proti lastnikovi zahtevi in projektnemu spominu. "
        "Svetovalec ne odloča. "
        "Odpravi vse napake in vrni samo končni JSON načrt.\n\n"
        f"SKUPNI MEMORY SHA-256: {memory_hash}\n\n"
        f"ZAHTEVA LASTNIKA:\n{task}\n\nPREDLOG SVETOVALCA:\n{qwen_proposal}"
        + correction
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


def run_planner(
    task: str, *, job_dir: Path, repository: Path,
    advisor_proposal: dict[str, Any] | None = None,
    advisor_snapshot: dict[str, Any] | None = None,
) -> str:
    template = json.loads(BRIDGE_TEMPLATE.read_text(encoding="utf-8"))
    lead_home = Path.home() / ".local/share/mikrofix-bridge"
    gpt_model = str(template.get("model", "gpt-oss:20b"))
    gpt_num_ctx = int(template.get("num_ctx", 131072))
    gpt_keep_alive = str(template.get("lead_keep_alive", "30m"))
    lead = ensure_context_ready(
        PROJECT_ROOT, lead_home, ollama_url=str(template["ollama_url"]),
        model=gpt_model, num_ctx=gpt_num_ctx, keep_alive=gpt_keep_alive,
    )
    qwen_model = str(template.get("planning_advisor_model", QWEN_PLANNER_MODEL))
    qwen_num_ctx = int(template.get("planning_advisor_num_ctx", 32768))
    qwen_keep_alive = str(template.get("planning_advisor_keep_alive", "15m"))
    planning_home = lead_home / "planning-team"
    gpt_planner_lead = ensure_context_ready(
        PROJECT_ROOT, planning_home / "gpt-oss",
        ollama_url=str(template["ollama_url"]), model=gpt_model,
        num_ctx=min(gpt_num_ctx, 32768), keep_alive=gpt_keep_alive,
        sources=PLANNING_SOURCES, memory_name="SHARED_PLANNING_MEMORY.md",
    )
    advisor_memory: dict[str, object] | None = None
    if advisor_proposal is None:
        advisor_memory = ensure_context_ready(
            PROJECT_ROOT, planning_home / "advisor",
            ollama_url=str(template["ollama_url"]), model=qwen_model,
            num_ctx=qwen_num_ctx, keep_alive=qwen_keep_alive,
            sources=PLANNING_SOURCES, memory_name="SHARED_PLANNING_MEMORY.md",
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
            keep_alive=qwen_keep_alive, system_prompt=QWEN_PLANNER_SYSTEM,
            memory=advisor_memory, role="Svetovalec za razčlenjevanje", think=True,
            require_tool_use=False,
        )
    bridge_config = write_stage_config(
        bridge_home, model=gpt_model, num_ctx=gpt_num_ctx,
        keep_alive=gpt_keep_alive, system_prompt=GPT_VALIDATOR_SYSTEM,
        memory=gpt_planner_lead,
        role="Konfigurirani neodvisni potrjevalec načrta",
        think="low",
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
            ),
        )
        final_proposal = parse_model_json(final_raw)
        atomic_write_immutable_json(
            job_dir / "validator-approved-plan.json", final_proposal,
        )
        atomic_write_immutable_json(job_dir / "planning-handoff.json", {
            "schema_version": 1,
            "shared_memory_sha256": gpt_planner_lead["memory_sha256"],
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
) -> str:
    """Build one bounded continuation turn for one isolated persistent lead."""
    history = turns[-MAX_CONVERSATION_TURNS:]
    rendered_history = "\n\n".join(
        ("LASTNIK" if turn.get("role") == "owner" else "VODJA")
        + ":\n" + str(turn.get("content") or "").strip()
        for turn in history
        if str(turn.get("content") or "").strip()
    )
    mode_instruction = (
        "Lastnik je izrecno sprožil /prepare-plan. Formalni izvedbeni načrt "
        "pripravi samo, če so vzrok oziroma relevantne ugotovitve preverjene, "
        "možnosti obravnavane in lastnikova izbira dovolj jasna. Če odločitev "
        "ali dokaz manjka, vrni NEEDS_INFO in zahtevaj najmanjši manjkajoči "
        "podatek. To še ni dovoljenje za izvedbo. "
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
        "besedilo, ki ga vidi lastnik."
    ]
    if repository_snapshot.strip():
        sections.append(
            "TRENUTNO STANJE REPOZITORIJA:\n" + repository_snapshot.strip()
        )
    if rendered_history:
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


def run_lead_conversation(
    message: str, *, job_dir: Path, repository: Path,
    turns: list[dict[str, str]], lead_profile: str = "gpt_oss",
    lead_snapshot: dict[str, Any] | None = None,
    planning_requested: bool = False,
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
    atomic_write_immutable_json(config_path, {
        "ollama_url": template["ollama_url"],
        "model": model,
        "executor_url": executor_url,
        "token_file": str(token_file),
        "project": str(repository),
        "system_prompt": str(system_prompt),
        "project_memory": str(lead["memory_file"]),
        "parent_session_id": str(lead["session_id"]),
        "num_ctx": num_ctx,
        "max_steps": 40 if planning_requested else 12,
        "keep_alive": keep_alive,
        "think": think,
        "require_tool_use": True,
        "completion_policy": "supervisor_only",
        "response_format": conversation_response_schema(
            planning_requested=planning_requested,
        ),
        "agent_role": role,
    })
    executor_env = {
        "MIKROFIX_EXECUTOR_ROOT": str(repository),
        "MIKROFIX_EXECUTOR_CONFIG_DIR": str(executor_config),
        "MIKROFIX_EXECUTOR_STATE_DIR": str(executor_state),
        "PYTHONDONTWRITEBYTECODE": "1",
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
    }
    try:
        wait_for_executor(executor_url, token_file, process)
        prompt = lead_conversation_prompt(
            message, turns=turns,
            planning_requested=planning_requested,
            repository_snapshot=repository_orientation(repository),
        )
        bridge_job_id = command(
            bridge + ["dispatch", "--name", f"Neposredni pogovor: {role}",
                      "--task", prompt],
            cwd=PROJECT_ROOT, env=bridge_env, timeout=30,
        ).splitlines()[-1]
        run_bridge_monitor(bridge, bridge_env, bridge_home, bridge_job_id)
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


def talk(
    message: str, *, parent_run_id: str,
    workspace: Path = DEFAULT_WORKSPACE,
    lead_profile: str = "gpt_oss",
    responder: Callable[..., tuple[dict[str, Any], dict[str, str]]] = run_lead_conversation,
    planning_requested: bool = False,
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
        decision, evidence = responder(
            clean_message, job_dir=job_dir, repository=repository, turns=turns,
            lead_profile=lead_profile, lead_snapshot=snapshot,
            planning_requested=planning_requested,
        )
        reported_routing = str(decision.get("routing", "NEEDS_INFO"))
        routing = reported_routing if planning_requested else "NEEDS_INFO"
        reply = str(decision["reply"]).strip()
        if not planning_requested and reported_routing == "READY_FOR_PROCESSING":
            reply += (
                "\n\nIzvedbeni načrt ni bil ustvarjen. Ko želiš zaključiti pogovor "
                "in pripraviti načrt, uporabi /prepare-plan."
            )
        routed_run_id = ""
        event_hashes = {
            "prompt_hash": evidence.get("prompt_hash") or None,
            "context_pack_hash": evidence.get("memory_sha256") or None,
            "input_hash": sha256_text(clean_message),
            "output_hash": sha256_text(reply),
        }
        if routing == "READY_FOR_PROCESSING":
            ready_task = str(decision.get("ready_task") or "").strip()
            proposal = {
                key: decision[key]
                for key in (
                    "objective", "allowed_files", "atoms", "constraints",
                    "acceptance_criteria",
                )
            }
            if not ready_task:
                raise ManagerError("READY_FOR_PROCESSING requires ready_task")
            errors = model_plan_errors(proposal, repository)
            if errors:
                raise ManagerError(
                    "READY_FOR_PROCESSING plan is incomplete: " + "; ".join(errors)
                )
            lifecycle = read_task_state(job_dir)
            if lifecycle["state"] == TaskState.DRAFT.value:
                transition_task(
                    job_dir, TaskState.READY_FOR_PROCESSING,
                    snapshot=snapshot, actor_type="lead", actor_id=lead_profile,
                    event_type="LEAD_PROPOSED_PLAN", **event_hashes,
                )
            elif lifecycle["state"] != TaskState.READY_FOR_PROCESSING.value:
                raise ManagerError(
                    f"Conversation cannot route from task state {lifecycle['state']}"
                )
            routed = submit(
                ready_task, workspace=workspace,
                generator=lambda task, **kwargs: run_planner(
                    task, advisor_proposal=proposal,
                    advisor_snapshot=snapshot, **kwargs,
                ),
                initialize=True, parent_run_id=parent_run_id,
                lead_snapshot=snapshot,
            )
            routed_run_id = str(routed["run_id"])
            transition_task(
                job_dir, TaskState.AWAITING_OWNER_APPROVAL,
                snapshot=snapshot, actor_type="manager", actor_id="manager",
                output_hash=sha256_text(json.dumps(proposal, sort_keys=True)),
                details={"routed_run_id": routed_run_id},
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
        turns.extend([
            {"role": "owner", "content": clean_message, "at": stamp},
            {"role": "lead", "content": reply, "at": stamp,
             "lead_profile": lead_profile,
             "routing": routing, "routed_run_id": routed_run_id,
             "bridge_job_id": evidence.get("bridge_job_id", "")},
        ])
        conversation = {
            "schema_version": 1,
            "parent_run_id": parent_run_id,
            "session_id": evidence.get("session_id", ""),
            "memory_sha256": evidence.get("memory_sha256", ""),
            "lead_profile": lead_profile,
            "turns": turns,
            "active_run_id": routed_run_id or conversation.get("active_run_id", ""),
            "updated_at": stamp,
        }
        atomic_write_json(conversation_file, conversation)
        return conversation


def prepare_plan(
    parent_run_id: str, *, workspace: Path = DEFAULT_WORKSPACE,
    responder: Callable[..., tuple[dict[str, Any], dict[str, str]]] = run_lead_conversation,
) -> dict[str, Any]:
    """Explicitly allow one conversation root to propose, but not execute, a plan."""
    workspace = workspace.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parent_run_id):
        raise ManagerError("Unsafe parent run id")
    job_dir = workspace / "jobs" / parent_run_id
    snapshot = read_profile_snapshot(job_dir)
    lifecycle = read_task_state(job_dir)
    if lifecycle["state"] != TaskState.DRAFT.value:
        raise ManagerError(
            f"Plan can only be prepared from DRAFT, not {lifecycle['state']}"
        )
    conversation = json.loads(
        (job_dir / "conversation.json").read_text(encoding="utf-8")
    )
    if conversation.get("active_run_id"):
        raise ManagerError("Conversation already has an active execution plan")
    return talk(
        "/prepare-plan", parent_run_id=parent_run_id, workspace=workspace,
        lead_profile=str(snapshot["profile_id"]), responder=responder,
        planning_requested=True,
    )


def start_conversation(
    message: str, *, workspace: Path = DEFAULT_WORKSPACE,
    lead_profile: str = "gpt_oss",
    responder: Callable[..., tuple[dict[str, Any], dict[str, str]]] = run_lead_conversation,
    snapshot_factory: Callable[[str], dict[str, Any]] = snapshot_for_new_task,
) -> dict[str, Any]:
    """Create one owner-visible conversation root, then ask only its chosen lead."""
    workspace = workspace.expanduser().resolve()
    clean_message = message.strip()
    if not clean_message:
        raise ManagerError("Conversation message must not be empty")
    # Validate the workspace before creating a durable root.
    workspace_config = json.loads(
        (workspace / "workspace.json").read_text(encoding="utf-8")
    )
    repository = Path(workspace_config["repository"]).resolve()
    checkpoint = command(
        ["git", "rev-parse", "HEAD"], cwd=repository, timeout=30,
    ).strip()
    snapshot = snapshot_factory(lead_profile)
    if snapshot.get("profile_id") != lead_profile:
        raise ManagerError("Snapshot factory returned a different lead profile")
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
        "lead_profile": lead_profile, "turns": [], "active_run_id": "",
        "updated_at": stamp,
    })
    try:
        return talk(
            clean_message, parent_run_id=run_id, workspace=workspace,
            lead_profile=lead_profile, responder=responder,
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
        "--profile", choices=tuple(enabled_lead_profiles()), default="gpt_oss"
    )
    talk_parser = sub.add_parser("talk")
    talk_parser.add_argument("--message", required=True)
    talk_parser.add_argument("--parent", required=True)
    talk_parser.add_argument(
        "--profile", choices=tuple(enabled_lead_profiles()), default="gpt_oss"
    )
    plan_parser = sub.add_parser("prepare-plan")
    plan_parser.add_argument("run_id")
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


def main() -> int:
    args = parse_args()
    try:
        if args.command_name == "submit":
            result = submit(
                args.task, workspace=args.workspace,
                parent_run_id=args.parent,
            )
        elif args.command_name == "start":
            result = start_conversation(
                args.message, workspace=args.workspace, lead_profile=args.profile,
            )
        elif args.command_name == "talk":
            result = talk(
                args.message, parent_run_id=args.parent,
                workspace=args.workspace, lead_profile=args.profile,
            )
        elif args.command_name == "prepare-plan":
            result = prepare_plan(args.run_id, workspace=args.workspace)
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


if __name__ == "__main__":
    raise SystemExit(main())
