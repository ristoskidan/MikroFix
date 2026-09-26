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
    from tools.mikrofix_workspace_lock import workspace_lock
except ModuleNotFoundError:  # direct execution adds tools/, not the repo root
    from mikrofix_completion import (  # type: ignore[no-redef]
        CompletionError,
        CompletionOutbox,
        validate_thread_id,
    )
    from mikrofix_workspace_lock import workspace_lock  # type: ignore[no-redef]


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
RUNTIME_OPTIONS = {
    field: CFG[field]
    for field in (
        "num_predict", "temperature", "top_k", "top_p", "min_p",
        "repeat_penalty", "seed",
    )
    if CFG.get(field) is not None
}
KEEP_ALIVE = str(CFG.get("keep_alive", "30m"))
THINK = CFG.get("think")
REQUIRE_TOOL_USE = bool(CFG.get("require_tool_use", True))
REQUEST_TIMEOUT_SECONDS = int(CFG.get("request_timeout_seconds", 900))
MINIMUM_SUCCESSFUL_TOOL_CALLS = int(CFG.get("minimum_successful_tool_calls", 1))
MAX_TOOLLESS_RESPONSES = int(CFG.get("max_toolless_responses", 2))
DETERMINISTIC_REPLY_WRAPPER = bool(CFG.get("deterministic_reply_wrapper", False))
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
                            "syntax_changed_python",
                            "decimal_value_edges"
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

_configured_allowed_tools = CFG.get("allowed_tools")
if _configured_allowed_tools is None:
    ALLOWED_TOOL_NAMES = frozenset(
        tool["function"]["name"] for tool in TOOLS
    )
