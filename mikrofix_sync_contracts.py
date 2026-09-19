"""Versioned, transport-neutral contracts for the MikroFIX sync boundary.

The desktop application does not depend on an HTTP framework here.  These
objects define the stable values that can later be serialized by a Service
client and validated again by the central API.
"""

from __future__ import annotations

import hashlib
import json
import base64
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import StrEnum
from typing import Any, Mapping
from uuid import UUID


SYNC_CONTRACT_VERSION = 1


class ContractError(ValueError):
    """A value cannot be represented by the current sync contract."""


class BillingDisposition(StrEnum):
    INVOICE_REQUIRED = "INVOICE_REQUIRED"


class PaymentMethod(StrEnum):
    CASH = "CASH"
    CARD = "CARD"
    BANK_TRANSFER = "BANK_TRANSFER"


class ServiceInvoiceMode(StrEnum):
    STANDARD = "STANDARD"
    ZERO_BALANCE = "ZERO_BALANCE"


class PartnerVerificationSource(StrEnum):
    MANUAL_REVIEW = "MANUAL_REVIEW"
    AJPES_REVIEW = "AJPES_REVIEW"


class ReceiptDocumentType(StrEnum):
    DELIVERY_NOTE = "DOB"
    COMBINED_DELIVERY_INVOICE = "DRA"
    CUSTOM_RECEIPT = "CUSTOM"


def normalized_uuid(value: object, *, field: str) -> str:
    """Return a canonical UUID string or raise a domain-focused error."""
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ContractError(f"{field} must be a valid UUID.") from exc


def normalized_datetime(value: object, *, field: str) -> str:
    """Require an ISO timestamp with a UTC offset and return its ISO form."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field} must be an ISO timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field} must include a UTC offset.")
    return parsed.isoformat(timespec="seconds")


def decimal_text(value: object, *, field: str, non_negative: bool = True) -> str:
    """Return a deterministic decimal representation without float rounding."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ContractError(f"{field} must be a decimal value.") from exc
    if not parsed.is_finite() or (non_negative and parsed < 0):
        raise ContractError(f"{field} must be a finite non-negative value.")
    rendered = format(parsed.normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def scaled_decimal_text(value: object, *, field: str, scale: str) -> str:
    rendered = decimal_text(value, field=field)
    return format(Decimal(rendered).quantize(Decimal(scale), rounding=ROUND_HALF_UP), "f")


def normalized_date(value: object, *, field: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field} must be an ISO date.") from exc


def canonical_json(value: Mapping[str, Any]) -> str:
    """Serialize a payload identically for hashing and idempotency checks."""
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def payload_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validated_create_service_order_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate central invariants for a new service order snapshot.

    Extra fields are retained for forwards-compatible snapshots, while fields
    that define the current business boundary are normalized here and checked
    again by the server.
    """
    result = dict(value)
    result["sync_uuid"] = normalized_uuid(
        result.get("sync_uuid"), field="payload.sync_uuid"
    )
    for field_name in (
        "customer_name",
        "device_model",
        "device_serial",
        "customer_request",
    ):
        cleaned = str(result.get(field_name) or "").strip()
        if not cleaned:
            raise ContractError(f"payload.{field_name} is required.")
        result[field_name] = cleaned

    phone = str(result.get("customer_phone") or "").strip()
    email = str(result.get("customer_email") or "").strip()
    if not phone and not email:
        raise ContractError("payload requires customer_phone or customer_email.")
    result["customer_phone"] = phone
    result["customer_email"] = email

    identity_method = str(result.get("identity_method") or "").strip().upper()
    if identity_method not in {"MICRODETECT", "MANUAL_OTHER"}:
        raise ContractError("payload.identity_method is not supported.")
    if identity_method == "MICRODETECT" and result.get("microdetect_confirmed") is not True:
        raise ContractError("payload.microdetect_confirmed must be true.")
    if identity_method == "MANUAL_OTHER" and result.get("manual_identity_confirmed") is not True:
        raise ContractError("payload.manual_identity_confirmed must be true.")
    result["identity_method"] = identity_method

    context = str(result.get("contract_context") or "").strip().upper()
    if context not in {"MFD", "MFT_TEREN", "NA_DALJAVO"}:
        raise ContractError("payload.contract_context is not supported.")
    result["contract_context"] = context

    delivery = str(result.get("delivery_channel") or "").strip().upper()
    if delivery not in {"EMAIL_PDF", "SMS_PDF", "PAPER"}:
        raise ContractError("payload.delivery_channel is not supported.")
    if delivery == "EMAIL_PDF" and not email:
        raise ContractError("payload.customer_email is required for EMAIL_PDF.")
    if delivery == "SMS_PDF" and not phone:
        raise ContractError("payload.customer_phone is required for SMS_PDF.")
    result["delivery_channel"] = delivery
    result["quote_eur"] = decimal_text(
        result.get("quote_eur", "0"), field="payload.quote_eur"
    )
    result["logistics_eur"] = decimal_text(
        result.get("logistics_eur", "0"), field="payload.logistics_eur"
    )
    return result


def validated_create_partner_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a reviewed new partner without allowing update semantics."""
    raw = dict(value)
    allowed = {
        "partner_id", "verification_source", "review_confirmed", "name",
        "address", "post_city", "country", "tax_number",
        "registration_number", "phone", "email", "source_reference",
    }
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "partner creation contains unsupported fields: " + ", ".join(unexpected)
        )
    result = {
        key: str(raw.get(key) or "").strip()
        for key in allowed
        if key != "review_confirmed"
    }
    result["partner_id"] = normalized_uuid(
        raw.get("partner_id"), field="partner_id"
    )
    if raw.get("review_confirmed") is not True:
        raise ContractError("partner creation requires explicit review confirmation.")
    result["review_confirmed"] = True
    try:
        source = PartnerVerificationSource(result["verification_source"].upper())
    except ValueError as exc:
        raise ContractError("verification_source is not supported.") from exc
    result["verification_source"] = source.value
    if not result["name"]:
        raise ContractError("partner.name is required.")
    if len(result["name"]) > 300:
        raise ContractError("partner.name is too long.")
    supplied_tax = result["tax_number"]
    tax = supplied_tax.upper().removeprefix("SI")
    tax = "".join(character for character in tax if character.isdigit())
    if supplied_tax and len(tax) != 8:
        raise ContractError("partner.tax_number must contain 8 digits.")
    result["tax_number"] = tax
    if source is PartnerVerificationSource.AJPES_REVIEW:
        if not tax or not result["registration_number"]:
            raise ContractError(
                "AJPES_REVIEW requires tax_number and registration_number."
            )
    if result["email"] and "@" not in result["email"]:
        raise ContractError("partner.email is invalid.")
    maximum_lengths = {
        "address": 300, "post_city": 200, "country": 100,
        "registration_number": 30, "phone": 100, "email": 300,
        "source_reference": 300,
    }
    for field_name, maximum in maximum_lengths.items():
        if len(result[field_name]) > maximum:
            raise ContractError(
                f"partner.{field_name} must be at most {maximum} characters."
            )
    return result


