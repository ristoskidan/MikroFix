"""Create Service sync operations inside the caller's SQLite transaction.

These helpers only write to the local outbox. They never contact the server or
commit, so an offline Service computer can keep completing work safely.
"""

from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mikrofix_sync_contracts import (
    SyncOperationEnvelope,
    validated_create_service_order_payload,
    validated_technical_completion_payload,
)
from mikrofix_sync_store import (
    enqueue_sync_operation,
    ensure_sync_store_schema,
    next_device_sequence,
)
from mikrofix_device_signing import sign_operation


def _with_offline_actor_signature(
    envelope: SyncOperationEnvelope,
    *,
    actor_user_id: str,
    actor_authorization_version: int,
    signing_private_key,
) -> SyncOperationEnvelope:
    supplied = bool(actor_user_id or actor_authorization_version or signing_private_key)
    if not supplied:
        return envelope
    if not actor_user_id or int(actor_authorization_version) < 1 or signing_private_key is None:
        raise ValueError("Offline actor identity and Service signing key must be complete.")
    identified = SyncOperationEnvelope(
        **{
            **envelope.signing_dict(),
            "actor_user_id": str(actor_user_id),
            "actor_authorization_version": int(actor_authorization_version),
        }
    )
    return sign_operation(identified, signing_private_key)


def service_device_id(premise: str, electronic_device: str) -> str:
    """Stable first-phase identity of the dedicated Service computer."""
    return (
        f"SERVICE-{str(premise or '').strip()}-"
        f"{str(electronic_device or '').strip()}"
    ).upper()


def _offset_timestamp(value: object) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.astimezone()
    return parsed.isoformat(timespec="seconds")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def enqueue_service_order_created(
    connection: sqlite3.Connection,
    record: dict,
    *,
    actor_id: str,
    device_id: str,
    actor_user_id: str = "",
    actor_authorization_version: int = 0,
    signing_private_key=None,
) -> SyncOperationEnvelope:
    """Journal one locally-created intake for later ordered synchronization."""
    ensure_sync_store_schema(connection)
    payload = dict(record)
    payload.update(
        {
            "sync_uuid": str(record["sync_uuid"]),
            "manual_identity_confirmed": (
                str(record.get("identity_method") or "").upper() == "MANUAL_OTHER"
            ),
            "microdetect_confirmed": bool(record.get("microdetect_confirmed")),
        }
    )
    payload = validated_create_service_order_payload(payload)
    envelope = SyncOperationEnvelope(
        operation_id=str(uuid4()),
        operation_type="CreateServiceOrder",
        aggregate_type="SERVICE_ORDER",
        aggregate_id=str(record["sync_uuid"]),
        expected_version=0,
        actor_id=str(actor_id or record.get("operator_code") or "SERVICE"),
        device_id=str(device_id),
        device_sequence=next_device_sequence(connection, device_id),
        occurred_at=_offset_timestamp(record["created_at"]),
        payload=payload,
    )
    envelope = _with_offline_actor_signature(
        envelope,
        actor_user_id=actor_user_id,
        actor_authorization_version=actor_authorization_version,
        signing_private_key=signing_private_key,
    )
    enqueue_sync_operation(connection, envelope)
    connection.execute(
        "UPDATE service_orders SET origin_device_id=?,sync_state='PENDING' WHERE id=?",
        (str(device_id), int(record["id"])),
    )
    return envelope


