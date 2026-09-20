#!/usr/bin/env python3
"""Restricted OpenAPI tool server for the local MikroFIX executor.

The service deliberately exposes project-shaped operations instead of a shell.
Read operations are always available to an authenticated local client. Mutating
operations additionally require a short lease granted from the desktop.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import tempfile
import time
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(
    os.environ.get(
        "MIKROFIX_EXECUTOR_ROOT",
        "/home/mikrofix/MikroFIX_Service_v30_R9_LINUX_DEV",
    )
).resolve()
CONFIG_DIR = Path(
    os.environ.get(
        "MIKROFIX_EXECUTOR_CONFIG_DIR",
        str(Path.home() / ".config" / "mikrofix-local-executor"),
    )
).expanduser().resolve()
STATE_DIR = Path(
    os.environ.get(
        "MIKROFIX_EXECUTOR_STATE_DIR",
        str(Path.home() / ".local" / "state" / "mikrofix"),
    )
).expanduser().resolve()
TOKEN_FILE = CONFIG_DIR / "token"
LEASE_FILE = STATE_DIR / "executor-lease.json"
AUDIT_FILE = STATE_DIR / "executor-audit.jsonl"
ALLOWED_FILES_MANIFEST = os.environ.get("MIKROFIX_EXECUTOR_ALLOWED_FILES", "").strip()
EXPECTED_RUN_ID = os.environ.get("MIKROFIX_EXECUTOR_RUN_ID", "").strip()
EXPECTED_PLAN_SHA256 = os.environ.get("MIKROFIX_EXECUTOR_PLAN_SHA256", "").strip()
EXPECTED_ALLOWED_FILES_SHA256 = os.environ.get(
    "MIKROFIX_EXECUTOR_ALLOWED_FILES_SHA256", ""
).strip()

READABLE_SUFFIXES = {
    ".py", ".md", ".txt", ".toml", ".ini", ".json", ".yaml", ".yml",
    ".sql", ".sh", ".desktop",
}
BLOCKED_PARTS = {
    ".git", ".venv", ".venv-server", "__pycache__", "certs", "backups",
    "MikroFixData", "MikroFixData_V30_E2E_TEST",
}
BLOCKED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".crt",
    ".cer", ".der", ".env",
}
MAX_READ_BYTES = 256_000
MAX_READ_LINES = 4000
MAX_LINE_NUMBER = 200_000
MAX_PATCH_BYTES = 128_000
MAX_SEARCH_RESULTS = 200
COMMAND_TIMEOUT_SECONDS = 900
DISPLAY = os.environ.get("DISPLAY", ":0.0")
XAUTHORITY = os.environ.get("XAUTHORITY", str(Path.home() / ".Xauthority"))


def _ensure_private_file(path: Path, content_factory) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        value = content_factory()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
    os.chmod(path, 0o600)
    return path.read_text(encoding="utf-8").strip()


API_TOKEN = _ensure_private_file(TOKEN_FILE, lambda: secrets.token_urlsafe(36))


def _audit(event: str, *, ok: bool, details: dict[str, object] | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        "ok": ok,
        "details": details or {},
    }
    with AUDIT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def authenticate(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {API_TOKEN}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        _audit("authentication", ok=False)
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def require_lease() -> dict[str, object]:
    try:
        data = json.loads(LEASE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(
            status_code=423,
            detail="Writing is locked. Click 'MikroFIX - dovoli lokalnemu izvajalcu' on the desktop.",
        )
    expires_at = float(data.get("expires_at", 0))
    if expires_at <= time.time():
        LEASE_FILE.unlink(missing_ok=True)
        raise HTTPException(status_code=423, detail="The write lease has expired")
    expected = {
        "run_id": EXPECTED_RUN_ID,
        "project_root": str(PROJECT_ROOT) if EXPECTED_RUN_ID else "",
        "plan_sha256": EXPECTED_PLAN_SHA256,
        "allowed_files_sha256": EXPECTED_ALLOWED_FILES_SHA256,
    }
    mismatched = [
        key for key, value in expected.items()
        if value and str(data.get(key) or "") != value
    ]
    if mismatched:
        raise HTTPException(
            status_code=423,
            detail="The write lease does not match this supervised run: "
            + ", ".join(mismatched),
        )
    return data


def safe_path(user_path: str, *, require_file: bool = False) -> Path:
    if not user_path or "\x00" in user_path:
        raise HTTPException(status_code=400, detail="Invalid path")
    path = (PROJECT_ROOT / user_path).resolve()
    try:
        relative = path.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Path escapes the project") from exc
    if any(part in BLOCKED_PARTS or part.startswith(".env") for part in relative.parts):
        raise HTTPException(status_code=403, detail="Path is not available to the executor")
    if path.suffix.casefold() in BLOCKED_SUFFIXES:
        raise HTTPException(status_code=403, detail="Sensitive file type is blocked")
    if require_file and (not path.is_file() or path.suffix.casefold() not in READABLE_SUFFIXES):
        raise HTTPException(status_code=404, detail="Readable project file not found")
    return path


def run_command(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def gui_confirm(title: str, message: str) -> bool:
    """Ask the locally logged-in owner before an exceptional operation."""
    try:
        result = subprocess.run(
            [
                "zenity",
                "--question",
                f"--title={title}",
                f"--text={message}",
                "--width=680",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
            env={**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": XAUTHORITY},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def run_administrator_command(
    command: str,
    working_directory: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run sudo in a visible terminal; the server never receives the password."""
    request_dir = STATE_DIR / "administrator-requests" / secrets.token_hex(12)
    request_dir.mkdir(parents=True, mode=0o700)
    wrapper = request_dir / "run.sh"
    output_file = request_dir / "output.txt"
    status_file = request_dir / "exit-code.txt"
    root_script = f"cd -- {shlex.quote(str(working_directory))} && {command}"
    wrapper_text = f"""#!/bin/bash
set +e
printf '%s\\n' 'MikroFIX Qwen zahteva potrjeni skrbniski ukaz.'
printf '%s\\n' 'Geslo vpisi samo v ta terminal; agent ga ne more prebrati.'
/usr/bin/sudo -k -p '[sudo] Geslo za potrjeni ukaz: ' -- /bin/bash -lc {shlex.quote(root_script)} > {shlex.quote(str(output_file))} 2>&1
status=$?
printf '%s\\n' "$status" > {shlex.quote(str(status_file))}
if [ "$status" -eq 0 ]; then
    printf '%s\\n' 'Ukaz je uspel. Terminal se bo zaprl.'
else
    printf '%s\\n' "Ukaz ni uspel (izhod $status). Terminal se bo zaprl."
fi
sleep 2
"""
    fd = os.open(wrapper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(wrapper_text)
    process = subprocess.Popen(
        [
            "/usr/bin/xfce4-terminal",
            "--title=MikroFIX Qwen - skrbniska potrditev",
            f"--command={wrapper}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": XAUTHORITY},
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not status_file.exists():
        time.sleep(0.25)
    if not status_file.exists():
        process.terminate()
        raise subprocess.TimeoutExpired(command, timeout_seconds)
    try:
        exit_code = int(status_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        exit_code = 125
    try:
        output = output_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        output = ""
    for path in (wrapper, output_file, status_file):
        path.unlink(missing_ok=True)
    try:
        request_dir.rmdir()
    except OSError:
        pass
    return subprocess.CompletedProcess(
        args=["sudo", "/bin/bash", "-lc", command],
        returncode=exit_code,
        stdout=output,
        stderr=None,
    )


class ReadRequest(BaseModel):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=240, ge=1, le=MAX_LINE_NUMBER)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    path: str = "."
    file_glob: str | None = Field(default=None, max_length=120)
    max_results: int = Field(default=80, ge=1, le=MAX_SEARCH_RESULTS)


class ListRequest(BaseModel):
    path: str = "."
    max_entries: int = Field(default=200, ge=1, le=500)


class PatchRequest(BaseModel):
    patch: str = Field(min_length=1, max_length=MAX_PATCH_BYTES)
    summary: str = Field(min_length=3, max_length=300)


class ReplaceRequest(BaseModel):
    path: str
    old: str = Field(min_length=1, max_length=MAX_PATCH_BYTES)
    new: str = Field(max_length=MAX_PATCH_BYTES)
    summary: str = Field(min_length=3, max_length=300)


class CheckRequest(BaseModel):
    profile: Literal[
        "local_agent_tests",
        "desktop_unit_tests",
        "central_api_tests",
        "syntax_changed_python",
    ]


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    purpose: str = Field(min_length=3, max_length=500)
    working_directory: str = "."
    administrator: bool = False
    timeout_seconds: int = Field(default=300, ge=1, le=COMMAND_TIMEOUT_SECONDS)


class DesktopActionRequest(BaseModel):
    action: Literal[
        "move_pointer",
        "click",
        "double_click",
        "type_text",
        "press_key",
        "scroll",
        "focus_window",
    ]
    x: int | None = None
    y: int | None = None
    button: int = Field(default=1, ge=1, le=5)
    text: str | None = Field(default=None, max_length=4000)
    key: str | None = Field(default=None, max_length=80)
    amount: int | None = Field(default=None, ge=-20, le=20)
    window_id: int | None = Field(default=None, ge=1)


app = FastAPI(
    title="MikroFIX Local Executor",
    description=(
        "Restricted tools for a supervised local MikroFIX coding agent. "
        "There is no shell tool. Writes require a short lease granted locally by the owner."
    ),
    version="1.0.0",
)


@app.exception_handler(subprocess.TimeoutExpired)
async def timeout_handler(_request, exc: subprocess.TimeoutExpired):
    _audit("command_timeout", ok=False, details={"seconds": exc.timeout})
    return JSONResponse(status_code=504, content={"detail": "The bounded check timed out"})


@app.get("/health")
def health() -> dict[str, object]:
    lease_seconds = 0
    try:
        lease = json.loads(LEASE_FILE.read_text(encoding="utf-8"))
        lease_seconds = max(0, int(float(lease.get("expires_at", 0)) - time.time()))
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "status": "ok",
        "project": str(PROJECT_ROOT),
        "write_lease_seconds": lease_seconds,
        "shell_available": "per-command owner confirmation",
        "administrator_available": "system password prompt; password is never exposed to the agent",
    }


@app.get("/v1/project/status", dependencies=[Depends(authenticate)])
def project_status() -> dict[str, object]:
    result = run_command(["git", "status", "--short", "--branch"])
    return {"exit_code": result.returncode, "output": result.stdout[-20_000:]}


@app.post("/v1/project/read", dependencies=[Depends(authenticate)])
def project_read(request: ReadRequest) -> dict[str, object]:
    path = safe_path(request.path, require_file=True)
    if path.stat().st_size > MAX_READ_BYTES:
        raise HTTPException(status_code=413, detail="File is too large for this tool")
    content_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = min(request.start_line, len(content_lines) + 1)
    end = min(max(start - 1, request.end_line), start + MAX_READ_LINES - 1, len(content_lines))
    raw_content = "\n".join(content_lines[start - 1 : end])
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "start_line": start,
        "end_line": end,
        "total_lines": len(content_lines),
        "content": raw_content,
        "raw_content": raw_content,
    }