def validated_update_partner_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a reviewed Office edit without allowing identity replacement."""
    raw = dict(value)
    allowed = {
        "partner_id", "expected_version", "verification_source",
        "review_confirmed", "name", "address", "post_city", "country",
        "tax_number", "registration_number", "phone", "email",
        "source_reference",
    }
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "partner update contains unsupported fields: " + ", ".join(unexpected)
        )
    try:
        expected_version = int(raw.get("expected_version"))
    except (TypeError, ValueError) as exc:
        raise ContractError("partner.expected_version must be an integer.") from exc
    if expected_version < 1:
        raise ContractError("partner.expected_version must be at least 1.")
    normalized = validated_create_partner_payload({
        key: value for key, value in raw.items() if key != "expected_version"
    })
    normalized["expected_version"] = expected_version
    return normalized


def validated_set_partner_active_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(value)
    allowed = {"partner_id", "expected_version", "active", "reason"}
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "partner status change contains unsupported fields: "
            + ", ".join(unexpected)
        )
    partner_id = normalized_uuid(raw.get("partner_id"), field="partner_id")
    try:
        expected_version = int(raw.get("expected_version"))
    except (TypeError, ValueError) as exc:
        raise ContractError("partner.expected_version must be an integer.") from exc
    if expected_version < 1:
        raise ContractError("partner.expected_version must be at least 1.")
    if not isinstance(raw.get("active"), bool):
        raise ContractError("partner.active must be true or false.")
    reason = str(raw.get("reason") or "").strip()
    if len(reason) < 3 or len(reason) > 500:
        raise ContractError("partner status change requires a reason from 3 to 500 characters.")
    return {
        "partner_id": partner_id,
        "expected_version": expected_version,
        "active": raw["active"],
        "reason": reason,
    }


def validated_create_inventory_item_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a new zero-stock article before its first documented receipt."""
    raw = dict(value)
    allowed = {"article_id", "main_barcode", "description", "track_mode"}
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "inventory item creation contains unsupported fields: "
            + ", ".join(unexpected)
        )
    article_id = normalized_uuid(raw.get("article_id"), field="article_id")
    barcode = str(raw.get("main_barcode") or "").strip()
    description = str(raw.get("description") or "").strip()
    track_mode = str(raw.get("track_mode") or "").strip().upper()
    if not barcode or len(barcode) > 200:
        raise ContractError(
            "inventory_item.main_barcode is required and must be at most 200 characters."
        )
    if not description or len(description) > 500:
        raise ContractError(
            "inventory_item.description is required and must be at most 500 characters."
        )
    if track_mode not in {"QUANTITY", "SERIAL"}:
        raise ContractError("inventory_item.track_mode must be QUANTITY or SERIAL.")
    return {
        "article_id": article_id,
        "main_barcode": barcode,
        "description": description,
        "track_mode": track_mode,
    }


