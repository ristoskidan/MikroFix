"""Transactional state changes around an external FURS submission.

Network I/O stays outside these functions. The immutable request is stored
before a sender starts, and every outcome updates the same invoice and outbox
row. An uncertain result never creates another invoice or number.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, select, update
from sqlalchemy.dialects.postgresql import insert

from mikrofix_sync_contracts import payload_sha256

from .models import (
    change_feed,
    fiscal_outbox,
    invoice_drafts,
    invoices,
    service_orders,
)


class FiscalStateError(RuntimeError):
    pass


def _publish(
    connection: Connection, *, event_type: str, aggregate_type: str,
    aggregate_uuid: UUID, aggregate_version: int, payload: dict,
    occurred_at: datetime,
) -> int:
    return int(connection.execute(
        insert(change_feed).values(
            event_id=uuid4(), event_type=event_type,
            aggregate_type=aggregate_type, aggregate_uuid=aggregate_uuid,
            aggregate_version=aggregate_version, payload_json=payload,
            occurred_at=occurred_at,
        ).returning(change_feed.c.central_sequence)
    ).scalar_one())


def start_fiscal_submission(connection: Connection, invoice_id: str | UUID) -> dict:
    """Mark one saved request as sending and return that exact saved request."""
    invoice_uuid = UUID(str(invoice_id))
    invoice = connection.execute(
        select(invoices).where(invoices.c.invoice_uuid == invoice_uuid).with_for_update()
    ).one_or_none()
    if invoice is None:
        raise FiscalStateError("Invoice does not exist.")
    outbox = connection.execute(
        select(fiscal_outbox)
        .where(fiscal_outbox.c.invoice_uuid == invoice_uuid)
        .with_for_update()
    ).one_or_none()
    if outbox is None:
        raise FiscalStateError("Invoice has no fiscal request.")
    request = dict(outbox.request_json)
    if payload_sha256(request) != outbox.request_sha256:
        raise FiscalStateError("Saved fiscal request hash does not match its content.")
    if str(request.get("message_id") or "") != str(outbox.message_uuid):
        raise FiscalStateError("Saved fiscal request has a different message identity.")
    if outbox.status == "CONFIRMED":
        return {
            "status": "CONFIRMED", "invoice_id": str(invoice_uuid),
            "request": request,
        }
    if outbox.status not in {"PENDING", "UNCERTAIN"}:
        raise FiscalStateError("Fiscal request is already being sent.")
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    connection.execute(
        update(fiscal_outbox)
        .where(fiscal_outbox.c.invoice_uuid == invoice_uuid)
        .values(
            status="SENDING",
            attempt_count=fiscal_outbox.c.attempt_count + 1,
            updated_at=now,
        )
    )
    return {
        "status": "SENDING", "invoice_id": str(invoice_uuid),
        "request": request,
        "request_sha256": outbox.request_sha256,
    }


def mark_fiscal_submission_uncertain(
    connection: Connection, invoice_id: str | UUID, *, error: str,
) -> dict:
    """Preserve the same invoice/request when the external result is unknown."""
    invoice_uuid = UUID(str(invoice_id))
    invoice = connection.execute(
        select(invoices).where(invoices.c.invoice_uuid == invoice_uuid).with_for_update()
    ).one_or_none()
    if invoice is None:
        raise FiscalStateError("Invoice does not exist.")
    outbox = connection.execute(
        select(fiscal_outbox)
        .where(fiscal_outbox.c.invoice_uuid == invoice_uuid)
        .with_for_update()
    ).one_or_none()
    if outbox is None:
        raise FiscalStateError("Invoice has no fiscal request.")
    if outbox.status == "CONFIRMED":
        return {"status": "CONFIRMED", "invoice_id": str(invoice_uuid)}
    if outbox.status not in {"SENDING", "UNCERTAIN"}:
        raise FiscalStateError("Fiscal request has not been started.")
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    message = str(error or "Unknown FURS result.")[:1000]
    connection.execute(
        update(fiscal_outbox)
        .where(fiscal_outbox.c.invoice_uuid == invoice_uuid)
        .values(status="UNCERTAIN", last_error=message, updated_at=now)
    )
    connection.execute(
        update(invoices)
        .where(invoices.c.invoice_uuid == invoice_uuid)
        .values(status="FISCAL_RECONCILIATION", updated_at=now)
    )
    aggregate_type = (
        "DIRECT_SALE" if invoice.source_type == "DIRECT_SALE" else "INVOICE_DRAFT"
    )
    aggregate_uuid = (
        invoice.direct_sale_uuid
        if invoice.source_type == "DIRECT_SALE" else invoice.draft_uuid
    )
    _publish(
        connection,
        event_type="InvoiceFiscalizationUncertain",
        aggregate_type=aggregate_type,
        aggregate_uuid=aggregate_uuid,
        aggregate_version=2,
        payload={
            "invoice_id": str(invoice_uuid),
            "invoice_number": invoice.invoice_number,
            "status": "FISCAL_RECONCILIATION", "error": message,
        },
        occurred_at=now,
    )
    return {"status": "FISCAL_RECONCILIATION", "invoice_id": str(invoice_uuid)}


def confirm_fiscal_submission(
    connection: Connection, invoice_id: str | UUID, *, zoi: str, eor: str,
    response: dict,
) -> dict:
    """Finalize invoice and Service once after a confirmed response."""
    invoice_uuid = UUID(str(invoice_id))
    invoice = connection.execute(
        select(invoices).where(invoices.c.invoice_uuid == invoice_uuid).with_for_update()
    ).one_or_none()
    if invoice is None:
        raise FiscalStateError("Invoice does not exist.")
    outbox = connection.execute(
        select(fiscal_outbox)
        .where(fiscal_outbox.c.invoice_uuid == invoice_uuid)
        .with_for_update()
    ).one_or_none()
    if outbox is None:
        raise FiscalStateError("Invoice has no fiscal request.")
    clean_zoi = str(zoi or "").strip()
    clean_eor = str(eor or "").strip()
    if not clean_zoi or not clean_eor:
        raise FiscalStateError("A confirmed FURS result requires ZOI and EOR.")
    if outbox.status == "CONFIRMED":
        if invoice.furs_zoi != clean_zoi or invoice.furs_eor != clean_eor:
            raise FiscalStateError("Confirmed fiscal identity cannot be changed.")
        return {
            "status": "ISSUED", "invoice_id": str(invoice_uuid),
            "invoice_number": invoice.invoice_number,
        }
    if outbox.status not in {"SENDING", "UNCERTAIN"}:
        raise FiscalStateError("Fiscal request has not been started.")

    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    connection.execute(
        update(fiscal_outbox)
        .where(fiscal_outbox.c.invoice_uuid == invoice_uuid)
        .values(
            status="CONFIRMED", response_json=dict(response),
            last_error=None, updated_at=now,
        )
    )
    connection.execute(
        update(invoices)
        .where(invoices.c.invoice_uuid == invoice_uuid)
        .values(
            status="ISSUED", furs_zoi=clean_zoi, furs_eor=clean_eor,
            finalized_at=now, updated_at=now,
        )
    )
    if invoice.source_type == "SERVICE":
        connection.execute(
            update(invoice_drafts)
            .where(invoice_drafts.c.draft_uuid == invoice.draft_uuid)
            .values(status="INVOICED", updated_at=now)
        )
        order = connection.execute(
            select(service_orders)
            .where(service_orders.c.sync_uuid == invoice.service_order_uuid)
            .with_for_update()
        ).one()
        if dict(order.payload_json).get("technical_status") != "BILLING_COMPLETED":
            order_payload = dict(order.payload_json)
            order_payload["technical_status"] = "BILLING_COMPLETED"
            order_version = int(order.row_version) + 1
            connection.execute(
                update(service_orders)
                .where(service_orders.c.sync_uuid == invoice.service_order_uuid)
                .values(
                    row_version=order_version, payload_json=order_payload,
                    updated_at=now,
                )
            )
            _publish(
                connection,
                event_type="ServiceBillingCompleted",
                aggregate_type="SERVICE_ORDER",
                aggregate_uuid=invoice.service_order_uuid,
                aggregate_version=order_version,
                payload={
                    "service_order_id": str(invoice.service_order_uuid),
                    "invoice_id": str(invoice_uuid),
                    "invoice_number": invoice.invoice_number,
                    "status": "BILLING_COMPLETED",
                },
                occurred_at=now,
            )
    aggregate_type = (
        "DIRECT_SALE" if invoice.source_type == "DIRECT_SALE" else "INVOICE_DRAFT"
    )
    aggregate_uuid = (
        invoice.direct_sale_uuid
        if invoice.source_type == "DIRECT_SALE" else invoice.draft_uuid
    )
    _publish(
        connection,
        event_type="InvoiceFiscalizationConfirmed",
        aggregate_type=aggregate_type,
        aggregate_uuid=aggregate_uuid,
        aggregate_version=2 if invoice.source_type == "DIRECT_SALE" else 3,
        payload={
            "invoice_id": str(invoice_uuid),
            "invoice_number": invoice.invoice_number,
            "status": "ISSUED", "zoi": clean_zoi, "eor": clean_eor,
        },
        occurred_at=now,
    )
    return {
        "status": "ISSUED", "invoice_id": str(invoice_uuid),
        "invoice_number": invoice.invoice_number,
    }