@app.post("/v1/project/list", dependencies=[Depends(authenticate)])
def project_list(request: ListRequest) -> dict[str, object]:
    base = safe_path(request.path)
    if not base.is_dir():
        raise HTTPException(status_code=404, detail="Project directory not found")
    entries: list[dict[str, object]] = []
    for item in sorted(base.iterdir(), key=lambda value: (not value.is_dir(), value.name.casefold())):
        rel = item.relative_to(PROJECT_ROOT)
        if any(part in BLOCKED_PARTS or part.startswith(".env") for part in rel.parts):
            continue
        if item.is_file() and item.suffix.casefold() in BLOCKED_SUFFIXES:
            continue
        entries.append({
            "path": str(rel),
            "kind": "directory" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
        })
        if len(entries) >= request.max_entries:
            break
    return {"entries": entries, "truncated": len(entries) >= request.max_entries}


@app.post("/v1/project/search", dependencies=[Depends(authenticate)])
def project_search(request: SearchRequest) -> dict[str, object]:
    base = safe_path(request.path)
    if not base.exists():
        raise HTTPException(status_code=404, detail="Search path not found")
    args = [
        "rg", "--line-number", "--no-heading", "--color=never", "--max-count",
        str(request.max_results),
    ]
    for part in sorted(BLOCKED_PARTS):
        args.extend(["--glob", f"!{part}/**"])
    for suffix in sorted(BLOCKED_SUFFIXES):
        args.extend(["--glob", f"!*{suffix}"])
    if request.file_glob:
        if ".." in request.file_glob or request.file_glob.startswith("/"):
            raise HTTPException(status_code=400, detail="Invalid file glob")
        args.extend(["--glob", request.file_glob])
    args.extend(["--", request.query, str(base.relative_to(PROJECT_ROOT) or Path("."))])
    result = run_command(args)
    lines = result.stdout.splitlines()[: request.max_results]
    return {"matches": lines, "count": len(lines), "exit_code": result.returncode}


