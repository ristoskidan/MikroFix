"""Trajni dnevnik kritičnih operacij in izhodnih dogodkov MikroFIX.

Modul ne izvaja poslovnega koraka namesto klicatelja. Zagotovi pa, da ima
vsak tak korak enoličen ID, nespremenljiv vhod in preverljiv zaključni dogodek.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any


FINAL_STATUSES = {"COMPLETED", "CANCELLED"}
RECOVERY_STATUSES = {"PREPARED", "APPLYING", "FAILED", "NEEDS_RECOVERY"}


class OperationError(RuntimeError):
    """Operacije ni mogoče varno pripraviti ali nadaljevati."""


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def new_operation_id(prefix: str = "OP") -> str:
    clean = "".join(ch for ch in str(prefix).upper() if ch.isalnum() or ch == "-")
    return f"{clean or 'OP'}-{uuid.uuid4()}"


def ensure_operation_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS operation_journal_v31(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL UNIQUE,
            operation_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id INTEGER,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            operator_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            error_text TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            CHECK(status IN (
                'PREPARED','APPLYING','COMPLETED','FAILED',
                'NEEDS_RECOVERY','CANCELLED'
            ))
        );
        CREATE INDEX IF NOT EXISTS idx_operation_recovery_v31
            ON operation_journal_v31(status, updated_at);

        CREATE TABLE IF NOT EXISTS operation_outbox_v31(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            operation_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id INTEGER,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            previous_event_sha256 TEXT NOT NULL,
            event_sha256 TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TEXT NOT NULL,
            synced_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            error_text TEXT,
            UNIQUE(operation_id, event_type),
            FOREIGN KEY(operation_id) REFERENCES operation_journal_v31(operation_id),
            CHECK(status IN ('PENDING','SYNCED','FAILED'))
        );
        CREATE INDEX IF NOT EXISTS idx_operation_outbox_pending_v31
            ON operation_outbox_v31(status, sequence);
        """
    )


def _row_dict(connection: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    cursor = connection.execute(
        "SELECT * FROM operation_journal_v31 WHERE operation_id=?",
        (str(operation_id),),
    )
    row = cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description), row)) if row else None


def prepare_operation(
    connection: sqlite3.Connection,
    *,
    operation_type: str,
    aggregate_type: str,
    payload: Any,
    operator_code: str,
    aggregate_id: int | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Pripravi idempotentno operacijo.

    Ponovitev istega ID z enakim vhodom vrne isti zapis. Isti ID z drugim
    vhodom je napaka, saj bi pomenil tiho prepisovanje poslovnega dogodka.
    """
    ensure_operation_schema(connection)
    op_id = str(operation_id or new_operation_id(operation_type[:12]))
    payload_text = canonical_json(payload)
    digest = payload_sha256(payload)
    existing = _row_dict(connection, op_id)
    if existing:
        if (
            existing["operation_type"] != str(operation_type)
            or existing["aggregate_type"] != str(aggregate_type)
            or existing["payload_sha256"] != digest
        ):
            raise OperationError(
                "Isti ID operacije je bil že uporabljen za drugačen poslovni dogodek."
            )
        return existing
    stamp = now_text()
    connection.execute(
        """
        INSERT INTO operation_journal_v31(
            operation_id,operation_type,aggregate_type,aggregate_id,status,
            payload_json,payload_sha256,result_json,operator_code,
            created_at,updated_at
        ) VALUES(?,?,?,?,'PREPARED',?,?, '{}',?,?,?)
        """,
        (
            op_id, str(operation_type), str(aggregate_type), aggregate_id,
            payload_text, digest, str(operator_code or "DR"), stamp, stamp,
        ),
    )
    return _row_dict(connection, op_id) or {}


def start_operation(connection: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    ensure_operation_schema(connection)
    current = _row_dict(connection, operation_id)
    if not current:
        raise OperationError("Operacija ni pripravljena.")
    if current["status"] == "COMPLETED":
        return current
    if current["status"] == "CANCELLED":
        raise OperationError("Preklicane operacije ni mogoče zagnati.")
    stamp = now_text()
    connection.execute(
        """
        UPDATE operation_journal_v31
        SET status='APPLYING', started_at=COALESCE(started_at,?), updated_at=?,
            error_text=NULL, attempt_count=attempt_count+1
        WHERE operation_id=?
        """,
        (stamp, stamp, operation_id),
    )
    return _row_dict(connection, operation_id) or {}


def _append_outbox_event(
    connection: sqlite3.Connection,
    *,
    operation: dict[str, Any],
    event_type: str,
    payload: Any,
) -> None:
    previous = connection.execute(
        "SELECT event_sha256 FROM operation_outbox_v31 ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    previous_hash = str(previous[0]) if previous else "GENESIS"
    payload_text = canonical_json(payload)
    digest = payload_sha256(payload)
    event_id = f"{operation['operation_id']}:{event_type}"
    stamp = now_text()
    envelope = {
        "event_id": event_id,
        "operation_id": operation["operation_id"],
        "event_type": event_type,
        "aggregate_type": operation["aggregate_type"],
        "aggregate_id": operation.get("aggregate_id"),
        "payload_sha256": digest,
        "previous_event_sha256": previous_hash,
        "created_at": stamp,
    }
    event_hash = payload_sha256(envelope)
    connection.execute(
        """
        INSERT OR IGNORE INTO operation_outbox_v31(
            event_id,operation_id,event_type,aggregate_type,aggregate_id,
            payload_json,payload_sha256,previous_event_sha256,event_sha256,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id, operation["operation_id"], event_type,
            operation["aggregate_type"], operation.get("aggregate_id"),
            payload_text, digest, previous_hash, event_hash, stamp,
        ),
    )


