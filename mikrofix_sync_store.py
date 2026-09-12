"""Local SQLite outbox primitives for the future MikroFIX Service sync client.

This module performs no network calls and never commits a caller's transaction.
Business data and an outbox row can therefore be persisted or rolled back as
one local SQLite unit.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from mikrofix_sync_contracts import (
    SyncOperationEnvelope,
    canonical_json,
    payload_sha256,
)


OUTBOX_STATUSES = {
    "PENDING",
    "SENDING",
    "ACKNOWLEDGED",
    "UNCERTAIN",
    "REJECTED",
    "SUPERSEDED",
}


class SyncStoreError(RuntimeError):
    """The local sync journal cannot safely accept a requested transition."""


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_sync_store_schema(connection: sqlite3.Connection) -> None:
    """Create the additive v32 sync tables without committing the connection."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_device_state_v32(
            device_id TEXT PRIMARY KEY,
            next_sequence INTEGER NOT NULL DEFAULT 1,
            central_checkpoint INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            CHECK(next_sequence>=1),
            CHECK(central_checkpoint>=0)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_outbox_v32(
            local_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL UNIQUE,
            operation_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_uuid TEXT NOT NULL,
            expected_version INTEGER NOT NULL,
            actor_id TEXT NOT NULL,
            actor_user_uuid TEXT NOT NULL DEFAULT '',
            actor_authorization_version INTEGER NOT NULL DEFAULT 0,
            device_id TEXT NOT NULL,
            device_sequence INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            contract_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            workstation_signature TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_attempt_at TEXT,
            acknowledged_at TEXT,
            error_code TEXT,
            error_text TEXT,
            UNIQUE(device_id,device_sequence),
            CHECK(expected_version>=0),
            CHECK(device_sequence>=1),
            CHECK(status IN (
                'PENDING','SENDING','ACKNOWLEDGED','UNCERTAIN',
                'REJECTED','SUPERSEDED'
            ))
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sync_outbox_dispatch_v32
        ON sync_outbox_v32(status,device_id,device_sequence)
        """
    )
    outbox_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(sync_outbox_v32)")
    }
    for column, declaration in {
        "actor_user_uuid": "TEXT NOT NULL DEFAULT ''",
        "actor_authorization_version": "INTEGER NOT NULL DEFAULT 0",
        "workstation_signature": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in outbox_columns:
            connection.execute(
                f"ALTER TABLE sync_outbox_v32 ADD COLUMN {column} {declaration}"
            )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_receipts_v32(
            operation_id TEXT PRIMARY KEY,
            result_code TEXT NOT NULL,
            response_json TEXT NOT NULL,
            response_sha256 TEXT NOT NULL,
            central_sequence INTEGER,
            received_at TEXT NOT NULL,
            FOREIGN KEY(operation_id) REFERENCES sync_outbox_v32(operation_id),
            CHECK(central_sequence IS NULL OR central_sequence>=0)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_inbox_v32(
            central_sequence INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_uuid TEXT NOT NULL,
            aggregate_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            received_at TEXT NOT NULL,
            applied_at TEXT,
            CHECK(central_sequence>=1),
            CHECK(aggregate_version>=1)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_conflicts_v32(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL UNIQUE,
            aggregate_uuid TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            CHECK(status IN ('OPEN','RESOLVED')),
            FOREIGN KEY(operation_id) REFERENCES sync_outbox_v32(operation_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_conflicts_open_v32 "
        "ON sync_conflicts_v32(status,created_at)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS central_inventory_snapshot_v32(
            article_uuid TEXT PRIMARY KEY,
            main_barcode TEXT NOT NULL,
            description TEXT NOT NULL,
            track_mode TEXT NOT NULL,
            quantity_available TEXT NOT NULL,
            current_sale_price TEXT NOT NULL,
            row_version INTEGER NOT NULL,
            blocked INTEGER NOT NULL DEFAULT 0,
            blocked_reason TEXT NOT NULL DEFAULT '',
            identities_json TEXT NOT NULL,
            central_sequence INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(track_mode IN ('QUANTITY','SERIAL')),
            CHECK(row_version>=1),
            CHECK(central_sequence>=0)
        )
        """
    )
    snapshot_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='central_inventory_snapshot_v32'"
    ).fetchone()
    if snapshot_sql and "central_sequence>=1" in str(snapshot_sql[0]).replace(" ", ""):
        # Additive local migration: a complete bootstrap may legitimately have
        # sequence 0 when central stock was imported before the first event.
        connection.execute(
            "ALTER TABLE central_inventory_snapshot_v32 "
            "RENAME TO central_inventory_snapshot_v32_old"
        )
        connection.execute(
            """
            CREATE TABLE central_inventory_snapshot_v32(
                article_uuid TEXT PRIMARY KEY,
                main_barcode TEXT NOT NULL,
                description TEXT NOT NULL,
                track_mode TEXT NOT NULL,
                quantity_available TEXT NOT NULL,
                current_sale_price TEXT NOT NULL,
                row_version INTEGER NOT NULL,
                blocked INTEGER NOT NULL DEFAULT 0,
                blocked_reason TEXT NOT NULL DEFAULT '',
                identities_json TEXT NOT NULL,
                central_sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(track_mode IN ('QUANTITY','SERIAL')),
                CHECK(row_version>=1),
                CHECK(central_sequence>=0)
            )
            """
        )
        connection.execute(
            "INSERT INTO central_inventory_snapshot_v32 SELECT * "
            "FROM central_inventory_snapshot_v32_old"
        )
        connection.execute("DROP TABLE central_inventory_snapshot_v32_old")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_inventory_state_v32(
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
            snapshot_complete INTEGER NOT NULL DEFAULT 0,
            snapshot_sequence INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            CHECK(snapshot_complete IN (0,1)),
            CHECK(snapshot_sequence>=0)
        )
        """
    )


def _row_dict(cursor: sqlite3.Cursor, row) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(zip((item[0] for item in cursor.description), row))


def outbox_operation(
    connection: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    cursor = connection.execute(
        "SELECT * FROM sync_outbox_v32 WHERE operation_id=?", (str(operation_id),)
    )
    return _row_dict(cursor, cursor.fetchone())


def operation_receipt(
    connection: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    cursor = connection.execute(
        "SELECT response_json FROM sync_receipts_v32 WHERE operation_id=?",
        (str(operation_id),),
    )
    row = cursor.fetchone()
    return json.loads(str(row[0])) if row else None


def next_device_sequence(connection: sqlite3.Connection, device_id: str) -> int:
    """Reserve the next device-local sequence inside the caller transaction."""
    device = str(device_id or "").strip()
    if not device:
        raise SyncStoreError("device_id is required.")
    stamp = now_text()
    connection.execute(
        """
        INSERT INTO sync_device_state_v32(device_id,next_sequence,updated_at)
        VALUES(?,1,?) ON CONFLICT(device_id) DO NOTHING
        """,
        (device, stamp),
    )
    row = connection.execute(
        "SELECT next_sequence FROM sync_device_state_v32 WHERE device_id=?",
        (device,),
    ).fetchone()
    sequence = int(row[0])
    connection.execute(
        "UPDATE sync_device_state_v32 SET next_sequence=?,updated_at=? WHERE device_id=?",
        (sequence + 1, stamp, device),
    )
    return sequence


def enqueue_sync_operation(
    connection: sqlite3.Connection, envelope: SyncOperationEnvelope
) -> dict[str, Any]:
    """Persist one operation idempotently without committing."""
    existing = outbox_operation(connection, envelope.operation_id)
    if existing:
        if (
            existing["operation_type"] != envelope.operation_type
            or existing["aggregate_type"] != envelope.aggregate_type
            or existing["aggregate_uuid"] != envelope.aggregate_id
            or int(existing["expected_version"]) != envelope.expected_version
            or existing["actor_id"] != envelope.actor_id
            or existing["actor_user_uuid"] != envelope.actor_user_id
            or int(existing["actor_authorization_version"])
            != envelope.actor_authorization_version
            or existing["device_id"] != envelope.device_id
            or existing["occurred_at"] != envelope.occurred_at
            or int(existing["contract_version"]) != envelope.contract_version
            or existing["payload_sha256"] != envelope.payload_hash
            or existing["workstation_signature"] != envelope.workstation_signature
            or int(existing["device_sequence"]) != envelope.device_sequence
        ):
            raise SyncStoreError(
                "The operation_id already belongs to a different sync operation."
            )
        return existing
    stamp = now_text()
    try:
        connection.execute(
            """
            INSERT INTO sync_outbox_v32(
                operation_id,operation_type,aggregate_type,aggregate_uuid,
                expected_version,actor_id,actor_user_uuid,
                actor_authorization_version,device_id,device_sequence,occurred_at,
                contract_version,payload_json,payload_sha256,workstation_signature,
                status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'PENDING',?,?)
            """,
            (
                envelope.operation_id, envelope.operation_type,
                envelope.aggregate_type, envelope.aggregate_id,
                envelope.expected_version, envelope.actor_id,
                envelope.actor_user_id, envelope.actor_authorization_version,
                envelope.device_id, envelope.device_sequence, envelope.occurred_at,
                envelope.contract_version, canonical_json(envelope.payload),
                envelope.payload_hash, envelope.workstation_signature, stamp, stamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        conflict = connection.execute(
            "SELECT operation_id FROM sync_outbox_v32 "
            "WHERE device_id=? AND device_sequence=?",
            (envelope.device_id, envelope.device_sequence),
        ).fetchone()
        if conflict:
            raise SyncStoreError(
                "The device sequence is already assigned to another operation."
            ) from exc
        raise
    return outbox_operation(connection, envelope.operation_id) or {}


def list_dispatchable_operations(
    connection: sqlite3.Connection, device_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    cursor = connection.execute(
        """
        SELECT * FROM sync_outbox_v32
        WHERE device_id=? AND status IN ('PENDING','UNCERTAIN')
        ORDER BY device_sequence LIMIT ?
        """,
        (str(device_id), max(1, int(limit))),
    )
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def mark_operation_sending(
    connection: sqlite3.Connection, operation_id: str
) -> dict[str, Any]:
    stamp = now_text()
    cursor = connection.execute(
        """
        UPDATE sync_outbox_v32
        SET status='SENDING',attempt_count=attempt_count+1,
            last_attempt_at=?,updated_at=?,error_code=NULL,error_text=NULL
        WHERE operation_id=? AND status IN ('PENDING','UNCERTAIN')
        """,
        (stamp, stamp, str(operation_id)),
    )
    if cursor.rowcount != 1:
        current = outbox_operation(connection, operation_id)
        if current and current["status"] == "ACKNOWLEDGED":
            return current
        raise SyncStoreError("The operation is not dispatchable.")
    return outbox_operation(connection, operation_id) or {}


def mark_operation_uncertain(
    connection: sqlite3.Connection, operation_id: str, error: object = ""
) -> None:
    cursor = connection.execute(
        """
        UPDATE sync_outbox_v32 SET status='UNCERTAIN',updated_at=?,error_text=?
        WHERE operation_id=? AND status='SENDING'
        """,
        (now_text(), str(error)[:2000], str(operation_id)),
    )
    if cursor.rowcount != 1:
        raise SyncStoreError("Only a sending operation can become uncertain.")


def acknowledge_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    result_code: str,
    response: Any,
    central_sequence: int | None = None,
) -> dict[str, Any]:
    """Store the server receipt and close the operation idempotently."""
    response_text = canonical_json(response)
    response_hash = payload_sha256(response)
    existing = connection.execute(
        "SELECT result_code,response_sha256,central_sequence "
        "FROM sync_receipts_v32 WHERE operation_id=?",
        (str(operation_id),),
    ).fetchone()
    if existing:
        if (
            str(existing[0]) != str(result_code)
            or str(existing[1]) != response_hash
            or existing[2] != central_sequence
        ):
            raise SyncStoreError("The server receipt changed for an operation.")
        return outbox_operation(connection, operation_id) or {}
    current = outbox_operation(connection, operation_id)
    if not current:
        raise SyncStoreError("The operation does not exist.")
    if current["status"] not in {"SENDING", "UNCERTAIN", "PENDING"}:
        raise SyncStoreError("The operation cannot be acknowledged from its state.")
    stamp = now_text()
    connection.execute(
        """
        INSERT INTO sync_receipts_v32(
            operation_id,result_code,response_json,response_sha256,
            central_sequence,received_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            str(operation_id), str(result_code), response_text, response_hash,
            int(central_sequence) if central_sequence is not None else None, stamp,
        ),
    )
    connection.execute(
        """
        UPDATE sync_outbox_v32
        SET status='ACKNOWLEDGED',acknowledged_at=?,updated_at=?,
            error_code=NULL,error_text=NULL
        WHERE operation_id=?
        """,
        (stamp, stamp, str(operation_id)),
    )
    return outbox_operation(connection, operation_id) or {}