@app.get("/v1/project/diff", dependencies=[Depends(authenticate)])
def project_diff() -> dict[str, object]:
    result = run_command(["git", "diff", "--no-ext-diff", "--unified=3", "--", "."])
    return {"exit_code": result.returncode, "diff": result.stdout[-80_000:]}


def require_approved_paths(paths: set[str]) -> None:
    if not ALLOWED_FILES_MANIFEST:
        return
    manifest_path = Path(ALLOWED_FILES_MANIFEST).expanduser().resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        allowed = manifest.get("allowed_files", manifest)
        allowed_files = {
            Path(str(item)).as_posix() for item in allowed
            if isinstance(item, str) and item
        }
    except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise HTTPException(status_code=500, detail="Invalid allowed-files manifest") from exc
    forbidden = sorted(paths - allowed_files)
    if forbidden:
        raise HTTPException(
            status_code=403,
            detail="Change touches files outside the approved plan: " + ", ".join(forbidden),
        )


def validate_patch(patch: str) -> list[str]:
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise HTTPException(status_code=413, detail="Patch is too large")
    if "GIT binary patch" in patch or "Binary files" in patch:
        raise HTTPException(status_code=400, detail="Binary patches are forbidden")
    paths = re.findall(r"^\+\+\+ b/(.+)$", patch, flags=re.MULTILINE)
    old_paths = re.findall(r"^--- a/(.+)$", patch, flags=re.MULTILINE)
    if not paths or len(paths) > 20:
        raise HTTPException(status_code=400, detail="Patch must modify between 1 and 20 text files")
    for raw_path in paths + old_paths:
        if raw_path == "/dev/null":
            continue
        safe_path(raw_path)
        if Path(raw_path).suffix.casefold() not in READABLE_SUFFIXES:
            raise HTTPException(status_code=403, detail=f"File type is not writable: {raw_path}")
    require_approved_paths({path for path in paths + old_paths if path != "/dev/null"})
    if re.search(r"^(rename|delete) (from|to|mode) ", patch, flags=re.MULTILINE):
        raise HTTPException(status_code=400, detail="Rename and deletion patches are forbidden")
    return paths