def validated_update_inventory_price_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate an explicit versioned Office price decision."""
    raw = dict(value)
    allowed = {"article_id", "expected_version", "new_sale_price", "reason"}
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "inventory price change contains unsupported fields: "
            + ", ".join(unexpected)
        )
    article_id = normalized_uuid(raw.get("article_id"), field="article_id")
    try:
        expected_version = int(raw.get("expected_version"))
    except (TypeError, ValueError) as exc:
        raise ContractError("inventory price expected_version must be an integer.") from exc
    if expected_version < 1:
        raise ContractError("inventory price expected_version must be at least 1.")
    price = scaled_decimal_text(
        raw.get("new_sale_price"), field="new_sale_price", scale="0.01"
    )
    if Decimal(price) <= 0:
        raise ContractError("new_sale_price must be greater than zero.")
    reason = str(raw.get("reason") or "").strip()
    if len(reason) < 3 or len(reason) > 500:
        raise ContractError("inventory price change requires a reason from 3 to 500 characters.")
    return {
        "article_id": article_id, "expected_version": expected_version,
        "new_sale_price": price, "reason": reason,
    }


@dataclass(frozen=True, slots=True)
class ReceiptAttachmentContract:
    attachment_id: str
    sha256: str
    size_bytes: int
    media_type: str
    original_name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "attachment_id",
            normalized_uuid(self.attachment_id, field="attachment.attachment_id"),
        )
        digest = str(self.sha256 or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ContractError("attachment.sha256 must be a 64-character hexadecimal digest.")
        object.__setattr__(self, "sha256", digest)
        if int(self.size_bytes) < 1 or int(self.size_bytes) > 25 * 1024 * 1024:
            raise ContractError("attachment.size_bytes must be between 1 byte and 25 MB.")
        object.__setattr__(self, "size_bytes", int(self.size_bytes))
        media_type = str(self.media_type or "").strip().lower()
        if media_type not in {"application/pdf", "image/jpeg", "image/png", "image/heic"}:
            raise ContractError("attachment.media_type is not supported.")
        object.__setattr__(self, "media_type", media_type)
        name = str(self.original_name or "").strip()
        if not name or len(name) > 300:
            raise ContractError("attachment.original_name is required and must be at most 300 characters.")
        object.__setattr__(self, "original_name", name)


@dataclass(frozen=True, slots=True)
class ReceiptLineContract:
    line_id: str
    article_id: str
    quantity: str
    net_unit_price: str
    discount_percent: str
    vat_rate: str
    supplier_line_total: str
    confirmed_sale_price: str
    supplier_barcode: str
    lot_serial: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "line_id", normalized_uuid(self.line_id, field="line.line_id"))
        object.__setattr__(
            self, "article_id", normalized_uuid(self.article_id, field="line.article_id")
        )
        for field_name, scale in (
            ("quantity", "0.0001"), ("net_unit_price", "0.0001"),
            ("discount_percent", "0.0001"), ("vat_rate", "0.0001"),
            ("supplier_line_total", "0.01"), ("confirmed_sale_price", "0.01"),
        ):
            object.__setattr__(
                self, field_name,
                scaled_decimal_text(getattr(self, field_name), field=f"line.{field_name}", scale=scale),
            )
        if Decimal(self.quantity) <= 0:
            raise ContractError("line.quantity must be greater than zero.")
        if Decimal(self.discount_percent) > 100 or Decimal(self.vat_rate) > 100:
            raise ContractError("line percentages must be between 0 and 100.")
        barcode = str(self.supplier_barcode or "").strip()
        if not barcode or len(barcode) > 200:
            raise ContractError("line.supplier_barcode is required and must be at most 200 characters.")
        object.__setattr__(self, "supplier_barcode", barcode)
        lot = str(self.lot_serial or "").strip()
        if len(lot) > 300:
            raise ContractError("line.lot_serial must be at most 300 characters.")
        object.__setattr__(self, "lot_serial", lot)


@dataclass(frozen=True, slots=True)
class ConfirmReceiptContract:
    receipt_id: str
    supplier_id: str
    supplier_expected_version: int
    document_type: str
    custom_type_code: str
    external_document_no: str
    document_date: str
    received_date: str
    document_total: str
    shipping_total: str
    document_review_confirmed: bool
    lines: tuple[ReceiptLineContract, ...]
    attachments: tuple[ReceiptAttachmentContract, ...] = ()
    invoice_due_date: str = ""
    invoice_payment_status: str = ""
    currency: str = "EUR"

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", normalized_uuid(self.receipt_id, field="receipt_id"))
        object.__setattr__(self, "supplier_id", normalized_uuid(self.supplier_id, field="supplier_id"))
        if int(self.supplier_expected_version) < 1:
            raise ContractError("supplier_expected_version must be at least 1.")
        object.__setattr__(self, "supplier_expected_version", int(self.supplier_expected_version))
        try:
            kind = ReceiptDocumentType(str(self.document_type or "").strip().upper())
        except ValueError as exc:
            raise ContractError("document_type is not supported.") from exc
        object.__setattr__(self, "document_type", kind.value)
        custom = str(self.custom_type_code or "").strip().upper()
        if kind is ReceiptDocumentType.CUSTOM_RECEIPT:
            if not (2 <= len(custom) <= 5 and custom.isalpha() and custom.isascii()):
                raise ContractError("custom_type_code must contain 2 to 5 ASCII letters.")
            if custom in {"DOB", "PRA", "DRA", "IRA", "MF"}:
                raise ContractError("custom_type_code is reserved.")
        elif custom:
            raise ContractError("custom_type_code is only valid for CUSTOM documents.")
        object.__setattr__(self, "custom_type_code", custom)
        number = str(self.external_document_no or "").strip()
        if not number or len(number) > 200:
            raise ContractError("external_document_no is required and must be at most 200 characters.")
        object.__setattr__(self, "external_document_no", number)
        object.__setattr__(self, "document_date", normalized_date(self.document_date, field="document_date"))
        object.__setattr__(self, "received_date", normalized_date(self.received_date, field="received_date"))
        object.__setattr__(
            self, "document_total", scaled_decimal_text(self.document_total, field="document_total", scale="0.01")
        )
        object.__setattr__(
            self, "shipping_total", scaled_decimal_text(self.shipping_total, field="shipping_total", scale="0.0001")
        )
        if str(self.currency or "").strip().upper() != "EUR":
            raise ContractError("currency must be EUR in the current contract.")
        object.__setattr__(self, "currency", "EUR")
        if self.document_review_confirmed is not True:
            raise ContractError("document_review_confirmed must be true.")
        if not self.lines:
            raise ContractError("receipt requires at least one line.")
        due = str(self.invoice_due_date or "").strip()
        payment = str(self.invoice_payment_status or "").strip().upper()
        if kind is ReceiptDocumentType.COMBINED_DELIVERY_INVOICE:
            if not due or payment not in {"NEPLACAN", "PLACAN"}:
                raise ContractError("DRA requires invoice_due_date and invoice_payment_status.")
            due = normalized_date(due, field="invoice_due_date")
        elif due or payment:
            raise ContractError("invoice fields are only valid for DRA.")
        object.__setattr__(self, "invoice_due_date", due)
        object.__setattr__(self, "invoice_payment_status", payment)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_confirm_receipt_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(value)
    try:
        raw["lines"] = tuple(
            item if isinstance(item, ReceiptLineContract) else ReceiptLineContract(**dict(item))
            for item in raw.get("lines", ())
        )
        raw["attachments"] = tuple(
            item if isinstance(item, ReceiptAttachmentContract)
            else ReceiptAttachmentContract(**dict(item))
            for item in raw.get("attachments", ())
        )
        return ConfirmReceiptContract(**raw).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("receipt confirmation payload is invalid.") from exc


@dataclass(frozen=True, slots=True)
class AttachReceiptInvoiceContract:
    receipt_id: str
    expected_version: int
    invoice_external_no: str
    invoice_date: str
    invoice_due_date: str
    invoice_payment_status: str
    document_total: str
    shipping_total: str
    document_review_confirmed: bool
    lines: tuple[ReceiptLineContract, ...]
    attachments: tuple[ReceiptAttachmentContract, ...] = ()
    currency: str = "EUR"

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", normalized_uuid(self.receipt_id, field="receipt_id"))
        if int(self.expected_version) < 1:
            raise ContractError("expected_version must be at least 1.")
        object.__setattr__(self, "expected_version", int(self.expected_version))
        number = str(self.invoice_external_no or "").strip()
        if not number or len(number) > 200:
            raise ContractError("invoice_external_no is required and must be at most 200 characters.")
        object.__setattr__(self, "invoice_external_no", number)
        object.__setattr__(self, "invoice_date", normalized_date(self.invoice_date, field="invoice_date"))
        object.__setattr__(self, "invoice_due_date", normalized_date(self.invoice_due_date, field="invoice_due_date"))
        payment = str(self.invoice_payment_status or "").strip().upper()
        if payment not in {"NEPLACAN", "PLACAN"}:
            raise ContractError("invoice_payment_status must be NEPLACAN or PLACAN.")
        object.__setattr__(self, "invoice_payment_status", payment)
        object.__setattr__(
            self, "document_total", scaled_decimal_text(self.document_total, field="document_total", scale="0.01")
        )
        object.__setattr__(
            self, "shipping_total", scaled_decimal_text(self.shipping_total, field="shipping_total", scale="0.0001")
        )
        if str(self.currency or "").strip().upper() != "EUR":
            raise ContractError("currency must be EUR in the current contract.")
        object.__setattr__(self, "currency", "EUR")
        if self.document_review_confirmed is not True:
            raise ContractError("document_review_confirmed must be true.")
        if not self.lines:
            raise ContractError("receipt invoice requires at least one line.")

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_attach_receipt_invoice_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(value)
    try:
        raw["lines"] = tuple(
            item if isinstance(item, ReceiptLineContract) else ReceiptLineContract(**dict(item))
            for item in raw.get("lines", ())
        )
        raw["attachments"] = tuple(
            item if isinstance(item, ReceiptAttachmentContract)
            else ReceiptAttachmentContract(**dict(item))
            for item in raw.get("attachments", ())
        )
        return AttachReceiptInvoiceContract(**raw).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("receipt invoice payload is invalid.") from exc


@dataclass(frozen=True, slots=True)
class WorkIntervalContract:
    interval_id: str
    technician_id: str
    started_at: str
    ended_at: str
    elapsed_seconds: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "interval_id", normalized_uuid(self.interval_id, field="interval_id")
        )
        if not str(self.technician_id).strip():
            raise ContractError("technician_id is required.")
        object.__setattr__(
            self, "started_at", normalized_datetime(self.started_at, field="started_at")
        )
        object.__setattr__(
            self, "ended_at", normalized_datetime(self.ended_at, field="ended_at")
        )
        if int(self.elapsed_seconds) < 0:
            raise ContractError("elapsed_seconds must not be negative.")
        object.__setattr__(self, "elapsed_seconds", int(self.elapsed_seconds))


@dataclass(frozen=True, slots=True)
class ServicePartContract:
    line_id: str
    article_id: str
    description: str
    quantity: str
    unit_price: str
    barcode: str = ""
    lot_serial: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "line_id", normalized_uuid(self.line_id, field="line_id"))
        if not str(self.article_id).strip():
            raise ContractError("article_id is required.")
        if not str(self.description).strip():
            raise ContractError("description is required.")
        if not str(self.barcode).strip():
            raise ContractError("barcode is required for a physical service part.")
        quantity = decimal_text(self.quantity, field="quantity")
        if Decimal(quantity) <= 0:
            raise ContractError("quantity must be greater than zero.")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(
            self, "unit_price", decimal_text(self.unit_price, field="unit_price")
        )


@dataclass(frozen=True, slots=True)
class ServiceTestContract:
    stage: str
    name: str
    status: str

    def __post_init__(self) -> None:
        if str(self.stage) not in {"PRED", "PO"}:
            raise ContractError("test.stage is not supported.")
        if not str(self.name).strip():
            raise ContractError("test.name is required.")
        if str(self.status) not in {"OK", "NE DELA", "NI TESTIRANO", "N/A"}:
            raise ContractError("test.status is not supported.")


@dataclass(frozen=True, slots=True)
class BillingLineContract:
    line_id: str
    line_kind: str
    description: str
    quantity: str
    unit_price: str
    billable_minutes: int = 0
    tariff_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "line_id", normalized_uuid(self.line_id, field="line_id"))
        if not str(self.line_kind).strip():
            raise ContractError("billing_line.line_kind is required.")
        if not str(self.description).strip():
            raise ContractError("billing_line.description is required.")
        quantity = decimal_text(self.quantity, field="billing_line.quantity")
        if Decimal(quantity) <= 0:
            raise ContractError("billing_line.quantity must be greater than zero.")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(
            self, "unit_price",
            decimal_text(self.unit_price, field="billing_line.unit_price"),
        )
        if int(self.billable_minutes) < 0:
            raise ContractError("billing_line.billable_minutes must not be negative.")
        object.__setattr__(self, "billable_minutes", int(self.billable_minutes))


@dataclass(frozen=True, slots=True)
class AttachmentContract:
    attachment_id: str
    sha256: str
    size_bytes: int
    media_type: str
    stage: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attachment_id",
            normalized_uuid(self.attachment_id, field="attachment_id"),
        )
        digest = str(self.sha256).strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ContractError("sha256 must be a 64-character hexadecimal digest.")
        object.__setattr__(self, "sha256", digest)
        if int(self.size_bytes) <= 0:
            raise ContractError("size_bytes must be greater than zero.")
        object.__setattr__(self, "size_bytes", int(self.size_bytes))
        if not str(self.media_type).strip():
            raise ContractError("media_type is required.")


@dataclass(frozen=True, slots=True)
class TechnicalCompletionContract:
    service_order_id: str
    expected_version: int
    completed_at: str
    billing_disposition: str
    work_summary: str
    findings: str
    billing_lines: tuple[BillingLineContract, ...] = ()
    intervals: tuple[WorkIntervalContract, ...] = ()
    parts: tuple[ServicePartContract, ...] = ()
    tests: tuple[ServiceTestContract, ...] = ()
    attachments: tuple[AttachmentContract, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "service_order_id",
            normalized_uuid(self.service_order_id, field="service_order_id"),
        )
        if int(self.expected_version) < 1:
            raise ContractError("expected_version must be at least 1.")
        object.__setattr__(self, "expected_version", int(self.expected_version))
        object.__setattr__(
            self,
            "completed_at",
            normalized_datetime(self.completed_at, field="completed_at"),
        )
        try:
            disposition = BillingDisposition(str(self.billing_disposition))
        except ValueError as exc:
            raise ContractError("billing_disposition is not supported.") from exc
        object.__setattr__(self, "billing_disposition", disposition.value)
        if not str(self.work_summary).strip():
            raise ContractError("work_summary is required.")
        if not self.billing_lines:
            raise ContractError("billing_lines requires at least one work line.")
        invoice_line_ids = [
            line.line_id for line in (*self.billing_lines, *self.parts)
        ]
        if len(invoice_line_ids) != len(set(invoice_line_ids)):
            raise ContractError(
                "billing_lines and parts must have globally unique line_id values."
            )

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_technical_completion_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and normalize nested technical-completion wire data."""
    raw = dict(value)
    try:
        raw["intervals"] = tuple(
            item if isinstance(item, WorkIntervalContract)
            else WorkIntervalContract(**dict(item))
            for item in raw.get("intervals", ())
        )
        raw["billing_lines"] = tuple(
            item if isinstance(item, BillingLineContract)
            else BillingLineContract(**dict(item))
            for item in raw.get("billing_lines", ())
        )
        raw["parts"] = tuple(
            item if isinstance(item, ServicePartContract)
            else ServicePartContract(**dict(item))
            for item in raw.get("parts", ())
        )
        raw["tests"] = tuple(
            item if isinstance(item, ServiceTestContract)
            else ServiceTestContract(**dict(item))
            for item in raw.get("tests", ())
        )
        raw["attachments"] = tuple(
            item if isinstance(item, AttachmentContract)
            else AttachmentContract(**dict(item))
            for item in raw.get("attachments", ())
        )
        completion = TechnicalCompletionContract(**raw)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("technical completion payload is invalid.") from exc
    return completion.payload()


