#!/usr/bin/env python3

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from tools.mikrofix_completion import (
        CompletionError,
        CompletionOutbox,
        validate_thread_id,
    )
except ModuleNotFoundError:  # direct execution adds tools/, not the repo root
    from mikrofix_completion import (  # type: ignore[no-redef]
        CompletionError,
        CompletionOutbox,
        validate_thread_id,
    )


BASE = Path(
    os.environ.get(
        "MIKROFIX_BRIDGE_HOME",
        str(Path.home() / ".local/share/mikrofix-bridge"),
    )
).expanduser().resolve()
CONFIG_FILE = Path(
    os.environ.get("MIKROFIX_BRIDGE_CONFIG", str(BASE / "config.json"))
).expanduser().resolve()
JOBS = BASE / "jobs"
LOGS = BASE / "logs"
COMPLETION_OUTBOX = BASE / "completion-outbox"


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_config():
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()

OLLAMA = CFG["ollama_url"]
MODEL = CFG["model"]
EXECUTOR = CFG["executor_url"].rstrip("/")
TOKEN_FILE = Path(CFG["token_file"])
PROJECT = Path(CFG["project"])
SYSTEM_PROMPT = Path(CFG["system_prompt"])
MAX_STEPS = int(CFG.get("max_steps", 30))
NUM_CTX = int(CFG.get("num_ctx", 65536))
KEEP_ALIVE = str(CFG.get("keep_alive", "30m"))
THINK = CFG.get("think")
REQUIRE_TOOL_USE = bool(CFG.get("require_tool_use", True))
REQUEST_TIMEOUT_SECONDS = int(CFG.get("request_timeout_seconds", 900))
MINIMUM_SUCCESSFUL_TOOL_CALLS = int(CFG.get("minimum_successful_tool_calls", 1))
MAX_TOOLLESS_RESPONSES = int(CFG.get("max_toolless_responses", 2))
RESPONSE_FORMAT = CFG.get("response_format")
AGENT_ROLE = str(CFG.get("agent_role", "lokalni agent"))
COMPLETION_POLICY = str(CFG.get("completion_policy", "per_job"))
PROJECT_MEMORY = Path(CFG["project_memory"]) if CFG.get("project_memory") else None
PARENT_SESSION_ID = str(CFG.get("parent_session_id", ""))

if not 30 <= REQUEST_TIMEOUT_SECONDS <= 1800:
    raise RuntimeError("request_timeout_seconds must be between 30 and 1800")
if not 1 <= MINIMUM_SUCCESSFUL_TOOL_CALLS <= MAX_STEPS:
    raise RuntimeError(
        "minimum_successful_tool_calls must be between 1 and max_steps"
    )