def complete_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    result: Any,
    event_type: str | None = None,
) -> dict[str, Any]:
    ensure_operation_schema(connection)
    current = _row_dict(connection, operation_id)
    if not current:
        raise OperationError("Operacija ni pripravljena.")
    if current["status"] == "CANCELLED":
        raise OperationError("Preklicane operacije ni mogoče zaključiti.")
    if current["status"] == "COMPLETED":
        if current["result_json"] != canonical_json(result):
            raise OperationError("Zaključene operacije ni dovoljeno spremeniti.")
        return current
    stamp = now_text()
    connection.execute(
        """
        UPDATE operation_journal_v31
        SET status='COMPLETED',result_json=?,completed_at=?,updated_at=?,error_text=NULL
        WHERE operation_id=?
        """,
        (canonical_json(result), stamp, stamp, operation_id),
    )
    updated = _row_dict(connection, operation_id) or {}
    _append_outbox_event(
        connection,
        operation=updated,
        event_type=event_type or f"{updated['operation_type']}_COMPLETED",
        payload=result,
    )
    return _row_dict(connection, operation_id) or {}


def fail_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    error: Exception | str,
    *,
    needs_recovery: bool = False,
) -> None:
    ensure_operation_schema(connection)
    status = "NEEDS_RECOVERY" if needs_recovery else "FAILED"
    connection.execute(
        """
        UPDATE operation_journal_v31
        SET status=?,error_text=?,updated_at=?
        WHERE operation_id=? AND status NOT IN ('COMPLETED','CANCELLED')
        """,
        (status, str(error)[:2000], now_text(), operation_id),
    )


def mark_stale_operations(
    connection: sqlite3.Connection, *, stale_minutes: int = 5
) -> int:
    ensure_operation_schema(connection)
    threshold = (datetime.now().astimezone() - timedelta(minutes=stale_minutes)).isoformat(
        timespec="seconds"
    )
    cursor = connection.execute(
        """
        UPDATE operation_journal_v31
        SET status='NEEDS_RECOVERY',
            error_text=COALESCE(error_text,'Program je bil prekinjen med operacijo.'),
            updated_at=?
        WHERE status='APPLYING' AND updated_at<?
        """,
        (now_text(), threshold),
    )
    return int(cursor.rowcount)


def list_recovery_operations(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_operation_schema(connection)
    cursor = connection.execute(
        """
        SELECT * FROM operation_journal_v31
        WHERE status IN ('PREPARED','APPLYING','FAILED','NEEDS_RECOVERY')
        ORDER BY created_at,id
        """
    )
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def verify_outbox_chain(connection: sqlite3.Connection) -> list[str]:
    """Vrne seznam napak; prazen seznam pomeni neprekinjeno verigo."""
    ensure_operation_schema(connection)
    errors: list[str] = []
    expected_sequence = 1
    previous_hash = "GENESIS"
    cursor = connection.execute(
        "SELECT * FROM operation_outbox_v31 ORDER BY sequence"
    )
    names = [item[0] for item in cursor.description]
    for raw in cursor.fetchall():
        row = dict(zip(names, raw))
        if int(row["sequence"]) != expected_sequence:
            errors.append(
                f"Manjka zaporedje pred dogodkom {row['event_id']} "
                f"(pričakovano {expected_sequence}, najdeno {row['sequence']})."
            )
            expected_sequence = int(row["sequence"])
        if row["previous_event_sha256"] != previous_hash:
            errors.append(f"Prekinjena veriga pred dogodkom {row['event_id']}.")
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = None
            errors.append(f"Neveljaven JSON dogodka {row['event_id']}.")
        if payload is not None and payload_sha256(payload) != row["payload_sha256"]:
            errors.append(f"Spremenjena vsebina dogodka {row['event_id']}.")
        envelope = {
            "event_id": row["event_id"],
            "operation_id": row["operation_id"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "payload_sha256": row["payload_sha256"],
            "previous_event_sha256": row["previous_event_sha256"],
            "created_at": row["created_at"],
        }
        if payload_sha256(envelope) != row["event_sha256"]:
            errors.append(f"Spremenjen ovoj dogodka {row['event_id']}.")
        previous_hash = row["event_sha256"]
        expected_sequence += 1
    return errors