@dataclass(frozen=True, slots=True)
class PaymentContract:
    method: str
    amount: str
    confirmed: bool

    def __post_init__(self) -> None:
        try:
            method = PaymentMethod(str(self.method).strip().upper())
        except ValueError as exc:
            raise ContractError("payment.method is not supported.") from exc
        object.__setattr__(self, "method", method.value)
        amount = decimal_text(self.amount, field="payment.amount")
        if Decimal(amount) <= 0:
            raise ContractError("payment.amount must be greater than zero.")
        object.__setattr__(self, "amount", amount)
        if not isinstance(self.confirmed, bool):
            raise ContractError("payment.confirmed must be true or false.")
        if method in {PaymentMethod.CASH, PaymentMethod.CARD} and not self.confirmed:
            raise ContractError(f"{method.value} payment must be confirmed.")


@dataclass(frozen=True, slots=True)
class BillingCustomerSelectionContract:
    source: str
    confirmed: bool
    partner_id: str = ""
    expected_version: int = 0

    def __post_init__(self) -> None:
        source = str(self.source or "").strip().upper()
        if source not in {"SERVICE_CUSTOMER", "CENTRAL_PARTNER", "WALK_IN"}:
            raise ContractError("billing_customer.source is not supported.")
        object.__setattr__(self, "source", source)
        if self.confirmed is not True:
            raise ContractError("billing customer must be explicitly confirmed.")
        if source in {"SERVICE_CUSTOMER", "WALK_IN"}:
            if str(self.partner_id or "").strip() or int(self.expected_version) != 0:
                raise ContractError(
                    f"{source} must not identify a central partner."
                )
            object.__setattr__(self, "partner_id", "")
            object.__setattr__(self, "expected_version", 0)
        else:
            object.__setattr__(
                self, "partner_id",
                normalized_uuid(self.partner_id, field="billing_customer.partner_id"),
            )
            if int(self.expected_version) < 1:
                raise ContractError(
                    "CENTRAL_PARTNER requires expected_version of at least 1."
                )
            object.__setattr__(self, "expected_version", int(self.expected_version))


