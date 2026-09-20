#!/usr/bin/env python3
"""Durable, at-most-once completion notifications for the Codex desktop app."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


CODEX_BINARY = "/usr/lib/chatgpt/resources/codex"
NOTIFY_BINARY = "/usr/bin/notify-send"
DEFAULT_TIMEOUT_SECONDS = 15
MAX_MESSAGE_BYTES = 768

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_INPUT_STATES = {
    "AWAIT_CODEX_REVIEW",
    "READY_FOR_CODEX_REVIEW",
    "COMPLETED",
    "FAILED",
    "BLOCKED",
}
_ALLOWED_PAYLOAD_KEYS = {
    "blocked_need",
    "display_name",
    "evidence_paths",
    "evidence_sha256",
    "job_id",
    "risks",
    "state",
    "summary",
    "test_result",
}


class CompletionError(ValueError):
    """Raised when a completion event or configured thread is invalid."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _validate_thread_id(thread_id: Optional[str]) -> Optional[str]:
    if thread_id is None:
        return None
    if not isinstance(thread_id, str):
        raise CompletionError("thread_id must be a canonical UUID string")
    try:
        parsed = uuid.UUID(thread_id)
    except (ValueError, AttributeError) as exc:
        raise CompletionError("thread_id must be a canonical UUID string") from exc
    canonical = str(parsed)
    if parsed.int == 0 or thread_id != canonical:
        raise CompletionError("thread_id must be a non-nil canonical UUID string")
    return canonical


def validate_thread_id(thread_id: Optional[str]) -> Optional[str]:
    """Return a canonical originating thread UUID, or ``None``."""
    return _validate_thread_id(thread_id)


def _short_text(payload: Mapping[str, Any], key: str, limit: int) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise CompletionError(f"{key} must be a string of at most {limit} bytes")
    if "\x00" in value:
        raise CompletionError(f"{key} contains a NUL character")
    return value