if not 1 <= MAX_TOOLLESS_RESPONSES <= MAX_STEPS:
    raise RuntimeError("max_toolless_responses must be between 1 and max_steps")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "project_status",
            "description": "Vrne trenutno stanje MikroFIX projekta.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_read",
            "description": "Prebere izbrani del datoteke v MikroFIX projektu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1
                    },
                    "end_line": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200000,
                        "default": 240
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_list",
            "description": "Izpiše datoteke in mape v MikroFIX projektu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "default": "."
                    },
                    "max_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "default": 200
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_search",
            "description": "Išče besedilo po datotekah MikroFIX projekta.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500
                    },
                    "path": {
                        "type": "string",
                        "default": "."
                    },
                    "file_glob": {
                        "type": ["string", "null"]
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 80
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_diff",
            "description": "Vrne trenutni git diff oziroma spremembe projekta.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_apply_patch",
            "description": "Nadzorovano uporabi unified diff patch na MikroFIX projektu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128000
                    },
                    "summary": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 300
                    }
                },
                "required": ["patch", "summary"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_create",
            "description": (
                "Varno ustvari eno novo besedilno datoteko na poti, ki jo je "
                "supervisor izrecno dovolil. Obstoječe datoteke ne prepiše."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "minLength": 1, "maxLength": 128000},
                    "summary": {"type": "string", "minLength": 3, "maxLength": 300}
                },
                "required": ["path", "content", "summary"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_replace",
            "description": "Varno zamenja en sam natancen besedilni blok v odobreni projektni datoteki.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string", "minLength": 1, "maxLength": 128000},
                    "new": {"type": "string", "maxLength": 128000},
                    "summary": {"type": "string", "minLength": 3, "maxLength": 300}
                },
                "required": ["path", "old", "new", "summary"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_run_check",
            "description": "Zažene enega od dovoljenih preverjevalnih profilov.",
            "parameters": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "enum": [
                            "local_agent_tests",
                            "desktop_unit_tests",
                            "central_api_tests",
                            "syntax_changed_python"
                        ]
                    }
                },
                "required": ["profile"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "system_command",
            "description": "Preko nadzorovanega MikroFIX executorja izvede dovoljen sistemski ukaz.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4000
                    },
                    "purpose": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 500
                    },
                    "working_directory": {
                        "type": "string",
                        "default": "."
                    },
                    "administrator": {
                        "type": "boolean",
                        "default": False
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 900,
                        "default": 300
                    }
                },
                "required": ["command", "purpose"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_inspect",
            "description": "Vrne podatke o trenutnem namizju in oknih.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_action",
            "description": "Izvede nadzorovano dejanje na lokalnem namizju.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "move_pointer",
                            "click",
                            "double_click",
                            "type_text",
                            "press_key",
                            "scroll",
                            "focus_window"
                        ]
                    },
                    "x": {"type": ["integer", "null"]},
                    "y": {"type": ["integer", "null"]},
                    "button": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "default": 1
                    },
                    "text": {
                        "type": ["string", "null"]
                    },
                    "key": {
                        "type": ["string", "null"]
                    },
                    "amount": {
                        "type": ["integer", "null"],
                        "minimum": -20,
                        "maximum": 20
                    },
                    "window_id": {
                        "type": ["integer", "null"],
                        "minimum": 1
                    }
                },
                "required": ["action"]
            }
        }
    }
]


TOOL_ENDPOINTS = {
    "project_status": ("GET", "/v1/project/status"),
    "project_read": ("POST", "/v1/project/read"),
    "project_list": ("POST", "/v1/project/list"),
    "project_search": ("POST", "/v1/project/search"),
    "project_diff": ("GET", "/v1/project/diff"),
    "project_apply_patch": ("POST", "/v1/project/apply-patch"),
    "project_create": ("POST", "/v1/project/create"),
    "project_replace": ("POST", "/v1/project/replace"),
    "project_run_check": ("POST", "/v1/project/run-check"),
    "system_command": ("POST", "/v1/system/command"),
    "desktop_inspect": ("GET", "/v1/desktop/inspect"),
    "desktop_action": ("POST", "/v1/desktop/action")
}


def atomic_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_text(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def request_json(url, method="GET", data=None, headers=None, timeout=900):
    hdrs = dict(headers or {})

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url,
        data=body,
        headers=hdrs,
        method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()

            if not raw:
                return {}

            text = raw.decode("utf-8")
            return json.loads(text)

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {e.code} za {url}: {body}"
        ) from e

    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Povezava ni uspela za {url}: {e}"
        ) from e


def token():
    return TOKEN_FILE.read_text(
        encoding="utf-8"
    ).strip()


def executor_request(method, path, data=None):
    return request_json(
        EXECUTOR + path,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {token()}"
        }
    )


def normalize_tool_arguments(name, arguments):
    """Repair harmless bounded model argument mistakes before executor validation."""
    normalized = dict(arguments) if isinstance(arguments, dict) else {}

    if name in {"project_list", "project_search"}:
        path = normalized.get("path")
        if path is None or (isinstance(path, str) and not path.strip()):
            normalized["path"] = "."

    limits = {
        "project_list": ("max_entries", 500),
        "project_search": ("max_results", 200),
    }
    limit = limits.get(name)
    if limit:
        key, maximum = limit
        value = normalized.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            normalized[key] = min(max(value, 1), maximum)

    return normalized