@dataclass(frozen=True, slots=True)
class IssueServiceInvoiceContract:
    draft_id: str
    expected_version: int
    premise_id: str
    electronic_device_id: str
    billing_customer: BillingCustomerSelectionContract
    payments: tuple[PaymentContract, ...]
    settlement_mode: str = ServiceInvoiceMode.STANDARD.value

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "draft_id", normalized_uuid(self.draft_id, field="draft_id")
        )
        if int(self.expected_version) != 1:
            raise ContractError("a READY invoice draft requires expected_version 1.")
        object.__setattr__(self, "expected_version", 1)
        if self.billing_customer.source == "WALK_IN":
            raise ContractError(
                "a Service invoice must keep its Service customer or select a partner."
            )
        for field_name in ("premise_id", "electronic_device_id"):
            cleaned = str(getattr(self, field_name) or "").strip().upper()
            if not cleaned or len(cleaned) > 40:
                raise ContractError(f"{field_name} is required and must be at most 40 characters.")
            if not all(character.isalnum() or character in {"-", "_"} for character in cleaned):
                raise ContractError(f"{field_name} contains unsupported characters.")
            object.__setattr__(self, field_name, cleaned)
        try:
            settlement_mode = ServiceInvoiceMode(
                str(self.settlement_mode or "").strip().upper()
            )
        except ValueError as exc:
            raise ContractError("settlement_mode is not supported.") from exc
        object.__setattr__(self, "settlement_mode", settlement_mode.value)
        if settlement_mode is ServiceInvoiceMode.ZERO_BALANCE:
            if self.payments:
                raise ContractError("ZERO_BALANCE must not contain payments.")
        elif not self.payments:
            raise ContractError("STANDARD requires at least one payment.")
        methods = [payment.method for payment in self.payments]
        if len(methods) != len(set(methods)):
            raise ContractError("each payment method may occur only once.")
        if len(self.payments) > 1:
            unconfirmed_bank = any(
                payment.method == PaymentMethod.BANK_TRANSFER.value
                and not payment.confirmed
                for payment in self.payments
            )
            if unconfirmed_bank:
                raise ContractError(
                    "a BANK_TRANSFER share of split payment must be confirmed."
                )

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_issue_service_invoice_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate only issuance choices; invoice content cannot cross this boundary."""
    raw = dict(value)
    allowed = {
        "draft_id", "expected_version", "premise_id",
        "electronic_device_id", "billing_customer", "payments", "settlement_mode",
    }
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "invoice issuance contains unsupported fields: " + ", ".join(unexpected)
        )
    try:
        customer = raw.get("billing_customer")
        raw["billing_customer"] = (
            customer if isinstance(customer, BillingCustomerSelectionContract)
            else BillingCustomerSelectionContract(**dict(customer or {}))
        )
        raw["payments"] = tuple(
            item if isinstance(item, PaymentContract)
            else PaymentContract(**dict(item))
            for item in raw.get("payments", ())
        )
        return IssueServiceInvoiceContract(**raw).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("invoice issuance payload is invalid.") from exc


@dataclass(frozen=True, slots=True)
class DirectSaleLineContract:
    line_id: str
    barcode: str
    quantity: str
    line_kind: str = "ARTICLE"
    requested_discount_percent: str = "0"
    lot_serial: str = ""
    service_tier: str = ""
    elapsed_minutes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "line_id", normalized_uuid(self.line_id, field="line.line_id")
        )
        line_kind = str(self.line_kind or "").strip().upper()
        if line_kind not in {"ARTICLE", "SERVICE"}:
            raise ContractError("line.line_kind must be ARTICLE or SERVICE.")
        object.__setattr__(self, "line_kind", line_kind)
        barcode = str(self.barcode or "").strip()
        if len(barcode) > 200 or (line_kind == "ARTICLE" and not barcode):
            raise ContractError(
                "line.barcode is required for ARTICLE and must be at most 200 characters."
            )
        if line_kind == "SERVICE" and barcode:
            raise ContractError("a SERVICE line cannot select an inventory barcode.")
        object.__setattr__(self, "barcode", barcode)
        quantity = Decimal(decimal_text(self.quantity, field="line.quantity"))
        if quantity <= 0 or quantity != quantity.to_integral_value() or quantity > 10000:
            raise ContractError("line.quantity must be a whole number from 1 to 10000.")
        if line_kind == "SERVICE" and quantity != 1:
            raise ContractError("a SERVICE line has quantity 1; elapsed_minutes carries time.")
        object.__setattr__(self, "quantity", format(quantity, "f"))
        discount = Decimal(decimal_text(
            self.requested_discount_percent,
            field="line.requested_discount_percent",
        ))
        if discount > 100:
            raise ContractError("line.requested_discount_percent cannot exceed 100.")
        object.__setattr__(
            self, "requested_discount_percent",
            format(discount.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f"),
        )
        lot_serial = str(self.lot_serial or "").strip()
        if len(lot_serial) > 300:
            raise ContractError("line.lot_serial must be at most 300 characters.")
        if line_kind == "SERVICE" and lot_serial:
            raise ContractError("a SERVICE line cannot select an inventory LOT.")
        object.__setattr__(self, "lot_serial", lot_serial)
        tier = str(self.service_tier or "").strip().upper()
        elapsed = int(self.elapsed_minutes or 0)
        if line_kind == "SERVICE":
            if tier not in {"T1", "T2"}:
                raise ContractError("a SERVICE line requires service_tier T1 or T2.")
            if elapsed < 1 or elapsed > 1440:
                raise ContractError("service elapsed_minutes must be from 1 to 1440.")
        elif tier or elapsed:
            raise ContractError("an ARTICLE line cannot contain Service tariff fields.")
        object.__setattr__(self, "service_tier", tier)
        object.__setattr__(self, "elapsed_minutes", elapsed)


@dataclass(frozen=True, slots=True)
class IssueDirectSaleInvoiceContract:
    sale_id: str
    expected_version: int
    premise_id: str
    electronic_device_id: str
    billing_customer: BillingCustomerSelectionContract
    payments: tuple[PaymentContract, ...]
    lines: tuple[DirectSaleLineContract, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sale_id", normalized_uuid(self.sale_id, field="sale_id")
        )
        if int(self.expected_version) != 0:
            raise ContractError("a new direct sale requires expected_version 0.")
        object.__setattr__(self, "expected_version", 0)
        if self.billing_customer.source == "SERVICE_CUSTOMER":
            raise ContractError("a direct sale cannot use a Service customer snapshot.")
        for field_name in ("premise_id", "electronic_device_id"):
            cleaned = str(getattr(self, field_name) or "").strip().upper()
            if not cleaned or len(cleaned) > 40:
                raise ContractError(f"{field_name} is required and must be at most 40 characters.")
            if not all(character.isalnum() or character in {"-", "_"} for character in cleaned):
                raise ContractError(f"{field_name} contains unsupported characters.")
            object.__setattr__(self, field_name, cleaned)
        if not self.lines:
            raise ContractError("lines requires at least one direct-sale line.")
        if len({line.line_id for line in self.lines}) != len(self.lines):
            raise ContractError("direct-sale line_id values must be unique.")
        article_barcodes = [
            line.barcode for line in self.lines if line.line_kind == "ARTICLE"
        ]
        if len(set(article_barcodes)) != len(article_barcodes):
            raise ContractError("a barcode may appear only once; use line.quantity.")
        if not self.payments:
            raise ContractError("payments requires at least one payment.")
        methods = [payment.method for payment in self.payments]
        if len(methods) != len(set(methods)):
            raise ContractError("each payment method may occur only once.")
        if len(self.payments) > 1 and any(
            payment.method == PaymentMethod.BANK_TRANSFER.value
            and not payment.confirmed for payment in self.payments
        ):
            raise ContractError(
                "a BANK_TRANSFER share of split payment must be confirmed."
            )

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_issue_direct_sale_invoice_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate Cashier choices; prices and FIFO LOTs remain server-owned."""
    raw = dict(value)
    allowed = {
        "sale_id", "expected_version", "premise_id", "electronic_device_id",
        "billing_customer", "payments", "lines",
    }
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "direct-sale issuance contains unsupported fields: "
            + ", ".join(unexpected)
        )
    try:
        customer = raw.get("billing_customer")
        raw["billing_customer"] = (
            customer if isinstance(customer, BillingCustomerSelectionContract)
            else BillingCustomerSelectionContract(**dict(customer or {}))
        )
        raw["payments"] = tuple(
            item if isinstance(item, PaymentContract)
            else PaymentContract(**dict(item))
            for item in raw.get("payments", ())
        )
        raw["lines"] = tuple(
            item if isinstance(item, DirectSaleLineContract)
            else DirectSaleLineContract(**dict(item))
            for item in raw.get("lines", ())
        )
        return IssueDirectSaleInvoiceContract(**raw).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("direct-sale issuance payload is invalid.") from exc


