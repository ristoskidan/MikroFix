"""Atomic and idempotent processing of central sync operations."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from mikrofix_sync_contracts import (
    ContractError,
    SyncOperationEnvelope,
    normalized_uuid,
    payload_sha256,
    validated_attach_receipt_invoice_payload,
    validated_close_cash_session_payload,
    validated_confirm_receipt_payload,
    validated_create_inventory_item_payload,
    validated_create_partner_payload,
    validated_create_service_order_payload,
    validated_issue_service_invoice_payload,
    validated_issue_direct_sale_invoice_payload,
    validated_manual_cash_entry_payload,
    validated_open_cash_session_payload,
    validated_settle_unpaid_bank_invoice_payload,
    validated_storno_invoice_payload,
    validated_set_partner_active_payload,
    validated_technical_completion_payload,
    validated_update_partner_payload,
    validated_update_inventory_price_payload,
)

from .models import (
    accepted_operations,
    cash_journal_entries,
    cash_session_number_sequences,
    cash_sessions,
    change_feed,
    direct_sale_discount_audit,
    direct_sale_stock_allocations,
    fiscal_outbox,
    inventory_items,
    inventory_identities,
    inventory_lots,
    inventory_price_history,
    invoice_number_sequences,
    invoice_drafts,
    invoices,
    partners,
    purchase_receipt_lines,
    purchase_receipt_revisions,
    purchase_receipts,
    receipt_attachments,
    receipt_document_types,
    receipt_number_sequences,
    service_orders,
    service_part_consumptions,
    service_stock_allocations,
    service_tariffs,
    technical_completions,
)


class ProtocolConflict(RuntimeError):
    """An operation identity was reused for different request content."""


class DirectSalePreparationError(RuntimeError):
    """A direct-sale quote or issue failed before any business write."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CentralCashJournalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _create_inventory_item(
    connection: Connection, envelope: SyncOperationEnvelope, article_uuid: UUID,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "INVENTORY_ITEM":
        return _reject(
            "INVALID_AGGREGATE_TYPE", "CreateInventoryItem requires INVENTORY_ITEM."
        )
    if envelope.expected_version != 0:
        return _reject("VERSION_CONFLICT", "A new inventory item requires version 0.")
    try:
        payload = validated_create_inventory_item_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["article_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.article_id must equal aggregate_id."
        )
    created = connection.execute(insert(inventory_items).values(
        article_uuid=article_uuid, barcode=payload["main_barcode"],
        description=payload["description"], track_mode=payload["track_mode"],
        quantity_on_hand=Decimal("0"), current_sale_price=Decimal("0"),
        blocked=False, row_version=1,
    ).on_conflict_do_nothing().returning(
        inventory_items.c.article_uuid
    )).scalar_one_or_none()
    if created is None:
        existing = connection.execute(select(
            inventory_items.c.article_uuid, inventory_items.c.description
        ).where(
            (inventory_items.c.article_uuid == article_uuid)
            | (inventory_items.c.barcode == payload["main_barcode"])
        )).first()
        return (
            "REJECTED",
            {
                "error_code": "INVENTORY_ITEM_ALREADY_EXISTS",
                "error_message": (
                    "An inventory item with this identity or main barcode "
                    "already exists."
                ),
                "existing_article_id": (
                    str(existing.article_uuid) if existing else None
                ),
                "existing_description": existing.description if existing else None,
            },
            None,
        )
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    updates = _inventory_updates_for_articles(connection, [article_uuid])
    sequence = _publish_change(
        connection, event_type="InventoryItemCreated",
        aggregate_type="INVENTORY_ITEM", aggregate_uuid=article_uuid,
        aggregate_version=1, payload={
            "article_id": str(article_uuid), "row_version": 1,
            "main_barcode": payload["main_barcode"],
            "description": payload["description"],
            "track_mode": payload["track_mode"],
            "inventory_updates": updates,
        }, occurred_at=now.isoformat(timespec="seconds"),
    )
    return "ACCEPTED", {
        "aggregate_version": 1, "central_sequence": sequence,
        "article_id": str(article_uuid), "row_version": 1,
        "inventory_updates": updates,
    }, sequence


def _update_inventory_sale_price(
    connection: Connection, envelope: SyncOperationEnvelope, article_uuid: UUID,
    operation_uuid: UUID, *, actor_uuid: UUID | None,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "INVENTORY_ITEM":
        return _reject(
            "INVALID_AGGREGATE_TYPE",
            "UpdateInventorySalePrice requires INVENTORY_ITEM.",
        )
    try:
        payload = validated_update_inventory_price_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["article_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.article_id must equal aggregate_id."
        )
    if payload["expected_version"] != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT",
            "payload.expected_version must equal envelope.expected_version.",
        )
    article = connection.execute(select(inventory_items).where(
        inventory_items.c.article_uuid == article_uuid
    ).with_for_update()).one_or_none()
    if article is None:
        return _reject("INVENTORY_ITEM_NOT_FOUND", "The inventory item does not exist.")
    if int(article.row_version) != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT", "The inventory item changed; reload it before pricing."
        )
    available_costs = [Decimal(value) for value in connection.execute(select(
        inventory_lots.c.landed_unit_cost
    ).where(
        inventory_lots.c.article_uuid == article_uuid,
        inventory_lots.c.status == "AVAILABLE",
        inventory_lots.c.available_quantity > 0,
    ).with_for_update()).scalars().all()]
    highest = max(available_costs, default=Decimal("0"))
    minimum = (highest * _PART_FLOOR).quantize(_CENT, rounding=ROUND_HALF_UP)
    selected = Decimal(payload["new_sale_price"])
    if selected == Decimal(article.current_sale_price):
        return _reject("SALE_PRICE_UNCHANGED", "The selected sale price is unchanged.")
    if selected < minimum:
        return _reject(
            "SALE_PRICE_BELOW_AVAILABLE_COST",
            f"Sale price must be at least {minimum:.2f} because of available LOT cost.",
        )
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    new_version = int(article.row_version) + 1
    connection.execute(update(inventory_items).where(
        inventory_items.c.article_uuid == article_uuid
    ).values(
        current_sale_price=selected, row_version=new_version, updated_at=now,
    ))
    connection.execute(insert(inventory_price_history).values(
        price_history_uuid=uuid4(), article_uuid=article_uuid,
        receipt_uuid=None, price_change_operation_id=operation_uuid,
        change_reason=payload["reason"], previous_price=article.current_sale_price,
        confirmed_price=selected, highest_available_cost=highest,
        effective_at=now, actor_uuid=actor_uuid,
    ))
    updates = _inventory_updates_for_articles(connection, [article_uuid])
    sequence = _publish_change(
        connection, event_type="InventorySalePriceChanged",
        aggregate_type="INVENTORY_ITEM", aggregate_uuid=article_uuid,
        aggregate_version=new_version,
        payload={
            "article_id": str(article_uuid), "row_version": new_version,
            "previous_sale_price": format(Decimal(article.current_sale_price), ".2f"),
            "new_sale_price": format(selected, ".2f"),
            "minimum_sale_price": format(minimum, ".2f"),
            "reason": payload["reason"], "inventory_updates": updates,
        },
        occurred_at=envelope.occurred_at,
    )
    return "ACCEPTED", {
        "aggregate_version": new_version, "central_sequence": sequence,
        "article_id": str(article_uuid), "row_version": new_version,
        "new_sale_price": format(selected, ".2f"),
        "minimum_sale_price": format(minimum, ".2f"),
        "inventory_updates": updates,
    }, sequence


def _receipt(row) -> dict:
    response = dict(row.response_json)
    return {
        "operation_id": row.operation_id,
        "result_code": row.result_code,
        "aggregate_id": row.aggregate_uuid,
        "aggregate_version": response.get("aggregate_version"),
        "central_sequence": row.central_sequence,
        "error_code": response.get("error_code"),
        "error_message": response.get("error_message"),
        "result": response,
    }


def _same_operation(row, envelope: SyncOperationEnvelope) -> bool:
    return (
        row.operation_type == envelope.operation_type
        and row.aggregate_type == envelope.aggregate_type
        and str(row.aggregate_uuid) == envelope.aggregate_id
        and row.expected_version == envelope.expected_version
        and row.actor_id == envelope.actor_id
        and str(row.actor_user_uuid or "") == envelope.actor_user_id
        and row.actor_authorization_version == envelope.actor_authorization_version
        and row.device_id == envelope.device_id
        and row.device_sequence == envelope.device_sequence
        and row.contract_version == envelope.contract_version
        and row.occurred_at == datetime.fromisoformat(envelope.occurred_at)
        and row.payload_sha256 == envelope.payload_hash
        and row.workstation_signature == envelope.workstation_signature
    )


def _reject(error_code: str, message: str) -> tuple[str, dict, int | None]:
    return (
        "REJECTED",
        {"error_code": error_code, "error_message": message},
        None,
    )


def _publish_change(
    connection: Connection, *, event_type: str, aggregate_uuid: UUID,
    aggregate_version: int, payload: dict, occurred_at: str,
    aggregate_type: str = "SERVICE_ORDER",
) -> int:
    return int(connection.execute(
        insert(change_feed)
        .values(
            event_id=uuid4(), event_type=event_type,
            aggregate_type=aggregate_type, aggregate_uuid=aggregate_uuid,
            aggregate_version=aggregate_version, payload_json=payload,
            occurred_at=occurred_at,
        )
        .returning(change_feed.c.central_sequence)
    ).scalar_one())


def _create_service_order(
    connection: Connection, envelope: SyncOperationEnvelope,
    aggregate_uuid: UUID,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "SERVICE_ORDER":
        return _reject(
            "INVALID_AGGREGATE_TYPE", "CreateServiceOrder requires SERVICE_ORDER."
        )
    if envelope.expected_version != 0:
        return _reject(
            "VERSION_CONFLICT", "A new service order requires expected_version 0."
        )
    try:
        payload_aggregate_id = normalized_uuid(
            envelope.payload.get("sync_uuid"), field="payload.sync_uuid"
        )
        validated = validated_create_service_order_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload_aggregate_id != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.sync_uuid must equal aggregate_id."
        )
    created = connection.execute(
        insert(service_orders)
        .values(
            sync_uuid=aggregate_uuid, row_version=1,
            origin_device_id=envelope.device_id, payload_json=validated,
        )
        .on_conflict_do_nothing(index_elements=[service_orders.c.sync_uuid])
        .returning(service_orders.c.sync_uuid)
    ).scalar_one_or_none()
    if created is None:
        return _reject("AGGREGATE_ALREADY_EXISTS", "The service order already exists.")
    sequence = _publish_change(
        connection, event_type="ServiceOrderCreated",
        aggregate_uuid=aggregate_uuid, aggregate_version=1,
        payload=validated, occurred_at=envelope.occurred_at,
    )
    return "ACCEPTED", {"aggregate_version": 1, "central_sequence": sequence}, sequence


def _create_partner(
    connection: Connection, envelope: SyncOperationEnvelope,
    partner_uuid: UUID,
) -> tuple[str, dict, int | None]:
    """Create a reviewed customer; this command never updates existing rows."""
    if envelope.aggregate_type != "PARTNER":
        return _reject("INVALID_AGGREGATE_TYPE", "CreatePartner requires PARTNER.")
    if envelope.expected_version != 0:
        return _reject("VERSION_CONFLICT", "A new partner requires expected_version 0.")
    try:
        payload = validated_create_partner_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["partner_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.partner_id must equal aggregate_id."
        )
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    tax_normalized = payload["tax_number"] or None
    _lock_partner_keys(connection, str(tax_normalized or ""))
    created = connection.execute(
        insert(partners)
        .values(
            partner_uuid=partner_uuid,
            name=payload["name"],
            address=payload["address"],
            post_city=payload["post_city"],
            country=payload["country"] or "Slovenija",
            tax_number=payload["tax_number"],
            tax_number_normalized=tax_normalized,
            registration_number=payload["registration_number"],
            phone=payload["phone"],
            email=payload["email"],
            row_version=1,
            active=True,
            verified_by=envelope.actor_id,
            verified_at=now,
            verification_source=payload["verification_source"],
            source_reference=payload["source_reference"],
        )
        .on_conflict_do_nothing()
        .returning(partners.c.partner_uuid)
    ).scalar_one_or_none()
    if created is None:
        existing = connection.execute(
            select(partners.c.partner_uuid, partners.c.name).where(
                (partners.c.partner_uuid == partner_uuid)
                | (
                    (partners.c.tax_number_normalized == tax_normalized)
                    if tax_normalized is not None else False
                )
            )
        ).first()
        return (
            "REJECTED",
            {
                "error_code": "PARTNER_ALREADY_EXISTS",
                "error_message": "A partner with this identity already exists.",
                "existing_partner_id": str(existing.partner_uuid) if existing else None,
                "existing_partner_name": existing.name if existing else None,
            },
            None,
        )
    sequence = _publish_change(
        connection,
        event_type="PartnerCreated",
        aggregate_type="PARTNER",
        aggregate_uuid=partner_uuid,
        aggregate_version=1,
        payload={
            "partner_id": str(partner_uuid), "row_version": 1,
            "name": payload["name"],
            "verification_source": payload["verification_source"],
        },
        occurred_at=now.isoformat(timespec="seconds"),
    )
    return (
        "ACCEPTED",
        {
            "aggregate_version": 1, "central_sequence": sequence,
            "partner_id": str(partner_uuid), "row_version": 1,
        },
        sequence,
    )


def _partner_lock_key(value: str) -> int:
    digest = hashlib.sha256(f"MikroFIX:partner:{value}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _lock_partner_keys(connection: Connection, *values: str) -> None:
    for value in sorted({str(item or "").strip() for item in values if item}):
        connection.execute(select(func.pg_advisory_xact_lock(_partner_lock_key(value))))


def _update_partner(
    connection: Connection, envelope: SyncOperationEnvelope, partner_uuid: UUID,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "PARTNER":
        return _reject("INVALID_AGGREGATE_TYPE", "UpdatePartner requires PARTNER.")
    try:
        payload = validated_update_partner_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["partner_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.partner_id must equal aggregate_id."
        )
    if payload["expected_version"] != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT", "payload.expected_version must equal expected_version."
        )
    row = connection.execute(
        select(partners).where(
            partners.c.partner_uuid == partner_uuid
        ).with_for_update()
    ).one_or_none()
    if row is None:
        return _reject("PARTNER_NOT_FOUND", "The selected partner does not exist.")
    if row.row_version != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT",
            f"Partner version is {row.row_version}, not {envelope.expected_version}.",
        )
    tax_normalized = payload["tax_number"] or None
    _lock_partner_keys(
        connection, str(row.tax_number_normalized or ""), str(tax_normalized or "")
    )
    duplicate = None
    if tax_normalized:
        duplicate = connection.execute(select(partners.c.partner_uuid).where(
            partners.c.tax_number_normalized == tax_normalized,
            partners.c.partner_uuid != partner_uuid,
        )).scalar_one_or_none()
    if duplicate is not None:
        return _reject(
            "PARTNER_TAX_NUMBER_EXISTS",
            "Another partner already uses this tax number.",
        )
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    new_version = int(row.row_version) + 1
    connection.execute(update(partners).where(
        partners.c.partner_uuid == partner_uuid
    ).values(
        name=payload["name"], address=payload["address"],
        post_city=payload["post_city"], country=payload["country"] or "Slovenija",
        tax_number=payload["tax_number"], tax_number_normalized=tax_normalized,
        registration_number=payload["registration_number"],
        phone=payload["phone"], email=payload["email"],
        row_version=new_version, verified_by=envelope.actor_id,
        verified_at=now, verification_source=payload["verification_source"],
        source_reference=payload["source_reference"], updated_at=now,
    ))
    sequence = _publish_change(
        connection, event_type="PartnerUpdated", aggregate_type="PARTNER",
        aggregate_uuid=partner_uuid, aggregate_version=new_version,
        payload={
            "partner_id": str(partner_uuid), "row_version": new_version,
            "name": payload["name"], "active": bool(row.active),
        }, occurred_at=now.isoformat(timespec="seconds"),
    )
    return "ACCEPTED", {
        "aggregate_version": new_version, "central_sequence": sequence,
        "partner_id": str(partner_uuid), "row_version": new_version,
        "active": bool(row.active),
    }, sequence


