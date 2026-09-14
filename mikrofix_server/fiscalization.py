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


def _invoice_event_aggregate(invoice) -> tuple[str, UUID]:
    if invoice.source_type == "DIRECT_SALE":
        return "DIRECT_SALE", invoice.direct_sale_uuid
    if invoice.source_type == "SERVICE":
        return "INVOICE_DRAFT", invoice.draft_uuid
    if invoice.source_type == "REVERSAL":
        return "INVOICE", invoice.invoice_uuid
    raise FiscalStateError("Invoice has an unsupported source type.")


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


def prepare_fiscal_submission(
    connection: Connection, invoice_id: str | UUID, *, prepared_request: dict,
) -> dict:
    """Persist the exact signed FURS body before any external network call."""
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
    try:
        request = dict(prepared_request)
        invoice_request = request["InvoiceRequest"]
        message_id = str(invoice_request["Header"]["MessageID"])
        zoi = str(invoice_request["Invoice"]["ProtectedID"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise FiscalStateError("Prepared FURS request is incomplete.") from exc
    if message_id != str(outbox.message_uuid):
        raise FiscalStateError("Prepared FURS request has a different message identity.")
    if not zoi:
        raise FiscalStateError("Prepared FURS request has no ZOI.")
    request_hash = payload_sha256(request)
    existing = dict(outbox.prepared_request_json or {})
    if existing:
        if (
            outbox.prepared_request_sha256 != request_hash
            or existing != request or outbox.prepared_zoi != zoi
        ):
            raise FiscalStateError("Prepared FURS request cannot be changed.")
        return {
            "status": outbox.status, "invoice_id": str(invoice_uuid),
            "request": existing, "request_sha256": request_hash, "zoi": zoi,
        }
    if outbox.status != "PENDING":
        raise FiscalStateError("Fiscal request can no longer be prepared.")
    connection.execute(
        update(fiscal_outbox)
        .where(fiscal_outbox.c.invoice_uuid == invoice_uuid)
        .values(
            prepared_request_json=request,
            prepared_request_sha256=request_hash,
            prepared_zoi=zoi,
        )
    )
    return {
        "status": "PENDING", "invoice_id": str(invoice_uuid),
        "request": request, "request_sha256": request_hash, "zoi": zoi,
    }


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
    request = dict(outbox.prepared_request_json or {})
    if not request or not outbox.prepared_zoi:
        raise FiscalStateError("Fiscal request has not been prepared and persisted.")
    if payload_sha256(request) != outbox.prepared_request_sha256:
        raise FiscalStateError("Prepared fiscal request hash does not match its content.")
    try:
        message_id = str(request["InvoiceRequest"]["Header"]["MessageID"])
        zoi = str(request["InvoiceRequest"]["Invoice"]["ProtectedID"])
    except (KeyError, TypeError) as exc:
        raise FiscalStateError("Prepared fiscal request is incomplete.") from exc
    if message_id != str(outbox.message_uuid) or zoi != outbox.prepared_zoi:
        raise FiscalStateError("Prepared fiscal identity does not match its outbox row.")
    if outbox.status == "CONFIRMED":
        return {
            "status": "CONFIRMED", "invoice_id": str(invoice_uuid),
            "request": request,
            "zoi": outbox.prepared_zoi,
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
        "request_sha256": outbox.prepared_request_sha256,
        "zoi": outbox.prepared_zoi,
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
        .values(
            status="FISCAL_RECONCILIATION",
            row_version=invoices.c.row_version + 1, updated_at=now,
        )
    )
    aggregate_type, aggregate_uuid = _invoice_event_aggregate(invoice)
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
    if outbox.prepared_zoi != clean_zoi:
        raise FiscalStateError("Confirmed ZOI does not match the prepared request.")

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
            finalized_at=now, row_version=invoices.c.row_version + 1,
            updated_at=now,
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
    if invoice.source_type == "REVERSAL":
        original = connection.execute(
            select(invoices)
            .where(invoices.c.invoice_uuid == invoice.reversal_of_invoice_uuid)
            .with_for_update()
        ).one_or_none()
        if original is None or original.status != "REVERSAL_PENDING":
            raise FiscalStateError("Original invoice is not awaiting this reversal.")
        connection.execute(
            update(invoices)
            .where(invoices.c.invoice_uuid == original.invoice_uuid)
            .values(
                status="REVERSED", row_version=invoices.c.row_version + 1,
                updated_at=now,
            )
        )
        _publish(
            connection, event_type="InvoiceReversed", aggregate_type="INVOICE",
            aggregate_uuid=original.invoice_uuid,
            aggregate_version=int(original.row_version) + 1,
            payload={
                "invoice_id": str(original.invoice_uuid),
                "invoice_number": original.invoice_number,
                "reversal_invoice_id": str(invoice_uuid),
                "reversal_invoice_number": invoice.invoice_number,
                "status": "REVERSED", "stock_effect": "NONE",
                "service_effect": "NONE",
            },
            occurred_at=now,
        )
    aggregate_type, aggregate_uuid = _invoice_event_aggregate(invoice)
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
