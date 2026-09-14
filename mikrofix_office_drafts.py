"""Durable local work-in-progress forms for the central Office client.

These rows protect unsent keyboard input. They are not business documents and
never change inventory; only an accepted central operation can do that.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping
from uuid import UUID, uuid4

from mikrofix_sync_store import now_text


WORK_DRAFT_KINDS = frozenset({"PURCHASE_RECEIPT", "ATTACH_PURCHASE_INVOICE"})


class OfficeDraftError(RuntimeError):
    pass


def ensure_office_draft_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS central_office_work_drafts_v32(
            draft_id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            draft_kind TEXT NOT NULL,
            source_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK(draft_kind IN ('PURCHASE_RECEIPT', 'ATTACH_PURCHASE_INVOICE')),
            CHECK(status IN ('ACTIVE', 'COMPLETED', 'ABANDONED'))
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_central_office_work_drafts_owner
        ON central_office_work_drafts_v32(device_id, actor_id, status, updated_at)
        """
    )


def _identity(value: object, *, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise OfficeDraftError(f"{field} is required.")
    return cleaned


def _draft_id(value: object | None) -> str:
    try:
        return str(UUID(str(value or uuid4())))
    except (TypeError, ValueError) as exc:
        raise OfficeDraftError("draft_id must be a valid UUID.") from exc


def _kind(value: object) -> str:
    cleaned = str(value or "").strip().upper()
    if cleaned not in WORK_DRAFT_KINDS:
        raise OfficeDraftError("Unsupported central Office work draft kind.")
    return cleaned


def _serialized(payload: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(payload, Mapping):
        raise OfficeDraftError("Work draft payload must be an object.")
    try:
        rendered = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise OfficeDraftError("Work draft contains a value that cannot be saved.") from exc
    return rendered, hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def save_office_work_draft(
    connection: sqlite3.Connection, *, actor_id: str, device_id: str,
    draft_kind: str, payload: Mapping[str, Any], draft_id: str | None = None,
    source_id: str = "",
) -> str:
    identity = _draft_id(draft_id)
    actor = _identity(actor_id, field="actor_id")
    device = _identity(device_id, field="device_id")
    kind = _kind(draft_kind)
    source = str(source_id or "").strip()
    rendered, digest = _serialized(payload)
    stamp = now_text()
    existing = connection.execute(
        """
        SELECT actor_id, device_id, draft_kind, status
        FROM central_office_work_drafts_v32 WHERE draft_id=?
        """,
        (identity,),
    ).fetchone()
    if existing:
        if tuple(existing[:3]) != (actor, device, kind):
            raise OfficeDraftError("Work draft identity belongs to another owner or form.")
        if existing[3] != "ACTIVE":
            raise OfficeDraftError("A completed or abandoned work draft cannot be changed.")
        connection.execute(
            """
            UPDATE central_office_work_drafts_v32
            SET source_id=?, payload_json=?, payload_sha256=?, updated_at=?
            WHERE draft_id=?
            """,
            (source, rendered, digest, stamp, identity),
        )
    else:
        connection.execute(
            """
            INSERT INTO central_office_work_drafts_v32(
                draft_id, actor_id, device_id, draft_kind, source_id,
                payload_json, payload_sha256, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (identity, actor, device, kind, source, rendered, digest, stamp, stamp),
        )
    return identity


def _decoded(row: sqlite3.Row | tuple[Any, ...], columns: tuple[str, ...]) -> dict[str, Any]:
    result = dict(zip(columns, row))
    rendered = str(result.pop("payload_json"))
    expected = str(result.pop("payload_sha256"))
    actual = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    if actual != expected:
        raise OfficeDraftError(
            f"Local Office work draft {result.get('draft_id')} is corrupted."
        )
    try:
        result["payload"] = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise OfficeDraftError("Local Office work draft is not valid JSON.") from exc
    return result


_DRAFT_COLUMNS = (
    "draft_id", "actor_id", "device_id", "draft_kind", "source_id",
    "payload_json", "payload_sha256", "status", "created_at", "updated_at",
    "completed_at",
)


def load_office_work_draft(
    connection: sqlite3.Connection, draft_id: str, *, actor_id: str, device_id: str,
) -> dict[str, Any] | None:
    identity = _draft_id(draft_id)
    row = connection.execute(
        f"SELECT {', '.join(_DRAFT_COLUMNS)} FROM central_office_work_drafts_v32 "
        "WHERE draft_id=? AND actor_id=? AND device_id=?",
        (identity, _identity(actor_id, field="actor_id"),
         _identity(device_id, field="device_id")),
    ).fetchone()
    return _decoded(row, _DRAFT_COLUMNS) if row else None


def list_active_office_work_drafts(
    connection: sqlite3.Connection, *, actor_id: str, device_id: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT {', '.join(_DRAFT_COLUMNS)} FROM central_office_work_drafts_v32 "
        "WHERE actor_id=? AND device_id=? AND status='ACTIVE' "
        "ORDER BY updated_at DESC, draft_id",
        (_identity(actor_id, field="actor_id"), _identity(device_id, field="device_id")),
    ).fetchall()
    return [_decoded(row, _DRAFT_COLUMNS) for row in rows]


def set_office_work_draft_status(
    connection: sqlite3.Connection, draft_id: str, *, actor_id: str,
    device_id: str, status: str,
) -> None:
    selected = str(status or "").strip().upper()
    if selected not in {"COMPLETED", "ABANDONED"}:
        raise OfficeDraftError("Work draft can only be completed or abandoned.")
    stamp = now_text()
    changed = connection.execute(
        """
        UPDATE central_office_work_drafts_v32
        SET status=?, updated_at=?, completed_at=?
        WHERE draft_id=? AND actor_id=? AND device_id=? AND status='ACTIVE'
        """,
        (
            selected, stamp, stamp, _draft_id(draft_id),
            _identity(actor_id, field="actor_id"),
            _identity(device_id, field="device_id"),
        ),
    ).rowcount
    if changed != 1:
        raise OfficeDraftError("Active work draft does not exist for this owner.")