def execute_tool(name, arguments):
    if name not in TOOL_ENDPOINTS:
        raise RuntimeError(f"Neznano orodje: {name}")

    method, path = TOOL_ENDPOINTS[name]

    if method == "GET":
        return executor_request(method, path)

    return executor_request(
        method, path, normalize_tool_arguments(name, arguments)
    )


def call_qwen(messages, *, structured=False):
    """Call Ollama in either tool-use or final structured-output mode.

    Ollama models may treat ``tools`` and a strict JSON ``format`` as competing
    output contracts.  Keep the schema out of evidence-gathering calls and add
    it only after the required real tool calls have succeeded.
    """
    payload = {
            "model": MODEL,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"num_ctx": NUM_CTX},
            "messages": messages,
    }
    if THINK is not None:
        payload["think"] = THINK
    if structured and RESPONSE_FORMAT is not None:
        payload["format"] = RESPONSE_FORMAT
    else:
        payload["tools"] = TOOLS
    return request_json(
        OLLAMA,
        method="POST",
        data=payload,
        timeout=REQUEST_TIMEOUT_SECONDS
    )


def next_job_id():
    counter = BASE / "counter"

    with counter.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

        f.seek(0)
        raw = f.read().strip()

        n = int(raw) + 1 if raw else 1

        f.seek(0)
        f.truncate()
        f.write(str(n))
        f.flush()

        return f"MF-{datetime.now().year}-{n:04d}"


def job_dir(job_id):
    return JOBS / job_id


def job_file(job_id):
    return job_dir(job_id) / "job.json"


def load_job(job_id):
    p = job_file(job_id)

    if not p.exists():
        raise RuntimeError(f"Naloga {job_id} ne obstaja.")

    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_job(job):
    atomic_json(
        job_file(job["id"]),
        job
    )


def write_log(job_id, text):
    p = job_dir(job_id) / "log.txt"

    line = f"[{now()}] {text}"

    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

    print(line, flush=True)


def write_trace(job_id, event):
    """Append one structured owner-visible event without hidden reasoning."""
    payload = {"at": now(), **event}
    path = job_dir(job_id) / "trace.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def create_job(task, *, display_name=None, codex_thread_id=None):
    codex_thread_id = validate_thread_id(codex_thread_id)
    job_id = next_job_id()
    d = job_dir(job_id)
    d.mkdir(parents=True)

    job = {
        "id": job_id,
        "display_name": (
            (display_name or f"MikroFIX local task {job_id}").strip()[:120] or job_id
        ),
        "task": task,
        "codex_thread_id": codex_thread_id,
        "status": "queued",
        "created_at": now(),
        "started_at": None,
        "finished_at": None,
        "steps": 0,
        "result": None,
        "error": None
    }

    save_job(job)

    return job


def _evidence_payload(job):
    paths = []
    hashes = {}
    for name in ("job.json", "result.txt"):
        path = job_dir(job["id"]) / name
        if path.is_file():
            resolved = str(path.resolve())
            paths.append(resolved)
            hashes[resolved] = hashlib.sha256(path.read_bytes()).hexdigest()
    log_path = job_dir(job["id"]) / "log.txt"
    # The path is stable even before the first fallback line creates the log.
    paths.append(str(log_path.resolve()))
    return paths, hashes


def notify_job_completion(job, *, runner=None, notify_runner=None):
    """Persist one terminal event and attempt the bound thread wake at most once."""
    if job.get("status") not in {"completed", "failed"}:
        return None
    if COMPLETION_POLICY == "supervisor_only":
        return {"status": "suppressed_supervisor_only"}
    paths, hashes = _evidence_payload(job)
    kwargs = {}
    if runner is not None:
        kwargs["runner"] = runner
    kwargs["notify_runner"] = notify_runner or subprocess.run
    outbox = CompletionOutbox(
        COMPLETION_OUTBOX,
        thread_id=job.get("codex_thread_id"),
        **kwargs,
    )
    state = "COMPLETED" if job["status"] == "completed" else "FAILED"
    result = outbox.publish({
        "job_id": job["id"],
        "display_name": job.get("display_name") or job["id"],
        "state": state,
        "summary": "Inspect the durable bridge evidence.",
        "test_result": "No separate named-test result is stored; inspect bridge evidence.",
        "risks": "Local model output still requires Codex review.",
        "blocked_need": (
            "Inspect the persisted bridge error and decide the next bounded step."
            if state == "FAILED" else ""
        ),
        "evidence_paths": paths,
        "evidence_sha256": hashes,
    })
    if result["status"] == "queued_no_thread":
        write_log(
            job["id"],
            "Codex nit ni bila vezana ob dispatchu; trajni completion dogodek "
            f"je v {COMPLETION_OUTBOX / 'events' / (result['event_id'] + '.json')}.",
        )
    return result


