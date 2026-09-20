#!/usr/bin/env python3
"""Local browser interface for the supervised MikroFix agent team."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
import urllib.request

try:
    from tools.mikrofix_local_manager_ui import (
        DEFAULT_WORKSPACE,
        MANAGER,
        STATE_LABELS,
        list_jobs,
        planner_activity,
        read_json,
        render_job,
    )
except ModuleNotFoundError:  # direct execution adds tools/, not the repo root
    from mikrofix_local_manager_ui import (  # type: ignore[no-redef]
        DEFAULT_WORKSPACE,
        MANAGER,
        STATE_LABELS,
        list_jobs,
        planner_activity,
        read_json,
        render_job,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTML_FILE = PROJECT_ROOT / "tools" / "mikrofix_local_manager_web.html"
HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"
MAX_BODY = 64_000


def serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    manager = job["manager"]
    supervisor = {}
    supervisor_path = manager.get("supervisor_state")
    if supervisor_path:
        supervisor = read_json(Path(supervisor_path))
    return {
        "run_id": job["run_id"],
        "task": job["task"],
        "parent_run_id": job.get("parent_run_id"),
        "created_at": job["created_at"],
        "state": job["state"],
        "state_label": STATE_LABELS.get(job["state"], job["state"]),
        "transcript": render_job(job),
        "activity": planner_activity(job["directory"], limit=20),
        "events": supervisor.get("events", [])[-20:],
        "checkpoint": manager.get("checkpoint") or supervisor.get("checkpoint"),
        "reason": manager.get("exact_reason") or supervisor.get("feedback", ""),
    }


def jobs_payload(workspace: Path) -> dict[str, Any]:
    return {"jobs": [serialize_job(job) for job in list_jobs(workspace)]}


def spawn_manager(workspace: Path, arguments: list[str]) -> Path:
    reports = workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    log_path = reports / f"web-{int(time.time() * 1000)}.log"
    log = log_path.open("w", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(MANAGER), "--workspace", str(workspace), *arguments],
        cwd=PROJECT_ROOT,
        env={**os.environ, "MIKROFIX_VISIBLE_PLANNER": "0"},
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
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
        if self.path == "/":
            self.send_bytes(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self.send_json(200, {"ok": True})
        elif self.path == "/api/jobs":
            self.send_json(200, jobs_payload(self.server.workspace))
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
            if self.path == "/api/execute":
                run_id = value.get("run_id")
                selected = next(
                    (job for job in list_jobs(self.server.workspace)
                     if job["run_id"] == run_id),
                    None,
                )
                if not selected or selected["state"] != "READY_FOR_OWNER_LEASE":
                    raise ValueError("Naloga še ni pripravljena za dovoljenje izvedbe")
                spawn_manager(
                    self.server.workspace,
                    ["execute", str(run_id), "--lease-minutes", "30"],
                )
                self.send_json(202, {"accepted": True})
                return
            self.send_json(404, {"error": "Not found"})
        except (ValueError, json.JSONDecodeError, OSError) as exc:
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
