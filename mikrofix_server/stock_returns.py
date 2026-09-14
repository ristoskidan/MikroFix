"""Read models for exact invoice-line/LOT physical-return eligibility."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Connection, func, select

from .models import (
    inventory_items,
    inventory_lots,
    physical_stock_returns,
    service_part_consumptions,
    service_stock_allocations,
    invoices,
)


def service_returnable_parts(connection: Connection, invoice) -> list[dict]:
    """Return server-owned installed-part allocations still removable."""
    rows = connection.execute(select(
        service_stock_allocations.c.service_line_uuid,
        service_stock_allocations.c.lot_uuid,
        service_stock_allocations.c.quantity,
        service_part_consumptions.c.article_uuid,
        service_part_consumptions.c.barcode,
        inventory_items.c.description,
        inventory_lots.c.lot_serial,
    ).join(
        service_part_consumptions,
        service_part_consumptions.c.line_uuid
        == service_stock_allocations.c.service_line_uuid,
    ).join(
        inventory_lots,
        inventory_lots.c.lot_uuid == service_stock_allocations.c.lot_uuid,
    ).join(
        inventory_items,
        inventory_items.c.article_uuid == service_part_consumptions.c.article_uuid,
    ).where(
        service_part_consumptions.c.service_order_uuid
        == invoice.service_order_uuid
    ).order_by(
        service_stock_allocations.c.service_line_uuid,
        service_stock_allocations.c.lot_uuid,
    )).all()
    returned_rows = connection.execute(select(
        physical_stock_returns.c.invoice_line_uuid,
        physical_stock_returns.c.lot_uuid,
        func.sum(physical_stock_returns.c.quantity).label("quantity"),
    ).where(
        physical_stock_returns.c.invoice_uuid == invoice.invoice_uuid,
        physical_stock_returns.c.receipt_source == "SERVICE_REMOVAL",
    ).group_by(
        physical_stock_returns.c.invoice_line_uuid,
        physical_stock_returns.c.lot_uuid,
    )).all()
    returned = {
        (row.invoice_line_uuid, row.lot_uuid): Decimal(row.quantity)
        for row in returned_rows
    }
    result = []
    for row in rows:
        received = returned.get(
            (row.service_line_uuid, row.lot_uuid), Decimal("0")
        )
        remaining = Decimal(row.quantity) - received
        if remaining <= 0:
            continue
        result.append({
            "service_order_id": str(invoice.service_order_uuid),
            "invoice_id": str(invoice.invoice_uuid),
            "invoice_number": invoice.invoice_number,
            "invoice_line_id": str(row.service_line_uuid),
            "lot_id": str(row.lot_uuid), "lot_serial": row.lot_serial,
            "article_id": str(row.article_uuid), "barcode": row.barcode,
            "description": row.description,
            "allocated_quantity": format(Decimal(row.quantity), "f"),
            "already_removed_quantity": format(received, "f"),
            "removable_quantity": format(remaining, "f"),
        })
    return result


def all_service_returnable_parts(connection: Connection) -> list[dict]:
    """Return a complete bootstrap view for every issued Service invoice."""
    invoice_rows = connection.execute(select(invoices).where(
        invoices.c.source_type == "SERVICE",
        invoices.c.status.in_(("ISSUED", "REVERSAL_PENDING", "REVERSED")),
    ).order_by(invoices.c.issued_at, invoices.c.invoice_uuid)).all()
    result: list[dict] = []
    for invoice in invoice_rows:
        result.extend(service_returnable_parts(connection, invoice))
    return result