def runtime_system_prompt():
    prompt = SYSTEM_PROMPT.read_text(
        encoding="utf-8"
    )

    addition = """
    
--- MIKROFIX BRIDGE RUNTIME ---

Deluješ preko lokalnega MikroFIX bridge izvajalca.

Ko potrebuješ dejanske podatke ali želiš nekaj spremeniti,
uporabi razpoložljiva orodja.

Ne trdi, da je bilo nekaj prebrano, spremenjeno ali preverjeno,
če za to nisi prejel rezultata ustreznega orodja.

Po spremembah uporabi primeren preverjevalni profil, kadar je to
smiselno.

Ko je naloga končana, podaj kratek in konkreten končni povzetek.
"""

    lead_rules = (PROJECT / "AGENTS.md").read_text(encoding="utf-8")
    inherited = ""
    if PROJECT_MEMORY is not None:
        inherited = (
            "\n\n--- INHERITED PARENT SESSION CONTEXT ---\n"
            f"parent_session_id: {PARENT_SESSION_ID}\n\n"
            + PROJECT_MEMORY.read_text(encoding="utf-8")
        )
    return (
        prompt + addition + "\n\n--- PERSISTENT LOCAL-LEAD RULES ---\n\n"
        + lead_rules + inherited
    )


def process_job(job_id):
    job = load_job(job_id)

    if job["status"] not in {"queued", "running"}:
        if job["status"] in {"completed", "failed"}:
            notify_job_completion(job)
        return

    job["status"] = "running"
    job["started_at"] = now()
    job["error"] = None
    save_job(job)

    write_log(
        job_id,
        f"Začenjam nalogo {job.get('display_name') or job_id} ({job_id}): {job['task']}",
    )
    write_trace(job_id, {
        "type": "request",
        "role": AGENT_ROLE,
        "content": job["task"],
    })

    messages = [
        {
            "role": "system",
            "content": runtime_system_prompt()
        },
        {
            "role": "user",
            "content": job["task"]
        }
    ]

    tool_use_count = 0
    successful_tool_use_count = 0
    tool_less_response_count = 0
    finalization_requested = False

    try:
        for step in range(1, MAX_STEPS + 1):
            job["steps"] = step
            save_job(job)

            write_log(
                job_id,
                f"Izvajalec korak {step}/{MAX_STEPS}"
            )

            structured = finalization_requested
            response = call_qwen(messages, structured=structured)

            message = response.get("message", {})
            tool_calls = message.get("tool_calls") or []
            write_trace(job_id, {
                "type": "model",
                "role": AGENT_ROLE,
                "step": step,
                "content": str(message.get("content") or "").strip(),
                "tool_call_count": len(tool_calls),
                "note": (
                    "Prikazani so dejanski odgovor, klici orodij in rezultati; "
                    "skrito notranje razmišljanje ni del pregledovalnika."
                ),
            })

            if not tool_calls:
                result = (
                    message.get("content") or ""
                ).strip()

                # Model ne sme zaključiti projektne naloge,
                # ne da bi dejansko uporabil vsaj eno orodje.
                if (
                    REQUIRE_TOOL_USE
                    and successful_tool_use_count < MINIMUM_SUCCESSFUL_TOOL_CALLS
                ):
                    tool_less_response_count += 1
                    if tool_less_response_count >= MAX_TOOLLESS_RESPONSES:
                        raise RuntimeError(
                            "Model ni vrnil strukturiranega klica projektnega "
                            "orodja; imen orodij v besedilu bridge ne izvaja."
                        )
                    write_log(
                        job_id,
                        "Model ni uporabil dejanskega orodja; zahtevam "
                        "strukturirani tool_calls odgovor."
                    )

                    messages.append(message)

                    messages.append({
                        "role": "user",
                        "content": (
                            "Ne zaključuj še. Naloga zahteva dejanski pregled projekta. "
                            f"Vsaj {MINIMUM_SUCCESSFUL_TOOL_CALLS} dejanska "
                            "strukturirana orodna klica morata biti uspešna. Uporabi "
                            "razpoložljiva projektna orodja "
                            "(project_status, project_list, project_search, project_read "
                            "in po potrebi project_diff). Imena orodja ne napiši v "
                            "reply in ne prosi lastnika za pot, ki jo lahko sam poiščeš. "
                            "Vrni pravi tool_calls odgovor, preveri dejansko stanje "
                            "in šele nato pripravi končni odgovor."
                        )
                    })

                    continue

                if RESPONSE_FORMAT is not None and not structured:
                    messages.append(message)
                    messages.append({
                        "role": "user",
                        "content": (
                            "Pregled z orodji je končan. Zdaj ne kliči več orodij. "
                            "Na podlagi že preverjenih rezultatov vrni samo končni "
                            "odgovor v zahtevani strukturirani obliki."
                        ),
                    })
                    finalization_requested = True
                    write_log(
                        job_id,
                        "Dokazi so zbrani; zahtevam ločen strukturirani končni odgovor."
                    )
                    continue

                if not result:
                    write_log(
                        job_id,
                        "Izvajalec je vrnil prazen končni odgovor; zahtevam besedni povzetek."
                    )

                    messages.append(message)

                    messages.append({
                        "role": "user",
                        "content": (
                            "Orodja si že uporabil. Zdaj ne kliči dodatnih orodij, "
                            "ampak napiši jasen končni odgovor uporabniku. "
                            "Povzemi konkretne ugotovitve, kaj si preveril in "
                            "kaj predlagaš kot naslednje korake. "
                            "Končni odgovor ne sme biti prazen."
                        )
                    })

                    continue

                atomic_text(job_dir(job_id) / "result.txt", result + "\n")
                job["status"] = "completed"
                job["finished_at"] = now()
                job["result"] = result
                save_job(job)

                write_log(job_id, "Naloga končana.")
                notify_job_completion(job)
                return

            tool_less_response_count = 0
            finalization_requested = False
            messages.append(message)

            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name")
                arguments = fn.get("arguments", {})

                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}

                tool_use_count += 1
                job["tool_calls"] = tool_use_count

                write_log(
                    job_id,
                    f"TOOL {name}: "
                    + json.dumps(
                        arguments,
                        ensure_ascii=False
                    )
                )
                write_trace(job_id, {
                    "type": "tool_call",
                    "role": AGENT_ROLE,
                    "step": step,
                    "tool": name,
                    "arguments": arguments,
                })

                try:
                    result = execute_tool(
                        name,
                        arguments
                    )

                    tool_result = {
                        "ok": True,
                        "result": result
                    }
                    successful_tool_use_count += 1
                    job["successful_tool_calls"] = successful_tool_use_count

                    write_log(
                        job_id,
                        f"TOOL {name}: OK"
                    )
                    write_trace(job_id, {
                        "type": "tool_result",
                        "role": AGENT_ROLE,
                        "step": step,
                        "tool": name,
                        "ok": True,
                        "result": result,
                    })

                except Exception as e:
                    tool_result = {
                        "ok": False,
                        "error": str(e)
                    }

                    write_log(
                        job_id,
                        f"TOOL {name}: ERROR: {e}"
                    )
                    write_trace(job_id, {
                        "type": "tool_result",
                        "role": AGENT_ROLE,
                        "step": step,
                        "tool": name,
                        "ok": False,
                        "error": str(e),
                    })

                messages.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": json.dumps(
                        tool_result,
                        ensure_ascii=False
                    )
                })
                save_job(job)

        raise RuntimeError(
            f"Dosežena omejitev {MAX_STEPS} korakov izvajalca."
        )

    except Exception as e:
        job = load_job(job_id)
        job["status"] = "failed"
        job["finished_at"] = now()
        job["error"] = str(e)
        save_job(job)

        write_log(
            job_id,
            f"Naloga je spodletela: {e}"
        )
        write_trace(job_id, {
            "type": "error",
            "role": AGENT_ROLE,
            "content": str(e),
        })
        notify_job_completion(job)