def enqueue_service_technical_completion(
    connection: sqlite3.Connection,
    *,
    service_order_id: int,
    service_id: int,
    completed_at: datetime,
    work_summary: str,
    findings: str,
    actor_id: str,
    device_id: str,
    data_dir: Path,
    actor_user_id: str = "",
    actor_authorization_version: int = 0,
    signing_private_key=None,
) -> SyncOperationEnvelope:
    """Journal a technical result before its editable local rows are removed."""
    ensure_sync_store_schema(connection)
    order = connection.execute(
        "SELECT sync_uuid FROM service_orders WHERE id=?", (int(service_order_id),)
    ).fetchone()
    if not order:
        raise ValueError("Service order does not exist.")

    intervals = []
    for row in connection.execute(
        "SELECT interval_uuid,started_by,started_at,ended_at,elapsed_seconds "
        "FROM service_work_sessions WHERE service_order_id=? ORDER BY id",
        (int(service_order_id),),
    ):
        if not row[3]:
            raise ValueError("A technical completion cannot contain a running interval.")
        intervals.append(
            {
                "interval_id": str(row[0]),
                "technician_id": str(row[1] or actor_id or "SERVICE"),
                "started_at": _offset_timestamp(row[2]),
                "ended_at": _offset_timestamp(row[3]),
                "elapsed_seconds": int(row[4] or 0),
            }
        )

    parts = [
        {
            "line_id": str(row[0]),
            "article_id": str(row[1]),
            "description": str(row[2] or row[3] or row[1]),
            "quantity": "1",
            "unit_price": str(row[4] or 0),
            "barcode": str(row[3] or "").strip(),
            "lot_serial": str(row[5] or "").strip(),
        }
        for row in connection.execute(
            "SELECT line_uuid,article_id,part_name,barcode,unit_price,lot_serial "
            "FROM service_order_parts WHERE service_order_id=? ORDER BY line_no,id",
            (int(service_order_id),),
        )
    ]
    tests = [
        {"stage": str(row[0]), "name": str(row[1]), "status": str(row[2])}
        for row in connection.execute(
            "SELECT stage,test_name,status FROM service_tests "
            "WHERE service_id=? ORDER BY id",
            (int(service_id),),
        )
    ]
    billing_lines = [
        {
            "line_id": str(row[0]),
            "line_kind": str(row[1]),
            "description": str(row[2]),
            "quantity": str(row[3]),
            "unit_price": str(row[4]),
            "billable_minutes": int(row[5] or 0),
            "tariff_version": str(row[6] or ""),
        }
        for row in connection.execute(
            "SELECT line_uuid,line_kind,description,quantity,unit_price,"
            "billable_minutes,tariff_version FROM service_manual_lines "
            "WHERE service_id=? ORDER BY id",
            (int(service_id),),
        )
    ]
    attachments = []
    for stage, rel_path in connection.execute(
        "SELECT stage,rel_path FROM service_photos WHERE service_id=? ORDER BY id",
        (int(service_id),),
    ):
        path = Path(data_dir) / str(rel_path)
        attachments.append(
            {
                "attachment_id": str(uuid4()),
                "sha256": _file_sha256(path),
                "size_bytes": path.stat().st_size,
                "media_type": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                "stage": str(stage or ""),
            }
        )

    aggregate_uuid = str(order[0])
    payload = validated_technical_completion_payload(
        {
            "service_order_id": aggregate_uuid,
            # Central version 1 is established by CreateServiceOrder. Local
            # timer/draft revisions deliberately do not alter that version.
            "expected_version": 1,
            "completed_at": _offset_timestamp(completed_at),
            "billing_disposition": "INVOICE_REQUIRED",
            "work_summary": str(work_summary or "").strip(),
            "findings": str(findings or "").strip(),
            "billing_lines": billing_lines,
            "intervals": intervals,
            "parts": parts,
            "tests": tests,
            "attachments": attachments,
        }
    )
    envelope = SyncOperationEnvelope(
        operation_id=str(uuid4()),
        operation_type="CompleteServiceTechnically",
        aggregate_type="SERVICE_ORDER",
        aggregate_id=aggregate_uuid,
        expected_version=1,
        actor_id=str(actor_id or "SERVICE"),
        device_id=str(device_id),
        device_sequence=next_device_sequence(connection, device_id),
        occurred_at=_offset_timestamp(completed_at),
        payload=payload,
    )
    envelope = _with_offline_actor_signature(
        envelope,
        actor_user_id=actor_user_id,
        actor_authorization_version=actor_authorization_version,
        signing_private_key=signing_private_key,
    )
    enqueue_sync_operation(connection, envelope)
    connection.execute(
        "UPDATE service_orders SET origin_device_id=?,sync_state='PENDING' WHERE id=?",
        (str(device_id), int(service_order_id)),
    )
    return envelope
