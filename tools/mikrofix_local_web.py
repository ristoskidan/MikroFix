#!/usr/bin/env python3
"""Local browser interface for the supervised MikroFix agent team."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import urllib.request

try:
    from tools.mikrofix_codex_review import (
        ReviewError, prepare_review, record_codex_approved,
        record_codex_rejected,
    )
    from tools.mikrofix_local_manager_ui import (
        DEFAULT_WORKSPACE,
        MANAGER,
        STATE_LABELS,
        list_jobs,
        planner_activity,
        read_json,
        render_job,
    )
    from tools.mikrofix_local_manager import (
        dispatch_plan, enabled_role_profiles, execute_run, installed_ollama_models,
        record_research_review, reject_plan, replace_conversation_goal,
    )
    from tools.mikrofix_owner_commands import (
        OwnerCommand, OwnerCommandError, help_text, parse_owner_command,
    )
    from tools.mikrofix_workspace_lock import workspace_lock
except ModuleNotFoundError:  # direct execution adds tools/, not the repo root
    from mikrofix_codex_review import (  # type: ignore[no-redef]
        ReviewError, prepare_review, record_codex_approved,
        record_codex_rejected,
    )
    from mikrofix_local_manager_ui import (  # type: ignore[no-redef]
        DEFAULT_WORKSPACE,
        MANAGER,
        STATE_LABELS,
        list_jobs,
        planner_activity,
        read_json,
        render_job,
    )
    from mikrofix_local_manager import (  # type: ignore[no-redef]
        dispatch_plan, enabled_role_profiles, execute_run, installed_ollama_models,
        record_research_review, reject_plan, replace_conversation_goal,
    )
    from mikrofix_owner_commands import (  # type: ignore[no-redef]
        OwnerCommand, OwnerCommandError, help_text, parse_owner_command,
    )
    from mikrofix_workspace_lock import workspace_lock  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTML_FILE = PROJECT_ROOT / "tools" / "mikrofix_local_manager_web.html"
HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"
MAX_BODY = 64_000
MAX_TRACE_BYTES = 1_000_000


def serialize_job(
    job: dict[str, Any], action_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective = action_job or job
    manager = effective["manager"]
    supervisor = {}
    supervisor_path = manager.get("supervisor_state")
    if supervisor_path:
        supervisor = read_json(Path(supervisor_path))
    review = read_json(job["directory"] / "codex-review.json")
    state = effective["state"]
    if review.get("status") == "REJECTED":
        state = "CODEX_REJECTED"
    elif review.get("status") == "APPLIED":
        state = "APPLIED"
    conversation = read_json(job["directory"] / "conversation.json")
    return {
        "run_id": job["run_id"],
        "task": job["task"],
        "parent_run_id": job.get("parent_run_id"),
        "created_at": job["created_at"],
        "state": state,
        "state_label": STATE_LABELS.get(state, state),
        "lead_profile": conversation.get(
            "lead_profile", job["manager"].get("lead_profile", "")
        ),
        "model_tag": conversation.get("model_tag", ""),
        "action_run_id": effective["run_id"] if action_job else "",
        "transcript": render_job(job),
        "activity": planner_activity(job["directory"], limit=20),
        "events": supervisor.get("events", [])[-20:],
        "checkpoint": manager.get("checkpoint") or supervisor.get("checkpoint"),
        "reason": manager.get("exact_reason") or supervisor.get("feedback", ""),
        "review": review,
    }


def jobs_payload(workspace: Path) -> dict[str, Any]:
    jobs = list_jobs(workspace)
    by_id = {job["run_id"]: job for job in jobs}
    routed_ids: set[str] = set()
    roots: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for job in jobs:
        conversation = read_json(job["directory"] / "conversation.json")
        action_id = str(conversation.get("active_run_id") or "")
        action_job = by_id.get(action_id)
        if action_job:
            routed_ids.add(action_id)
        if not job.get("parent_run_id"):
            roots.append((job, action_job))
    return {
        "jobs": [
            serialize_job(job, action_job)
            for job, action_job in roots
            if job["run_id"] not in routed_ids
        ]
    }


def _selected_job(workspace: Path, run_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Izberi veljaven obstoječi pogovor")
    roots = jobs_payload(workspace)["jobs"]
    selected = next((job for job in roots if job["run_id"] == run_id), None)
    if selected is None:
        raw = next(
            (job for job in list_jobs(workspace) if job["run_id"] == run_id), None,
        )
        parent = str((raw or {}).get("parent_run_id") or "")
        selected = next((job for job in roots if job["run_id"] == parent), None)
    if selected is None:
        raise ValueError("Izbrani pogovor ne obstaja")
    return selected


def allowed_owner_commands(job: dict[str, Any]) -> list[str]:
    allowed = ["/status", "/help", "/codex-opinion"]
    state = job["state"]
    if state == "CONVERSATION" and not job.get("action_run_id"):
        allowed.extend((
            "/prepare-plan", "/plan-dispatch", "/replace-goal",
            "/research-approved", "/research-revise", "/research-rejected",
        ))
    if state == "READY_FOR_OWNER_LEASE":
        allowed.extend(("/approve-plan", "/reject-plan"))
    if state == "READY_FOR_CODEX_REVIEW":
        allowed.append("/prepare-review")
    if (job.get("review") or {}).get("status") == "PREPARED":
        allowed.extend(("/codex-rejected", "/codex-approved"))
    return allowed


def _require_allowed(command: OwnerCommand, job: dict[str, Any]) -> None:
    if command.name not in allowed_owner_commands(job):
        raise ValueError(
            f"Ukaz {command.name} ni dovoljen v stanju {job['state']}. "
            f"Dovoljeni ukazi: {', '.join(allowed_owner_commands(job))}"
        )


def dispatch_owner_command(
    workspace: Path, run_id: str, message: str, *, wait: bool = False,
) -> dict[str, Any]:
    """Execute one allow-listed owner command without involving an LLM or shell."""
    command = parse_owner_command(message)
    if command is None:
        raise ValueError("Sporočilo ni ukaz")
    if command.name == "/help":
        return {"command": command.name, "message": help_text()}
    job = _selected_job(workspace, run_id)
    _require_allowed(command, job)
    action_run_id = str(job.get("action_run_id") or job["run_id"])
    if command.name == "/status":
        return {
            "command": command.name,
            "state": job["state"],
            "state_label": job["state_label"],
            "allowed_commands": allowed_owner_commands(job),
            "message": (
                f"Stanje: {job['state_label']}\nDovoljeni ukazi: "
                + ", ".join(allowed_owner_commands(job))
            ),
        }
    if command.name == "/prepare-plan":
        spawn_manager(workspace, ["prepare-plan", job["run_id"]])
        return {
            "command": command.name, "accepted": True,
            "message": "Lokalni vodja pripravlja izvedbeni načrt.",
        }
    if command.name in {
        "/research-approved", "/research-revise", "/research-rejected",
    }:
        parts = command.argument.split(None, 2)
        dossier_sha = parts[0] if parts else ""
        if not re.fullmatch(r"[0-9a-f]{64}", dossier_sha):
            raise ValueError("Raziskovalni verdict zahteva veljaven SHA-256 dosjeja")
        if command.name == "/research-approved":
            if len(parts) != 1:
                raise ValueError("/research-approved sprejme samo SHA-256 dosjeja")
            verdict, task_id, reason = "APPROVE", None, ""
        elif command.name == "/research-revise":
            if len(parts) != 3 or parts[1] not in {"R1", "R2", "R3"}:
                raise ValueError("/research-revise zahteva SHA-256, R1-R3 in razlog")
            verdict, task_id, reason = "REVISE", parts[1], parts[2]
        else:
            if len(parts) < 2:
                raise ValueError("/research-rejected zahteva SHA-256 in razlog")
            verdict, task_id, reason = "REJECT", None, command.argument.split(None, 1)[1]
        if verdict == "REVISE":
            record_research_review(
                job["run_id"], workspace=workspace, dossier_sha256=dossier_sha,
                verdict=verdict, research_task_id=task_id, reason=reason,
                execute_revision=False,
            )
            spawn_manager(workspace, [
                "research-revise", job["run_id"], "--dossier", dossier_sha,
                "--task", str(task_id), "--reason", reason,
            ])
        else:
            record_research_review(
                job["run_id"], workspace=workspace, dossier_sha256=dossier_sha,
                verdict=verdict, research_task_id=task_id, reason=reason,
            )
        return {
            "command": command.name, "accepted": True,
            "message": {
                "APPROVE": "Trenutni raziskovalni dosje je Codex potrdil; /prepare-plan je dovoljen.",
                "REVISE": "Zahtevana je nova ciljna revizija izbrane podnaloge.",
                "REJECT": "Raziskava je zavrnjena in blokirana.",
            }[verdict],
        }
    if command.name == "/plan-dispatch":
        if wait:
            conversation = dispatch_plan(job["run_id"], workspace=workspace)
            action_run_id = str(conversation.get("active_run_id") or "")
            if not action_run_id:
                raise ValueError("Predani načrt nima izvedbenega ID-ja")
            return {
                "command": command.name,
                "accepted": True,
                "state": "READY_FOR_OWNER_LEASE",
                "action_run_id": action_run_id,
                "message": (
                    "Načrt je neodvisno preverjen in pripravljen za izvedbo.\n"
                    f"Izvedbeni kandidat: {action_run_id}\n"
                    "Naslednji korak: /approve-plan"
                ),
            }
        spawn_manager(workspace, ["plan-dispatch", job["run_id"]])
        return {
            "command": command.name, "accepted": True,
            "message": "Zadnja prikazana revizija načrta se preverja in pošilja naprej.",
        }
    if command.name == "/replace-goal":
        replace_conversation_goal(
            job["run_id"], command.argument, workspace=workspace,
        )
        return {
            "command": command.name, "accepted": True,
            "message": "Glavni cilj pogovora je bil izrecno zamenjan.",
        }
    if command.name == "/reject-plan":
        reject_plan(job["run_id"], command.argument, workspace=workspace)
        return {
            "command": command.name, "accepted": True,
            "state": "CONVERSATION",
            "message": (
                "Načrt je zavrnjen in ohranjen v revizijski sledi. "
                "Pogovor je ponovno odprt; po dopolnitvah uporabi /prepare-plan."
            ),
        }
    if command.name == "/approve-plan":
        if wait:
            result = execute_run(
                action_run_id, workspace=workspace, lease_minutes=30,
            )
            state = str(result.get("state") or "BLOCKED")
            if state == "READY_FOR_CODEX_REVIEW":
                message_text = (
                    "Lokalna izvedba in neodvisni pregled sta končana.\n"
                    f"Izvedbeni kandidat: {action_run_id}\n"
                    "Naslednji korak: /prepare-review"
                )
            else:
                reason = str(result.get("exact_reason") or "Neznan razlog")
                message_text = (
                    "Lokalna izvedba potrebuje pozornost.\n"
                    f"Izvedbeni kandidat: {action_run_id}\n"
                    f"Stanje: {state}\nRazlog: {reason}"
                )
            return {
                "command": command.name,
                "accepted": True,
                "state": state,
                "action_run_id": action_run_id,
                "message": message_text,
            }
        spawn_manager(
            workspace, ["execute", action_run_id, "--lease-minutes", "30"],
        )
        return {
            "command": command.name, "accepted": True,
            "message": "Varna lokalna izvedba se zaganja.",
        }
    if command.name == "/prepare-review":
        result = prepare_review(workspace, action_run_id)
        return {"command": command.name, **result}
    if command.name == "/codex-rejected":
        result = record_codex_rejected(workspace, action_run_id, command.argument)
        return {"command": command.name, **result}
    if command.name == "/codex-approved":
        result = record_codex_approved(workspace, action_run_id, command.argument)
        return {"command": command.name, **result}
    if command.name == "/codex-opinion":
        question = command.argument or (
            "Povej svoje mnenje in predlagaj naslednji korak, vendar ničesar "
            "ne spreminjaj brez moje izrecne zahteve."
        )
        candidate = (
            f" Aktivni izvedbeni kandidat je {job['action_run_id']}."
            if job.get("action_run_id") else ""
        )
        clipboard = (
            f"Preglej pogovor lokalnega vodje pri nalogi {job['run_id']}."
            f"{candidate} {question}"
        )
        return {
            "command": command.name,
            "clipboard_text": clipboard,
            "message": "Vprašanje za Codex je pripravljeno za kopiranje.",
        }
    raise ValueError(f"Ukaz nima izvajalca: {command.name}")


def _read_trace_file(path: Path, limit: int = 200_000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    prefix = ""
    if len(data) > limit:
        data = data[-limit:]
        prefix = "[... starejši izpis te datoteke je skrajšan ...]\n"
    return prefix + data.decode("utf-8", errors="replace").strip()


def redact_trace(text: str) -> str:
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <skrito>", text)
    text = re.sub(
        r'(?i)("(?:password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)"\s*:\s*)"[^"]*"',
        r'\1"<skrito>"',
        text,
    )
    return text


def _structured_trace(path: Path) -> str:
    """Render each durable model/tool event as an owner-readable step."""
    raw = _read_trace_file(path, limit=600_000)
    if not raw:
        return ""
    rendered: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            rendered.append(line)
            continue
        kind = str(event.get("type") or "dogodek")
        step = event.get("step")
        role = str(event.get("role") or "lokalni agent")
        prefix = f"KORAK {step}" if step is not None else kind.upper()
        rendered.append(f"--- {prefix} · {role} · {kind} ---")
        if event.get("content"):
            rendered.append(str(event["content"]))
        if kind == "model" and not event.get("content"):
            rendered.append(
                f"Model je izbral {event.get('tool_call_count', 0)} klic(ev) orodij; "
                "vmesnega besedilnega odgovora ni vrnil."
            )
        if event.get("tool"):
            rendered.append(f"Orodje: {event['tool']}")
        for key, label in (("arguments", "Parametri"), ("result", "Rezultat")):
            if key in event:
                rendered.append(
                    label + ":\n" + json.dumps(
                        event[key], ensure_ascii=False, indent=2, default=str
                    )
                )
        if event.get("error"):
            rendered.append("Napaka: " + str(event["error"]))
        if event.get("note"):
            rendered.append("Opomba: " + str(event["note"]))
    return "\n".join(rendered)


def _bridge_trace_sections(root: Path, heading: str) -> list[str]:
    sections: list[str] = []
    for directory in sorted(root.glob("MF-*")):
        structured = _structured_trace(directory / "trace.jsonl")
        log = _read_trace_file(directory / "log.txt")
        result = _read_trace_file(directory / "result.txt")
        parts = []
        if structured:
            parts.append(structured)
        elif log:
            parts.append(log)
        if result:
            parts.append("KONČNI ODGOVOR:\n" + result)
        if parts:
            sections.append(f"=== {heading} · {directory.name} ===\n" + "\n\n".join(parts))
    return sections


def build_trace(workspace: Path, run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Neveljaven ID naloge")
    job_dir = (workspace / "jobs" / run_id).resolve()
    try:
        job_dir.relative_to((workspace / "jobs").resolve())
    except ValueError as exc:
        raise ValueError("Neveljavna pot naloge") from exc
    if not job_dir.is_dir():
        raise ValueError("Naloga ne obstaja")

    sections: list[str] = []
    conversation_jobs: list[dict[str, Any]] = []
    for turn_root in sorted(
        (job_dir / "conversation-runtime" / "turns").glob("turn-*")
    ):
        for bridge_job in sorted((turn_root / "bridge" / "jobs").glob("MF-*")):
            state = read_json(bridge_job / "job.json")
            if state:
                conversation_jobs.append(state)
        sections.extend(_bridge_trace_sections(
            turn_root / "bridge" / "jobs",
            "IZBRANI LOKALNI VODJA · POGOVOR IN PREMISLEK",
        ))
    sections.extend(_bridge_trace_sections(
        job_dir / "planner-runtime" / "qwen-bridge" / "jobs",
        "QWEN · PREDLOG NAČRTA",
    ))
    sections.extend(_bridge_trace_sections(
        job_dir / "planner-runtime" / "bridge" / "jobs",
        "GPT-OSS · POTRDITEV NAČRTA",
    ))

    manager = read_json(job_dir / "manager.json")
    supervisor_path = Path(
        manager.get("supervisor_state") or (job_dir / "supervisor.json")
    )
    supervisor = read_json(supervisor_path)
    events = supervisor.get("events") or []
    if events:
        event_lines = [
            f"[{event.get('at', '')}] {event.get('stage', '')}: {event.get('message', '')}"
            for event in events if isinstance(event, dict)
        ]
        sections.append("=== SUPERVISOR · ODLOČITVE ===\n" + "\n".join(event_lines))

    environment = supervisor.get("environment") or {}
    run_dir_raw = environment.get("run_dir")
    if run_dir_raw:
        run_dir = Path(str(run_dir_raw))
        sections.extend(_bridge_trace_sections(
            run_dir / "bridge" / "jobs",
            "GPT-OSS · IZVEDBA",
        ))

    if supervisor.get("test_evidence"):
        sections.append("=== LOKALNI TESTI ===\n" + str(supervisor["test_evidence"]))
    if supervisor.get("review_evidence"):
        sections.append("=== GPT-OSS · LOKALNI PREGLED ===\n" + str(supervisor["review_evidence"]))
    if supervisor:
        progress = f"Izvedbeni poskus: {supervisor.get('attempt', 0)}"
    elif conversation_jobs:
        latest = conversation_jobs[-1]
        progress = (
            f"Pogovorni korak: {latest.get('steps', 0)}; "
            f"dejanski klici orodij: {latest.get('successful_tool_calls', 0)}; "
            f"bridge: {latest.get('status', 'unknown')}"
        )
    else:
        progress = "Pogovorni korak: še ni začet"
    sections.append(
        "=== TRENUTNO STANJE ===\n"
        f"Stopnja: {supervisor.get('stage', manager.get('state', 'UNKNOWN'))}\n"
        f"{progress}\n"
        f"Razlog / povratna informacija: {supervisor.get('feedback') or manager.get('exact_reason') or '-'}"
    )
    conversation = read_json(job_dir / "conversation.json")
    action_run_id = str(conversation.get("active_run_id") or "")
    if (
        action_run_id and action_run_id != run_id
        and (workspace / "jobs" / action_run_id).is_dir()
    ):
        sections.append(
            "=== PREDANI IZVEDBENI KANDIDAT · " + action_run_id + " ===\n"
            + build_trace(workspace, action_run_id)
        )
    trace = redact_trace("\n\n".join(sections))
    if len(trace.encode("utf-8")) > MAX_TRACE_BYTES:
        encoded = trace.encode("utf-8")[-MAX_TRACE_BYTES:]
        trace = "[... najstarejši del celotnega izpisa je skrajšan ...]\n" + encoded.decode(
            "utf-8", errors="replace"
        )
    return trace


def spawn_manager(workspace: Path, arguments: list[str]) -> Path:
    reports = workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    log_path = reports / f"web-{int(time.time() * 1000)}.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(MANAGER), "--workspace", str(workspace), *arguments],
        cwd=PROJECT_ROOT,
        env={**os.environ, "MIKROFIX_VISIBLE_PLANNER": "0"},
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    threading.Thread(target=process.wait, daemon=True).start()
    log.close()
    return log_path


class LocalWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], workspace: Path):
        self.workspace = workspace.expanduser().resolve()
        super().__init__(address, LocalWebHandler)


class LocalWebHandler(BaseHTTPRequestHandler):
    server: LocalWebServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_bytes(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status: int, value: dict[str, Any]) -> None:
        self.send_bytes(
            status,
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/api/health":
            self.send_json(200, {"ok": True})
        elif parsed.path == "/api/jobs":
            self.send_json(200, jobs_payload(self.server.workspace))
        elif parsed.path == "/api/options":
            self.send_json(200, {
                "roles": [
                    {"id": role_id, "display_name": role["display_name"]}
                    for role_id, role in enabled_role_profiles().items()
                ],
                "models": installed_ollama_models(),
            })
        elif parsed.path == "/api/trace":
            try:
                run_id = (parse_qs(parsed.query).get("run_id") or [""])[0]
                self.send_json(200, {"run_id": run_id, "trace": build_trace(self.server.workspace, run_id)})
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
        else:
            self.send_json(404, {"error": "Not found"})

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or origin in {URL, f"http://localhost:{PORT}"}

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Neveljavna dolžina zahteve") from exc
        if length <= 0 or length > MAX_BODY:
            raise ValueError("Zahteva je prazna ali predolga")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Zahteva mora biti predmet")
        return value

    def do_POST(self) -> None:  # noqa: N802
        if not self._same_origin():
            self.send_json(403, {"error": "Zahteva ni iz lokalnega vmesnika"})
            return
        try:
            value = self._read_json()
            if self.path == "/api/submit":
                task = value.get("task")
                parent = value.get("parent_run_id")
                if not isinstance(task, str) or not task.strip() or len(task) > 12_000:
                    raise ValueError("Napiši veljavno nalogo")
                arguments = ["submit", "--task", task.strip()]
                if parent is not None:
                    if not isinstance(parent, str):
                        raise ValueError("Neveljavna povezava nadaljevanja")
                    arguments.extend(["--parent", parent])
                spawn_manager(self.server.workspace, arguments)
                self.send_json(202, {"accepted": True})
                return
            if self.path in {"/api/start", "/api/talk"}:
                message = value.get("message")
                profile = value.get("profile", "conversation_lead")
                model = value.get("model")
                if (
                    not isinstance(message, str) or not message.strip()
                    or len(message) > 12_000
                ):
                    raise ValueError("Napiši veljavno sporočilo")
                if profile not in enabled_role_profiles():
                    raise ValueError("Izberi veljaven profil lokalnega vodje")
                if self.path == "/api/start" and (
                    not isinstance(model, str)
                    or model not in installed_ollama_models()
                ):
                    raise ValueError("Izberi nameščen lokalni model")
                parsed_command = parse_owner_command(message)
                if self.path == "/api/start":
                    if parsed_command is None or parsed_command.name != "/research":
                        raise ValueError(
                            "Nova naloga se začne samo z veljavnim /research."
                        )
                    arguments = [
                        "research", "--envelope", parsed_command.argument,
                        "--profile", profile,
                        "--model", model,
                    ]
                else:
                    parent = value.get("parent_run_id")
                    if not isinstance(parent, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]+", parent
                    ):
                        raise ValueError("Izberi veljaven obstoječi pogovor")
                    if parsed_command is not None:
                        result = dispatch_owner_command(
                            self.server.workspace, parent, message,
                        )
                        self.send_json(
                            202 if result.get("accepted") else 200, result,
                        )
                        return
                    arguments = [
                        "talk", "--message", message.strip(), "--parent", parent,
                        "--profile", profile,
                    ]
                spawn_manager(self.server.workspace, arguments)
                self.send_json(202, {"accepted": True})
                return
            if self.path == "/api/command":
                run_id = value.get("run_id")
                command = value.get("command")
                if not isinstance(run_id, str) or not isinstance(command, str):
                    raise ValueError("Manjka pogovor ali ukaz")
                result = dispatch_owner_command(
                    self.server.workspace, run_id, command,
                )
                self.send_json(202 if result.get("accepted") else 200, result)
                return
            if self.path == "/api/prepare-plan":
                run_id = value.get("run_id")
                if not isinstance(run_id, str):
                    raise ValueError("Izberi veljaven pogovor")
                result = dispatch_owner_command(
                    self.server.workspace, run_id, "/prepare-plan",
                )
                self.send_json(202, result)
                return
            if self.path == "/api/prepare-review":
                run_id = value.get("run_id")
                if not isinstance(run_id, str):
                    raise ValueError("Izberi veljavno nalogo")
                self.send_json(200, dispatch_owner_command(
                    self.server.workspace, run_id, "/prepare-review",
                ))
                return
            if self.path == "/api/codex-rejected":
                run_id = value.get("run_id")
                feedback = value.get("feedback")
                if not isinstance(run_id, str) or not isinstance(feedback, str):
                    raise ValueError("Manjka naloga ali razlog zavrnitve")
                self.send_json(200, dispatch_owner_command(
                    self.server.workspace, run_id,
                    f"/codex-rejected {feedback}",
                ))
                return
            if self.path == "/api/codex-approved":
                run_id = value.get("run_id")
                commit = value.get("commit")
                if not isinstance(run_id, str) or not isinstance(commit, str):
                    raise ValueError("Manjka naloga ali commit SHA")
                self.send_json(200, dispatch_owner_command(
                    self.server.workspace, run_id,
                    f"/codex-approved {commit}",
                ))
                return
            if self.path == "/api/execute":
                run_id = value.get("run_id")
                if not isinstance(run_id, str):
                    raise ValueError("Izberi veljavno nalogo")
                result = dispatch_owner_command(
                    self.server.workspace, run_id, "/approve-plan",
                )
                self.send_json(202, result)
                return
            self.send_json(404, {"error": "Not found"})
        except (
            ValueError, OwnerCommandError, ReviewError, json.JSONDecodeError,
            OSError,
        ) as exc:
            self.send_json(400, {"error": str(exc)})


def server_healthy() -> bool:
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def activate_open_window() -> bool:
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return False
    try:
        search = subprocess.run(
            [xdotool, "search", "--name", "MikroFix.*lokalni vodja"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
        windows = [line.strip() for line in search.stdout.splitlines() if line.strip()]
        if search.returncode != 0 or not windows:
            return False
        window = windows[-1]
        subprocess.run(
            [xdotool, "windowraise", window],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=3, check=False,
        )
        focus = subprocess.run(
            [xdotool, "windowfocus", "--sync", window],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=3, check=False,
        )
        return focus.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_in_brave() -> int:
    if not server_healthy():
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not server_healthy():
            time.sleep(0.1)
    if not server_healthy():
        return 2
    if activate_open_window():
        return 0
    browser = shutil.which("brave-browser") or shutil.which("brave")
    command = [browser, f"--app={URL}"] if browser else ["xdg-open", URL]
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MikroFix local manager web interface")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--open", action="store_true")
    mode.add_argument("--serve", action="store_true")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--port", type=int, default=PORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.open:
        return open_in_brave()
    if not (args.workspace / "workspace.json").is_file():
        return 2
    with workspace_lock(args.workspace):
        server = LocalWebServer((HOST, args.port), args.workspace)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