def _bounded_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise CompletionError("payload must be an object")
    unknown = set(payload) - _ALLOWED_PAYLOAD_KEYS
    if unknown:
        raise CompletionError("unsupported payload fields: " + ", ".join(sorted(unknown)))

    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise CompletionError("job_id has an invalid format")

    state = payload.get("state")
    if state not in _INPUT_STATES:
        raise CompletionError("state is not a supported completion state")
    outward_state = (
        "READY_FOR_CODEX_REVIEW" if state == "AWAIT_CODEX_REVIEW" else state
    )

    display_name = _short_text(payload, "display_name", 256) or job_id
    summary = _short_text(payload, "summary", 4096)
    test_result = _short_text(payload, "test_result", 2048)
    risks = _short_text(payload, "risks", 2048)
    blocked_need = _short_text(payload, "blocked_need", 2048)

    evidence_paths = payload.get("evidence_paths", [])
    if not isinstance(evidence_paths, list) or len(evidence_paths) > 12:
        raise CompletionError("evidence_paths must be a list of at most 12 paths")
    normalized_paths: list[str] = []
    for value in evidence_paths:
        if (not isinstance(value, str) or not value.startswith("/")
                or "\x00" in value or len(value.encode("utf-8")) > 1024):
            raise CompletionError("evidence_paths entries must be bounded absolute paths")
        normalized_paths.append(value)
    if len(set(normalized_paths)) != len(normalized_paths):
        raise CompletionError("evidence_paths entries must be unique")

    evidence_sha256 = payload.get("evidence_sha256", {})
    if not isinstance(evidence_sha256, Mapping) or len(evidence_sha256) > 12:
        raise CompletionError("evidence_sha256 must be an object with at most 12 entries")
    normalized_hashes: dict[str, str] = {}
    for key, value in evidence_sha256.items():
        if key not in normalized_paths or not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-f]{64}", value
        ):
            raise CompletionError("evidence_sha256 must map evidence paths to SHA-256 values")
        normalized_hashes[key] = value

    return {
        "job_id": job_id,
        "display_name": display_name,
        "state": outward_state,
        "summary": summary,
        "test_result": test_result,
        "risks": risks,
        "blocked_need": blocked_need,
        "evidence_paths": normalized_paths,
        "evidence_sha256": normalized_hashes,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive_json(path: Path, value: Mapping[str, Any]) -> bool:
    data = _canonical_json(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(descriptor)
    _fsync_directory(path.parent)
    return True


def _read_private_json(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


class CompletionOutbox:
    """Persist completion events and wake one configured Codex thread once."""

    def __init__(
        self,
        root: os.PathLike[str] | str,
        *,
        thread_id: Optional[str] = None,
        runner: Callable[..., Any] = subprocess.run,
        notify_runner: Optional[Callable[..., Any]] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 60:
            raise CompletionError("timeout_seconds must be between 1 and 60")
        self.root = Path(root)
        self.thread_id = _validate_thread_id(thread_id)
        self.runner = runner
        self.notify_runner = notify_runner
        self.timeout_seconds = timeout_seconds
        self.events = self.root / "events"
        self.claims = self.root / "claims"
        self.receipts = self.root / "receipts"
        self.notifications = self.root / "notifications"
        for directory in (
            self.root,
            self.events,
            self.claims,
            self.receipts,
            self.notifications,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)

    def enqueue(self, payload: Mapping[str, Any]) -> str:
        normalized = _validate_payload(payload)
        digest = hashlib.sha256(_canonical_json(normalized)).hexdigest()
        event_id = "completion-" + digest
        event = {"event_id": event_id, "payload": normalized, "version": 1}
        event_path = self.events / f"{event_id}.json"
        if not _write_exclusive_json(event_path, event):
            existing = _read_private_json(event_path)
            if existing != event:
                raise CompletionError("event_id collision with different content")
        return event_id

    def publish(self, payload: Mapping[str, Any]) -> dict[str, str]:
        event_id = self.enqueue(payload)
        event = _read_private_json(self.events / f"{event_id}.json")
        if self.thread_id is None:
            self._notify_terminal_once(event_id, event["payload"])
            return {"event_id": event_id, "status": "queued_no_thread"}

        event_path = self.events / f"{event_id}.json"
        message = self._message(event_id, event_path, event["payload"])

        claim_path = self.claims / f"{event_id}.json"
        claim = {"event_id": event_id, "thread_id": self.thread_id, "version": 1}
        if not _write_exclusive_json(claim_path, claim):
            receipt_path = self.receipts / f"{event_id}.json"
            if receipt_path.exists():
                receipt = _read_private_json(receipt_path)
                self._notify_terminal_once(
                    event_id,
                    event["payload"],
                    delivery_failed=receipt.get("status") == "failed",
                )
                return {"event_id": event_id, "status": str(receipt["status"])}
            self._notify_terminal_once(event_id, event["payload"])
            return {"event_id": event_id, "status": "claimed"}

        argv = [
            CODEX_BINARY,
            "queue",
            "--thread",
            self.thread_id,
            "--message",
            message,
        ]
        status = "delivered"
        detail = "accepted"
        try:
            completed = self.runner(
                argv,
                shell=False,
                check=False,
                timeout=self.timeout_seconds,
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
            )
            returncode = int(getattr(completed, "returncode", 0))
            if returncode != 0:
                status = "failed"
                detail = f"returncode:{returncode}"
        except Exception as exc:  # the durable claim intentionally prevents retry
            status = "failed"
            detail = "exception:" + type(exc).__name__

        receipt = {
            "detail": detail,
            "event_id": event_id,
            "status": status,
            "version": 1,
        }
        _write_exclusive_json(self.receipts / f"{event_id}.json", receipt)
        self._notify_terminal_once(
            event_id, event["payload"], delivery_failed=status == "failed"
        )
        return {"event_id": event_id, "status": status}

    @staticmethod
    def _message(
        event_id: str, event_path: Path, payload: Mapping[str, Any]
    ) -> str:
        message = json.dumps(
            {
                "event_id": event_id,
                "event_path": str(event_path.resolve()),
                "job_id": payload["job_id"],
                "display_name": _bounded_utf8(payload["display_name"], 160),
                "state": payload["state"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise CompletionError("generated completion message is too large")
        return message

    def _notify_terminal_once(
        self,
        event_id: str,
        payload: Mapping[str, Any],
        *,
        delivery_failed: bool = False,
    ) -> None:
        if self.notify_runner is None:
            return
        notification_path = self.notifications / f"{event_id}.json"
        notification = {
            "event_id": event_id,
            "state": payload["state"],
            "version": 1,
        }
        if not _write_exclusive_json(notification_path, notification):
            return

        state = str(payload["state"])
        if state in {"COMPLETED", "READY_FOR_CODEX_REVIEW"}:
            title = "MikroFIX: naloga je končana"
        elif state == "BLOCKED":
            title = "MikroFIX: naloga potrebuje pozornost"
        else:
            title = "MikroFIX: naloga ni uspela"
        body = f"{payload['display_name']}\nStanje: {state}"
        if delivery_failed:
            body += "\nObvestilo Codexu ni bilo dostavljeno; dokaz je varno shranjen."

        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        for name in (
            "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "XDG_RUNTIME_DIR",
        ):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        try:
            self.notify_runner(
                [
                    NOTIFY_BINARY,
                    "--app-name=MikroFIX",
                    "--urgency=normal",
                    title,
                    body,
                ],
                shell=False,
                check=False,
                timeout=min(self.timeout_seconds, 10),
                env=environment,
                capture_output=True,
                text=True,
            )
        except Exception:
            pass


__all__ = ["CompletionError", "CompletionOutbox", "validate_thread_id"]