def queued_jobs():
    result = []

    for d in sorted(JOBS.iterdir()):
        if not d.is_dir():
            continue

        p = d / "job.json"

        if not p.exists():
            continue

        try:
            with p.open("r", encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            continue

        if job.get("status") == "queued":
            result.append(job["id"])

    return result


def recover_jobs():
    for d in JOBS.iterdir():
        if not d.is_dir():
            continue

        p = d / "job.json"

        if not p.exists():
            continue

        try:
            with p.open("r", encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            continue

        if job.get("status") == "running":
            job["status"] = "queued"
            job["error"] = None
            atomic_json(p, job)


def latest_job():
    jobs = []

    for d in JOBS.iterdir():
        p = d / "job.json"

        if not p.exists():
            continue

        try:
            with p.open("r", encoding="utf-8") as f:
                jobs.append(json.load(f))
        except Exception:
            pass

    if not jobs:
        return None

    jobs.sort(
        key=lambda j: j.get("created_at", ""),
        reverse=True
    )

    return jobs[0]


def render_monitor(job=None):
    # Clear screen + cursor home.
    print("\033[2J\033[H", end="")

    print("=" * 74)
    print("                    MikroFIX - lokalni agenti")
    print("=" * 74)
    print()
    print(f"Vloga:     {AGENT_ROLE}")
    print(f"Model:     {MODEL}")
    print(f"Ollama:    {OLLAMA}")
    print(f"Executor:  {EXECUTOR}")
    print()

    if not job:
        print("STATUS: čakam na nalogo")
        print()
        print("Pošlji nalogo z:")
        print('  bridge dispatch --task "..."')
        print()
        print("CTRL+C za izhod.")
        return

    print(f"NALOGA:    {job.get('display_name') or job.get('id')} ({job.get('id')})")
    print(f"STATUS:    {job.get('status')}")
    print(f"KORAK:     {job.get('steps', 0)}/{MAX_STEPS}")
    print(f"ZAČETEK:   {job.get('started_at') or '-'}")
    print()
    print("NAVODILO:")
    print(job.get("task", ""))
    print()

    log_file = job_dir(job["id"]) / "log.txt"

    print("-" * 74)
    print("DOGAJANJE")
    print("-" * 74)

    if log_file.exists():
        try:
            lines = log_file.read_text(
                encoding="utf-8",
                errors="replace"
            ).splitlines()

            for line in lines[-12:]:
                print(line)
        except Exception as e:
            print(f"Napaka pri branju loga: {e}")
    else:
        print("Čakam na prvi zapis ...")

    if job.get("status") == "completed":
        print()
        print("=" * 74)
        print("REZULTAT")
        print("=" * 74)
        print()
        print(job.get("result") or "(prazen rezultat)")
        print()
        print("=" * 74)

    elif job.get("status") == "failed":
        print()
        print("=" * 74)
        print("NAPAKA")
        print("=" * 74)
        print()
        print(job.get("error") or "(neznana napaka)")

    print()
    print("CTRL+C za izhod.")


def monitor(once=False):
    lock_path = BASE / "monitor.lock"
    lock = lock_path.open("w")

    try:
        fcntl.flock(
            lock.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB
        )
    except BlockingIOError:
        print("Monitor že teče.")
        return 1

    recover_jobs()

    worker = None
    active_job_id = None

    try:
        while True:
            # Če worker ne teče, poberi naslednjo queued nalogo.
            if worker is None or not worker.is_alive():
                if active_job_id:
                    try:
                        finished = load_job(active_job_id)
                    except Exception:
                        finished = None

                    # Pri --once ostani z rezultatom na zaslonu,
                    # nato končaj po kratkem zaključnem prikazu.
                    if (
                        once
                        and finished
                        and finished.get("status")
                        in {"completed", "failed"}
                    ):
                        render_monitor(finished)
                        return 0

                queue = queued_jobs()

                if queue:
                    active_job_id = queue[0]

                    worker = threading.Thread(
                        target=process_job,
                        args=(active_job_id,),
                        daemon=True
                    )

                    worker.start()

            # Prikaži trenutno aktivno ali zadnjo nalogo.
            job = None

            if active_job_id:
                try:
                    job = load_job(active_job_id)
                except Exception:
                    job = None

            if job is None:
                job = latest_job()

            render_monitor(job)

            time.sleep(1)

    except KeyboardInterrupt:
        print("\nMonitor zaprt.")
        return 0

def show_status(job_id=None):
    if job_id:
        job = load_job(job_id)

        print(json.dumps(
            job,
            ensure_ascii=False,
            indent=2
        ))

        return

    jobs = []

    for d in JOBS.iterdir():
        p = d / "job.json"

        if not p.exists():
            continue

        try:
            with p.open("r", encoding="utf-8") as f:
                jobs.append(json.load(f))
        except Exception:
            pass

    jobs.sort(
        key=lambda j: j.get("created_at", ""),
        reverse=True
    )

    for job in jobs[:20]:
        print(
            f"{job['id']:15} "
            f"{job['status']:10} "
            f"{job['created_at']}  "
            f"{(job.get('display_name') or job['task'])[:70]}"
        )


def show_result(job_id):
    job = load_job(job_id)

    if job["status"] == "completed":
        print(job.get("result") or "")
        return

    if job["status"] == "failed":
        print(
            f"Naloga {job_id} je spodletela:",
            file=sys.stderr
        )
        print(job.get("error") or "", file=sys.stderr)
        sys.exit(1)

    print(
        f"{job_id}: {job['status']}"
    )


def health():
    print("=== MikroFIX bridge health ===")

    checks = [
        ("Config", CONFIG_FILE.exists()),
        ("Token", TOKEN_FILE.exists()),
        ("Projekt", PROJECT.is_dir()),
        ("System prompt", SYSTEM_PROMPT.is_file())
    ]

    for name, ok in checks:
        print(
            f"{name:16}: {'OK' if ok else 'MANJKA'}"
        )

    if not all(ok for _, ok in checks):
        sys.exit(1)

    try:
        r = executor_request(
            "GET",
            "/health"
        )
        print("Executor API    : OK")
        print(
            "  "
            + json.dumps(
                r,
                ensure_ascii=False
            )[:300]
        )
    except Exception as e:
        print(f"Executor API    : NAPAKA - {e}")

    try:
        r = request_json(
            OLLAMA.rsplit("/api/chat", 1)[0]
            + "/api/version"
        )
        print(
            f"Ollama API      : OK - "
            f"{r.get('version', '?')}"
        )
    except Exception as e:
        print(f"Ollama API      : NAPAKA - {e}")


def main():
    JOBS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="MikroFIX lokalni agentni bridge"
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True
    )

    p = sub.add_parser("dispatch")
    p.add_argument(
        "--task",
        required=True
    )
    p.add_argument("--name")

    p = sub.add_parser("status")
    p.add_argument(
        "job_id",
        nargs="?"
    )

    p = sub.add_parser("result")
    p.add_argument("job_id")

    p = sub.add_parser("monitor")
    p.add_argument(
        "--once",
        action="store_true"
    )

    sub.add_parser("health")

    args = parser.parse_args()

    if args.command == "dispatch":
        try:
            job = create_job(
                args.task,
                display_name=args.name,
                codex_thread_id=os.environ.get("CODEX_THREAD_ID") or None,
            )
        except CompletionError as exc:
            parser.error(str(exc))

        # stdout is a machine-readable protocol consumed by LocalAdapter.
        print(job["id"])
        return

    if args.command == "status":
        show_status(args.job_id)
        return

    if args.command == "result":
        show_result(args.job_id)
        return

    if args.command == "monitor":
        sys.exit(monitor(args.once))

    if args.command == "health":
        health()
        return


if __name__ == "__main__":
    main()