def reject_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    error_code: str,
    error_text: str,
) -> None:
    cursor = connection.execute(
        """
        UPDATE sync_outbox_v32
        SET status='REJECTED',error_code=?,error_text=?,updated_at=?
        WHERE operation_id=? AND status IN ('PENDING','SENDING','UNCERTAIN')
        """,
        (str(error_code), str(error_text)[:2000], now_text(), str(operation_id)),
    )
    if cursor.rowcount != 1:
        raise SyncStoreError("The operation cannot be rejected from its state.")


def record_sync_conflict(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    conflict_type: str,
    details: list[dict[str, Any]],
) -> None:
    """Persist a visible business conflict and mark its local service order."""
    ensure_sync_store_schema(connection)
    operation = outbox_operation(connection, operation_id)
    if not operation:
        raise SyncStoreError("The conflicting operation does not exist.")
    details_json = json.dumps(
        list(details), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    existing = connection.execute(
        "SELECT conflict_type,details_json FROM sync_conflicts_v32 "
        "WHERE operation_id=?",
        (str(operation_id),),
    ).fetchone()
    if existing:
        if tuple(existing) != (str(conflict_type), details_json):
            raise SyncStoreError("The sync conflict changed for an operation.")
        return
    connection.execute(
        "INSERT INTO sync_conflicts_v32(operation_id,aggregate_uuid,conflict_type,"
        "details_json,status,created_at) VALUES(?,?,?,?, 'OPEN',?)",
        (
            str(operation_id), str(operation["aggregate_uuid"]),
            str(conflict_type), details_json, now_text(),
        ),
    )
    has_orders = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_orders'"
    ).fetchone()
    if has_orders:
        connection.execute(
            "UPDATE service_orders SET sync_state='SYNC_CONFLICT' WHERE sync_uuid=?",
            (str(operation["aggregate_uuid"]),),
        )


