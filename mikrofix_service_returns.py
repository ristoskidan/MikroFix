"""Offline-safe Service view of installed parts that can be removed."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from mikrofix_sync_store import ensure_sync_store_schema


FOUR = Decimal("0.0001")


class ServiceReturnProjectionError(RuntimeError):
    pass


def _quantity(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(FOUR)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ServiceReturnProjectionError(
            "Evidenca odstranljivih servisnih delov vsebuje napačno količino."
        ) from exc


@dataclass(frozen=True, slots=True)
class ServiceReturnablePart:
    service_order_uuid: str
    invoice_uuid: str
    invoice_number: str
    invoice_line_uuid: str
    lot_uuid: str
    article_uuid: str
    barcode: str
    description: str
    lot_serial: str
    allocated_quantity: Decimal
    returned_central_quantity: Decimal
    pending_local_quantity: Decimal
    removable_quantity: Decimal
    central_sequence: int


def service_returnable_parts(
    connection: sqlite3.Connection, *, service_order_uuid: str = "",
) -> list[ServiceReturnablePart]:
    ensure_sync_store_schema(connection)
    parameters: list[str] = []
    where = ""
    if str(service_order_uuid or "").strip():
        where = "WHERE service_order_uuid=?"
        parameters.append(str(service_order_uuid))
    rows = connection.execute(
        "SELECT service_order_uuid,invoice_uuid,invoice_number,invoice_line_uuid,"
        "lot_uuid,article_uuid,barcode,description,lot_serial,allocated_quantity,"
        "returned_quantity,central_sequence FROM central_service_part_returns_v32 "
        f"{where} ORDER BY invoice_number,description,invoice_line_uuid,lot_uuid",
        parameters,
    ).fetchall()
    pending_rows = connection.execute(
        """
        SELECT o.payload_json,r.result_code,r.central_sequence
        FROM sync_outbox_v32 o
        LEFT JOIN sync_receipts_v32 r ON r.operation_id=o.operation_id
        WHERE o.operation_type='ReceiveRemovedServicePart'
          AND o.status<>'SUPERSEDED'
        """
    ).fetchall()
    result = []
    for row in rows:
        pending = Decimal("0.0000")
        row_sequence = int(row[11])
        for payload_json, result_code, receipt_sequence in pending_rows:
            payload = json.loads(str(payload_json))
            if (
                str(payload.get("invoice_id")) != str(row[1])
                or str(payload.get("invoice_line_id")) != str(row[3])
                or str(payload.get("lot_id")) != str(row[4])
            ):
                continue
            reflected = (
                str(result_code or "") == "ACCEPTED"
                and receipt_sequence is not None
                and row_sequence >= int(receipt_sequence)
            )
            if not reflected:
                pending += _quantity(payload.get("quantity"))
        allocated = _quantity(row[9])
        returned = _quantity(row[10])
        removable = max(Decimal("0.0000"), allocated - returned - pending)
        result.append(ServiceReturnablePart(
            service_order_uuid=str(row[0]), invoice_uuid=str(row[1]),
            invoice_number=str(row[2]), invoice_line_uuid=str(row[3]),
            lot_uuid=str(row[4]), article_uuid=str(row[5]), barcode=str(row[6]),
            description=str(row[7]), lot_serial=str(row[8]),
            allocated_quantity=allocated,
            returned_central_quantity=returned,
            pending_local_quantity=pending.quantize(FOUR),
            removable_quantity=removable.quantize(FOUR),
            central_sequence=row_sequence,
        ))
    return result