def _set_partner_active(
    connection: Connection, envelope: SyncOperationEnvelope, partner_uuid: UUID,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "PARTNER":
        return _reject("INVALID_AGGREGATE_TYPE", "SetPartnerActive requires PARTNER.")
    try:
        payload = validated_set_partner_active_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["partner_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.partner_id must equal aggregate_id."
        )
    if payload["expected_version"] != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT", "payload.expected_version must equal expected_version."
        )
    row = connection.execute(select(partners).where(
        partners.c.partner_uuid == partner_uuid
    ).with_for_update()).one_or_none()
    if row is None:
        return _reject("PARTNER_NOT_FOUND", "The selected partner does not exist.")
    if row.row_version != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT",
            f"Partner version is {row.row_version}, not {envelope.expected_version}.",
        )
    if bool(row.active) == payload["active"]:
        return _reject(
            "PARTNER_STATUS_UNCHANGED", "The partner already has the requested status."
        )
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    new_version = int(row.row_version) + 1
    connection.execute(update(partners).where(
        partners.c.partner_uuid == partner_uuid
    ).values(active=payload["active"], row_version=new_version, updated_at=now))
    event_type = "PartnerReactivated" if payload["active"] else "PartnerDeactivated"
    sequence = _publish_change(
        connection, event_type=event_type, aggregate_type="PARTNER",
        aggregate_uuid=partner_uuid, aggregate_version=new_version,
        payload={
            "partner_id": str(partner_uuid), "row_version": new_version,
            "name": row.name, "active": payload["active"],
            "reason": payload["reason"],
        }, occurred_at=now.isoformat(timespec="seconds"),
    )
    return "ACCEPTED", {
        "aggregate_version": new_version, "central_sequence": sequence,
        "partner_id": str(partner_uuid), "row_version": new_version,
        "active": payload["active"],
    }, sequence


def _invoice_total(draft_payload: dict) -> Decimal:
    total = Decimal("0")
    for collection in ("billing_lines", "parts"):
        for line in draft_payload.get(collection, ()):  # immutable server snapshot
            total += Decimal(str(line["quantity"])) * Decimal(str(line["unit_price"]))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _service_invoice_public_lines(draft_payload: dict) -> list[dict]:
    """Normalize the immutable Service draft for printing without repricing."""
    result = []
    for line in draft_payload.get("billing_lines", ()):
        quantity = Decimal(str(line["quantity"]))
        unit_price = Decimal(str(line["unit_price"]))
        result.append({
            "line_id": line["line_id"], "line_kind": "SERVICE",
            "article_id": None, "barcode": "", "lot_serial": "",
            "description": line["description"],
            "quantity": format(quantity, "f"),
            "billable_minutes": int(line.get("billable_minutes") or 0),
            "service_tier": str(line.get("line_kind") or ""),
            "tariff_version": str(line.get("tariff_version") or ""),
            "base_unit_price": format(unit_price, ".2f"),
            "applied_discount_percent": "0.0000",
            "unit_price": format(unit_price, ".2f"),
            "line_total": format(
                (quantity * unit_price).quantize(_CENT, rounding=ROUND_HALF_UP),
                ".2f",
            ),
        })
    for line in draft_payload.get("parts", ()):
        quantity = Decimal(str(line["quantity"]))
        unit_price = Decimal(str(line["unit_price"]))
        result.append({
            "line_id": line["line_id"], "line_kind": "ARTICLE",
            "article_id": line["article_id"], "barcode": line["barcode"],
            "lot_serial": str(line.get("lot_serial") or ""),
            "description": line["description"],
            "quantity": format(quantity, "f"),
            "base_unit_price": format(unit_price, ".2f"),
            "applied_discount_percent": "0.0000",
            "unit_price": format(unit_price, ".2f"),
            "line_total": format(
                (quantity * unit_price).quantize(_CENT, rounding=ROUND_HALF_UP),
                ".2f",
            ),
        })
    return result


def _billing_customer_snapshot(
    connection: Connection, *, selection: dict, draft_payload: dict,
    actor_id: str, confirmed_at: datetime,
) -> tuple[UUID | None, dict]:
    if selection["source"] == "SERVICE_CUSTOMER":
        customer = dict(draft_payload.get("customer") or {})
        if not str(customer.get("name") or "").strip():
            raise ContractError("The Service draft has no customer name.")
        return None, {
            "source": "SERVICE_CUSTOMER",
            "service_order_id": draft_payload["service_order_id"],
            "name": str(customer.get("name") or "").strip(),
            "address": str(customer.get("address") or "").strip(),
            "post_city": "",
            "tax_number": "",
            "phone": str(customer.get("phone") or "").strip(),
            "email": str(customer.get("email") or "").strip(),
            "confirmed_by": actor_id,
            "confirmed_at": confirmed_at.isoformat(timespec="seconds"),
        }

    if selection["source"] == "WALK_IN":
        return None, {
            "source": "WALK_IN",
            "name": "",
            "address": "",
            "post_city": "",
            "country": "Slovenija",
            "tax_number": "",
            "registration_number": "",
            "phone": "",
            "email": "",
            "confirmed_by": actor_id,
            "confirmed_at": confirmed_at.isoformat(timespec="seconds"),
        }

    partner_uuid = UUID(selection["partner_id"])
    partner = connection.execute(
        select(partners)
        .where(partners.c.partner_uuid == partner_uuid)
        .with_for_update()
    ).one_or_none()
    if partner is None:
        raise ContractError("The selected central partner does not exist.")
    if not partner.active:
        raise ContractError("The selected central partner is inactive.")
    if int(partner.row_version) != int(selection["expected_version"]):
        raise ContractError(
            f"Central partner version changed from {selection['expected_version']} "
            f"to {partner.row_version}; confirm the customer again."
        )
    return partner_uuid, {
        "source": "CENTRAL_PARTNER",
        "partner_id": str(partner.partner_uuid),
        "partner_version": int(partner.row_version),
        "name": partner.name,
        "address": partner.address,
        "post_city": partner.post_city,
        "country": partner.country,
        "tax_number": partner.tax_number,
        "registration_number": partner.registration_number,
        "phone": partner.phone,
        "email": partner.email,
        "partner_verified_by": partner.verified_by,
        "partner_verified_at": partner.verified_at.isoformat(timespec="seconds"),
        "confirmed_by": actor_id,
        "confirmed_at": confirmed_at.isoformat(timespec="seconds"),
    }


def _next_invoice_number(
    connection: Connection, *, fiscal_year: int, premise_id: str,
    electronic_device_id: str,
) -> tuple[int, str]:
    sequence = int(connection.execute(
        insert(invoice_number_sequences)
        .values(
            fiscal_year=fiscal_year,
            premise_id=premise_id,
            electronic_device_id=electronic_device_id,
            last_number=1,
        )
        .on_conflict_do_update(
            index_elements=[
                invoice_number_sequences.c.fiscal_year,
                invoice_number_sequences.c.premise_id,
                invoice_number_sequences.c.electronic_device_id,
            ],
            set_={
                "last_number": invoice_number_sequences.c.last_number + 1,
                "updated_at": datetime.now().astimezone(),
            },
        )
        .returning(invoice_number_sequences.c.last_number)
    ).scalar_one())
    return sequence, f"{premise_id}-{electronic_device_id}-{sequence}"


def _next_cash_session_number(connection: Connection, *, fiscal_year: int) -> str:
    sequence = int(connection.execute(
        insert(cash_session_number_sequences)
        .values(fiscal_year=fiscal_year, last_number=1)
        .on_conflict_do_update(
            index_elements=[cash_session_number_sequences.c.fiscal_year],
            set_={
                "last_number": cash_session_number_sequences.c.last_number + 1,
                "updated_at": datetime.now().astimezone(),
            },
        )
        .returning(cash_session_number_sequences.c.last_number)
    ).scalar_one())
    return f"BD-{fiscal_year}-{sequence:04d}"


def _cash_payment_amount(payments: list[dict]) -> Decimal:
    return sum(
        (
            Decimal(str(payment["amount"]))
            for payment in payments if payment["method"] == "CASH"
        ),
        Decimal("0"),
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _open_cash_session_for_invoice(
    connection: Connection, *, premise_id: str, electronic_device_id: str,
    payments: list[dict],
):
    if _cash_payment_amount(payments) == 0:
        return None
    session = connection.execute(
        select(cash_sessions).where(
            cash_sessions.c.premise_id == premise_id,
            cash_sessions.c.electronic_device_id == electronic_device_id,
            cash_sessions.c.status == "OPEN",
        ).with_for_update()
    ).one_or_none()
    if session is None:
        raise CentralCashJournalError(
            "CASH_SESSION_REQUIRED",
            "Cash payment requires an open cash session for this premise and device.",
        )
    return session


def _record_central_cash_entry(
    connection: Connection, *, session, entry_uuid: UUID, entry_type: str,
    amount: Decimal, reference_type: str, reference_uuid: UUID | None,
    reference_no: str, description: str, actor_id: str,
    actor_user_uuid: UUID | None, workstation_uuid: UUID | None,
    occurred_at: datetime,
) -> dict:
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if value == 0:
        raise CentralCashJournalError(
            "CASH_ENTRY_ZERO", "A cash journal entry cannot be zero."
        )
    last_no, movement = connection.execute(select(
        func.coalesce(func.max(cash_journal_entries.c.entry_no), 0),
        func.coalesce(func.sum(cash_journal_entries.c.amount), 0),
    ).where(cash_journal_entries.c.session_uuid == session.session_uuid)).one()
    balance = (
        Decimal(session.opening_cash) + Decimal(movement) + value
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if balance < 0:
        raise CentralCashJournalError(
            "NEGATIVE_CASH_BALANCE",
            "The cash expense would create a negative physical cash balance.",
        )
    entry_no = int(last_no) + 1
    connection.execute(insert(cash_journal_entries).values(
        entry_uuid=entry_uuid, session_uuid=session.session_uuid,
        entry_no=entry_no, occurred_at=occurred_at, entry_type=entry_type,
        amount=value, balance_after=balance, reference_type=reference_type,
        reference_uuid=reference_uuid, reference_no=str(reference_no or ""),
        description=description, actor_user_uuid=actor_user_uuid,
        actor_id=actor_id, workstation_uuid=workstation_uuid,
    ))
    return {
        "entry_id": str(entry_uuid), "entry_no": entry_no,
        "amount": f"{value:.2f}", "balance_after": f"{balance:.2f}",
    }


def _open_cash_session(
    connection: Connection, envelope: SyncOperationEnvelope,
    session_uuid: UUID, *, actor_user_uuid: UUID | None,
    workstation_uuid: UUID | None,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "CASH_SESSION":
        return _reject("INVALID_AGGREGATE_TYPE", "OpenCashSession requires CASH_SESSION.")
    try:
        payload = validated_open_cash_session_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["session_id"] != envelope.aggregate_id:
        return _reject("AGGREGATE_ID_MISMATCH", "payload.session_id must equal aggregate_id.")
    if envelope.expected_version != 0:
        return _reject("VERSION_CONFLICT", "A new cash session requires version 0.")
    connection.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(
        f"{payload['premise_id']}|{payload['electronic_device_id']}", 0
    ))))
    if connection.execute(select(cash_sessions.c.session_uuid).where(
        cash_sessions.c.session_uuid == session_uuid
    )).scalar_one_or_none() is not None:
        return _reject("CASH_SESSION_ALREADY_EXISTS", "This cash session already exists.")
    opened = connection.execute(select(cash_sessions).where(
        cash_sessions.c.premise_id == payload["premise_id"],
        cash_sessions.c.electronic_device_id == payload["electronic_device_id"],
        cash_sessions.c.status == "OPEN",
    )).one_or_none()
    if opened is not None:
        return _reject(
            "CASH_SESSION_ALREADY_OPEN",
            f"Cash session {opened.session_no} is already open for this location.",
        )
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    session_no = _next_cash_session_number(connection, fiscal_year=now.year)
    connection.execute(insert(cash_sessions).values(
        session_uuid=session_uuid, session_no=session_no,
        premise_id=payload["premise_id"],
        electronic_device_id=payload["electronic_device_id"],
        status="OPEN", row_version=1, opened_at=now,
        opened_by_user_uuid=actor_user_uuid, opened_by=envelope.actor_id,
        opened_by_workstation_uuid=workstation_uuid,
        opening_cash=Decimal(payload["opening_cash"]),
    ))
    sequence = _publish_change(
        connection, event_type="CashSessionOpened", aggregate_type="CASH_SESSION",
        aggregate_uuid=session_uuid, aggregate_version=1,
        payload={
            "session_id": str(session_uuid), "session_no": session_no,
            "status": "OPEN", "premise_id": payload["premise_id"],
            "electronic_device_id": payload["electronic_device_id"],
            "opening_cash": f"{Decimal(payload['opening_cash']):.2f}",
        },
        occurred_at=now.isoformat(timespec="seconds"),
    )
    return "ACCEPTED", {
        "aggregate_version": 1, "central_sequence": sequence,
        "session_id": str(session_uuid), "session_no": session_no,
        "status": "OPEN", "opening_cash": f"{Decimal(payload['opening_cash']):.2f}",
    }, sequence