def list_open_sync_conflicts(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return unresolved conflicts with user-facing local service identity."""
    has_conflicts = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_conflicts_v32'"
    ).fetchone()
    if not has_conflicts:
        ensure_sync_store_schema(connection)
    has_orders = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_orders'"
    ).fetchone()
    if has_orders:
        rows = connection.execute(
            "SELECT c.operation_id,c.aggregate_uuid,c.conflict_type,c.details_json,"
            "c.created_at,COALESCE(o.order_no,'') FROM sync_conflicts_v32 c "
            "LEFT JOIN service_orders o ON o.sync_uuid=c.aggregate_uuid "
            "WHERE c.status='OPEN' ORDER BY c.created_at,c.id"
        ).fetchall()
    else:
        rows = [(*row, "") for row in connection.execute(
            "SELECT operation_id,aggregate_uuid,conflict_type,details_json,created_at "
            "FROM sync_conflicts_v32 WHERE status='OPEN' ORDER BY created_at,id"
        ).fetchall()]
    return [
        {
            "operation_id": str(row[0]), "aggregate_uuid": str(row[1]),
            "conflict_type": str(row[2]), "details": json.loads(str(row[3])),
            "created_at": str(row[4]), "order_no": str(row[5] or ""),
        }
        for row in rows
    ]


def recover_stale_sending(
    connection: sqlite3.Connection, *, stale_after_seconds: int = 300
) -> int:
    """Move interrupted sends to UNCERTAIN so result lookup happens first."""
    threshold = (
        datetime.now().astimezone() - timedelta(seconds=max(1, stale_after_seconds))
    ).isoformat(timespec="seconds")
    cursor = connection.execute(
        """
        UPDATE sync_outbox_v32
        SET status='UNCERTAIN',updated_at=?,
            error_text=COALESCE(error_text,'Interrupted while awaiting server response.')
        WHERE status='SENDING' AND last_attempt_at<?
        """,
        (now_text(), threshold),
    )
    return int(cursor.rowcount)


def central_checkpoint(connection: sqlite3.Connection, device_id: str) -> int:
    row = connection.execute(
        "SELECT central_checkpoint FROM sync_device_state_v32 WHERE device_id=?",
        (str(device_id),),
    ).fetchone()
    return int(row[0]) if row else 0


def advance_central_checkpoint(
    connection: sqlite3.Connection, device_id: str, sequence: int
) -> int:
    """Advance, but never rewind, the last durably received central sequence."""
    device = str(device_id or "").strip()
    value = int(sequence)
    if not device or value < 0:
        raise SyncStoreError("A device and non-negative checkpoint are required.")
    stamp = now_text()
    connection.execute(
        """
        INSERT INTO sync_device_state_v32(
            device_id,next_sequence,central_checkpoint,updated_at
        ) VALUES(?,1,?,?)
        ON CONFLICT(device_id) DO UPDATE SET
            central_checkpoint=MAX(central_checkpoint,excluded.central_checkpoint),
            updated_at=excluded.updated_at
        """,
        (device, value, stamp),
    )
    return central_checkpoint(connection, device)


def record_incoming_change(
    connection: sqlite3.Connection,
    *,
    central_sequence: int,
    event_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_uuid: str,
    aggregate_version: int,
    payload: dict[str, Any],
) -> bool:
    """Journal one central change idempotently without applying projections.

    Returns ``True`` for a new row and ``False`` for an identical replay.  A
    sequence or event identity reused for different content is corruption and
    must stop synchronization.
    """
    sequence = int(central_sequence)
    version = int(aggregate_version)
    if sequence < 1 or version < 1:
        raise SyncStoreError("Central sequence and aggregate version must be positive.")
    payload_json = canonical_json(payload)
    payload_hash = payload_sha256(payload)
    existing = connection.execute(
        "SELECT event_id,event_type,aggregate_type,aggregate_uuid,"
        "aggregate_version,payload_sha256 FROM sync_inbox_v32 "
        "WHERE central_sequence=?",
        (sequence,),
    ).fetchone()
    if existing:
        expected = (
            str(event_id),
            str(event_type),
            str(aggregate_type),
            str(aggregate_uuid),
            version,
            payload_hash,
        )
        if tuple(existing) != expected:
            raise SyncStoreError("A central sequence was replayed with different data.")
        return False
    conflicting_event = connection.execute(
        "SELECT central_sequence FROM sync_inbox_v32 WHERE event_id=?",
        (str(event_id),),
    ).fetchone()
    if conflicting_event:
        raise SyncStoreError("A central event_id was reused at another sequence.")
    connection.execute(
        """
        INSERT INTO sync_inbox_v32(
            central_sequence,event_id,event_type,aggregate_type,aggregate_uuid,
            aggregate_version,payload_json,payload_sha256,received_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            sequence,
            str(event_id),
            str(event_type),
            str(aggregate_type),
            str(aggregate_uuid),
            version,
            payload_json,
            payload_hash,
            now_text(),
        ),
    )
    return True


