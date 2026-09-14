"""Framework-neutral Service outbox synchronization coordinator."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from mikrofix_sync_store import (
    acknowledge_operation,
    apply_complete_service_return_snapshot,
    apply_complete_inventory_snapshot,
    apply_inventory_updates,
    apply_service_part_return_updates,
    advance_central_checkpoint,
    central_checkpoint,
    inventory_snapshot_complete,
    list_dispatchable_operations,
    mark_operation_sending,
    mark_operation_uncertain,
    now_text,
    record_sync_conflict,
    record_incoming_change,
    recover_stale_sending,
    service_return_snapshot_complete,
)


class SyncTransport(Protocol):
    def submit_operation(self, operation: dict[str, Any]) -> dict[str, Any]: ...

    def pull_changes(self, *, after: int, limit: int) -> dict[str, Any]: ...

    def pull_inventory_snapshot(self) -> dict[str, Any]: ...

    def pull_service_return_snapshot(self) -> dict[str, Any]: ...


class SyncTransportError(RuntimeError):
    """The central result is unavailable or cannot be trusted yet."""


@dataclass(frozen=True, slots=True)
class SyncRunResult:
    sent: int
    received: int
    checkpoint: int
    interrupted: bool


class RequestsSyncTransport:
    """Small requests-based adapter; no Tkinter or database dependency."""

    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 15):
        self.base_url = str(base_url).rstrip("/")
        self.token = str(token)
        self.timeout_seconds = float(timeout_seconds)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _json_response(self, response) -> dict[str, Any]:
        if response.status_code != 200:
            raise SyncTransportError(
                f"Central API returned HTTP {response.status_code}."
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise SyncTransportError("Central API returned invalid JSON.") from exc
        if not isinstance(value, dict):
            raise SyncTransportError("Central API returned an invalid response object.")
        return value

    def submit_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        import requests

        try:
            response = requests.post(
                f"{self.base_url}/v1/sync/operations",
                json=operation,
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SyncTransportError(str(exc)) from exc
        return self._json_response(response)

    def pull_changes(self, *, after: int, limit: int) -> dict[str, Any]:
        import requests

        try:
            response = requests.get(
                f"{self.base_url}/v1/sync/changes",
                params={"after": after, "limit": limit},
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SyncTransportError(str(exc)) from exc
        return self._json_response(response)

    def pull_inventory_snapshot(self) -> dict[str, Any]:
        import requests

        try:
            response = requests.get(
                f"{self.base_url}/v1/service/inventory-snapshot",
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SyncTransportError(str(exc)) from exc
        return self._json_response(response)

    def pull_service_return_snapshot(self) -> dict[str, Any]:
        import requests

        try:
            response = requests.get(
                f"{self.base_url}/v1/service/returnable-parts-snapshot",
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SyncTransportError(str(exc)) from exc
        return self._json_response(response)


def _apply_accepted_service_receipt(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    """Project a trusted receipt without confusing local and central versions."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_orders'"
    ).fetchone()
    if not exists or row.get("aggregate_type") != "SERVICE_ORDER":
        return
    version = receipt.get("aggregate_version")
    if version is not None:
        connection.execute(
            "UPDATE service_orders SET central_version=MAX(central_version,?) "
            "WHERE sync_uuid=?",
            (int(version), str(row["aggregate_uuid"])),
        )
    still_pending = connection.execute(
        "SELECT 1 FROM sync_outbox_v32 WHERE aggregate_uuid=? "
        "AND operation_id<>? AND status IN ('PENDING','SENDING','UNCERTAIN') LIMIT 1",
        (str(row["aggregate_uuid"]), str(row["operation_id"])),
    ).fetchone()
    connection.execute(
        "UPDATE service_orders SET sync_state=? WHERE sync_uuid=?",
        (
            "PENDING" if still_pending else "SYNCED",
            str(row["aggregate_uuid"]),
        ),
    )