@app.post("/v1/project/apply-patch", dependencies=[Depends(authenticate)])
def project_apply_patch(request: PatchRequest) -> dict[str, object]:
    lease = require_lease()
    paths = validate_patch(request.patch)
    before = run_command(["git", "status", "--porcelain"]).stdout
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".patch", dir=STATE_DIR, delete=False
    ) as handle:
        handle.write(request.patch)
        patch_path = Path(handle.name)
    try:
        check = run_command(["git", "apply", "--check", "--whitespace=error", str(patch_path)])
        if check.returncode != 0:
            _audit("apply_patch", ok=False, details={"summary": request.summary, "reason": check.stdout[-2000:]})
            raise HTTPException(status_code=409, detail=check.stdout[-4000:] or "Patch check failed")
        applied = run_command(["git", "apply", "--whitespace=error", str(patch_path)])
        if applied.returncode != 0:
            _audit("apply_patch", ok=False, details={"summary": request.summary, "reason": applied.stdout[-2000:]})
            raise HTTPException(status_code=409, detail=applied.stdout[-4000:] or "Patch apply failed")
    finally:
        patch_path.unlink(missing_ok=True)
    after = run_command(["git", "status", "--short"]).stdout
    patch_sha256 = hashlib.sha256(request.patch.encode("utf-8")).hexdigest()
    _audit(
        "apply_patch",
        ok=True,
        details={
            "summary": request.summary,
            "paths": paths,
            "patch_sha256": patch_sha256,
            "lease_id": lease.get("lease_id"),
            "working_tree_was_dirty": bool(before.strip()),
        },
    )
    return {"applied": True, "paths": paths, "patch_sha256": patch_sha256, "status": after[-20_000:]}


@app.post("/v1/project/replace", dependencies=[Depends(authenticate)])
def project_replace(request: ReplaceRequest) -> dict[str, object]:
    lease = require_lease()
    path = safe_path(request.path, require_file=True)
    relative = path.relative_to(PROJECT_ROOT).as_posix()
    require_approved_paths({relative})
    content = path.read_text(encoding="utf-8")
    matches = content.count(request.old)
    if matches != 1:
        raise HTTPException(
            status_code=409,
            detail=f"Exact replacement requires one match; found {matches}",
        )
    updated = content.replace(request.old, request.new, 1)
    mode = path.stat().st_mode
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    replacement_sha256 = hashlib.sha256(
        (request.old + "\0" + request.new).encode("utf-8")
    ).hexdigest()
    _audit(
        "replace",
        ok=True,
        details={
            "summary": request.summary,
            "path": relative,
            "replacement_sha256": replacement_sha256,
            "lease_id": lease.get("lease_id"),
        },
    )
    return {
        "replaced": True,
        "path": relative,
        "replacement_sha256": replacement_sha256,
    }