def apply_inventory_updates(
    connection: sqlite3.Connection, *, central_sequence: int,
    payload: dict[str, Any],
) -> int:
    """Apply a central stock projection without inventing local stock events."""
    updates = payload.get("inventory_updates", [])
    if not isinstance(updates, list):
        raise SyncStoreError("inventory_updates must be a list.")
    applied = 0
    for item in updates:
        if not isinstance(item, dict):
            raise SyncStoreError("An inventory update must be an object.")
        article_uuid = str(item.get("article_id") or "").strip()
        identities = item.get("identities")
        track_mode = str(item.get("track_mode") or "").strip().upper()
        if not article_uuid or not isinstance(identities, list):
            raise SyncStoreError("Inventory update identity data is invalid.")
        if track_mode not in {"QUANTITY", "SERIAL"}:
            raise SyncStoreError("Inventory update track_mode is invalid.")
        version = int(item.get("row_version", 0))
        if version < 1:
            raise SyncStoreError("Inventory update row_version must be positive.")
        connection.execute(
            """
            INSERT INTO central_inventory_snapshot_v32(
                article_uuid,main_barcode,description,track_mode,
                quantity_available,current_sale_price,row_version,blocked,
                blocked_reason,identities_json,central_sequence,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(article_uuid) DO UPDATE SET
                main_barcode=excluded.main_barcode,
                description=excluded.description,
                track_mode=excluded.track_mode,
                quantity_available=excluded.quantity_available,
                current_sale_price=excluded.current_sale_price,
                row_version=excluded.row_version,
                blocked=excluded.blocked,
                blocked_reason=excluded.blocked_reason,
                identities_json=excluded.identities_json,
                central_sequence=excluded.central_sequence,
                updated_at=excluded.updated_at
            WHERE excluded.row_version > central_inventory_snapshot_v32.row_version
               OR (
                    excluded.row_version = central_inventory_snapshot_v32.row_version
                    AND excluded.central_sequence >=
                        central_inventory_snapshot_v32.central_sequence
               )
            """,
            (
                article_uuid, str(item.get("main_barcode") or ""),
                str(item.get("description") or ""), track_mode,
                str(item.get("quantity_available") or "0"),
                str(item.get("current_sale_price") or "0"), version,
                1 if item.get("blocked") else 0,
                str(item.get("blocked_reason") or ""),
                canonical_json({"identities": identities}), int(central_sequence),
                now_text(),
            ),
        )
        applied += 1
    return applied