def _ten_working_day_deadline(value: date) -> date:
    """Conservative deadline; public holidays may extend it, never shorten it."""
    cursor = value
    remaining = 10
    while remaining:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def _close_cash_session(
    connection: Connection, envelope: SyncOperationEnvelope,
    session_uuid: UUID, *, actor_user_uuid: UUID | None,
    workstation_uuid: UUID | None,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "CASH_SESSION":
        return _reject("INVALID_AGGREGATE_TYPE", "CloseCashSession requires CASH_SESSION.")
    try:
        payload = validated_close_cash_session_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["session_id"] != envelope.aggregate_id:
        return _reject("AGGREGATE_ID_MISMATCH", "payload.session_id must equal aggregate_id.")
    if int(payload["expected_version"]) != envelope.expected_version:
        return _reject("VERSION_CONFLICT", "Cash session version does not match the envelope.")
    session = connection.execute(
        select(cash_sessions).where(cash_sessions.c.session_uuid == session_uuid).with_for_update()
    ).one_or_none()
    if session is None:
        return _reject("CASH_SESSION_NOT_FOUND", "The cash session does not exist.")
    if session.status != "OPEN" or int(session.row_version) != 1:
        return _reject("VERSION_CONFLICT", "The cash session is no longer open at version 1.")
    movement = Decimal(connection.execute(select(
        func.coalesce(func.sum(cash_journal_entries.c.amount), 0)
    ).where(cash_journal_entries.c.session_uuid == session_uuid)).scalar_one())
    expected = (Decimal(session.opening_cash) + movement).quantize(Decimal("0.01"))
    counted = Decimal(payload["counted_cash"])
    difference = (counted - expected).quantize(Decimal("0.01"))
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    connection.execute(update(cash_sessions).where(
        cash_sessions.c.session_uuid == session_uuid,
        cash_sessions.c.status == "OPEN",
    ).values(
        status="CLOSED", row_version=2, closed_at=now,
        closed_by_user_uuid=actor_user_uuid, closed_by=envelope.actor_id,
        closed_by_workstation_uuid=workstation_uuid,
        expected_cash=expected, counted_cash=counted, difference=difference,
        close_note=payload["note"], updated_at=now,
    ))
    sequence = _publish_change(
        connection, event_type="CashSessionClosed", aggregate_type="CASH_SESSION",
        aggregate_uuid=session_uuid, aggregate_version=2,
        payload={
            "session_id": str(session_uuid), "session_no": session.session_no,
            "status": "CLOSED", "expected_cash": f"{expected:.2f}",
            "counted_cash": f"{counted:.2f}", "difference": f"{difference:.2f}",
        },
        occurred_at=now.isoformat(timespec="seconds"),
    )
    return "ACCEPTED", {
        "aggregate_version": 2, "central_sequence": sequence,
        "session_id": str(session_uuid), "session_no": session.session_no,
        "status": "CLOSED", "expected_cash": f"{expected:.2f}",
        "counted_cash": f"{counted:.2f}", "difference": f"{difference:.2f}",
    }, sequence


def _record_manual_cash_entry(
    connection: Connection, envelope: SyncOperationEnvelope,
    entry_uuid: UUID, *, actor_user_uuid: UUID | None,
    workstation_uuid: UUID | None,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "CASH_ENTRY":
        return _reject("INVALID_AGGREGATE_TYPE", "RecordManualCashEntry requires CASH_ENTRY.")
    try:
        payload = validated_manual_cash_entry_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["entry_id"] != envelope.aggregate_id:
        return _reject("AGGREGATE_ID_MISMATCH", "payload.entry_id must equal aggregate_id.")
    if envelope.expected_version != 0:
        return _reject("VERSION_CONFLICT", "A new cash entry requires version 0.")
    session_uuid = UUID(payload["session_id"])
    session = connection.execute(
        select(cash_sessions).where(cash_sessions.c.session_uuid == session_uuid).with_for_update()
    ).one_or_none()
    if session is None or session.status != "OPEN":
        return _reject("CASH_SESSION_NOT_OPEN", "The selected cash session is not open.")
    amount = Decimal(payload["amount"])
    if payload["direction"] == "EXPENSE":
        amount = -amount
    try:
        result = _record_central_cash_entry(
            connection, session=session, entry_uuid=entry_uuid,
            entry_type=(
                "CASH_RECEIPT" if payload["direction"] == "RECEIPT"
                else "CASH_EXPENSE"
            ),
            amount=amount, reference_type="MANUAL_DOCUMENT",
            reference_uuid=entry_uuid, reference_no=payload["reference_no"],
            description=payload["description"], actor_id=envelope.actor_id,
            actor_user_uuid=actor_user_uuid, workstation_uuid=workstation_uuid,
            occurred_at=datetime.now(ZoneInfo("Europe/Ljubljana")),
        )
    except CentralCashJournalError as exc:
        return _reject(exc.code, str(exc))
    sequence = _publish_change(
        connection, event_type="CashJournalEntryRecorded",
        aggregate_type="CASH_SESSION", aggregate_uuid=session_uuid,
        aggregate_version=result["entry_no"],
        payload={**result, "session_id": str(session_uuid),
                 "entry_type": (
                     "CASH_RECEIPT" if amount > 0 else "CASH_EXPENSE"
                 )},
        occurred_at=datetime.now(ZoneInfo("Europe/Ljubljana")).isoformat(timespec="seconds"),
    )
    return "ACCEPTED", {
        "aggregate_version": 1, "central_sequence": sequence,
        "session_id": str(session_uuid), **result,
    }, sequence


def _issue_service_invoice(
    connection: Connection,
    envelope: SyncOperationEnvelope,
    draft_uuid: UUID,
    operation_uuid: UUID,
    operator_tax_number: str = "",
    actor_user_uuid: UUID | None = None,
    workstation_uuid: UUID | None = None,
) -> tuple[str, dict, int | None]:
    """Create at most one invoice from server-owned immutable draft content."""
    if envelope.aggregate_type != "INVOICE_DRAFT":
        return _reject(
            "INVALID_AGGREGATE_TYPE", "IssueServiceInvoice requires INVOICE_DRAFT."
        )
    try:
        payload = validated_issue_service_invoice_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["draft_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.draft_id must equal aggregate_id."
        )
    if int(payload["expected_version"]) != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT",
            "payload.expected_version must equal envelope.expected_version.",
        )

    draft = connection.execute(
        select(invoice_drafts)
        .where(invoice_drafts.c.draft_uuid == draft_uuid)
        .with_for_update()
    ).one_or_none()
    if draft is None:
        return _reject("DRAFT_NOT_FOUND", "The invoice draft does not exist.")
    existing = connection.execute(
        select(invoices).where(invoices.c.draft_uuid == draft_uuid)
    ).one_or_none()
    if existing is not None:
        return (
            "REJECTED",
            {
                "error_code": "INVOICE_ALREADY_EXISTS",
                "error_message": "This Service draft already has an invoice.",
                "invoice_id": str(existing.invoice_uuid),
                "invoice_number": existing.invoice_number,
                "invoice_status": existing.status,
            },
            None,
        )
    if draft.status != "READY" or envelope.expected_version != 1:
        return _reject(
            "VERSION_CONFLICT", "The invoice draft is no longer READY at version 1."
        )

    draft_payload = dict(draft.payload_json)
    total = _invoice_total(draft_payload)
    payments = list(payload["payments"])
    payment_total = sum(
        (Decimal(str(payment["amount"])) for payment in payments), Decimal("0")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if payment_total != total:
        return _reject(
            "PAYMENT_TOTAL_MISMATCH",
            f"Payments total {payment_total:.2f} must equal invoice total {total:.2f}.",
        )

    cash_like = any(
        payment["method"] in {"CASH", "CARD"} for payment in payments
    )


    bank_only_unpaid = (
        len(payments) == 1
        and payments[0]["method"] == "BANK_TRANSFER"
        and not payments[0]["confirmed"]
    )
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    try:
        billing_partner_uuid, billing_customer = _billing_customer_snapshot(
            connection,
            selection=dict(payload["billing_customer"]),
            draft_payload=draft_payload,
            actor_id=envelope.actor_id,
            confirmed_at=now,
        )
    except ContractError as exc:
        return _reject("BILLING_CUSTOMER_CONFLICT", str(exc))
    try:
        cash_session = _open_cash_session_for_invoice(
            connection, premise_id=payload["premise_id"],
            electronic_device_id=payload["electronic_device_id"],
            payments=payments,
        )
    except CentralCashJournalError as exc:
        return _reject(exc.code, str(exc))
    fiscal_year = now.year
    sequence, invoice_number = _next_invoice_number(
        connection,
        fiscal_year=fiscal_year,
        premise_id=payload["premise_id"],
        electronic_device_id=payload["electronic_device_id"],
    )
    invoice_uuid = uuid4()
    invoice_status = "PENDING_FISCALIZATION" if cash_like else "ISSUED"
    draft_status = "ISSUING" if cash_like else "INVOICED"
    payment_status = "UNPAID" if bank_only_unpaid else "PAID"
    tax_note = "Oproščeno po prvem odstavku 94. člena ZDDV-1."
    snapshot = {
        "invoice_id": str(invoice_uuid),
        "invoice_number": invoice_number,
        "draft": draft_payload,
        "source_type": "SERVICE",
        "lines": _service_invoice_public_lines(draft_payload),
        "billing_customer": billing_customer,
        "payments": payments,
        "total_amount": f"{total:.2f}",
        "currency": "EUR",
        "tax_mode": "NON_VAT",
        "tax_rate": "0",
        "tax_note": tax_note,
        "cashier_id": envelope.actor_id,
        "operator_tax_number": str(operator_tax_number or ""),
        "premise_id": payload["premise_id"],
        "electronic_device_id": payload["electronic_device_id"],
        "cash_session_id": str(cash_session.session_uuid) if cash_session else "",
        "issued_at": now.isoformat(timespec="seconds"),
    }
    connection.execute(insert(invoices).values(
        invoice_uuid=invoice_uuid,
        draft_uuid=draft_uuid,
        service_order_uuid=draft.service_order_uuid,
        issue_operation_id=operation_uuid,
        invoice_number=invoice_number,
        fiscal_year=fiscal_year,
        fiscal_sequence=sequence,
        premise_id=payload["premise_id"],
        electronic_device_id=payload["electronic_device_id"],
        cash_session_uuid=cash_session.session_uuid if cash_session else None,
        cashier_id=envelope.actor_id,
        operator_tax_number=str(operator_tax_number or "") or None,
        billing_partner_uuid=billing_partner_uuid,
        billing_customer_json=billing_customer,
        status=invoice_status,
        payment_status=payment_status,
        payments_json=payments,
        total_amount=total,
        tax_mode="NON_VAT",
        tax_rate=Decimal("0"),
        tax_note=tax_note,
        snapshot_json=snapshot,
        issued_at=now,
        finalized_at=None if cash_like else now,
    ))
    cash_amount = _cash_payment_amount(payments)
    if cash_session is not None:
        _record_central_cash_entry(
            connection, session=cash_session, entry_uuid=uuid4(),
            entry_type="CASH_SALE", amount=cash_amount,
            reference_type="INVOICE", reference_uuid=invoice_uuid,
            reference_no=invoice_number,
            description=f"Gotovinsko plačilo računa {invoice_number}",
            actor_id=envelope.actor_id, actor_user_uuid=actor_user_uuid,
            workstation_uuid=workstation_uuid, occurred_at=now,
        )
    connection.execute(
        update(invoice_drafts)
        .where(invoice_drafts.c.draft_uuid == draft_uuid)
        .values(status=draft_status, updated_at=now)
    )

    if cash_like:
        fiscal_request = {
            "message_id": str(uuid4()),
            "invoice_id": str(invoice_uuid),
            "invoice_number": invoice_number,
            "invoice_snapshot": snapshot,
        }
        connection.execute(insert(fiscal_outbox).values(
            message_uuid=UUID(fiscal_request["message_id"]),
            invoice_uuid=invoice_uuid,
            status="PENDING",
            request_sha256=payload_sha256(fiscal_request),
            request_json=fiscal_request,
        ))
        event_type = "InvoiceFiscalizationQueued"
    else:
        order = connection.execute(
            select(service_orders)
            .where(service_orders.c.sync_uuid == draft.service_order_uuid)
            .with_for_update()
        ).one()
        updated_order_payload = dict(order.payload_json)
        updated_order_payload["technical_status"] = "BILLING_COMPLETED"
        order_version = int(order.row_version) + 1
        connection.execute(
            update(service_orders)
            .where(service_orders.c.sync_uuid == draft.service_order_uuid)
            .values(
                row_version=order_version,
                payload_json=updated_order_payload,
                updated_at=now,
            )
        )
        _publish_change(
            connection,
            event_type="ServiceBillingCompleted",
            aggregate_uuid=draft.service_order_uuid,
            aggregate_version=order_version,
            payload={
                "service_order_id": str(draft.service_order_uuid),
                "invoice_id": str(invoice_uuid),
                "invoice_number": invoice_number,
                "status": "BILLING_COMPLETED",
            },
            occurred_at=now.isoformat(timespec="seconds"),
        )
        event_type = "InvoiceIssued"

    central_sequence = _publish_change(
        connection,
        event_type=event_type,
        aggregate_type="INVOICE_DRAFT",
        aggregate_uuid=draft_uuid,
        aggregate_version=2,
        payload={
            "draft_uuid": str(draft_uuid),
            "service_order_id": str(draft.service_order_uuid),
            "invoice_id": str(invoice_uuid),
            "invoice_number": invoice_number,
            "invoice_status": invoice_status,
            "payment_status": payment_status,
            "total_amount": f"{total:.2f}",
        },
        occurred_at=now.isoformat(timespec="seconds"),
    )
    return (
        "ACCEPTED",
        {
            "aggregate_version": 2,
            "central_sequence": central_sequence,
            "invoice_id": str(invoice_uuid),
            "invoice_number": invoice_number,
            "invoice_status": invoice_status,
            "draft_status": draft_status,
            "payment_status": payment_status,
            "total_amount": f"{total:.2f}",
        },
        central_sequence,
    )


def prepare_direct_sale_quote(
    connection: Connection, lines: list[dict], *, lock_rows: bool = False,
) -> tuple[list[dict], dict[UUID, Decimal], Decimal]:
    """Resolve server prices and LOTs once for both quote and atomic issuance."""
    prepared_lines: list[dict] = []
    input_positions = {
        line["line_id"]: position for position, line in enumerate(lines)
    }
    article_requested: dict[UUID, Decimal] = {}
    article_rows: dict[UUID, object] = {}

    def locked(query):
        return query.with_for_update() if lock_rows else query

    article_lines = [line for line in lines if line["line_kind"] == "ARTICLE"]
    for line in sorted(article_lines, key=lambda value: value["barcode"]):
        barcode = str(line["barcode"])
        identity = connection.execute(locked(select(inventory_identities).where(
            inventory_identities.c.supplier_barcode == barcode
        ))).one_or_none()
        if identity is None:
            article = connection.execute(locked(select(inventory_items).where(
                inventory_items.c.barcode == barcode
            ))).one_or_none()
        else:
            article = connection.execute(locked(select(inventory_items).where(
                inventory_items.c.article_uuid == identity.article_uuid
            ))).one_or_none()
        if article is None:
            raise DirectSalePreparationError(
                "STOCK_ITEM_NOT_FOUND",
                f"Barcode {barcode} does not exist in confirmed central stock.",
            )
        if article.blocked:
            raise DirectSalePreparationError(
                "STOCK_ITEM_BLOCKED",
                f"Barcode {barcode} is blocked: "
                f"{article.blocked_reason or 'review required'}.",
            )
        if article.article_uuid in article_rows:
            raise DirectSalePreparationError(
                "DUPLICATE_ARTICLE_LINE",
                "One article may appear only once on a direct-sale invoice; "
                "use one combined quantity.",
            )
        quantity = Decimal(str(line["quantity"]))
        requested_lot = str(line.get("lot_serial") or "").strip()
        if article.track_mode == "SERIAL" and (quantity != 1 or not requested_lot):
            raise DirectSalePreparationError(
                "SERIAL_IDENTITY_REQUIRED",
                f"Serial-tracked barcode {barcode} requires one exact serial.",
            )
        article_requested[article.article_uuid] = quantity
        article_rows[article.article_uuid] = article
        lots_query = select(inventory_lots).where(
            inventory_lots.c.article_uuid == article.article_uuid,
            inventory_lots.c.status == "AVAILABLE",
            inventory_lots.c.available_quantity > 0,
        )
        if requested_lot:
            lots_query = lots_query.where(inventory_lots.c.lot_serial == requested_lot)
        lots = connection.execute(locked(lots_query.order_by(
            inventory_lots.c.received_date,
            inventory_lots.c.created_at,
            inventory_lots.c.lot_uuid,
        ))).all()
        if not lots:
            raise DirectSalePreparationError(
                "MISSING_LOT_COST",
                f"Barcode {barcode} has no available LOT with a verified acquisition cost.",
            )
        remaining = quantity
        allocations = []
        for lot in lots:
            if remaining <= 0:
                break
            used = min(Decimal(lot.available_quantity), remaining)
            if used > 0:
                allocations.append((lot, used))
                remaining -= used
        if remaining > 0:
            raise DirectSalePreparationError(
                "INSUFFICIENT_STOCK",
                f"Barcode {barcode} does not have {quantity} available pieces.",
            )
        highest_cost = max(Decimal(lot.landed_unit_cost) for lot, _ in allocations)
        floor_price = (highest_cost * _PART_FLOOR).quantize(
            _CENT, rounding=ROUND_CEILING
        )
        base_price = Decimal(article.current_sale_price).quantize(_CENT)
        if base_price < floor_price:
            raise DirectSalePreparationError(
                "INVALID_SALE_PRICE",
                f"Barcode {barcode} has a sale price below its LOT cost guard.",
            )
        requested_discount = Decimal(line["requested_discount_percent"])
        requested_price = (
            base_price * (Decimal("1") - requested_discount / Decimal("100"))
        ).quantize(_CENT, rounding=ROUND_HALF_UP)
        applied_price = max(requested_price, floor_price)
        applied_discount = (
            (Decimal("1") - applied_price / base_price) * Decimal("100")
        ).quantize(_FOUR, rounding=ROUND_HALF_UP)
        prepared_lines.append({
            "line_id": line["line_id"], "line_kind": "ARTICLE",
            "article_id": str(article.article_uuid), "barcode": barcode,
            "lot_serial": requested_lot, "description": article.description,
            "quantity": format(quantity, "f"),
            "base_unit_price": format(base_price, ".2f"),
            "requested_discount_percent": format(requested_discount, ".4f"),
            "applied_discount_percent": format(applied_discount, ".4f"),
            "unit_price": format(applied_price, ".2f"),
            "line_total": format(
                (quantity * applied_price).quantize(_CENT, rounding=ROUND_HALF_UP),
                ".2f",
            ),
            "allocations": allocations,
        })

    for line in lines:
        if line["line_kind"] != "SERVICE":
            continue
        tariff = connection.execute(locked(select(service_tariffs).where(
            service_tariffs.c.tier == line["service_tier"],
            service_tariffs.c.active.is_(True),
        ))).one_or_none()
        if tariff is None:
            raise DirectSalePreparationError(
                "SERVICE_TARIFF_NOT_CONFIGURED",
                f"No active central tariff exists for {line['service_tier']}.",
            )
        elapsed_minutes = int(line["elapsed_minutes"])
        extra_half_hours = max(0, (elapsed_minutes - 60 + 29) // 30)
        billable_minutes = 60 + 30 * extra_half_hours
        base_price = (
            Decimal(tariff.first_hour_rate)
            + Decimal(tariff.extra_half_hour_rate) * extra_half_hours
        ).quantize(_CENT, rounding=ROUND_HALF_UP)
        requested_discount = Decimal(line["requested_discount_percent"])
        requested_price = (
            base_price * (Decimal("1") - requested_discount / Decimal("100"))
        ).quantize(_CENT, rounding=ROUND_HALF_UP)
        applied_price = max(
            requested_price, Decimal(tariff.minimum_line_price).quantize(_CENT)
        )
        applied_discount = (
            (Decimal("1") - applied_price / base_price) * Decimal("100")
        ).quantize(_FOUR, rounding=ROUND_HALF_UP)
        tier = str(tariff.tier)
        prepared_lines.append({
            "line_id": line["line_id"], "line_kind": "SERVICE",
            "article_id": None, "barcode": "", "lot_serial": "",
            "description": "Delo na terenu" if tier == "T2" else "Delo v delavnici",
            "quantity": "1", "elapsed_minutes": elapsed_minutes,
            "billable_minutes": billable_minutes, "service_tier": tier,
            "tariff_version": tariff.tariff_version,
            "first_hour_rate": format(Decimal(tariff.first_hour_rate), ".2f"),
            "extra_half_hour_rate": format(
                Decimal(tariff.extra_half_hour_rate), ".2f"
            ),
            "base_unit_price": format(base_price, ".2f"),
            "requested_discount_percent": format(requested_discount, ".4f"),
            "applied_discount_percent": format(applied_discount, ".4f"),
            "unit_price": format(applied_price, ".2f"),
            "line_total": format(applied_price, ".2f"), "allocations": [],
        })

    prepared_lines.sort(key=lambda line: input_positions[line["line_id"]])
    for article_uuid, requested in article_requested.items():
        if Decimal(article_rows[article_uuid].quantity_on_hand) < requested:
            raise DirectSalePreparationError(
                "INSUFFICIENT_STOCK",
                "The combined direct-sale quantity exceeds article stock.",
            )
    total = sum(
        (Decimal(line["line_total"]) for line in prepared_lines), Decimal("0")
    ).quantize(_CENT, rounding=ROUND_HALF_UP)
    return prepared_lines, article_requested, total


def _issue_direct_sale_invoice(
    connection: Connection,
    envelope: SyncOperationEnvelope,
    sale_uuid: UUID,
    operation_uuid: UUID,
    operator_tax_number: str = "",
    actor_user_uuid: UUID | None = None,
    workstation_uuid: UUID | None = None,
) -> tuple[str, dict, int | None]:
    """Sell central stock once, keeping customer lines and FIFO costs separate."""
    if envelope.aggregate_type != "DIRECT_SALE":
        return _reject(
            "INVALID_AGGREGATE_TYPE", "IssueDirectSaleInvoice requires DIRECT_SALE."
        )
    try:
        payload = validated_issue_direct_sale_invoice_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["sale_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.sale_id must equal aggregate_id."
        )
    if envelope.expected_version != 0:
        return _reject("VERSION_CONFLICT", "A direct sale requires version 0.")

    # A new direct sale has no aggregate row to lock yet.  Serialize its UUID so
    # two Cashiers using different operation IDs still receive one clean winner
    # and one stable business rejection instead of a late unique-key failure.
    sale_lock_key = sale_uuid.int & ((1 << 63) - 1)
    connection.execute(select(func.pg_advisory_xact_lock(sale_lock_key)))
    existing = connection.execute(select(invoices).where(
        invoices.c.direct_sale_uuid == sale_uuid
    )).one_or_none()
    if existing is not None:
        return (
            "REJECTED",
            {
                "error_code": "DIRECT_SALE_ALREADY_EXISTS",
                "error_message": "This direct sale already has an invoice.",
                "invoice_id": str(existing.invoice_uuid),
                "invoice_number": existing.invoice_number,
                "invoice_status": existing.status,
            },
            None,
        )

    try:
        prepared_lines, article_requested, total = prepare_direct_sale_quote(
            connection, list(payload["lines"]), lock_rows=True
        )
    except DirectSalePreparationError as exc:
        return _reject(exc.code, str(exc))
    payments = list(payload["payments"])
    payment_total = sum(
        (Decimal(str(payment["amount"])) for payment in payments), Decimal("0")
    ).quantize(_CENT, rounding=ROUND_HALF_UP)
    if payment_total != total:
        return _reject(
            "PAYMENT_TOTAL_MISMATCH",
            f"Payments total {payment_total:.2f} must equal invoice total {total:.2f}.",
        )

    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    try:
        billing_partner_uuid, billing_customer = _billing_customer_snapshot(
            connection, selection=dict(payload["billing_customer"]),
            draft_payload={}, actor_id=envelope.actor_id, confirmed_at=now,
        )
    except ContractError as exc:
        return _reject("BILLING_CUSTOMER_CONFLICT", str(exc))
    cash_like = any(
        payment["method"] in {"CASH", "CARD"} for payment in payments
    )

    bank_only_unpaid = (
        len(payments) == 1
        and payments[0]["method"] == "BANK_TRANSFER"
        and not payments[0]["confirmed"]
    )
    try:
        cash_session = _open_cash_session_for_invoice(
            connection, premise_id=payload["premise_id"],
            electronic_device_id=payload["electronic_device_id"],
            payments=payments,
        )
    except CentralCashJournalError as exc:
        return _reject(exc.code, str(exc))
    sequence, invoice_number = _next_invoice_number(
        connection, fiscal_year=now.year, premise_id=payload["premise_id"],
        electronic_device_id=payload["electronic_device_id"],
    )
    invoice_uuid = uuid4()
    public_lines = [{
        key: value for key, value in line.items()
        if key not in {"allocations", "requested_discount_percent"}
    } for line in prepared_lines]
    invoice_status = "PENDING_FISCALIZATION" if cash_like else "ISSUED"
    payment_status = "UNPAID" if bank_only_unpaid else "PAID"
    tax_note = "Oproščeno po prvem odstavku 94. člena ZDDV-1."
    snapshot = {
        "invoice_id": str(invoice_uuid), "invoice_number": invoice_number,
        "source_type": "DIRECT_SALE", "direct_sale_id": str(sale_uuid),
        "lines": public_lines, "billing_customer": billing_customer,
        "payments": payments, "total_amount": format(total, ".2f"),
        "currency": "EUR", "tax_mode": "NON_VAT", "tax_rate": "0",
        "tax_note": tax_note, "cashier_id": envelope.actor_id,
        "operator_tax_number": str(operator_tax_number or ""),
        "premise_id": payload["premise_id"],
        "electronic_device_id": payload["electronic_device_id"],
        "cash_session_id": str(cash_session.session_uuid) if cash_session else "",
        "issued_at": now.isoformat(timespec="seconds"),
    }
    connection.execute(insert(invoices).values(
        invoice_uuid=invoice_uuid, draft_uuid=None, service_order_uuid=None,
        source_type="DIRECT_SALE", direct_sale_uuid=sale_uuid,
        issue_operation_id=operation_uuid, invoice_number=invoice_number,
        fiscal_year=now.year, fiscal_sequence=sequence,
        premise_id=payload["premise_id"],
        electronic_device_id=payload["electronic_device_id"],
        cash_session_uuid=cash_session.session_uuid if cash_session else None,
        cashier_id=envelope.actor_id,
        operator_tax_number=str(operator_tax_number or "") or None,
        billing_partner_uuid=billing_partner_uuid,
        billing_customer_json=billing_customer,
        status=invoice_status, payment_status=payment_status,
        payments_json=payments, total_amount=total, tax_mode="NON_VAT",
        tax_rate=Decimal("0"), tax_note=tax_note, snapshot_json=snapshot,
        issued_at=now, finalized_at=None if cash_like else now,
    ))
    cash_amount = _cash_payment_amount(payments)
    if cash_session is not None:
        _record_central_cash_entry(
            connection, session=cash_session, entry_uuid=uuid4(),
            entry_type="CASH_SALE", amount=cash_amount,
            reference_type="INVOICE", reference_uuid=invoice_uuid,
            reference_no=invoice_number,
            description=f"Gotovinsko plačilo računa {invoice_number}",
            actor_id=envelope.actor_id, actor_user_uuid=actor_user_uuid,
            workstation_uuid=workstation_uuid, occurred_at=now,
        )
    for line in prepared_lines:
        requested = Decimal(line["requested_discount_percent"])
        applied = Decimal(line["applied_discount_percent"])
        connection.execute(insert(direct_sale_discount_audit).values(
            invoice_uuid=invoice_uuid,
            sale_line_uuid=UUID(line["line_id"]),
            requested_discount_percent=requested,
            applied_discount_percent=applied,
            decision=(
                "AS_REQUESTED" if requested == applied
                else "CLAMPED_TO_MINIMUM"
            ),
        ))
    for article_uuid, requested in article_requested.items():
        connection.execute(update(inventory_items).where(
            inventory_items.c.article_uuid == article_uuid
        ).values(
            quantity_on_hand=inventory_items.c.quantity_on_hand - requested,
            row_version=inventory_items.c.row_version + 1, updated_at=now,
        ))
    for line in prepared_lines:
        for lot, used in line["allocations"]:
            new_available = Decimal(lot.available_quantity) - used
            connection.execute(update(inventory_lots).where(
                inventory_lots.c.lot_uuid == lot.lot_uuid
            ).values(
                available_quantity=new_available,
                status="CONSUMED" if new_available == 0 else "AVAILABLE",
            ))
            connection.execute(insert(direct_sale_stock_allocations).values(
                invoice_uuid=invoice_uuid,
                sale_line_uuid=UUID(line["line_id"]), lot_uuid=lot.lot_uuid,
                quantity=used, landed_unit_cost=lot.landed_unit_cost,
            ))

    if cash_like:
        fiscal_request = {
            "message_id": str(uuid4()), "invoice_id": str(invoice_uuid),
            "invoice_number": invoice_number, "invoice_snapshot": snapshot,
        }
        connection.execute(insert(fiscal_outbox).values(
            message_uuid=UUID(fiscal_request["message_id"]),
            invoice_uuid=invoice_uuid, status="PENDING",
            request_sha256=payload_sha256(fiscal_request),
            request_json=fiscal_request,
        ))
        event_type = "DirectSaleFiscalizationQueued"
    else:
        event_type = "DirectSaleInvoiceIssued"
    inventory_updates = _inventory_updates_for_articles(
        connection, sorted(article_requested, key=str)
    )
    central_sequence = _publish_change(
        connection, event_type=event_type, aggregate_type="DIRECT_SALE",
        aggregate_uuid=sale_uuid, aggregate_version=1,
        payload={
            "direct_sale_id": str(sale_uuid), "invoice_id": str(invoice_uuid),
            "invoice_number": invoice_number, "invoice_status": invoice_status,
            "payment_status": payment_status, "total_amount": format(total, ".2f"),
            "inventory_updates": inventory_updates,
        },
        occurred_at=now.isoformat(timespec="seconds"),
    )
    return "ACCEPTED", {
        "aggregate_version": 1, "central_sequence": central_sequence,
        "invoice_id": str(invoice_uuid), "invoice_number": invoice_number,
        "invoice_status": invoice_status, "payment_status": payment_status,
        "total_amount": format(total, ".2f"), "lines": public_lines,
        "inventory_updates": inventory_updates,
    }, central_sequence


def _settle_unpaid_bank_invoice(
    connection: Connection, envelope: SyncOperationEnvelope,
    invoice_uuid: UUID, operation_uuid: UUID, *,
    operator_tax_number: str, actor_user_uuid: UUID | None,
    workstation_uuid: UUID | None,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "INVOICE":
        return _reject(
            "INVALID_AGGREGATE_TYPE", "SettleUnpaidBankInvoice requires INVOICE."
        )
    try:
        payload = validated_settle_unpaid_bank_invoice_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["invoice_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.invoice_id must equal aggregate_id."
        )
    if envelope.expected_version != 1:
        return _reject("VERSION_CONFLICT", "An unpaid bank invoice requires version 1.")
    invoice = connection.execute(
        select(invoices).where(invoices.c.invoice_uuid == invoice_uuid).with_for_update()
    ).one_or_none()
    if invoice is None:
        return _reject("INVOICE_NOT_FOUND", "The selected invoice does not exist.")
    if invoice.status != "ISSUED" or invoice.payment_status != "UNPAID":
        return _reject(
            "INVOICE_NOT_UNPAID",
            "Only an issued, unpaid bank-transfer invoice can receive late payment.",
        )
    original_payments = [dict(item) for item in (invoice.payments_json or [])]
    if (
        len(original_payments) != 1
        or str(original_payments[0].get("method") or "").upper() != "BANK_TRANSFER"
        or bool(original_payments[0].get("confirmed"))
        or Decimal(str(original_payments[0].get("amount") or 0))
        != Decimal(invoice.total_amount)
    ):
        return _reject(
            "INVOICE_PAYMENT_SHAPE_INVALID",
            "The invoice does not contain one exact unpaid bank-transfer payment.",
        )

    method = payload["method"]
    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    if method == "BANK_TRANSFER":
        connection.execute(
            update(invoices).where(invoices.c.invoice_uuid == invoice_uuid).values(
                payment_status="PAID", settlement_operation_id=operation_uuid,
                payment_received_at=now, payment_received_method=method,
                row_version=invoices.c.row_version + 1,
                updated_at=now,
            )
        )
        central_sequence = _publish_change(
            connection, event_type="LateBankTransferPaymentConfirmed",
            aggregate_type="INVOICE", aggregate_uuid=invoice_uuid,
            aggregate_version=2,
            payload={
                "invoice_id": str(invoice_uuid),
                "invoice_number": invoice.invoice_number,
                "invoice_status": "ISSUED", "payment_status": "PAID",
                "payment_received_method": method,
                "payment_received_at": now.isoformat(timespec="seconds"),
            },
            occurred_at=now.isoformat(timespec="seconds"),
        )
        return "ACCEPTED", {
            "aggregate_version": 2, "central_sequence": central_sequence,
            "invoice_id": str(invoice_uuid),
            "invoice_number": invoice.invoice_number,
            "invoice_status": "ISSUED", "payment_status": "PAID",
            "payment_received_method": method,
        }, central_sequence

    cash_session = _open_cash_session_for_invoice(
        connection, premise_id=invoice.premise_id,
        electronic_device_id=invoice.electronic_device_id,
        payments=[{"method": method, "amount": str(invoice.total_amount)}],
    )
    deadline = _ten_working_day_deadline(now.date())
    fiscal_request = {
        "message_id": str(uuid4()), "invoice_id": str(invoice_uuid),
        "invoice_number": invoice.invoice_number,
        "invoice_snapshot": dict(invoice.snapshot_json),
        "subsequent_submit": True,
        "payment_received_at": now.isoformat(timespec="seconds"),
        "submission_deadline": deadline.isoformat(),
    }
    connection.execute(insert(fiscal_outbox).values(
        message_uuid=UUID(fiscal_request["message_id"]),
        invoice_uuid=invoice_uuid, status="PENDING",
        request_sha256=payload_sha256(fiscal_request), request_json=fiscal_request,
    ))
    connection.execute(
        update(invoices).where(invoices.c.invoice_uuid == invoice_uuid).values(
            status="PENDING_FISCALIZATION", payment_status="PAID",
            settlement_operation_id=operation_uuid, payment_received_at=now,
            payment_received_method=method,
            fiscal_submission_deadline=deadline,
            cash_session_uuid=(cash_session.session_uuid if cash_session else None),
            row_version=invoices.c.row_version + 1,
            updated_at=now,
        )
    )
    if cash_session is not None:
        _record_central_cash_entry(
            connection, session=cash_session, entry_uuid=uuid4(),
            entry_type="CASH_SALE", amount=Decimal(invoice.total_amount),
            reference_type="INVOICE_LATE_PAYMENT", reference_uuid=invoice_uuid,
            reference_no=invoice.invoice_number,
            description=f"Poznejše gotovinsko plačilo TRR računa {invoice.invoice_number}",
            actor_id=envelope.actor_id, actor_user_uuid=actor_user_uuid,
            workstation_uuid=workstation_uuid, occurred_at=now,
        )
    central_sequence = _publish_change(
        connection, event_type="LateInvoicePaymentFiscalizationQueued",
        aggregate_type="INVOICE", aggregate_uuid=invoice_uuid,
        aggregate_version=2,
        payload={
            "invoice_id": str(invoice_uuid), "invoice_number": invoice.invoice_number,
            "invoice_status": "PENDING_FISCALIZATION", "payment_status": "PAID",
            "payment_received_method": method,
            "payment_received_at": now.isoformat(timespec="seconds"),
            "fiscal_submission_deadline": deadline.isoformat(),
            "cash_session_id": str(cash_session.session_uuid) if cash_session else "",
        },
        occurred_at=now.isoformat(timespec="seconds"),
    )
    return "ACCEPTED", {
        "aggregate_version": 2, "central_sequence": central_sequence,
        "invoice_id": str(invoice_uuid), "invoice_number": invoice.invoice_number,
        "invoice_status": "PENDING_FISCALIZATION", "payment_status": "PAID",
        "payment_received_method": method,
        "fiscal_submission_deadline": deadline.isoformat(),
    }, central_sequence


def _negative_invoice_lines(snapshot: dict) -> list[dict]:
    """Build printable reversal lines without changing original line identity."""
    result: list[dict] = []
    for raw in snapshot.get("lines", ()):  # immutable invoice snapshot
        line = dict(raw)
        for field in ("quantity", "line_total"):
            if field in line:
                line[field] = format(-Decimal(str(line[field])), "f")
        result.append(line)
    return result


def _effective_invoice_payments(invoice) -> list[dict]:
    """Return what was actually paid, including a late TRR settlement."""
    if invoice.payment_status != "PAID":
        return []
    if invoice.payment_received_method:
        return [{
            "method": str(invoice.payment_received_method),
            "amount": format(Decimal(invoice.total_amount), ".2f"),
            "confirmed": True,
        }]
    return [dict(item) for item in (invoice.payments_json or [])]


def _storno_invoice(
    connection: Connection, envelope: SyncOperationEnvelope,
    invoice_uuid: UUID, operation_uuid: UUID, *, operator_tax_number: str,
    actor_user_uuid: UUID | None, workstation_uuid: UUID | None,
) -> tuple[str, dict, int | None]:
    """Create one financial reversal without changing stock or Service state."""
    if envelope.aggregate_type != "INVOICE":
        return _reject("INVALID_AGGREGATE_TYPE", "StornoInvoice requires INVOICE.")
    try:
        payload = validated_storno_invoice_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["invoice_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH", "payload.invoice_id must equal aggregate_id."
        )
    if int(payload["expected_version"]) != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT",
            "payload.expected_version must equal envelope.expected_version.",
        )

    invoice = connection.execute(
        select(invoices).where(invoices.c.invoice_uuid == invoice_uuid).with_for_update()
    ).one_or_none()
    if invoice is None:
        return _reject("INVOICE_NOT_FOUND", "The selected invoice does not exist.")
    if invoice.source_type == "REVERSAL":
        return _reject("REVERSAL_CANNOT_BE_REVERSED", "A storno document cannot be reversed.")
    existing = connection.execute(
        select(invoices).where(invoices.c.reversal_of_invoice_uuid == invoice_uuid)
    ).one_or_none()
    if existing is not None:
        return (
            "REJECTED",
            {
                "error_code": "INVOICE_ALREADY_REVERSED",
                "error_message": "This invoice already has a storno document.",
                "reversal_invoice_id": str(existing.invoice_uuid),
                "reversal_invoice_number": existing.invoice_number,
                "reversal_status": existing.status,
            },
            None,
        )
    if int(invoice.row_version) != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT",
            f"Invoice version changed from {envelope.expected_version} "
            f"to {invoice.row_version}.",
        )
    if invoice.status != "ISSUED":
        return _reject(
            "INVOICE_NOT_FINAL",
            "Only a successfully issued invoice can be reversed.",
        )

    actual_payments = _effective_invoice_payments(invoice)
    refund_payments = [{
        "method": str(payment["method"]),
        "amount": format(-Decimal(str(payment["amount"])), ".2f"),
        "confirmed": True,
    } for payment in actual_payments]
    fiscal_required = any(
        payment["method"] in {"CASH", "CARD"} for payment in actual_payments
    )
    cash_amount = sum(
        (Decimal(str(payment["amount"])) for payment in actual_payments
         if payment["method"] == "CASH"),
        Decimal("0"),
    ).quantize(_CENT, rounding=ROUND_HALF_UP)
    cash_session = _open_cash_session_for_invoice(
        connection, premise_id=invoice.premise_id,
        electronic_device_id=invoice.electronic_device_id,
        payments=[{"method": "CASH", "amount": str(cash_amount)}]
        if cash_amount else [],
    )

    now = datetime.now(ZoneInfo("Europe/Ljubljana"))
    sequence, reversal_number = _next_invoice_number(
        connection, fiscal_year=now.year, premise_id=invoice.premise_id,
        electronic_device_id=invoice.electronic_device_id,
    )
    reversal_uuid = uuid4()
    total = -Decimal(invoice.total_amount)
    original_snapshot = dict(invoice.snapshot_json or {})
    reference_invoice = {
        "ReferenceInvoiceIdentifier": {
            "BusinessPremiseID": invoice.premise_id,
            "ElectronicDeviceID": invoice.electronic_device_id,
            "InvoiceNumber": str(invoice.fiscal_sequence),
        },
        "ReferenceInvoiceIssueDateTime": invoice.issued_at.strftime(
            "%Y-%m-%dT%H:%M:%S"
        ),
    }
    snapshot = {
        "invoice_id": str(reversal_uuid), "invoice_number": reversal_number,
        "source_type": "REVERSAL", "reversal_of_invoice_id": str(invoice_uuid),
        "reversal_of_invoice_number": invoice.invoice_number,
        "reason": payload["reason"],
        "lines": _negative_invoice_lines(original_snapshot),
        "billing_customer": dict(invoice.billing_customer_json or {}),
        "payments": refund_payments, "total_amount": format(total, ".2f"),
        "currency": invoice.currency, "tax_mode": invoice.tax_mode,
        "tax_rate": format(Decimal(invoice.tax_rate), "f"),
        "tax_note": invoice.tax_note, "cashier_id": envelope.actor_id,
        "operator_tax_number": str(operator_tax_number or ""),
        "premise_id": invoice.premise_id,
        "electronic_device_id": invoice.electronic_device_id,
        "cash_session_id": str(cash_session.session_uuid) if cash_session else "",
        "issued_at": now.isoformat(timespec="seconds"),
        "reference_invoice": reference_invoice,
        "stock_effect": "NONE", "service_effect": "NONE",
    }
    reversal_status = "PENDING_FISCALIZATION" if fiscal_required else "ISSUED"
    original_status = "REVERSAL_PENDING" if fiscal_required else "REVERSED"
    connection.execute(insert(invoices).values(
        invoice_uuid=reversal_uuid, row_version=1, draft_uuid=None,
        service_order_uuid=None, source_type="REVERSAL", direct_sale_uuid=None,
        reversal_of_invoice_uuid=invoice_uuid, storno_reason=payload["reason"],
        issue_operation_id=operation_uuid, invoice_number=reversal_number,
        fiscal_year=now.year, fiscal_sequence=sequence,
        premise_id=invoice.premise_id,
        electronic_device_id=invoice.electronic_device_id,
        cash_session_uuid=cash_session.session_uuid if cash_session else None,
        cashier_id=envelope.actor_id,
        operator_tax_number=str(operator_tax_number or "") or None,
        billing_partner_uuid=invoice.billing_partner_uuid,
        billing_customer_json=dict(invoice.billing_customer_json or {}),
        status=reversal_status, payment_status="PAID",
        payments_json=refund_payments, total_amount=total,
        currency=invoice.currency, tax_mode=invoice.tax_mode,
        tax_rate=invoice.tax_rate, tax_note=invoice.tax_note,
        snapshot_json=snapshot, issued_at=now,
        finalized_at=None if fiscal_required else now,
    ))
    connection.execute(
        update(invoices).where(invoices.c.invoice_uuid == invoice_uuid).values(
            status=original_status, row_version=invoices.c.row_version + 1,
            updated_at=now,
        )
    )
    if cash_session is not None and cash_amount:
        _record_central_cash_entry(
            connection, session=cash_session, entry_uuid=uuid4(),
            entry_type="CASH_REFUND", amount=-cash_amount,
            reference_type="INVOICE_REVERSAL", reference_uuid=reversal_uuid,
            reference_no=reversal_number,
            description=f"Vračilo po stornu računa {invoice.invoice_number}",
            actor_id=envelope.actor_id, actor_user_uuid=actor_user_uuid,
            workstation_uuid=workstation_uuid, occurred_at=now,
        )
    if fiscal_required:
        fiscal_request = {
            "message_id": str(uuid4()), "invoice_id": str(reversal_uuid),
            "invoice_number": reversal_number, "invoice_snapshot": snapshot,
        }
        connection.execute(insert(fiscal_outbox).values(
            message_uuid=UUID(fiscal_request["message_id"]),
            invoice_uuid=reversal_uuid, status="PENDING",
            request_sha256=payload_sha256(fiscal_request),
            request_json=fiscal_request,
        ))
    central_sequence = _publish_change(
        connection, event_type=(
            "InvoiceReversalFiscalizationQueued" if fiscal_required
            else "InvoiceReversed"
        ),
        aggregate_type="INVOICE", aggregate_uuid=invoice_uuid,
        aggregate_version=int(invoice.row_version) + 1,
        payload={
            "invoice_id": str(invoice_uuid), "invoice_number": invoice.invoice_number,
            "invoice_status": original_status,
            "reversal_invoice_id": str(reversal_uuid),
            "reversal_invoice_number": reversal_number,
            "reversal_status": reversal_status,
            "stock_effect": "NONE", "service_effect": "NONE",
        },
        occurred_at=now.isoformat(timespec="seconds"),
    )
    return "ACCEPTED", {
        "aggregate_version": int(invoice.row_version) + 1,
        "central_sequence": central_sequence,
        "invoice_id": str(invoice_uuid), "invoice_status": original_status,
        "reversal_invoice_id": str(reversal_uuid),
        "reversal_invoice_number": reversal_number,
        "reversal_status": reversal_status,
        "stock_effect": "NONE", "service_effect": "NONE",
    }, central_sequence


_CENT = Decimal("0.01")
_FOUR = Decimal("0.0001")
_PART_FLOOR = Decimal("1.05")


def _checked_receipt_lines(payload: dict) -> list[dict]:
    """Repeat all financial checks at the trusted central boundary."""
    raw: list[dict] = []
    supplier_sum = Decimal("0")
    total_quantity = Decimal("0")
    line_ids: set[str] = set()
    for index, source in enumerate(payload["lines"], start=1):
        line = dict(source)
        if line["line_id"] in line_ids:
            raise ContractError(f"Receipt line {index} has a duplicate line_id.")
        line_ids.add(line["line_id"])
        quantity = Decimal(line["quantity"])
        net = Decimal(line["net_unit_price"])
        discount = Decimal(line["discount_percent"])
        vat_rate = Decimal(line["vat_rate"])
        base = quantity * net * (Decimal("1") - discount / Decimal("100"))
        vat_amount = (base * vat_rate / Decimal("100")).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
        calculated = (base * (Decimal("1") + vat_rate / Decimal("100"))).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
        supplier_total = Decimal(line["supplier_line_total"])
        if calculated != supplier_total:
            raise ContractError(
                f"Receipt line {index} differs from the supplier amount: "
                f"{calculated:.2f} != {supplier_total:.2f}."
            )
        line.update(
            quantity=quantity, net_unit_price=net,
            discount_percent=discount, vat_rate=vat_rate,
            vat_amount=vat_amount, calculated_total=calculated,
            supplier_line_total=supplier_total,
            confirmed_sale_price=Decimal(line["confirmed_sale_price"]),
        )
        raw.append(line)
        total_quantity += quantity
        supplier_sum += supplier_total

    shipping = Decimal(payload["shipping_total"])
    stated_total = Decimal(payload["document_total"])
    calculated_document_total = (supplier_sum + shipping).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )
    if calculated_document_total != stated_total:
        raise ContractError(
            f"Receipt total differs from the supplier document: "
            f"{calculated_document_total:.2f} != {stated_total:.2f}."
        )
    allocated_so_far = Decimal("0")
    for index, line in enumerate(raw):
        if index == len(raw) - 1:
            allocated = shipping - allocated_so_far
        else:
            allocated = (shipping * line["quantity"] / total_quantity).quantize(
                _FOUR, rounding=ROUND_HALF_UP
            )
            allocated_so_far += allocated
        allocated = allocated.quantize(_FOUR, rounding=ROUND_HALF_UP)
        landed = (
            (line["supplier_line_total"] + allocated) / line["quantity"]
        ).quantize(_FOUR, rounding=ROUND_HALF_UP)
        minimum = (landed * _PART_FLOOR).quantize(_CENT, rounding=ROUND_HALF_UP)
        if line["confirmed_sale_price"] < minimum:
            raise ContractError(
                f"Receipt line {index + 1} sale price must be at least {minimum:.2f}."
            )
        line["allocated_shipping"] = allocated
        line["landed_unit_cost"] = landed
    return raw


def _supplier_snapshot(connection: Connection, payload: dict) -> tuple[UUID, dict]:
    supplier_uuid = UUID(payload["supplier_id"])
    supplier = connection.execute(
        select(partners).where(partners.c.partner_uuid == supplier_uuid).with_for_update()
    ).one_or_none()
    if supplier is None or not supplier.active:
        raise ContractError("The selected supplier does not exist or is inactive.")
    if int(supplier.row_version) != int(payload["supplier_expected_version"]):
        raise ContractError(
            f"Supplier version changed from {payload['supplier_expected_version']} "
            f"to {supplier.row_version}; confirm the supplier again."
        )
    return supplier_uuid, {
        "partner_id": str(supplier.partner_uuid), "partner_version": supplier.row_version,
        "name": supplier.name, "address": supplier.address,
        "post_city": supplier.post_city, "country": supplier.country,
        "tax_number": supplier.tax_number,
        "registration_number": supplier.registration_number,
        "phone": supplier.phone, "email": supplier.email,
    }


def _next_receipt_number(
    connection: Connection, *, year: int, series_code: str,
) -> str:
    number = int(connection.execute(
        insert(receipt_number_sequences)
        .values(calendar_year=year, series_code=series_code, last_number=1)
        .on_conflict_do_update(
            index_elements=[
                receipt_number_sequences.c.calendar_year,
                receipt_number_sequences.c.series_code,
            ],
            set_={
                "last_number": receipt_number_sequences.c.last_number + 1,
                "updated_at": datetime.now().astimezone(),
            },
        )
        .returning(receipt_number_sequences.c.last_number)
    ).scalar_one())
    return f"{series_code}-{year}-{number:04d}"


def _write_receipt_lines_and_pending_lots(
    connection: Connection, *, receipt_uuid: UUID, receipt_number: str,
    received_date: date, lines: list[dict],
) -> None:
    for line_no, line in enumerate(lines, start=1):
        article_uuid = UUID(line["article_id"])
        article = connection.execute(
            select(inventory_items).where(
                inventory_items.c.article_uuid == article_uuid
            ).with_for_update()
        ).one_or_none()
        if article is None or article.blocked:
            raise ContractError(
                f"Receipt line {line_no} article does not exist or is blocked."
            )
        barcode = line["supplier_barcode"]
        identity = connection.execute(
            select(inventory_identities).where(
                inventory_identities.c.supplier_barcode == barcode
            ).with_for_update()
        ).one_or_none()
        if identity is not None and identity.article_uuid != article_uuid:
            raise ContractError(
                f"Barcode {barcode} already belongs to another main article."
            )
        if identity is None:
            identity_uuid = uuid4()
            created_identity = connection.execute(insert(inventory_identities).values(
                identity_uuid=identity_uuid, article_uuid=article_uuid,
                supplier_barcode=barcode, created_from_receipt_uuid=receipt_uuid,
            ).on_conflict_do_nothing(
                index_elements=[inventory_identities.c.supplier_barcode]
            ).returning(inventory_identities.c.identity_uuid)).scalar_one_or_none()
            if created_identity is None:
                identity = connection.execute(select(inventory_identities).where(
                    inventory_identities.c.supplier_barcode == barcode
                ).with_for_update()).one()
                if identity.article_uuid != article_uuid:
                    raise ContractError(
                        f"Barcode {barcode} already belongs to another main article."
                    )
                identity_uuid = identity.identity_uuid
        else:
            identity_uuid = identity.identity_uuid
        line_uuid = UUID(line["line_id"])
        if article.track_mode == "SERIAL" and (
            line["quantity"] != Decimal("1.0000") or not line["lot_serial"]
        ):
            raise ContractError(
                f"Receipt line {line_no} is serial-tracked and requires quantity 1 "
                "and a serial number."
            )
        lot_serial = line["lot_serial"] or f"LOT-{receipt_number}-{line_no:03d}"
        connection.execute(insert(purchase_receipt_lines).values(
            line_uuid=line_uuid, receipt_uuid=receipt_uuid, line_no=line_no,
            article_uuid=article_uuid, quantity=line["quantity"],
            net_unit_price=line["net_unit_price"],
            discount_percent=line["discount_percent"], vat_rate=line["vat_rate"],
            vat_amount=line["vat_amount"], calculated_total=line["calculated_total"],
            supplier_line_total=line["supplier_line_total"],
            allocated_shipping=line["allocated_shipping"],
            landed_unit_cost=line["landed_unit_cost"],
            confirmed_sale_price=line["confirmed_sale_price"],
            supplier_barcode=barcode, lot_serial=lot_serial,
        ))
        connection.execute(insert(inventory_lots).values(
            lot_uuid=uuid4(), receipt_line_uuid=line_uuid,
            identity_uuid=identity_uuid, article_uuid=article_uuid,
            lot_serial=lot_serial,
            serial_unique_key=lot_serial if article.track_mode == "SERIAL" else None,
            received_quantity=line["quantity"],
            available_quantity=0, landed_unit_cost=line["landed_unit_cost"],
            sale_price_snapshot=line["confirmed_sale_price"], status="PENDING",
            received_date=received_date,
        ))


def _write_receipt_attachments(
    connection: Connection, *, receipt_uuid: UUID, receipt_version: int,
    attachments: list[dict], actor_uuid: UUID | None,
) -> None:
    for item in attachments:
        connection.execute(insert(receipt_attachments).values(
            attachment_uuid=UUID(item["attachment_id"]), receipt_uuid=receipt_uuid,
            receipt_version=receipt_version, sha256=item["sha256"],
            size_bytes=item["size_bytes"], media_type=item["media_type"],
            original_name=item["original_name"], actor_uuid=actor_uuid,
        ).on_conflict_do_nothing(index_elements=[
            receipt_attachments.c.receipt_uuid, receipt_attachments.c.sha256,
        ]))


def _release_receipt_stock(
    connection: Connection, *, receipt_uuid: UUID, now: datetime,
    actor_uuid: UUID | None,
) -> dict[str, str]:
    lines = connection.execute(
        select(purchase_receipt_lines).where(
            purchase_receipt_lines.c.receipt_uuid == receipt_uuid
        ).order_by(purchase_receipt_lines.c.line_no)
    ).all()
    prices: dict[UUID, Decimal] = {}
    incoming_costs: dict[UUID, list[Decimal]] = {}
    quantities: dict[UUID, Decimal] = {}
    for line in lines:
        proposed = Decimal(line.confirmed_sale_price)
        previous = prices.setdefault(line.article_uuid, proposed)
        if previous != proposed:
            raise ContractError(
                "All receipt lines for the same main article must confirm one shared sale price."
            )
        incoming_costs.setdefault(line.article_uuid, []).append(Decimal(line.landed_unit_cost))
        quantities[line.article_uuid] = quantities.get(line.article_uuid, Decimal("0")) + Decimal(line.quantity)

    minimums: dict[str, str] = {}
    for article_uuid, proposed_price in prices.items():
        article = connection.execute(
            select(inventory_items).where(
                inventory_items.c.article_uuid == article_uuid
            ).with_for_update()
        ).one()
        existing_costs = [Decimal(value) for value in connection.execute(
            select(inventory_lots.c.landed_unit_cost).where(
                inventory_lots.c.article_uuid == article_uuid,
                inventory_lots.c.status == "AVAILABLE",
                inventory_lots.c.available_quantity > 0,
            ).with_for_update()
        ).scalars().all()]
        highest = max(existing_costs + incoming_costs[article_uuid])
        minimum = (highest * _PART_FLOOR).quantize(_CENT, rounding=ROUND_HALF_UP)
        minimums[str(article_uuid)] = f"{minimum:.2f}"
        if proposed_price < minimum:
            raise ContractError(
                f"Article {article_uuid} sale price must be at least {minimum:.2f} "
                "because of the highest available acquisition cost."
            )
        previous_price = Decimal(article.current_sale_price)
        connection.execute(
            update(inventory_items)
            .where(inventory_items.c.article_uuid == article_uuid)
            .values(
                quantity_on_hand=inventory_items.c.quantity_on_hand + quantities[article_uuid],
                current_sale_price=proposed_price,
                row_version=inventory_items.c.row_version + 1,
                updated_at=now,
            )
        )
        connection.execute(insert(inventory_price_history).values(
            price_history_uuid=uuid4(), article_uuid=article_uuid,
            receipt_uuid=receipt_uuid, previous_price=previous_price,
            confirmed_price=proposed_price, highest_available_cost=highest,
            effective_at=now, actor_uuid=actor_uuid,
        ))
    connection.execute(
        update(inventory_lots)
        .where(
            inventory_lots.c.receipt_line_uuid.in_([line.line_uuid for line in lines]),
            inventory_lots.c.status == "PENDING",
        )
        .values(
            status="AVAILABLE",
            available_quantity=inventory_lots.c.received_quantity,
            released_at=now,
        )
    )
    return minimums


def _inventory_updates_for_articles(
    connection: Connection, article_ids,
) -> list[dict]:
    updates: list[dict] = []
    for article_uuid in article_ids:
        article = connection.execute(select(inventory_items).where(
            inventory_items.c.article_uuid == article_uuid
        )).one()
        identities = connection.execute(select(inventory_identities).where(
            inventory_identities.c.article_uuid == article_uuid
        ).order_by(inventory_identities.c.supplier_barcode)).all()
        identity_updates = [{
            "identity_id": str(identity.identity_uuid),
            "supplier_barcode": identity.supplier_barcode,
            "quantity_available": format(connection.execute(select(
                func.coalesce(func.sum(inventory_lots.c.available_quantity), 0)
            ).where(
                inventory_lots.c.identity_uuid == identity.identity_uuid,
                inventory_lots.c.status == "AVAILABLE",
            )).scalar_one(), "f"),
        } for identity in identities]
        # Imported legacy stock can predate supplier identities.  The server
        # already accepts its main barcode; expose the same identity in the
        # bootstrap so a migrated article does not disappear from Service.
        if not identity_updates:
            identity_updates.append({
                "identity_id": str(article.article_uuid),
                "supplier_barcode": article.barcode,
                "quantity_available": format(article.quantity_on_hand, "f"),
                "legacy_main_identity": True,
            })
        updates.append({
            "article_id": str(article.article_uuid),
            "main_barcode": article.barcode,
            "description": article.description,
            "track_mode": article.track_mode,
            "quantity_available": format(article.quantity_on_hand, "f"),
            "current_sale_price": format(article.current_sale_price, "f"),
            "row_version": int(article.row_version),
            "blocked": bool(article.blocked),
            "blocked_reason": article.blocked_reason or "",
            "identities": identity_updates,
        })
    return updates


def _receipt_inventory_updates(
    connection: Connection, *, receipt_uuid: UUID,
) -> list[dict]:
    article_ids = connection.execute(select(
        purchase_receipt_lines.c.article_uuid
    ).where(
        purchase_receipt_lines.c.receipt_uuid == receipt_uuid
    ).distinct()).scalars().all()
    return _inventory_updates_for_articles(connection, article_ids)


def _assert_receipt_line_identity_unchanged(
    connection: Connection, *, receipt_uuid: UUID, receipt_number: str,
    lines: list[dict],
) -> None:
    """An invoice may price a delivery, but cannot replace received physical goods."""
    existing_rows = connection.execute(select(
        purchase_receipt_lines.c.line_uuid,
        purchase_receipt_lines.c.line_no,
        purchase_receipt_lines.c.article_uuid,
        purchase_receipt_lines.c.quantity,
        purchase_receipt_lines.c.supplier_barcode,
        purchase_receipt_lines.c.lot_serial,
    ).where(
        purchase_receipt_lines.c.receipt_uuid == receipt_uuid
    ).order_by(purchase_receipt_lines.c.line_no)).all()
    existing = {str(row.line_uuid): row for row in existing_rows}
    incoming = {str(line["line_id"]): line for line in lines}
    if (
        set(existing) != set(incoming)
        or [str(row.line_uuid) for row in existing_rows]
        != [str(line["line_id"]) for line in lines]
    ):
        raise ContractError(
            "The supplier invoice must contain the same receipt lines in the same order."
        )
    for line_id, old in existing.items():
        new = incoming[line_id]
        unchanged = (
            str(old.article_uuid) == str(new["article_id"])
            and Decimal(old.quantity) == Decimal(new["quantity"])
            and str(old.supplier_barcode or "") == str(new["supplier_barcode"])
            and (
                (
                    not str(new["lot_serial"])
                    and str(old.lot_serial or "")
                    == f"LOT-{receipt_number}-{int(old.line_no):03d}"
                )
                or str(old.lot_serial or "") == str(new["lot_serial"])
            )
        )
        if not unchanged:
            raise ContractError(
                "The supplier invoice cannot change the article, quantity, "
                f"barcode or lot of received line {line_id}."
            )


def _confirm_purchase_receipt(
    connection: Connection, envelope: SyncOperationEnvelope,
    receipt_uuid: UUID, operation_uuid: UUID,
    *, actor_uuid: UUID | None, workstation_uuid: UUID | None,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "PURCHASE_RECEIPT":
        return _reject("INVALID_AGGREGATE_TYPE", "ConfirmPurchaseReceipt requires PURCHASE_RECEIPT.")
    if envelope.expected_version != 0:
        return _reject("VERSION_CONFLICT", "A new purchase receipt requires expected_version 0.")
    try:
        payload = validated_confirm_receipt_payload(envelope.payload)
        if payload["receipt_id"] != envelope.aggregate_id:
            raise ContractError("payload.receipt_id must equal aggregate_id.")
        lines = _checked_receipt_lines(payload)
        supplier_uuid, supplier = _supplier_snapshot(connection, payload)
        if payload["document_type"] == "CUSTOM":
            custom = connection.execute(select(receipt_document_types).where(
                receipt_document_types.c.type_code == payload["custom_type_code"],
                receipt_document_types.c.is_active.is_(True),
            )).one_or_none()
            if custom is None:
                raise ContractError("The custom receipt document type is not active.")
        duplicate = connection.execute(select(purchase_receipts).where(
            purchase_receipts.c.supplier_uuid == supplier_uuid,
            purchase_receipts.c.document_type == payload["document_type"],
            purchase_receipts.c.external_document_no == payload["external_document_no"],
        )).one_or_none()
        if duplicate is not None:
            return "REJECTED", {
                "error_code": "SUPPLIER_DOCUMENT_ALREADY_EXISTS",
                "error_message": "This supplier document is already recorded.",
                "existing_receipt_id": str(duplicate.receipt_uuid),
                "existing_receipt_number": duplicate.receipt_number,
            }, None
        now = datetime.now(ZoneInfo("Europe/Ljubljana"))
        series = payload["custom_type_code"] if payload["document_type"] == "CUSTOM" else payload["document_type"]
        receipt_number = _next_receipt_number(connection, year=now.year, series_code=series)
        release_now = payload["document_type"] == "DRA"
        created = connection.execute(insert(purchase_receipts).values(
            receipt_uuid=receipt_uuid, row_version=1, receipt_number=receipt_number,
            document_type=payload["document_type"], series_code=series,
            status="RELEASED" if release_now else "PENDING_INVOICE",
            supplier_uuid=supplier_uuid, supplier_version=payload["supplier_expected_version"],
            supplier_snapshot_json=supplier,
            external_document_no=payload["external_document_no"],
            document_date=date.fromisoformat(payload["document_date"]),
            received_date=date.fromisoformat(payload["received_date"]),
            currency="EUR", document_total=payload["document_total"],
            shipping_total=payload["shipping_total"],
            invoice_external_no=payload["external_document_no"] if release_now else None,
            invoice_date=date.fromisoformat(payload["document_date"]) if release_now else None,
            invoice_due_date=date.fromisoformat(payload["invoice_due_date"]) if release_now else None,
            invoice_payment_status=payload["invoice_payment_status"] if release_now else None,
            confirmed_operation_id=operation_uuid,
            release_operation_id=operation_uuid if release_now else None,
            confirmed_at=now, released_at=now if release_now else None,
        ).on_conflict_do_nothing().returning(
            purchase_receipts.c.receipt_uuid
        )).scalar_one_or_none()
        if created is None:
            duplicate = connection.execute(select(purchase_receipts).where(
                purchase_receipts.c.supplier_uuid == supplier_uuid,
                purchase_receipts.c.document_type == payload["document_type"],
                purchase_receipts.c.external_document_no == payload["external_document_no"],
            )).one_or_none()
            return "REJECTED", {
                "error_code": "SUPPLIER_DOCUMENT_ALREADY_EXISTS",
                "error_message": "This supplier document is already recorded.",
                "existing_receipt_id": str(duplicate.receipt_uuid) if duplicate else None,
                "existing_receipt_number": duplicate.receipt_number if duplicate else None,
            }, None
        _write_receipt_lines_and_pending_lots(
            connection, receipt_uuid=receipt_uuid, receipt_number=receipt_number,
            received_date=date.fromisoformat(payload["received_date"]), lines=lines,
        )
        minimums = _release_receipt_stock(
            connection, receipt_uuid=receipt_uuid, now=now, actor_uuid=actor_uuid,
        ) if release_now else {}
        snapshot = dict(payload)
        snapshot["receipt_number"] = receipt_number
        snapshot["status"] = "RELEASED" if release_now else "PENDING_INVOICE"
        snapshot["supplier_snapshot"] = supplier
        snapshot["computed_lines"] = [{
            **line,
            **{key: format(value, "f") for key, value in line.items() if isinstance(value, Decimal)},
        } for line in lines]
        connection.execute(insert(purchase_receipt_revisions).values(
            receipt_uuid=receipt_uuid, row_version=1, operation_id=operation_uuid,
            snapshot_json=snapshot, actor_uuid=actor_uuid, workstation_uuid=workstation_uuid,
        ))
        _write_receipt_attachments(
            connection, receipt_uuid=receipt_uuid, receipt_version=1,
            attachments=list(payload["attachments"]), actor_uuid=actor_uuid,
        )
        event_payload = {
            "receipt_id": str(receipt_uuid), "receipt_number": receipt_number,
            "status": snapshot["status"], "stock_released": release_now,
            "attachment_count": len(payload["attachments"]),
            "minimum_sale_prices": minimums,
            "inventory_updates": (
                _receipt_inventory_updates(connection, receipt_uuid=receipt_uuid)
                if release_now else []
            ),
        }
        sequence = _publish_change(
            connection, event_type="PurchaseReceiptConfirmed",
            aggregate_type="PURCHASE_RECEIPT", aggregate_uuid=receipt_uuid,
            aggregate_version=1, payload=event_payload, occurred_at=envelope.occurred_at,
        )
        return "ACCEPTED", {"aggregate_version": 1, "central_sequence": sequence, **event_payload}, sequence
    except ContractError as exc:
        return _reject("INVALID_RECEIPT", str(exc))


def _attach_purchase_invoice(
    connection: Connection, envelope: SyncOperationEnvelope,
    receipt_uuid: UUID, operation_uuid: UUID,
    *, actor_uuid: UUID | None, workstation_uuid: UUID | None,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "PURCHASE_RECEIPT":
        return _reject("INVALID_AGGREGATE_TYPE", "AttachPurchaseInvoice requires PURCHASE_RECEIPT.")
    try:
        payload = validated_attach_receipt_invoice_payload(envelope.payload)
        if payload["receipt_id"] != envelope.aggregate_id:
            raise ContractError("payload.receipt_id must equal aggregate_id.")
        if payload["expected_version"] != envelope.expected_version:
            raise ContractError("payload.expected_version must equal envelope.expected_version.")
        lines = _checked_receipt_lines(payload)
        receipt = connection.execute(select(purchase_receipts).where(
            purchase_receipts.c.receipt_uuid == receipt_uuid
        ).with_for_update()).one_or_none()
        if receipt is None:
            return _reject("RECEIPT_NOT_FOUND", "The pending purchase receipt does not exist.")
        if receipt.status != "PENDING_INVOICE" or receipt.row_version != envelope.expected_version:
            return _reject("VERSION_CONFLICT", "The purchase receipt is no longer pending at this version.")
        _assert_receipt_line_identity_unchanged(
            connection, receipt_uuid=receipt_uuid,
            receipt_number=receipt.receipt_number, lines=lines,
        )
        duplicate = connection.execute(select(purchase_receipts).where(
            purchase_receipts.c.supplier_uuid == receipt.supplier_uuid,
            purchase_receipts.c.invoice_external_no == payload["invoice_external_no"],
            purchase_receipts.c.receipt_uuid != receipt_uuid,
        )).one_or_none()
        if duplicate is not None:
            return _reject("SUPPLIER_INVOICE_ALREADY_EXISTS", "This supplier invoice is already recorded.")
        now = datetime.now(ZoneInfo("Europe/Ljubljana"))
        invoice_number = _next_receipt_number(connection, year=now.year, series_code="PRA")
        old_lines = connection.execute(select(purchase_receipt_lines.c.line_uuid).where(
            purchase_receipt_lines.c.receipt_uuid == receipt_uuid
        )).scalars().all()
        if old_lines:
            connection.execute(delete(inventory_lots).where(
                inventory_lots.c.receipt_line_uuid.in_(old_lines)
            ))
        connection.execute(delete(purchase_receipt_lines).where(
            purchase_receipt_lines.c.receipt_uuid == receipt_uuid
        ))
        _write_receipt_lines_and_pending_lots(
            connection, receipt_uuid=receipt_uuid, receipt_number=receipt.receipt_number,
            received_date=receipt.received_date, lines=lines,
        )
        minimums = _release_receipt_stock(
            connection, receipt_uuid=receipt_uuid, now=now, actor_uuid=actor_uuid,
        )
        new_version = int(receipt.row_version) + 1
        connection.execute(update(purchase_receipts).where(
            purchase_receipts.c.receipt_uuid == receipt_uuid
        ).values(
            row_version=new_version, status="RELEASED",
            document_total=payload["document_total"], shipping_total=payload["shipping_total"],
            invoice_receipt_number=invoice_number,
            invoice_external_no=payload["invoice_external_no"],
            invoice_date=date.fromisoformat(payload["invoice_date"]),
            invoice_due_date=date.fromisoformat(payload["invoice_due_date"]),
            invoice_payment_status=payload["invoice_payment_status"],
            release_operation_id=operation_uuid, released_at=now, updated_at=now,
        ))
        snapshot = dict(payload)
        snapshot.update(
            receipt_number=receipt.receipt_number, invoice_receipt_number=invoice_number,
            status="RELEASED", supplier_snapshot=dict(receipt.supplier_snapshot_json),
            computed_lines=[{
                **line,
                **{key: format(value, "f") for key, value in line.items() if isinstance(value, Decimal)},
            } for line in lines],
        )
        connection.execute(insert(purchase_receipt_revisions).values(
            receipt_uuid=receipt_uuid, row_version=new_version, operation_id=operation_uuid,
            snapshot_json=snapshot, actor_uuid=actor_uuid, workstation_uuid=workstation_uuid,
        ))
        _write_receipt_attachments(
            connection, receipt_uuid=receipt_uuid, receipt_version=new_version,
            attachments=list(payload["attachments"]), actor_uuid=actor_uuid,
        )
        event_payload = {
            "receipt_id": str(receipt_uuid), "receipt_number": receipt.receipt_number,
            "invoice_receipt_number": invoice_number, "status": "RELEASED",
            "stock_released": True, "attachment_count": len(payload["attachments"]),
            "minimum_sale_prices": minimums,
            "inventory_updates": _receipt_inventory_updates(
                connection, receipt_uuid=receipt_uuid
            ),
        }
        sequence = _publish_change(
            connection, event_type="PurchaseInvoiceAttachedAndStockReleased",
            aggregate_type="PURCHASE_RECEIPT", aggregate_uuid=receipt_uuid,
            aggregate_version=new_version, payload=event_payload,
            occurred_at=envelope.occurred_at,
        )
        return "ACCEPTED", {"aggregate_version": new_version, "central_sequence": sequence, **event_payload}, sequence
    except ContractError as exc:
        return _reject("INVALID_RECEIPT_INVOICE", str(exc))
def _complete_service_technically(
    connection: Connection, envelope: SyncOperationEnvelope,
    aggregate_uuid: UUID, operation_uuid: UUID,
) -> tuple[str, dict, int | None]:
    if envelope.aggregate_type != "SERVICE_ORDER":
        return _reject(
            "INVALID_AGGREGATE_TYPE",
            "CompleteServiceTechnically requires SERVICE_ORDER.",
        )
    try:
        payload = validated_technical_completion_payload(envelope.payload)
    except ContractError as exc:
        return _reject("INVALID_PAYLOAD", str(exc))
    if payload["service_order_id"] != envelope.aggregate_id:
        return _reject(
            "AGGREGATE_ID_MISMATCH",
            "payload.service_order_id must equal aggregate_id.",
        )
    if int(payload["expected_version"]) != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT",
            "payload.expected_version must equal envelope.expected_version.",
        )

    order = connection.execute(
        select(service_orders)
        .where(service_orders.c.sync_uuid == aggregate_uuid)
        .with_for_update()
    ).one_or_none()
    if order is None:
        return _reject("AGGREGATE_NOT_FOUND", "The service order does not exist.")
    if int(order.row_version) != envelope.expected_version:
        return _reject(
            "VERSION_CONFLICT",
            f"Expected version {envelope.expected_version}, current version is "
            f"{order.row_version}.",
        )
    already_completed = connection.execute(
        select(technical_completions.c.operation_id).where(
            technical_completions.c.service_order_uuid == aggregate_uuid
        )
    ).scalar_one_or_none()
    if already_completed is not None:
        return _reject(
            "ALREADY_TECHNICALLY_COMPLETED",
            "The service order already has a technical completion.",
        )

    requested_by_identity: dict[tuple[str, str], Decimal] = {}
    article_by_identity: dict[tuple[str, str], UUID | None] = {}
    stock_by_identity = {}
    lots_by_identity: dict[tuple[str, str], list] = {}
    lot_available: dict[UUID, Decimal] = {}
    for part in payload["parts"]:
        barcode = str(part["barcode"]).strip()
        lot_serial = str(part.get("lot_serial") or "").strip()
        key = (barcode, lot_serial)
        try:
            article_uuid = UUID(str(part["article_id"]))
        except ValueError:
            # The current SQLite Service still carries its local integer row id.
            # It is not a central identity; the scanned barcode is authoritative.
            article_uuid = None
        previous_article = article_by_identity.setdefault(key, article_uuid)
        if (
            previous_article is not None and article_uuid is not None
            and previous_article != article_uuid
        ):
            return _reject(
                "STOCK_IDENTITY_MISMATCH",
                f"Barcode {barcode} is linked to conflicting article identifiers.",
            )
        requested_by_identity[key] = (
            requested_by_identity.get(key, Decimal("0"))
            + Decimal(str(part["quantity"]))
        )
    lots_per_barcode: dict[str, set[str]] = {}
    for barcode, lot_serial in requested_by_identity:
        lots_per_barcode.setdefault(barcode, set()).add(lot_serial)
    ambiguous = [
        barcode for barcode, lots in lots_per_barcode.items()
        if "" in lots and len(lots) > 1
    ]
    if ambiguous:
        return _reject(
            "AMBIGUOUS_LOT_SELECTION",
            "A barcode cannot mix explicit LOT selection with FIFO selection in "
            "the same technical completion: " + ", ".join(sorted(ambiguous)),
        )
    # Lock stock rows in one deterministic order. Two multi-part offline
    # completions must not deadlock merely because their scan order differs.
    for barcode, lot_serial in sorted(requested_by_identity):
        identity = connection.execute(
            select(inventory_identities).where(
                inventory_identities.c.supplier_barcode == barcode
            ).with_for_update()
        ).one_or_none()
        if identity is not None:
            stock = connection.execute(
                select(inventory_items).where(
                    inventory_items.c.article_uuid == identity.article_uuid
                ).with_for_update()
            ).one_or_none()
        else:
            stock = connection.execute(
                select(inventory_items)
                .where(inventory_items.c.barcode == barcode)
                .with_for_update()
            ).one_or_none()
        if stock is None:
            return (
                "REJECTED",
                {
                    "error_code": "STOCK_ITEM_NOT_FOUND",
                    "error_message": (
                        f"Barcode {barcode} does not exist in confirmed central stock."
                    ),
                    "conflicting_barcodes": [barcode],
                },
                None,
            )
        expected_article = article_by_identity[(barcode, lot_serial)]
        if expected_article is not None and stock.article_uuid != expected_article:
            return _reject(
                "STOCK_IDENTITY_MISMATCH",
                f"Barcode {barcode} does not belong to the selected article.",
            )
        key = (barcode, lot_serial)
        stock_by_identity[key] = stock
        if identity is not None:
            lots_query = select(inventory_lots).where(
                inventory_lots.c.identity_uuid == identity.identity_uuid,
                inventory_lots.c.status == "AVAILABLE",
                inventory_lots.c.available_quantity > 0,
            )
            if lot_serial:
                lots_query = lots_query.where(
                    inventory_lots.c.lot_serial == lot_serial
                )
            lots_by_identity[key] = connection.execute(
                lots_query.order_by(
                    inventory_lots.c.received_date,
                    inventory_lots.c.created_at,
                    inventory_lots.c.lot_uuid,
                ).with_for_update()
            ).all()
            lot_available.update({
                lot.lot_uuid: Decimal(lot.available_quantity)
                for lot in lots_by_identity[key]
            })

    requested_by_article: dict[UUID, Decimal] = {}
    for key, requested in requested_by_identity.items():
        article_uuid = stock_by_identity[key].article_uuid
        requested_by_article[article_uuid] = (
            requested_by_article.get(article_uuid, Decimal("0")) + requested
        )
    conflicting = []
    for key, requested in requested_by_identity.items():
        stock = stock_by_identity[key]
        tracked_available = sum(
            (lot_available[lot.lot_uuid] for lot in lots_by_identity.get(key, ())),
            Decimal("0"),
        )
        if (
            stock.blocked
            or Decimal(stock.quantity_on_hand) < requested_by_article[stock.article_uuid]
            or (key in lots_by_identity and tracked_available < requested)
        ):
            conflicting.append(key[0])
    conflicting = sorted(set(conflicting))
    if conflicting:
        for barcode in conflicting:
            article_ids = {
                stock.article_uuid for key, stock in stock_by_identity.items()
                if key[0] == barcode
            }
            connection.execute(
                update(inventory_items)
                .where(inventory_items.c.article_uuid.in_(article_ids))
                .values(
                    blocked=True,
                    blocked_reason=(
                        "OFFLINE_CONSUMPTION_CONFLICT: fizične porabe ni mogoče "
                        "uskladiti brez popravka izvornega dogodka"
                    ),
                    updated_at=datetime.now().astimezone(),
                    row_version=inventory_items.c.row_version + 1,
                )
            )
        return (
            "REJECTED",
            {
                "error_code": "OFFLINE_STOCK_CONFLICT",
                "error_message": (
                    "Physical stock is insufficient or already blocked for barcode(s): "
                    + ", ".join(conflicting)
                    + ". No stock was created and no consumption was partially posted."
                ),
                "conflicting_barcodes": conflicting,
            },
            None,
        )

    for article_uuid, quantity in sorted(requested_by_article.items(), key=lambda item: str(item[0])):
        connection.execute(
            update(inventory_items)
            .where(inventory_items.c.article_uuid == article_uuid)
            .values(
                quantity_on_hand=inventory_items.c.quantity_on_hand - quantity,
                updated_at=datetime.now().astimezone(),
                row_version=inventory_items.c.row_version + 1,
            )
        )
    for part in payload["parts"]:
        key = (
            str(part["barcode"]).strip(),
            str(part.get("lot_serial") or "").strip(),
        )
        stock = stock_by_identity[key]
        line_uuid = UUID(str(part["line_id"]))
        connection.execute(
            insert(service_part_consumptions).values(
                line_uuid=line_uuid,
                service_order_uuid=aggregate_uuid,
                article_uuid=stock.article_uuid,
                barcode=part["barcode"], quantity=part["quantity"],
                unit_price=part["unit_price"], lot_serial=part.get("lot_serial", ""),
                occurred_at=payload["completed_at"],
            )
        )
        remaining = Decimal(str(part["quantity"]))
        for lot in lots_by_identity.get(key, ()):  # legacy migrated stock has no lots
            if remaining <= 0:
                break
            available = lot_available[lot.lot_uuid]
            used = min(available, remaining)
            new_available = available - used
            connection.execute(
                update(inventory_lots)
                .where(inventory_lots.c.lot_uuid == lot.lot_uuid)
                .values(
                    available_quantity=new_available,
                    status="CONSUMED" if new_available == 0 else "AVAILABLE",
                )
            )
            connection.execute(insert(service_stock_allocations).values(
                service_line_uuid=line_uuid, lot_uuid=lot.lot_uuid, quantity=used,
            ))
            remaining -= used
            lot_available[lot.lot_uuid] = new_available

    draft_uuid = uuid4()
    draft_payload = {
        "draft_uuid": str(draft_uuid),
        "service_order_id": envelope.aggregate_id,
        "customer": {
            "name": dict(order.payload_json).get("customer_name", ""),
            "address": dict(order.payload_json).get("customer_address", ""),
            "phone": dict(order.payload_json).get("customer_phone", ""),
            "email": dict(order.payload_json).get("customer_email", ""),
        },
        "service": {
            "device_model": dict(order.payload_json).get("device_model", ""),
            "device_serial": dict(order.payload_json).get("device_serial", ""),
            "customer_request": dict(order.payload_json).get("customer_request", ""),
            "contract_context": dict(order.payload_json).get("contract_context", ""),
        },
        "work_summary": payload["work_summary"],
        "findings": payload["findings"],
        "billing_lines": payload["billing_lines"],
        "parts": payload["parts"],
        "billing_disposition": "INVOICE_REQUIRED",
    }
    connection.execute(
        insert(technical_completions).values(
            service_order_uuid=aggregate_uuid, operation_id=operation_uuid,
            payload_json=payload, completed_at=payload["completed_at"],
        )
    )
    connection.execute(
        insert(invoice_drafts).values(
            draft_uuid=draft_uuid, service_order_uuid=aggregate_uuid,
            status="READY", payload_json=draft_payload,
        )
    )
    new_version = int(order.row_version) + 1
    updated_order_payload = dict(order.payload_json)
    updated_order_payload["technical_status"] = "READY_FOR_INVOICE"
    connection.execute(
        update(service_orders)
        .where(service_orders.c.sync_uuid == aggregate_uuid)
        .values(
            row_version=new_version, payload_json=updated_order_payload,
            updated_at=datetime.now().astimezone(),
        )
    )
    event_payload = {
        "service_order_id": envelope.aggregate_id,
        "draft_uuid": str(draft_uuid),
        "status": "READY_FOR_INVOICE",
        "parts": payload["parts"],
        "inventory_updates": _inventory_updates_for_articles(
            connection, requested_by_article.keys()
        ),
    }
    sequence = _publish_change(
        connection, event_type="ServiceTechnicallyCompleted",
        aggregate_uuid=aggregate_uuid, aggregate_version=new_version,
        payload=event_payload, occurred_at=envelope.occurred_at,
    )
    return (
        "ACCEPTED",
        {
            "aggregate_version": new_version,
            "central_sequence": sequence,
            "draft_uuid": str(draft_uuid),
        },
        sequence,
    )


def process_operation(
    connection: Connection, envelope: SyncOperationEnvelope,
    *,
    transmitted_by_user_uuid: UUID | None = None,
    transmitted_by_workstation_uuid: UUID | None = None,
    operator_tax_number: str = "",
) -> dict:
    """Process once; a retry with the same operation id returns one receipt."""
    operation_uuid = UUID(envelope.operation_id)
    aggregate_uuid = UUID(envelope.aggregate_id)
    inserted = connection.execute(
        insert(accepted_operations)
        .values(
            operation_id=operation_uuid,
            operation_type=envelope.operation_type,
            aggregate_type=envelope.aggregate_type,
            aggregate_uuid=aggregate_uuid,
            expected_version=envelope.expected_version,
            actor_id=envelope.actor_id,
            actor_user_uuid=UUID(envelope.actor_user_id)
            if envelope.actor_user_id else None,
            actor_authorization_version=envelope.actor_authorization_version,
            device_id=envelope.device_id,
            transmitted_by_user_uuid=transmitted_by_user_uuid,
            transmitted_by_workstation_uuid=transmitted_by_workstation_uuid,
            workstation_signature=envelope.workstation_signature,
            device_sequence=envelope.device_sequence,
            contract_version=envelope.contract_version,
            occurred_at=envelope.occurred_at,
            payload_sha256=envelope.payload_hash,
            result_code="PROCESSING",
            response_json={},
        )
        .on_conflict_do_nothing(index_elements=[accepted_operations.c.operation_id])
        .returning(accepted_operations.c.operation_id)
    ).scalar_one_or_none()

    if inserted is None:
        existing = connection.execute(
            select(accepted_operations).where(
                accepted_operations.c.operation_id == operation_uuid
            )
        ).one()
        if not _same_operation(existing, envelope):
            raise ProtocolConflict(
                "operation_id is already bound to a different request"
            )
        return _receipt(existing)

    if envelope.operation_type == "CreateServiceOrder":
        result_code, response, central_sequence = _create_service_order(
            connection, envelope, aggregate_uuid
        )
    elif envelope.operation_type == "CreateInventoryItem":
        result_code, response, central_sequence = _create_inventory_item(
            connection, envelope, aggregate_uuid
        )
    elif envelope.operation_type == "UpdateInventorySalePrice":
        result_code, response, central_sequence = _update_inventory_sale_price(
            connection, envelope, aggregate_uuid, operation_uuid,
            actor_uuid=transmitted_by_user_uuid,
        )
    elif envelope.operation_type in {"CreatePartner", "CreateOfficePartner"}:
        result_code, response, central_sequence = _create_partner(
            connection, envelope, aggregate_uuid
        )
    elif envelope.operation_type == "UpdatePartner":
        result_code, response, central_sequence = _update_partner(
            connection, envelope, aggregate_uuid
        )
    elif envelope.operation_type == "SetPartnerActive":
        result_code, response, central_sequence = _set_partner_active(
            connection, envelope, aggregate_uuid
        )
    elif envelope.operation_type == "CompleteServiceTechnically":
        result_code, response, central_sequence = _complete_service_technically(
            connection, envelope, aggregate_uuid, operation_uuid
        )
    elif envelope.operation_type == "IssueServiceInvoice":
        result_code, response, central_sequence = _issue_service_invoice(
            connection, envelope, aggregate_uuid, operation_uuid,
            operator_tax_number=operator_tax_number,
            actor_user_uuid=transmitted_by_user_uuid,
            workstation_uuid=transmitted_by_workstation_uuid,
        )
    elif envelope.operation_type == "IssueDirectSaleInvoice":
        result_code, response, central_sequence = _issue_direct_sale_invoice(
            connection, envelope, aggregate_uuid, operation_uuid,
            operator_tax_number=operator_tax_number,
            actor_user_uuid=transmitted_by_user_uuid,
            workstation_uuid=transmitted_by_workstation_uuid,
        )
    elif envelope.operation_type == "OpenCashSession":
        result_code, response, central_sequence = _open_cash_session(
            connection, envelope, aggregate_uuid,
            actor_user_uuid=transmitted_by_user_uuid,
            workstation_uuid=transmitted_by_workstation_uuid,
        )
    elif envelope.operation_type == "CloseCashSession":
        result_code, response, central_sequence = _close_cash_session(
            connection, envelope, aggregate_uuid,
            actor_user_uuid=transmitted_by_user_uuid,
            workstation_uuid=transmitted_by_workstation_uuid,
        )
    elif envelope.operation_type == "RecordManualCashEntry":
        result_code, response, central_sequence = _record_manual_cash_entry(
            connection, envelope, aggregate_uuid,
            actor_user_uuid=transmitted_by_user_uuid,
            workstation_uuid=transmitted_by_workstation_uuid,
        )
    elif envelope.operation_type == "SettleUnpaidBankInvoice":
        result_code, response, central_sequence = _settle_unpaid_bank_invoice(
            connection, envelope, aggregate_uuid, operation_uuid,
            operator_tax_number=operator_tax_number,
            actor_user_uuid=transmitted_by_user_uuid,
            workstation_uuid=transmitted_by_workstation_uuid,
        )
    elif envelope.operation_type == "StornoInvoice":
        result_code, response, central_sequence = _storno_invoice(
            connection, envelope, aggregate_uuid, operation_uuid,
            operator_tax_number=operator_tax_number,
            actor_user_uuid=transmitted_by_user_uuid,
            workstation_uuid=transmitted_by_workstation_uuid,
        )
    elif envelope.operation_type == "ConfirmPurchaseReceipt":
        receipt_savepoint = connection.begin_nested()
        try:
            result_code, response, central_sequence = _confirm_purchase_receipt(
                connection, envelope, aggregate_uuid, operation_uuid,
                actor_uuid=transmitted_by_user_uuid,
                workstation_uuid=transmitted_by_workstation_uuid,
            )
            if result_code == "ACCEPTED":
                receipt_savepoint.commit()
            else:
                receipt_savepoint.rollback()
        except Exception:
            receipt_savepoint.rollback()
            raise
    elif envelope.operation_type == "AttachPurchaseInvoice":
        receipt_savepoint = connection.begin_nested()
        try:
            result_code, response, central_sequence = _attach_purchase_invoice(
                connection, envelope, aggregate_uuid, operation_uuid,
                actor_uuid=transmitted_by_user_uuid,
                workstation_uuid=transmitted_by_workstation_uuid,
            )
            if result_code == "ACCEPTED":
                receipt_savepoint.commit()
            else:
                receipt_savepoint.rollback()
        except Exception:
            receipt_savepoint.rollback()
            raise
    else:
        result_code, response, central_sequence = _reject(
            "UNSUPPORTED_OPERATION", "This operation type is not implemented."
        )

    connection.execute(
        update(accepted_operations)
        .where(accepted_operations.c.operation_id == operation_uuid)
        .values(
            result_code=result_code,
            response_json=response,
            central_sequence=central_sequence,
        )
    )
    row = connection.execute(
        select(accepted_operations).where(
            accepted_operations.c.operation_id == operation_uuid
        )
    ).one()
    return _receipt(row)