def _wire_operation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": row["operation_id"],
        "operation_type": row["operation_type"],
        "aggregate_type": row["aggregate_type"],
        "aggregate_id": row["aggregate_uuid"],
        "expected_version": row["expected_version"],
        "actor_id": row["actor_id"],
        "actor_user_id": row.get("actor_user_uuid") or None,
        "actor_authorization_version": row.get(
            "actor_authorization_version", 0
        ),
        "device_id": row["device_id"],
        "device_sequence": row["device_sequence"],
        "occurred_at": row["occurred_at"],
        "payload": json.loads(row["payload_json"]),
        "contract_version": row["contract_version"],
        "workstation_signature": row.get("workstation_signature", ""),
    }


def synchronize_once(
    connection: sqlite3.Connection,
    transport: SyncTransport,
    device_id: str,
    *,
    batch_size: int = 100,
) -> SyncRunResult:
    """Push ordered operations, then journal one ordered page of changes.

    The connection must be idle when called.  SQLite transactions never span
    a network request: SENDING is committed before transmission, and a receipt
    or UNCERTAIN result is committed afterwards.
    """
    with connection:
        recover_stale_sending(connection)
    sent = 0
    interrupted = False
    rows = list_dispatchable_operations(connection, device_id, limit=batch_size)
    for row in rows:
        with connection:
            mark_operation_sending(connection, row["operation_id"])
        try:
            receipt = transport.submit_operation(_wire_operation(row))
            if receipt.get("operation_id") != row["operation_id"]:
                raise SyncTransportError("Receipt operation_id does not match request.")
            if receipt.get("result_code") not in {"ACCEPTED", "REJECTED"}:
                raise SyncTransportError("Receipt result_code is not supported.")
            with connection:
                acknowledge_operation(
                    connection,
                    row["operation_id"],
                    result_code=receipt["result_code"],
                    response=receipt,
                    central_sequence=receipt.get("central_sequence"),
                )
                if receipt.get("result_code") == "ACCEPTED":
                    _apply_accepted_service_receipt(connection, row, receipt)
                if (
                    receipt.get("result_code") == "REJECTED"
                    and row.get("operation_type") == "CompleteServiceTechnically"
                    and receipt.get("error_code") in {
                        "OFFLINE_STOCK_CONFLICT", "STOCK_ITEM_NOT_FOUND"
                    }
                ):
                    payload = json.loads(row["payload_json"])
                    result = receipt.get("result") or {}
                    barcodes = {
                        str(value) for value in result.get("conflicting_barcodes", [])
                    }
                    details = [
                        {
                            "barcode": str(part.get("barcode") or ""),
                            "article_id": str(part.get("article_id") or ""),
                            "description": str(part.get("description") or ""),
                            "quantity": str(part.get("quantity") or ""),
                        }
                        for part in payload.get("parts", [])
                        if not barcodes or str(part.get("barcode") or "") in barcodes
                    ]
                    record_sync_conflict(
                        connection,
                        row["operation_id"],
                        conflict_type="STOCK_SYNC_CONFLICT",
                        details=details,
                    )
                if (
                    receipt.get("result_code") == "REJECTED"
                    and row.get("operation_type") == "ReceiveRemovedServicePart"
                ):
                    payload = json.loads(row["payload_json"])
                    projection = connection.execute(
                        "SELECT service_order_uuid,invoice_number,barcode,"
                        "description,lot_serial FROM "
                        "central_service_part_returns_v32 WHERE invoice_uuid=? "
                        "AND invoice_line_uuid=? AND lot_uuid=?",
                        (
                            str(payload.get("invoice_id") or ""),
                            str(payload.get("invoice_line_id") or ""),
                            str(payload.get("lot_id") or ""),
                        ),
                    ).fetchone()
                    record_sync_conflict(
                        connection,
                        row["operation_id"],
                        conflict_type="SERVICE_PART_RETURN_CONFLICT",
                        details=[
                            {
                                "invoice_id": str(payload.get("invoice_id") or ""),
                                "invoice_line_id": str(
                                    payload.get("invoice_line_id") or ""
                                ),
                                "lot_id": str(payload.get("lot_id") or ""),
                                "quantity": str(payload.get("quantity") or ""),
                                "service_order_id": str(
                                    projection[0] if projection else ""
                                ),
                                "invoice_number": str(
                                    projection[1] if projection else ""
                                ),
                                "barcode": str(
                                    projection[2] if projection else ""
                                ),
                                "description": str(
                                    projection[3] if projection else ""
                                ),
                                "lot_serial": str(
                                    projection[4] if projection else ""
                                ),
                                "error_code": str(
                                    receipt.get("error_code") or ""
                                ),
                                "error_message": str(
                                    receipt.get("error_message") or ""
                                ),
                            }
                        ],
                    )
            sent += 1
        except Exception as exc:
            with connection:
                mark_operation_uncertain(connection, row["operation_id"], exc)
            interrupted = True
            break

    checkpoint = central_checkpoint(connection, device_id)
    received = 0
    if not interrupted:
        try:
            page = transport.pull_changes(after=checkpoint, limit=batch_size)
            if int(page.get("after", -1)) != checkpoint:
                raise SyncTransportError("Change page does not match requested checkpoint.")
            changes = page.get("changes")
            if not isinstance(changes, list):
                raise SyncTransportError("Change page has no valid changes list.")
            last = checkpoint
            with connection:
                for change in changes:
                    sequence = int(change["central_sequence"])
                    if sequence <= last:
                        raise SyncTransportError("Central changes are not strictly ordered.")
                    record_incoming_change(
                        connection,
                        central_sequence=sequence,
                        event_id=change["event_id"],
                        event_type=change["event_type"],
                        aggregate_type=change["aggregate_type"],
                        aggregate_uuid=change["aggregate_id"],
                        aggregate_version=change["aggregate_version"],
                        payload=change["payload"],
                    )
                    apply_inventory_updates(
                        connection, central_sequence=sequence,
                        payload=change["payload"],
                    )
                    apply_service_part_return_updates(
                        connection, central_sequence=sequence,
                        event_type=str(change["event_type"]),
                        payload=change["payload"],
                    )
                    connection.execute(
                        "UPDATE sync_inbox_v32 SET applied_at=? "
                        "WHERE central_sequence=?",
                        (now_text(), sequence),
                    )
                    last = sequence
                if int(page.get("next_checkpoint", -1)) != last:
                    raise SyncTransportError("Change page checkpoint is inconsistent.")
                checkpoint = advance_central_checkpoint(connection, device_id, last)
            received = len(changes)
        except Exception:
            received = 0
            interrupted = True

    if not interrupted and not inventory_snapshot_complete(connection):
        try:
            pull_snapshot = getattr(transport, "pull_inventory_snapshot", None)
            if pull_snapshot is None:
                raise SyncTransportError(
                    "Transport does not support the complete inventory bootstrap."
                )
            snapshot = pull_snapshot()
            if snapshot.get("complete") is not True:
                raise SyncTransportError("Inventory bootstrap is not marked complete.")
            updates = snapshot.get("inventory_updates")
            if not isinstance(updates, list):
                raise SyncTransportError("Inventory bootstrap has no valid item list.")
            with connection:
                apply_complete_inventory_snapshot(
                    connection,
                    snapshot_sequence=int(snapshot["snapshot_sequence"]),
                    inventory_updates=updates,
                )
        except Exception:
            interrupted = True

    if not interrupted and not service_return_snapshot_complete(connection):
        try:
            pull_snapshot = getattr(transport, "pull_service_return_snapshot", None)
            if pull_snapshot is None:
                raise SyncTransportError(
                    "Transport does not support the complete Service return bootstrap."
                )
            snapshot = pull_snapshot()
            if snapshot.get("complete") is not True:
                raise SyncTransportError(
                    "Service return bootstrap is not marked complete."
                )
            parts = snapshot.get("returnable_parts")
            if not isinstance(parts, list):
                raise SyncTransportError(
                    "Service return bootstrap has no valid part list."
                )
            with connection:
                apply_complete_service_return_snapshot(
                    connection,
                    snapshot_sequence=int(snapshot["snapshot_sequence"]),
                    returnable_parts=parts,
                )
        except Exception:
            interrupted = True

    return SyncRunResult(
        sent=sent,
        received=received,
        checkpoint=checkpoint,
        interrupted=interrupted,
    )