def inventory_snapshot_complete(connection: sqlite3.Connection) -> bool:
    ensure_sync_store_schema(connection)
    row = connection.execute(
        "SELECT snapshot_complete FROM sync_inventory_state_v32 WHERE singleton_id=1"
    ).fetchone()
    return bool(row and row[0])


def apply_complete_inventory_snapshot(
    connection: sqlite3.Connection, *, snapshot_sequence: int,
    inventory_updates: list[dict[str, Any]],
) -> int:
    """Replace the bootstrap projection with one server-declared complete view."""
    ensure_sync_store_schema(connection)
    sequence = int(snapshot_sequence)
    if sequence < 0 or not isinstance(inventory_updates, list):
        raise SyncStoreError("A complete inventory snapshot is invalid.")
    newest_local = int(connection.execute(
        "SELECT COALESCE(MAX(central_sequence),0) "
        "FROM central_inventory_snapshot_v32"
    ).fetchone()[0])
    if sequence < newest_local:
        raise SyncStoreError(
            "A complete inventory snapshot is older than the local projection."
        )
    article_ids = []
    for update in inventory_updates:
        if not isinstance(update, dict) or not str(update.get("article_id") or "").strip():
            raise SyncStoreError("A complete inventory snapshot has an invalid article.")
        article_ids.append(str(update["article_id"]).strip())
    if len(article_ids) != len(set(article_ids)):
        raise SyncStoreError("A complete inventory snapshot repeats an article.")

    apply_inventory_updates(
        connection, central_sequence=sequence,
        payload={"inventory_updates": inventory_updates},
    )
    if article_ids:
        placeholders = ",".join("?" for _ in article_ids)
        connection.execute(
            f"DELETE FROM central_inventory_snapshot_v32 "
            f"WHERE article_uuid NOT IN ({placeholders})",
            article_ids,
        )
    else:
        connection.execute("DELETE FROM central_inventory_snapshot_v32")
    connection.execute(
        """
        INSERT INTO sync_inventory_state_v32(
            singleton_id,snapshot_complete,snapshot_sequence,updated_at
        ) VALUES(1,1,?,?)
        ON CONFLICT(singleton_id) DO UPDATE SET
            snapshot_complete=1,
            snapshot_sequence=MAX(snapshot_sequence,excluded.snapshot_sequence),
            updated_at=excluded.updated_at
        """,
        (sequence, now_text()),
    )
    return len(inventory_updates)