def validated_direct_sale_quote_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate quoteable lines without accepting client-owned prices."""
    raw = dict(value)
    unexpected = sorted(set(raw) - {"lines"})
    if unexpected:
        raise ContractError(
            "direct-sale quote contains unsupported fields: "
            + ", ".join(unexpected)
        )
    try:
        lines = tuple(
            item if isinstance(item, DirectSaleLineContract)
            else DirectSaleLineContract(**dict(item))
            for item in raw.get("lines", ())
        )
        if not lines:
            raise ContractError("lines requires at least one direct-sale line.")
        if len({line.line_id for line in lines}) != len(lines):
            raise ContractError("direct-sale line_id values must be unique.")
        article_barcodes = [
            line.barcode for line in lines if line.line_kind == "ARTICLE"
        ]
        if len(set(article_barcodes)) != len(article_barcodes):
            raise ContractError("a barcode may appear only once; use line.quantity.")
        return {"lines": [asdict(line) for line in lines]}
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("direct-sale quote payload is invalid.") from exc


def _cash_location(value: object, *, field: str) -> str:
    cleaned = str(value or "").strip().upper()
    if not cleaned or len(cleaned) > 40:
        raise ContractError(f"{field} is required and must be at most 40 characters.")
    if not all(character.isalnum() or character in {"-", "_"} for character in cleaned):
        raise ContractError(f"{field} contains unsupported characters.")
    return cleaned


@dataclass(frozen=True, slots=True)
class OpenCashSessionContract:
    session_id: str
    premise_id: str
    electronic_device_id: str
    opening_cash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_id", normalized_uuid(self.session_id, field="session_id")
        )
        object.__setattr__(
            self, "premise_id", _cash_location(self.premise_id, field="premise_id")
        )
        object.__setattr__(
            self, "electronic_device_id",
            _cash_location(self.electronic_device_id, field="electronic_device_id"),
        )
        object.__setattr__(
            self, "opening_cash",
            scaled_decimal_text(self.opening_cash, field="opening_cash", scale="0.01"),
        )

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_open_cash_session_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return OpenCashSessionContract(**dict(value)).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("cash-session opening payload is invalid.") from exc


@dataclass(frozen=True, slots=True)
class CloseCashSessionContract:
    session_id: str
    expected_version: int
    counted_cash: str
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_id", normalized_uuid(self.session_id, field="session_id")
        )
        if int(self.expected_version) != 1:
            raise ContractError("an OPEN cash session requires expected_version 1.")
        object.__setattr__(self, "expected_version", 1)
        object.__setattr__(
            self, "counted_cash",
            scaled_decimal_text(self.counted_cash, field="counted_cash", scale="0.01"),
        )
        note = str(self.note or "").strip()
        if len(note) > 1000:
            raise ContractError("cash-session close note must be at most 1000 characters.")
        object.__setattr__(self, "note", note)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_close_cash_session_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return CloseCashSessionContract(**dict(value)).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("cash-session closing payload is invalid.") from exc


@dataclass(frozen=True, slots=True)
class ManualCashEntryContract:
    entry_id: str
    session_id: str
    direction: str
    amount: str
    reference_no: str
    description: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "entry_id", normalized_uuid(self.entry_id, field="entry_id")
        )
        object.__setattr__(
            self, "session_id", normalized_uuid(self.session_id, field="session_id")
        )
        direction = str(self.direction or "").strip().upper()
        if direction not in {"RECEIPT", "EXPENSE"}:
            raise ContractError("manual cash direction must be RECEIPT or EXPENSE.")
        object.__setattr__(self, "direction", direction)
        amount = scaled_decimal_text(self.amount, field="amount", scale="0.01")
        if Decimal(amount) <= 0:
            raise ContractError("manual cash amount must be greater than zero.")
        object.__setattr__(self, "amount", amount)
        for field_name, maximum in (("reference_no", 200), ("description", 500)):
            cleaned = str(getattr(self, field_name) or "").strip()
            if not cleaned or len(cleaned) > maximum:
                raise ContractError(
                    f"{field_name} is required and must be at most {maximum} characters."
                )
            object.__setattr__(self, field_name, cleaned)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_manual_cash_entry_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return ManualCashEntryContract(**dict(value)).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("manual cash entry payload is invalid.") from exc


@dataclass(frozen=True, slots=True)
class SettleUnpaidBankInvoiceContract:
    invoice_id: str
    expected_version: int
    method: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "invoice_id", normalized_uuid(self.invoice_id, field="invoice_id")
        )
        if int(self.expected_version) != 1:
            raise ContractError("an unpaid bank invoice requires expected_version 1.")
        object.__setattr__(self, "expected_version", 1)
        method = str(self.method or "").strip().upper()
        if method not in {
            PaymentMethod.BANK_TRANSFER.value,
            PaymentMethod.CASH.value,
            PaymentMethod.CARD.value,
        }:
            raise ContractError(
                "late bank-invoice payment must be BANK_TRANSFER, CASH or CARD."
            )
        object.__setattr__(self, "method", method)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_settle_unpaid_bank_invoice_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(value)
    unexpected = sorted(set(raw) - {"invoice_id", "expected_version", "method"})
    if unexpected:
        raise ContractError(
            "late bank-invoice payment contains unsupported fields: "
            + ", ".join(unexpected)
        )
    try:
        return SettleUnpaidBankInvoiceContract(**raw).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("late bank-invoice payment payload is invalid.") from exc


@dataclass(frozen=True, slots=True)
class StornoInvoiceContract:
    invoice_id: str
    expected_version: int
    reason: str
    confirmed: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "invoice_id", normalized_uuid(self.invoice_id, field="invoice_id")
        )
        version = int(self.expected_version)
        if version < 1:
            raise ContractError("an issued invoice requires expected_version of at least 1.")
        object.__setattr__(self, "expected_version", version)
        reason = str(self.reason or "").strip()
        if len(reason) < 3 or len(reason) > 500:
            raise ContractError("storno reason must contain 3 to 500 characters.")
        object.__setattr__(self, "reason", reason)
        if self.confirmed is not True:
            raise ContractError("invoice storno must be explicitly confirmed.")

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_storno_invoice_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(value)
    unexpected = sorted(
        set(raw) - {"invoice_id", "expected_version", "reason", "confirmed"}
    )
    if unexpected:
        raise ContractError(
            "invoice storno contains unsupported fields: " + ", ".join(unexpected)
        )
    try:
        return StornoInvoiceContract(**raw).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("invoice storno payload is invalid.") from exc


@dataclass(frozen=True, slots=True)
class ReceivePhysicalStockReturnContract:
    return_id: str
    invoice_id: str
    invoice_line_id: str
    lot_id: str
    quantity: str
    condition_note: str
    confirmed: bool

    def __post_init__(self) -> None:
        for field_name in ("return_id", "invoice_id", "invoice_line_id", "lot_id"):
            object.__setattr__(
                self, field_name,
                normalized_uuid(getattr(self, field_name), field=field_name),
            )
        quantity = decimal_text(self.quantity, field="quantity")
        if Decimal(quantity) <= 0:
            raise ContractError("physical return quantity must be greater than zero.")
        object.__setattr__(self, "quantity", quantity)
        note = str(self.condition_note or "").strip()
        if len(note) > 500:
            raise ContractError("physical return condition_note is too long.")
        object.__setattr__(self, "condition_note", note)
        if self.confirmed is not True:
            raise ContractError("physical stock receipt must be explicitly confirmed.")

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_receive_physical_stock_return_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(value)
    allowed = {
        "return_id", "invoice_id", "invoice_line_id", "lot_id", "quantity",
        "condition_note", "confirmed",
    }
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "physical stock receipt contains unsupported fields: "
            + ", ".join(unexpected)
        )
    try:
        return ReceivePhysicalStockReturnContract(**raw).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("physical stock receipt payload is invalid.") from exc


@dataclass(frozen=True, slots=True)
class ResolvePhysicalStockReturnContract:
    return_id: str
    expected_version: int
    decision: str
    reason: str
    confirmed: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "return_id", normalized_uuid(self.return_id, field="return_id")
        )
        version = int(self.expected_version)
        if version != 1:
            raise ContractError("a quarantined physical return requires expected_version 1.")
        object.__setattr__(self, "expected_version", version)
        decision = str(self.decision or "").strip().upper()
        if decision not in {
            "RETURN_TO_STOCK", "MARK_DEFECTIVE", "REFER_TO_CLAIM",
        }:
            raise ContractError("physical return decision is not supported.")
        object.__setattr__(self, "decision", decision)
        reason = str(self.reason or "").strip()
        if len(reason) < 3 or len(reason) > 500:
            raise ContractError("physical return decision reason must contain 3 to 500 characters.")
        object.__setattr__(self, "reason", reason)
        if self.confirmed is not True:
            raise ContractError("physical return decision must be explicitly confirmed.")

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def validated_resolve_physical_stock_return_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(value)
    allowed = {"return_id", "expected_version", "decision", "reason", "confirmed"}
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "physical return decision contains unsupported fields: "
            + ", ".join(unexpected)
        )
    try:
        return ResolvePhysicalStockReturnContract(**raw).payload()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("physical return decision payload is invalid.") from exc


def validated_begin_full_inventory_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(value)
    allowed = {"session_id", "reference", "confirmed"}
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "full inventory preparation contains unsupported fields: "
            + ", ".join(unexpected)
        )
    reference = str(raw.get("reference") or "").strip()
    if len(reference) < 3 or len(reference) > 200:
        raise ContractError(
            "full inventory reference must contain 3 to 200 characters."
        )
    if raw.get("confirmed") is not True:
        raise ContractError(
            "full inventory preparation must be explicitly confirmed."
        )
    return {
        "session_id": normalized_uuid(
            raw.get("session_id"), field="payload.session_id"
        ),
        "reference": reference,
        "confirmed": True,
    }


def validated_inventory_readiness_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(value)
    allowed = {
        "session_id", "pending_outbox_count", "uncertain_outbox_count",
        "sync_conflict_count", "central_checkpoint", "local_write_lock_active",
    }
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "inventory readiness contains unsupported fields: "
            + ", ".join(unexpected)
        )
    result: dict[str, Any] = {
        "session_id": normalized_uuid(
            raw.get("session_id"), field="payload.session_id"
        )
    }
    for field_name in (
        "pending_outbox_count", "uncertain_outbox_count",
        "sync_conflict_count", "central_checkpoint",
    ):
        value = raw.get(field_name)
        if isinstance(value, bool):
            raise ContractError(f"payload.{field_name} must be a non-negative integer.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ContractError(
                f"payload.{field_name} must be a non-negative integer."
            ) from exc
        if parsed < 0 or str(value).strip() != str(parsed):
            raise ContractError(
                f"payload.{field_name} must be a non-negative integer."
            )
        result[field_name] = parsed
    if not isinstance(raw.get("local_write_lock_active"), bool):
        raise ContractError("payload.local_write_lock_active must be boolean.")
    result["local_write_lock_active"] = raw["local_write_lock_active"]
    return result


def validated_cancel_full_inventory_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(value)
    allowed = {"session_id", "reason", "confirmed"}
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ContractError(
            "full inventory cancellation contains unsupported fields: "
            + ", ".join(unexpected)
        )
    reason = str(raw.get("reason") or "").strip()
    if len(reason) < 3 or len(reason) > 500:
        raise ContractError(
            "full inventory cancellation reason must contain 3 to 500 characters."
        )
    if raw.get("confirmed") is not True:
        raise ContractError(
            "full inventory cancellation must be explicitly confirmed."
        )
    return {
        "session_id": normalized_uuid(
            raw.get("session_id"), field="payload.session_id"
        ),
        "reason": reason,
        "confirmed": True,
    }


@dataclass(frozen=True, slots=True)
class SyncOperationEnvelope:
    operation_id: str
    operation_type: str
    aggregate_type: str
    aggregate_id: str
    expected_version: int
    actor_id: str
    device_id: str
    device_sequence: int
    occurred_at: str
    payload: Mapping[str, Any]
    contract_version: int = SYNC_CONTRACT_VERSION
    actor_user_id: str = ""
    actor_authorization_version: int = 0
    workstation_signature: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_id", normalized_uuid(self.operation_id, field="operation_id")
        )
        object.__setattr__(
            self, "aggregate_id", normalized_uuid(self.aggregate_id, field="aggregate_id")
        )
        for field_name in ("operation_type", "aggregate_type", "actor_id", "device_id"):
            if not str(getattr(self, field_name)).strip():
                raise ContractError(f"{field_name} is required.")
        if int(self.expected_version) < 0:
            raise ContractError("expected_version must not be negative.")
        if int(self.device_sequence) < 1:
            raise ContractError("device_sequence must be at least 1.")
        if int(self.contract_version) != SYNC_CONTRACT_VERSION:
            raise ContractError("contract_version is not supported.")
        object.__setattr__(self, "expected_version", int(self.expected_version))
        object.__setattr__(self, "device_sequence", int(self.device_sequence))
        object.__setattr__(
            self, "occurred_at", normalized_datetime(self.occurred_at, field="occurred_at")
        )
        object.__setattr__(self, "payload", dict(self.payload))
        actor_user_id = str(self.actor_user_id or "").strip()
        if actor_user_id:
            actor_user_id = normalized_uuid(actor_user_id, field="actor_user_id")
            if int(self.actor_authorization_version) < 1:
                raise ContractError(
                    "signed actor identity requires actor_authorization_version."
                )
        elif int(self.actor_authorization_version) != 0:
            raise ContractError(
                "actor_authorization_version requires actor_user_id."
            )
        object.__setattr__(self, "actor_user_id", actor_user_id)
        object.__setattr__(
            self, "actor_authorization_version", int(self.actor_authorization_version)
        )
        signature = str(self.workstation_signature or "").strip()
        if signature:
            try:
                decoded = base64.b64decode(signature, validate=True)
            except (ValueError, TypeError) as exc:
                raise ContractError("workstation_signature must be base64.") from exc
            if len(decoded) != 64:
                raise ContractError("workstation_signature must be an Ed25519 signature.")
            if not actor_user_id:
                raise ContractError("a workstation signature requires actor_user_id.")
        object.__setattr__(self, "workstation_signature", signature)

    @property
    def payload_hash(self) -> str:
        return payload_sha256(self.payload)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["payload_hash"] = self.payload_hash
        return result

    def signing_dict(self) -> dict[str, Any]:
        """Canonical fields protected by a Service workstation signature."""
        result = asdict(self)
        result.pop("workstation_signature", None)
        return result

    @property
    def signing_bytes(self) -> bytes:
        return canonical_json(self.signing_dict()).encode("utf-8")