CHECKS: dict[str, tuple[list[str], int]] = {
    "local_agent_tests": ([".venv/bin/python", "-m", "unittest", "-v", "tests/test_mikrofix_local_agent.py"], 180),
    "desktop_unit_tests": ([".venv/bin/python", "-m", "unittest", "discover", "-v"], 900),
    "central_api_tests": ([".venv-server/bin/python", "-m", "unittest", "-v", "test_v32_central_api.py"], 600),
}


@app.post("/v1/project/run-check", dependencies=[Depends(authenticate)])
def project_run_check(request: CheckRequest) -> dict[str, object]:
    if EXPECTED_RUN_ID:
        raise HTTPException(
            status_code=403,
            detail=(
                "project_run_check is disabled for supervised runs; "
                "the supervisor owns the fail-closed sandboxed test gate"
            ),
        )
    require_lease()
    if request.profile == "syntax_changed_python":
        changed = run_command(["git", "diff", "--name-only", "--diff-filter=ACM", "--", "*.py"]).stdout.splitlines()
        files = [item for item in changed if safe_path(item).suffix == ".py"]
        if not files:
            return {"profile": request.profile, "exit_code": 0, "output": "No changed Python files."}
        command, timeout = [".venv/bin/python", "-m", "py_compile", *files], 120
    else:
        command, timeout = CHECKS[request.profile]
    result = run_command(command, timeout=timeout)
    _audit(
        "run_check",
        ok=result.returncode == 0,
        details={"profile": request.profile, "exit_code": result.returncode},
    )
    return {
        "profile": request.profile,
        "exit_code": result.returncode,
        "output": result.stdout[-60_000:],
        "truncated": len(result.stdout) > 60_000,
    }


@app.post("/v1/system/command", dependencies=[Depends(authenticate)])
def system_command(request: CommandRequest) -> dict[str, object]:
    """Run an exceptional shell command only after an on-screen confirmation.

    This is intentionally the slow path. Routine project work should use the
    structured project endpoints. Administrator mode additionally invokes
    pkexec, whose password dialog is owned by the operating system.
    """
    if EXPECTED_RUN_ID:
        raise HTTPException(
            status_code=403,
            detail="system_command is disabled for supervised executor runs",
        )
    lease = require_lease()
    working_directory = safe_path(request.working_directory)
    if not working_directory.is_dir():
        raise HTTPException(status_code=404, detail="Working directory not found")
    mode = "SKRBNIŠKI UKAZ" if request.administrator else "UPORABNIŠKI UKAZ"
    message = (
        f"{mode}\n\nNamen:\n{request.purpose}\n\n"
        f"Mapa:\n{working_directory}\n\nUkaz:\n{request.command}\n\n"
        "Izberi Da samo, če ukaz razumeš in ga želiš izvesti."
    )
    if not gui_confirm("MikroFIX Qwen – potrditev ukaza", message):
        _audit(
            "system_command",
            ok=False,
            details={"purpose": request.purpose, "administrator": request.administrator, "reason": "owner_rejected"},
        )
        raise HTTPException(status_code=403, detail="The owner rejected the command")

    command_hash = hashlib.sha256(request.command.encode("utf-8")).hexdigest()
    try:
        if request.administrator:
            result = run_administrator_command(
                request.command,
                working_directory,
                request.timeout_seconds,
            )
        else:
            result = subprocess.run(
                ["/bin/bash", "-lc", request.command],
                cwd=working_directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
                env={**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": XAUTHORITY},
            )
    except subprocess.TimeoutExpired as exc:
        _audit(
            "system_command",
            ok=False,
            details={"purpose": request.purpose, "administrator": request.administrator, "command_sha256": command_hash, "reason": "timeout"},
        )
        raise HTTPException(status_code=504, detail="Command timed out") from exc
    _audit(
        "system_command",
        ok=result.returncode == 0,
        details={
            "purpose": request.purpose,
            "administrator": request.administrator,
            "command_sha256": command_hash,
            "exit_code": result.returncode,
            "lease_id": lease.get("lease_id"),
        },
    )
    return {
        "exit_code": result.returncode,
        "output": result.stdout[-80_000:],
        "truncated": len(result.stdout) > 80_000,
        "administrator": request.administrator,
    }


@app.get("/v1/desktop/inspect", dependencies=[Depends(authenticate)])
def desktop_inspect() -> dict[str, object]:
    active = run_command(["xdotool", "getactivewindow", "getwindowname"])
    pointer = run_command(["xdotool", "getmouselocation", "--shell"])
    geometry = run_command(["xdotool", "getdisplaygeometry"])
    windows = run_command(["xdotool", "search", "--onlyvisible", "--name", ".*"])
    visible: list[dict[str, object]] = []
    for raw_id in windows.stdout.splitlines()[:100]:
        if not raw_id.isdigit():
            continue
        name = run_command(["xdotool", "getwindowname", raw_id]).stdout.strip()
        visible.append({"id": int(raw_id), "name": name[:300]})
    return {
        "active_window": active.stdout.strip(),
        "pointer": pointer.stdout.strip(),
        "display_geometry": geometry.stdout.strip(),
        "visible_windows": visible,
    }


@app.get("/v1/desktop/screenshot", dependencies=[Depends(authenticate)])
def desktop_screenshot() -> dict[str, object]:
    capture_dir = STATE_DIR / "executor-captures"
    capture_dir.mkdir(parents=True, exist_ok=True)
    path = capture_dir / "latest.png"
    result = subprocess.run(
        ["/usr/bin/xfce4-screenshooter", "--fullscreen", "--save", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": XAUTHORITY},
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stdout[-2000:] or "Screenshot failed")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "note": "The image remains local. Attach this path to the chat if visual inspection is needed.",
    }


def _screen_geometry() -> tuple[int, int]:
    result = run_command(["xdotool", "getdisplaygeometry"])
    try:
        width, height = (int(value) for value in result.stdout.split()[:2])
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=500, detail="Could not determine display geometry") from exc
    return width, height