else:
    if (
        not isinstance(_configured_allowed_tools, list)
        or not _configured_allowed_tools
        or any(not isinstance(name, str) for name in _configured_allowed_tools)
        or len(set(_configured_allowed_tools)) != len(_configured_allowed_tools)
    ):
        raise RuntimeError("allowed_tools must be a non-empty unique string list")
    known_tool_names = {
        tool["function"]["name"] for tool in TOOLS
    }
    unknown_tool_names = sorted(set(_configured_allowed_tools) - known_tool_names)
    if unknown_tool_names:
        raise RuntimeError(
            "allowed_tools contains unknown tools: " + ", ".join(unknown_tool_names)
        )
    ALLOWED_TOOL_NAMES = frozenset(_configured_allowed_tools)
    TOOLS = [
        tool for tool in TOOLS
        if tool["function"]["name"] in ALLOWED_TOOL_NAMES
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


TOOL_EXAMPLES = {
    "project_status": {},
    "project_read": {"path": "mikrofix_server/operations.py", "start_line": 1, "end_line": 240},
    "project_list": {"path": "mikrofix_server", "max_entries": 200},
    "project_search": {
        "query": "reversal_of_invoice_uuid", "path": ".",
        "file_glob": "*.py", "max_results": 80,
    },
    "project_diff": {},
    "project_apply_patch": {
        "patch": "*** Begin Patch\n...\n*** End Patch",
        "summary": "Kratek opis dovoljene spremembe",
    },
    "project_create": {
        "path": "dovoljena/pot.txt", "content": "vsebina",
        "summary": "Ustvari dovoljeno datoteko",
    },
    "project_replace": {
        "path": "dovoljena/pot.py", "old": "stari blok", "new": "novi blok",
        "summary": "Zamenja en dovoljeni blok",
    },
    "project_run_check": {"profile": "local_agent_tests"},
    "system_command": {
        "command": "dovoljen ukaz", "purpose": "Namen ukaza",
    },
    "desktop_inspect": {},
    "desktop_action": {"action": "focus_window", "window_id": 1},
}


class ToolCallValidationError(RuntimeError):
    pass


def tool_definition(name):
    return next(
        (tool["function"] for tool in TOOLS if tool["function"]["name"] == name),
        None,
    )


def available_tool_guidance(preferred=None):
    """Return compact, session-specific usage guidance after a bad call."""
    names = sorted(ALLOWED_TOOL_NAMES)
    details = []
    ordered = ([preferred] if preferred in ALLOWED_TOOL_NAMES else []) + [
        name for name in names if name != preferred
    ]
    for name in ordered:
        definition = tool_definition(name)
        if definition is None:
            continue
        parameters = definition.get("parameters") or {}
        details.append({
            "name": name,
            "purpose": definition.get("description", ""),
            "required": parameters.get("required", []),
            "accepted_arguments": sorted((parameters.get("properties") or {}).keys()),
            "example": TOOL_EXAMPLES.get(name),
        })
    return {
        "message": (
            "Uporabi enega od dovoljenih strukturiranih orodnih klicev spodaj. "
            "`file_glob` filtrira IMENA DATOTEK (npr. `*.py`); iskani simbol "
            "ali besedilo vedno sodi v `query`."
        ),
        "available_tools": names,
        "use_them_as_follows": details,
    }


def _matches_json_type(value, expected):
    kinds = expected if isinstance(expected, list) else [expected]
    for kind in kinds:
        if kind == "null" and value is None:
            return True
        if kind == "string" and isinstance(value, str):
            return True
        if kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if kind == "boolean" and isinstance(value, bool):
            return True
        if kind == "object" and isinstance(value, dict):
            return True
    return False


def validate_tool_arguments(name, arguments):
    """Fail closed before contacting the executor and return normalized arguments."""
    if not isinstance(name, str) or not name:
        raise ToolCallValidationError("Ime orodja manjka ali ni besedilo.")
    if name not in ALLOWED_TOOL_NAMES:
        raise ToolCallValidationError(f"Orodje ni dovoljeno v tej seji: {name}")
    definition = tool_definition(name)
    if definition is None or name not in TOOL_ENDPOINTS:
        raise ToolCallValidationError(f"Neznano orodje: {name}")
    if not isinstance(arguments, dict):
        raise ToolCallValidationError("Argumenti orodja morajo biti JSON objekt.")
    normalized = normalize_tool_arguments(name, arguments)
    schema = definition.get("parameters") or {}
    properties = schema.get("properties") or {}
    unknown = sorted(set(normalized) - set(properties))
    if unknown:
        raise ToolCallValidationError("Neznani argumenti: " + ", ".join(unknown))
    missing = [key for key in schema.get("required", []) if key not in normalized]
    if missing:
        raise ToolCallValidationError("Manjkajo obvezni argumenti: " + ", ".join(missing))
    for key, value in normalized.items():
        rule = properties[key]
        if not _matches_json_type(value, rule.get("type")):
            raise ToolCallValidationError(f"Argument {key!r} ima napačen tip.")
        if "enum" in rule and value not in rule["enum"]:
            raise ToolCallValidationError(
                f"Argument {key!r} mora biti eden od: "
                + ", ".join(map(str, rule["enum"]))
            )
        if isinstance(value, str) and len(value) < int(rule.get("minLength", 0)):
            raise ToolCallValidationError(f"Argument {key!r} je prekratek.")
        if (
            isinstance(value, str)
            and "maxLength" in rule
            and len(value) > int(rule["maxLength"])
        ):
            raise ToolCallValidationError(f"Argument {key!r} je predolg.")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                raise ToolCallValidationError(f"Argument {key!r} je pod dovoljeno mejo.")
            if "maximum" in rule and value > rule["maximum"]:
                raise ToolCallValidationError(f"Argument {key!r} je nad dovoljeno mejo.")
    return normalized


def tool_failure_payload(name, error, *, code="TOOL_CALL_FAILED", extra=None):
    payload = {
        "ok": False,
        "error_code": code,
        "error": str(error),
        "guidance": available_tool_guidance(name),
    }
    if extra:
        payload.update(extra)
    return payload


def tool_fingerprint(name, arguments):
    return hashlib.sha256(json.dumps(
        {"tool": name, "arguments": arguments},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def canonical_tool_arguments(name, arguments):
    """Add schema defaults only for stable duplicate-call comparison."""
    normalized = normalize_tool_arguments(name, arguments)
    definition = tool_definition(name) or {}
    properties = (definition.get("parameters") or {}).get("properties") or {}
    for key, rule in properties.items():
        if key not in normalized and "default" in rule:
            normalized[key] = rule["default"]
    if name == "project_search" and "file_glob" not in normalized:
        normalized["file_glob"] = None
    return normalized


def suspicious_search_arguments(arguments):
    """Detect the common symbol-in-file_glob mistake without rejecting valid globs."""
    if not isinstance(arguments, dict):
        return False
    file_glob = arguments.get("file_glob")
    if not isinstance(file_glob, str) or not file_glob.strip():
        return False
    value = file_glob.strip()
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return False
    candidate = value.strip("*?[]").strip()
    query = query.strip()
    query_looks_like_file_glob = (
        query.startswith("*.")
        or query.startswith("**/")
    )
    file_glob_looks_like_symbol = (
        "." not in value and "/" not in value and "\\" not in value
        and not any(marker in value for marker in "*?[")
    )
    return (
        "." not in value and "/" not in value and "\\" not in value
        and bool(candidate)
        and (
            candidate == query
            or candidate in query
            or query in candidate
            or (query_looks_like_file_glob and file_glob_looks_like_symbol)
        )
    )


def search_result_count(result):
    if not isinstance(result, dict):
        return None
    count = result.get("count")
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def execute_tool(name, arguments):
    arguments = validate_tool_arguments(name, arguments)

    method, path = TOOL_ENDPOINTS[name]

    if method == "GET":
        return executor_request(method, path)

    return executor_request(
        method, path, arguments
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
            "options": {"num_ctx": NUM_CTX, **RUNTIME_OPTIONS},
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

Pri `project_search` je `query` iskani simbol ali besedilo, `file_glob` pa
izključno filter imen datotek, na primer `*.py`. Če bridge vrne
`TOOL_CALL_INVALID`, `SEARCH_ARGUMENT_HINT`, `DUPLICATE_CALL_WARNING` ali
`DUPLICATE_CALL_BLOCKED`, preberi priloženi `guidance` in spremeni strategijo.
Ne ponavljaj blokiranega klica.

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
    empty_final_response_count = 0
    finalization_requested = False
    tool_call_repeats = {}
    tool_result_cache = {}
    suspicious_empty_searches = 0
    suspicious_search_blocks = 0
    job.setdefault("tool_feedback_events", 0)
    job.setdefault("duplicate_tool_calls", 0)
    job.setdefault("blocked_tool_calls", 0)
    job["empty_final_responses"] = 0
    job.pop("error_code", None)

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
            trace_event = {
                "type": "model",
                "role": AGENT_ROLE,
                "step": step,
                "content": str(message.get("content") or "").strip(),
                "tool_call_count": len(tool_calls),
                "note": (
                    "Prikazani so dejanski odgovor, modelov vrnjeni premislek, "
                    "klici orodij in rezultati."
                ),
            }
            thinking = str(message.get("thinking") or "").strip()
            if thinking:
                trace_event["thinking"] = thinking
            write_trace(job_id, trace_event)

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

                    evidence_instruction = (
                        "Pred zaključkom uporabi ustrezno strukturirano projektno "
                        "orodje in preveri dejansko stanje."
                        if MINIMUM_SUCCESSFUL_TOOL_CALLS == 1 else
                        f"Pred zaključkom zberi najmanj "
                        f"{MINIMUM_SUCCESSFUL_TOOL_CALLS} uspešne strukturirane "
                        "projektne dokaze."
                    )
                    messages.append({
                        "role": "user",
                        "content": (
                            "Ne zaključuj še. Naloga zahteva dejanski pregled projekta. "
                            + evidence_instruction + " Uporabi "
                            "razpoložljiva projektna orodja "
                            "(project_status, project_list, project_search, project_read "
                            "in po potrebi project_diff). Imena orodja ne napiši v "
                            "reply in ne prosi lastnika za pot, ki jo lahko sam poiščeš. "
                            "Vrni pravi tool_calls odgovor, preveri dejansko stanje "
                            "in šele nato pripravi končni odgovor."
                        )
                    })

                    continue

                visible_reply = result
                if RESPONSE_FORMAT is not None and result:
                    try:
                        decoded = json.loads(result)
                        if (
                            isinstance(decoded, dict)
                            and isinstance(decoded.get("reply"), str)
                        ):
                            visible_reply = decoded["reply"].strip()
                    except json.JSONDecodeError:
                        pass

                if not visible_reply:
                    empty_final_response_count += 1
                    job["empty_final_responses"] = empty_final_response_count
                    save_job(job)
                    if empty_final_response_count >= MAX_TOOLLESS_RESPONSES:
                        job["error_code"] = "EMPTY_FINAL_RESPONSE"
                        save_job(job)
                        raise RuntimeError(
                            "EMPTY_FINAL_RESPONSE: model po zbranih dokazih "
                            f"{empty_final_response_count}-krat ni vrnil "
                            "nepraznega odgovora lastniku."
                        )
                    write_log(
                        job_id,
                        "Model je po zbranih dokazih vrnil samo premislek brez "
                        "vidnega odgovora; zahtevam neposreden odgovor lastniku."
                    )
                    messages.append(message)
                    messages.append({
                        "role": "user",
                        "content": (
                            "Dokazi so že zbrani. Ne kliči dodatnih orodij in ne "
                            "nadaljuj samo s premislekom. Zdaj vrni neposreden, "
                            "neprazen odgovor lastniku na podlagi že zbranih dokazov."
                        ),
                    })
                    continue

                if RESPONSE_FORMAT is not None and not structured:
                    if DETERMINISTIC_REPLY_WRAPPER:
                        result = json.dumps(
                            {"reply": visible_reply}, ensure_ascii=False, indent=2,
                        )
                        atomic_text(job_dir(job_id) / "result.txt", result + "\n")
                        job["status"] = "completed"
                        job["finished_at"] = now()
                        job["result"] = result
                        save_job(job)
                        write_log(
                            job_id,
                            "Pogovorni odgovor je deterministično zapakiran; "
                            "dodatni modelski klic ni potreben."
                        )
                        notify_job_completion(job)
                        return
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
                argument_error = None

                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError as exc:
                        argument_error = f"Argumenti niso veljaven JSON: {exc}"
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

                fingerprint = None
                repeat_count = 1
                if name == "project_search" and isinstance(arguments, dict):
                    fingerprint_arguments = canonical_tool_arguments(name, arguments)
                    fingerprint = tool_fingerprint(name, fingerprint_arguments)
                    repeat_count = tool_call_repeats.get(fingerprint, 0) + 1
                    tool_call_repeats[fingerprint] = repeat_count

                guarded_result = None
                if fingerprint is not None and repeat_count >= 2:
                    job["duplicate_tool_calls"] += 1
                    job["tool_feedback_events"] += 1
                    if repeat_count >= 4:
                        job["blocked_tool_calls"] += 1
                        save_job(job)
                        raise RuntimeError(
                            "LOOP_DETECTED: isti project_search je bil zahtevan "
                            "štirikrat. Naloga je varno blokirana."
                        )
                    code = (
                        "DUPLICATE_CALL_WARNING" if repeat_count == 2
                        else "DUPLICATE_CALL_BLOCKED"
                    )
                    if repeat_count == 3:
                        job["blocked_tool_calls"] += 1
                    guarded_result = tool_failure_payload(
                        name,
                        (
                            "Ta identični project_search je bil že izveden. "
                            + (
                                "Prejšnji rezultat je že v pogovoru; spremeni query, "
                                "path ali file_glob. Naslednja enaka ponovitev bo blokirana."
                                if repeat_count == 2 else
                                "Klic ni bil ponovno izveden. Uporabi drugo strategijo; "
                                "naslednja enaka ponovitev bo zaključila nalogo."
                            )
                        ),
                        code=code,
                        extra={
                            "repeat_count": repeat_count,
                            "previous_result_available": fingerprint in tool_result_cache,
                        },
                    )

                suspicious = (
                    name == "project_search"
                    and suspicious_search_arguments(arguments)
                )
                if (
                    guarded_result is None and suspicious
                    and suspicious_empty_searches >= 2
                ):
                    suspicious_search_blocks += 1
                    job["blocked_tool_calls"] += 1
                    job["tool_feedback_events"] += 1
                    if suspicious_search_blocks >= 2:
                        save_job(job)
                        raise RuntimeError(
                            "LOOP_DETECTED: model po blokadi še vedno uporablja simbol "
                            "kot file_glob. Naloga je varno blokirana."
                        )
                    guarded_result = tool_failure_payload(
                        name,
                        "Klic ni bil izveden: file_glob je bil že dvakrat uporabljen "
                        "kot iskani simbol namesto filtra imen datotek.",
                        code="SEARCH_MISUSE_BLOCKED",
                        extra={"repeat_count": suspicious_empty_searches + 1},
                    )

                try:
                    if guarded_result is not None:
                        tool_result = guarded_result
                    else:
                        if argument_error is not None:
                            raise ToolCallValidationError(argument_error)
                        normalized_arguments = validate_tool_arguments(name, arguments)
                        result = execute_tool(name, normalized_arguments)
                        tool_result = {"ok": True, "result": result}
                        counts_as_evidence = True
                        if name == "project_search":
                            count = search_result_count(result)
                            if count == 0 and suspicious:
                                suspicious_empty_searches += 1
                                counts_as_evidence = False
                                code = (
                                    "SEARCH_ARGUMENT_HINT"
                                    if suspicious_empty_searches == 1
                                    else "SEARCH_MISUSE_WARNING"
                                )
                                tool_result["warning"] = {
                                    "code": code,
                                    "message": (
                                        "Iskanje ni našlo rezultatov in file_glob je "
                                        "videti kot simbol. Uporabi file_glob='*.py' ter "
                                        "iskani simbol v query."
                                    ),
                                    "repeat_count": suspicious_empty_searches,
                                    "next_action": (
                                        "Popravi argumente. Naslednji podoben napačen "
                                        "klic bo blokiran."
                                        if suspicious_empty_searches >= 2 else
                                        "Popravi argumente pred naslednjim iskanjem."
                                    ),
                                    "guidance": available_tool_guidance(name),
                                }
                                tool_result["counts_as_evidence"] = False
                                job["tool_feedback_events"] += 1
                            else:
                                suspicious_empty_searches = 0
                                suspicious_search_blocks = 0
                        if counts_as_evidence:
                            successful_tool_use_count += 1
                            job["successful_tool_calls"] = successful_tool_use_count
                        if fingerprint is not None:
                            tool_result_cache[fingerprint] = tool_result

                except Exception as e:
                    tool_result = tool_failure_payload(
                        name,
                        e,
                        code=(
                            "TOOL_CALL_INVALID"
                            if isinstance(e, ToolCallValidationError)
                            else "TOOL_CALL_FAILED"
                        ),
                    )
                    job["tool_feedback_events"] += 1
                    if fingerprint is not None:
                        tool_result_cache[fingerprint] = tool_result

                if tool_result.get("ok") and tool_result.get("warning"):
                    write_log(
                        job_id,
                        f"TOOL {name}: OK_WITH_WARNING: "
                        f"{tool_result['warning'].get('code', 'LOW_VALUE')}",
                    )
                elif tool_result.get("ok"):
                    write_log(job_id, f"TOOL {name}: OK")
                else:
                    write_log(
                        job_id,
                        f"TOOL {name}: {tool_result.get('error_code', 'ERROR')}: "
                        f"{tool_result.get('error', '')}",
                    )

                trace_result = {
                    "type": "tool_result",
                    "role": AGENT_ROLE,
                    "step": step,
                    "tool": name,
                    **tool_result,
                }
                write_trace(job_id, trace_result)

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
            **(
                {"error_code": job["error_code"]}
                if job.get("error_code") else {}
            ),
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
    workspace_value = os.environ.get("MIKROFIX_WORKSPACE")
    if workspace_value:
        with workspace_lock(Path(workspace_value)):
            main()
    else:
        main()
