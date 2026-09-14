"""One-at-a-time central FURS outbox processor.

The caller controls scheduling. This module never enables production FURS and
never loads a certificate by itself; it only accepts an explicitly configured
provider.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, select

from .fiscalization import (
    confirm_fiscal_submission,
    mark_fiscal_submission_uncertain,
    prepare_fiscal_submission,
    start_fiscal_submission,
)
from .models import fiscal_outbox, invoices


class FiscalProvider(Protocol):
    def prepare_request(
        self, request: dict[str, Any], *, message_id: str
    ) -> dict[str, Any]: ...

    def submit_prepared(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def _provider_input(invoice, outbox) -> dict[str, Any]:
    customer = dict(invoice.billing_customer_json or {})
    result = {
        "issue_datetime": invoice.issued_at,
        "sequence": invoice.fiscal_sequence,
        "premise_id": invoice.premise_id,
        "device_id": invoice.electronic_device_id,
        "invoice_amount": invoice.total_amount,
        "payment_amount": invoice.total_amount,
        "operator_tax_number": invoice.operator_tax_number,
        "customer_vat_number": str(customer.get("tax_number") or ""),
        "numbering_structure": "B",
    }
    if bool(dict(outbox.request_json or {}).get("subsequent_submit")):
        result["subsequent_submit"] = True
    if invoice.source_type == "REVERSAL":
        reference = dict(invoice.snapshot_json or {}).get("reference_invoice")
        if not isinstance(reference, dict) or not reference:
            raise ValueError("Invoice reversal has no immutable original reference.")
        result["reference_invoice"] = reference
    return result


def process_fiscal_invoice(
    engine: Engine, invoice_id: str | UUID, provider: FiscalProvider,
) -> dict[str, Any]:
    """Prepare, persist, send and finalize one existing invoice idempotently."""
    invoice_uuid = UUID(str(invoice_id))
    with engine.begin() as connection:
        invoice = connection.execute(
            select(invoices)
            .where(invoices.c.invoice_uuid == invoice_uuid)
            .with_for_update()
        ).one_or_none()
        outbox = connection.execute(
            select(fiscal_outbox)
            .where(fiscal_outbox.c.invoice_uuid == invoice_uuid)
            .with_for_update()
        ).one_or_none()
        if invoice is None or outbox is None:
            raise ValueError("Invoice has no central fiscal outbox entry.")
        if outbox.status == "CONFIRMED":
            return {
                "status": "ISSUED", "invoice_id": str(invoice_uuid),
                "invoice_number": invoice.invoice_number,
            }
        prepared = dict(outbox.prepared_request_json or {})
        if not prepared:
            prepared = provider.prepare_request(
                _provider_input(invoice, outbox), message_id=str(outbox.message_uuid)
            )
            prepare_fiscal_submission(
                connection, invoice_uuid, prepared_request=prepared
            )
        sending = start_fiscal_submission(connection, invoice_uuid)

    try:
        result = dict(provider.submit_prepared(sending["request"]) or {})
    except Exception as exc:
        with engine.begin() as connection:
            return mark_fiscal_submission_uncertain(
                connection, invoice_uuid, error=str(exc)
            )

    if str(result.get("status") or "") == "CONFIRMED":
        with engine.begin() as connection:
            return confirm_fiscal_submission(
                connection, invoice_uuid,
                zoi=str(result.get("zoi") or sending["zoi"]),
                eor=str(result.get("eor") or ""),
                response=dict(result.get("response") or {}),
            )
    with engine.begin() as connection:
        return mark_fiscal_submission_uncertain(
            connection, invoice_uuid,
            error=str(
                result.get("error_message")
                or result.get("error_code")
                or "FURS response was not confirmed."
            ),
        )