@app.post("/v1/desktop/action", dependencies=[Depends(authenticate)])
def desktop_action(request: DesktopActionRequest) -> dict[str, object]:
    if EXPECTED_RUN_ID:
        raise HTTPException(
            status_code=403,
            detail="desktop_action is disabled for supervised executor runs",
        )
    lease = require_lease()
    width, height = _screen_geometry()
    if request.action in {"move_pointer", "click", "double_click"}:
        if request.x is None or request.y is None or not (0 <= request.x < width and 0 <= request.y < height):
            raise HTTPException(status_code=400, detail="Valid on-screen x and y coordinates are required")
        args = ["xdotool", "mousemove", "--sync", str(request.x), str(request.y)]
        if request.action == "click":
            args += ["click", str(request.button)]
        elif request.action == "double_click":
            args += ["click", "--repeat", "2", "--delay", "120", str(request.button)]
    elif request.action == "type_text":
        if not request.text:
            raise HTTPException(status_code=400, detail="Text is required")
        args = ["xdotool", "type", "--clearmodifiers", "--delay", "15", "--", request.text]
    elif request.action == "press_key":
        if not request.key:
            raise HTTPException(status_code=400, detail="Key is required")
        normalized = request.key.casefold().replace(" ", "")
        forbidden = {"ctrl+alt+t", "super+r", "alt+f2", "shift+delete", "ctrl+alt+delete"}
        if normalized in forbidden:
            raise HTTPException(status_code=403, detail="This shortcut is blocked; use the confirmed command tool instead")
        args = ["xdotool", "key", "--clearmodifiers", request.key]
    elif request.action == "scroll":
        if request.amount is None or request.amount == 0:
            raise HTTPException(status_code=400, detail="Non-zero scroll amount is required")
        button = "4" if request.amount > 0 else "5"
        args = ["xdotool", "click", "--repeat", str(abs(request.amount)), "--delay", "40", button]
    else:
        if request.window_id is None:
            raise HTTPException(status_code=400, detail="Window id is required")
        args = ["xdotool", "windowactivate", "--sync", str(request.window_id)]
    result = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": XAUTHORITY},
    )
    details: dict[str, object] = {
        "action": request.action,
        "exit_code": result.returncode,
        "lease_id": lease.get("lease_id"),
    }
    if request.action == "type_text" and request.text is not None:
        details["text_length"] = len(request.text)
        details["text_sha256"] = hashlib.sha256(request.text.encode("utf-8")).hexdigest()
    _audit("desktop_action", ok=result.returncode == 0, details=details)
    return {"exit_code": result.returncode, "output": result.stdout[-4000:]}
