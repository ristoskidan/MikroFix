"""Central PostgreSQL model for the first synchronization vertical slice."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING_CONVENTION)


central_users = Table(
    "central_users_v32",
    metadata,
    Column("user_uuid", UUID(as_uuid=True), primary_key=True),
    Column("username", String(100), nullable=False),
    Column("username_normalized", String(100), nullable=False, unique=True),
    Column("display_name", String(200), nullable=False),
    Column("role", String(30), nullable=False),
    Column("pin_salt", LargeBinary, nullable=False),
    Column("pin_hash", LargeBinary, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("failed_attempts", Integer, nullable=False, server_default="0"),
    Column("locked_until", DateTime(timezone=True)),
    Column("must_change_pin", Boolean, nullable=False, server_default="false"),
    Column("operator_tax_number", String(8)),
    Column("row_version", Integer, nullable=False, server_default="1"),
    Column("last_login_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "role IN ('ADMIN','SERVICE','OFFICE','BUYBACK','CASHIER')",
        name="central_user_role",
    ),
    CheckConstraint("failed_attempts >= 0", name="central_user_failed_attempts"),
    CheckConstraint("row_version >= 1", name="central_user_version"),
    CheckConstraint(
        "operator_tax_number IS NULL OR operator_tax_number ~ '^[0-9]{8}$'",
        name="central_user_operator_tax_number",
    ),
)


central_user_permissions = Table(
    "central_user_permissions_v32",
    metadata,
    Column(
        "user_uuid", UUID(as_uuid=True),
        ForeignKey("central_users_v32.user_uuid"), primary_key=True,
    ),
    Column("permission", String(100), primary_key=True),
    Column("granted_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("granted_by", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
)


central_user_permission_history = Table(
    "central_user_permission_history_v32",
    metadata,
    Column(
        "user_uuid", UUID(as_uuid=True),
        ForeignKey("central_users_v32.user_uuid"), primary_key=True,
    ),
    Column("permission", String(100), primary_key=True),
    Column("valid_from_version", Integer, primary_key=True),
    Column("valid_until_version", Integer),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("recorded_by", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    CheckConstraint("valid_from_version >= 1", name="permission_history_from_version"),
    CheckConstraint(
        "valid_until_version IS NULL OR valid_until_version >= valid_from_version",
        name="permission_history_version_range",
    ),
)


workstations = Table(
    "workstations_v32",
    metadata,
    Column("workstation_uuid", UUID(as_uuid=True), primary_key=True),
    Column("workstation_code", String(100), nullable=False, unique=True),
    Column("display_name", String(200), nullable=False),
    Column("application_type", String(20), nullable=False),
    Column("credential_hash", String(64), nullable=False),
    Column("signing_public_key", LargeBinary),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("approved_by", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    Column("approved_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "application_type IN ('SERVICE','CASHIER','OFFICE')",
        name="workstation_application_type",
    ),
)


central_sessions = Table(
    "central_sessions_v32",
    metadata,
    Column("session_uuid", UUID(as_uuid=True), primary_key=True),
    Column(
        "user_uuid", UUID(as_uuid=True),
        ForeignKey("central_users_v32.user_uuid"), nullable=False,
    ),
    Column(
        "workstation_uuid", UUID(as_uuid=True),
        ForeignKey("workstations_v32.workstation_uuid"), nullable=False,
    ),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("revoked_at", DateTime(timezone=True)),
)


central_audit_events = Table(
    "central_audit_events_v32",
    metadata,
    Column("audit_sequence", BigInteger, Identity(always=False), primary_key=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("event_type", String(100), nullable=False),
    Column("success", Boolean, nullable=False),
    Column("actor_uuid", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    Column("workstation_uuid", UUID(as_uuid=True), ForeignKey("workstations_v32.workstation_uuid")),
    Column("entity_type", String(100)),
    Column("entity_id", String(200)),
    Column("details_json", JSONB, nullable=False, server_default="{}"),
)


service_orders = Table(
    "service_orders_v32",
    metadata,
    Column("sync_uuid", UUID(as_uuid=True), primary_key=True),
    Column("row_version", Integer, nullable=False),
    Column("origin_device_id", String(200), nullable=False),
    Column("payload_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint("row_version >= 1", name="row_version_positive"),
)


change_feed = Table(
    "change_feed_v32",
    metadata,
    Column(
        "central_sequence",
        BigInteger,
        Identity(always=False),
        primary_key=True,
    ),
    Column("event_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column("event_type", String(100), nullable=False),
    Column("aggregate_type", String(100), nullable=False),
    Column("aggregate_uuid", UUID(as_uuid=True), nullable=False),
    Column("aggregate_version", Integer, nullable=False),
    Column("payload_json", JSONB, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("aggregate_version >= 1", name="aggregate_version_positive"),
)


accepted_operations = Table(
    "accepted_operations_v32",
    metadata,
    Column("operation_id", UUID(as_uuid=True), primary_key=True),
    Column("operation_type", String(100), nullable=False),
    Column("aggregate_type", String(100), nullable=False),
    Column("aggregate_uuid", UUID(as_uuid=True), nullable=False),
    Column("expected_version", Integer, nullable=False),
    Column("actor_id", String(200), nullable=False),
    Column("actor_user_uuid", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    Column("actor_authorization_version", Integer, nullable=False, server_default="0"),
    Column("device_id", String(200), nullable=False),
    Column("transmitted_by_user_uuid", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    Column("transmitted_by_workstation_uuid", UUID(as_uuid=True), ForeignKey("workstations_v32.workstation_uuid")),
    Column("workstation_signature", String(200), nullable=False, server_default=""),
    Column("device_sequence", BigInteger, nullable=False),
    Column("contract_version", Integer, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("result_code", String(40), nullable=False),
    Column("response_json", JSONB, nullable=False),
    Column("central_sequence", BigInteger),
    Column("accepted_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("device_id", "device_sequence", name="uq_operation_device_sequence"),
    CheckConstraint("expected_version >= 0", name="expected_version_nonnegative"),
    CheckConstraint("device_sequence >= 1", name="device_sequence_positive"),
    CheckConstraint(
        "actor_authorization_version >= 0",
        name="actor_authorization_version_nonnegative",
    ),
)


inventory_items = Table(
    "inventory_items_v32",
    metadata,
    Column("article_uuid", UUID(as_uuid=True), primary_key=True),
    Column("barcode", String(200), nullable=False, unique=True),
    Column("description", String(500), nullable=False),
    Column("track_mode", String(20), nullable=False, server_default="QUANTITY"),
    Column("quantity_on_hand", Numeric(18, 4), nullable=False),
    Column("current_sale_price", Numeric(18, 2), nullable=False, server_default="0"),
    Column("blocked", Boolean, nullable=False, server_default="false"),
    Column("blocked_reason", String(500)),
    Column("row_version", Integer, nullable=False, server_default="1"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("quantity_on_hand >= 0", name="inventory_quantity_nonnegative"),
    CheckConstraint("track_mode IN ('QUANTITY','SERIAL')", name="inventory_track_mode"),
    CheckConstraint("row_version >= 1", name="inventory_version_positive"),
)


receipt_document_types = Table(
    "receipt_document_types_v32",
    metadata,
    Column("type_code", String(5), primary_key=True),
    Column("display_name", String(200), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("created_by", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("type_code ~ '^[A-Z]{2,5}$'", name="receipt_document_type_code"),
)


receipt_number_sequences = Table(
    "receipt_number_sequences_v32",
    metadata,
    Column("calendar_year", Integer, primary_key=True),
    Column("series_code", String(5), primary_key=True),
    Column("last_number", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("calendar_year >= 2020", name="receipt_sequence_year"),
    CheckConstraint("last_number >= 1", name="receipt_sequence_positive"),
)


purchase_receipts = Table(
    "purchase_receipts_v32",
    metadata,
    Column("receipt_uuid", UUID(as_uuid=True), primary_key=True),
    Column("row_version", Integer, nullable=False),
    Column("receipt_number", String(40), nullable=False, unique=True),
    Column("document_type", String(10), nullable=False),
    Column("series_code", String(5), nullable=False),
    Column("status", String(30), nullable=False),
    Column("supplier_uuid", UUID(as_uuid=True), ForeignKey("partners_v32.partner_uuid"), nullable=False),
    Column("supplier_version", Integer, nullable=False),
    Column("supplier_snapshot_json", JSONB, nullable=False),
    Column("external_document_no", String(200), nullable=False),
    Column("document_date", Date, nullable=False),
    Column("received_date", Date, nullable=False),
    Column("currency", String(3), nullable=False, server_default="EUR"),
    Column("document_total", Numeric(18, 2), nullable=False),
    Column("shipping_total", Numeric(18, 4), nullable=False),
    Column("invoice_receipt_number", String(40), unique=True),
    Column("invoice_external_no", String(200)),
    Column("invoice_date", Date),
    Column("invoice_due_date", Date),
    Column("invoice_payment_status", String(20)),
    Column("confirmed_operation_id", UUID(as_uuid=True), ForeignKey("accepted_operations_v32.operation_id"), nullable=False, unique=True),
    Column("release_operation_id", UUID(as_uuid=True), ForeignKey("accepted_operations_v32.operation_id"), unique=True),
    Column("confirmed_at", DateTime(timezone=True), nullable=False),
    Column("released_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "supplier_uuid", "document_type", "external_document_no",
        name="uq_purchase_receipt_supplier_document",
    ),
    UniqueConstraint(
        "supplier_uuid", "invoice_external_no",
        name="uq_purchase_receipt_supplier_invoice",
    ),
    CheckConstraint("row_version >= 1", name="purchase_receipt_version"),
    CheckConstraint("document_type IN ('DOB','DRA','CUSTOM')", name="purchase_receipt_document_type"),
    CheckConstraint("status IN ('PENDING_INVOICE','RELEASED')", name="purchase_receipt_status"),
    CheckConstraint("currency = 'EUR'", name="purchase_receipt_currency"),
    CheckConstraint("document_total >= 0 AND shipping_total >= 0", name="purchase_receipt_totals"),
)


purchase_receipt_revisions = Table(
    "purchase_receipt_revisions_v32",
    metadata,
    Column("receipt_uuid", UUID(as_uuid=True), ForeignKey("purchase_receipts_v32.receipt_uuid"), primary_key=True),
    Column("row_version", Integer, primary_key=True),
    Column("operation_id", UUID(as_uuid=True), ForeignKey("accepted_operations_v32.operation_id"), nullable=False, unique=True),
    Column("snapshot_json", JSONB, nullable=False),
    Column("actor_uuid", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    Column("workstation_uuid", UUID(as_uuid=True), ForeignKey("workstations_v32.workstation_uuid")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("row_version >= 1", name="purchase_receipt_revision_version"),
)


purchase_receipt_lines = Table(
    "purchase_receipt_lines_v32",
    metadata,
    Column("line_uuid", UUID(as_uuid=True), primary_key=True),
    Column("receipt_uuid", UUID(as_uuid=True), ForeignKey("purchase_receipts_v32.receipt_uuid"), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("article_uuid", UUID(as_uuid=True), ForeignKey("inventory_items_v32.article_uuid"), nullable=False),
    Column("quantity", Numeric(18, 4), nullable=False),
    Column("net_unit_price", Numeric(18, 4), nullable=False),
    Column("discount_percent", Numeric(7, 4), nullable=False),
    Column("vat_rate", Numeric(7, 4), nullable=False),
    Column("vat_amount", Numeric(18, 2), nullable=False),
    Column("calculated_total", Numeric(18, 2), nullable=False),
    Column("supplier_line_total", Numeric(18, 2), nullable=False),
    Column("allocated_shipping", Numeric(18, 4), nullable=False),
    Column("landed_unit_cost", Numeric(18, 4), nullable=False),
    Column("confirmed_sale_price", Numeric(18, 2), nullable=False),
    Column("supplier_barcode", String(200), nullable=False),
    Column("lot_serial", String(300), nullable=False),
    UniqueConstraint("receipt_uuid", "line_no", name="uq_purchase_receipt_line_no"),
    CheckConstraint("quantity > 0", name="purchase_receipt_line_quantity"),
)


inventory_identities = Table(
    "inventory_identities_v32",
    metadata,
    Column("identity_uuid", UUID(as_uuid=True), primary_key=True),
    Column("article_uuid", UUID(as_uuid=True), ForeignKey("inventory_items_v32.article_uuid"), nullable=False),
    Column("supplier_barcode", String(200), nullable=False, unique=True),
    Column("created_from_receipt_uuid", UUID(as_uuid=True), ForeignKey("purchase_receipts_v32.receipt_uuid"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


inventory_lots = Table(
    "inventory_lots_v32",
    metadata,
    Column("lot_uuid", UUID(as_uuid=True), primary_key=True),
    Column("receipt_line_uuid", UUID(as_uuid=True), ForeignKey("purchase_receipt_lines_v32.line_uuid"), nullable=False, unique=True),
    Column("identity_uuid", UUID(as_uuid=True), ForeignKey("inventory_identities_v32.identity_uuid"), nullable=False),
    Column("article_uuid", UUID(as_uuid=True), ForeignKey("inventory_items_v32.article_uuid"), nullable=False),
    Column("lot_serial", String(300), nullable=False),
    Column("serial_unique_key", String(300), unique=True),
    Column("received_quantity", Numeric(18, 4), nullable=False),
    Column("available_quantity", Numeric(18, 4), nullable=False),
    Column("landed_unit_cost", Numeric(18, 4), nullable=False),
    Column("sale_price_snapshot", Numeric(18, 2), nullable=False),
    Column("status", String(20), nullable=False),
    Column("received_date", Date, nullable=False),
    Column("released_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("received_quantity > 0", name="inventory_lot_received_quantity"),
    CheckConstraint("available_quantity >= 0", name="inventory_lot_available_quantity"),
    CheckConstraint("status IN ('PENDING','AVAILABLE','CONSUMED')", name="inventory_lot_status"),
)


inventory_price_history = Table(
    "inventory_price_history_v32",
    metadata,
    Column("price_history_uuid", UUID(as_uuid=True), primary_key=True),
    Column("article_uuid", UUID(as_uuid=True), ForeignKey("inventory_items_v32.article_uuid"), nullable=False),
    Column("receipt_uuid", UUID(as_uuid=True), ForeignKey("purchase_receipts_v32.receipt_uuid")),
    Column(
        "price_change_operation_id", UUID(as_uuid=True),
        ForeignKey("accepted_operations_v32.operation_id"), unique=True,
    ),
    Column("change_reason", String(500)),
    Column("previous_price", Numeric(18, 2), nullable=False),
    Column("confirmed_price", Numeric(18, 2), nullable=False),
    Column("highest_available_cost", Numeric(18, 4), nullable=False),
    Column("effective_at", DateTime(timezone=True), nullable=False),
    Column("actor_uuid", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    CheckConstraint(
        "(receipt_uuid IS NOT NULL AND price_change_operation_id IS NULL) OR "
        "(receipt_uuid IS NULL AND price_change_operation_id IS NOT NULL)",
        name="inventory_price_history_one_source",
    ),
)


receipt_attachments = Table(
    "receipt_attachments_v32",
    metadata,
    Column("attachment_uuid", UUID(as_uuid=True), primary_key=True),
    Column("receipt_uuid", UUID(as_uuid=True), ForeignKey("purchase_receipts_v32.receipt_uuid"), nullable=False),
    Column("receipt_version", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("media_type", String(100), nullable=False),
    Column("original_name", String(300), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("actor_uuid", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    UniqueConstraint("receipt_uuid", "sha256", name="uq_receipt_attachment_digest"),
    CheckConstraint("size_bytes BETWEEN 1 AND 26214400", name="receipt_attachment_size"),
)


technical_completions = Table(
    "technical_completions_v32",
    metadata,
    Column(
        "service_order_uuid", UUID(as_uuid=True),
        ForeignKey("service_orders_v32.sync_uuid"), primary_key=True,
    ),
    Column(
        "operation_id", UUID(as_uuid=True),
        ForeignKey("accepted_operations_v32.operation_id"), nullable=False, unique=True,
    ),
    Column("payload_json", JSONB, nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


service_part_consumptions = Table(
    "service_part_consumptions_v32",
    metadata,
    Column("line_uuid", UUID(as_uuid=True), primary_key=True),
    Column(
        "service_order_uuid", UUID(as_uuid=True),
        ForeignKey("service_orders_v32.sync_uuid"), nullable=False,
    ),
    Column(
        "article_uuid", UUID(as_uuid=True),
        ForeignKey("inventory_items_v32.article_uuid"), nullable=False,
    ),
    Column("barcode", String(200), nullable=False),
    Column("quantity", Numeric(18, 4), nullable=False),
    Column("unit_price", Numeric(18, 4), nullable=False),
    Column("lot_serial", String(300), nullable=False, server_default=""),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "service_order_uuid", "line_uuid", name="uq_service_consumption_line"
    ),
    CheckConstraint("quantity > 0", name="service_consumption_quantity_positive"),
)


service_stock_allocations = Table(
    "service_stock_allocations_v32",
    metadata,
    Column(
        "service_line_uuid", UUID(as_uuid=True),
        ForeignKey("service_part_consumptions_v32.line_uuid"), primary_key=True,
    ),
    Column(
        "lot_uuid", UUID(as_uuid=True),
        ForeignKey("inventory_lots_v32.lot_uuid"), primary_key=True,
    ),
    Column("quantity", Numeric(18, 4), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("quantity > 0", name="service_stock_allocation_quantity"),
)


invoice_drafts = Table(
    "invoice_drafts_v32",
    metadata,
    Column("draft_uuid", UUID(as_uuid=True), primary_key=True),
    Column(
        "service_order_uuid", UUID(as_uuid=True),
        ForeignKey("service_orders_v32.sync_uuid"), nullable=False, unique=True,
    ),
    Column("status", String(40), nullable=False),
    Column("payload_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN ('READY','ISSUING','INVOICED')", name="invoice_draft_status"
    ),
)


invoice_number_sequences = Table(
    "invoice_number_sequences_v32",
    metadata,
    Column("fiscal_year", Integer, primary_key=True),
    Column("premise_id", String(40), primary_key=True),
    Column("electronic_device_id", String(40), primary_key=True),
    Column("last_number", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("fiscal_year >= 2020", name="invoice_sequence_year"),
    CheckConstraint("last_number >= 1", name="invoice_sequence_positive"),
)


partners = Table(
    "partners_v32",
    metadata,
    Column("partner_uuid", UUID(as_uuid=True), primary_key=True),
    Column("name", String(300), nullable=False),
    Column("address", String(300), nullable=False, server_default=""),
    Column("post_city", String(200), nullable=False, server_default=""),
    Column("country", String(100), nullable=False, server_default="Slovenija"),
    Column("tax_number", String(20), nullable=False, server_default=""),
    Column("tax_number_normalized", String(8), unique=True),
    Column("registration_number", String(30), nullable=False, server_default=""),
    Column("phone", String(100), nullable=False, server_default=""),
    Column("email", String(300), nullable=False, server_default=""),
    Column("row_version", Integer, nullable=False, server_default="1"),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("verified_by", String(200), nullable=False),
    Column("verified_at", DateTime(timezone=True), nullable=False),
    Column("verification_source", String(30), nullable=False),
    Column("source_reference", String(300), nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("row_version >= 1", name="partner_version_positive"),
    CheckConstraint(
        "verification_source IN ('MANUAL_REVIEW','AJPES_REVIEW')",
        name="partner_verification_source",
    ),
)


cash_session_number_sequences = Table(
    "cash_session_number_sequences_v32",
    metadata,
    Column("fiscal_year", Integer, primary_key=True),
    Column("last_number", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("fiscal_year >= 2020", name="cash_session_sequence_year"),
    CheckConstraint("last_number >= 1", name="cash_session_sequence_positive"),
)


cash_sessions = Table(
    "cash_sessions_v32",
    metadata,
    Column("session_uuid", UUID(as_uuid=True), primary_key=True),
    Column("session_no", String(40), nullable=False, unique=True),
    Column("premise_id", String(40), nullable=False),
    Column("electronic_device_id", String(40), nullable=False),
    Column("status", String(20), nullable=False),
    Column("row_version", Integer, nullable=False, server_default="1"),
    Column("opened_at", DateTime(timezone=True), nullable=False),
    Column("opened_by_user_uuid", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    Column("opened_by", String(200), nullable=False),
    Column("opened_by_workstation_uuid", UUID(as_uuid=True), ForeignKey("workstations_v32.workstation_uuid")),
    Column("opening_cash", Numeric(18, 2), nullable=False),
    Column("closed_at", DateTime(timezone=True)),
    Column("closed_by_user_uuid", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    Column("closed_by", String(200)),
    Column("closed_by_workstation_uuid", UUID(as_uuid=True), ForeignKey("workstations_v32.workstation_uuid")),
    Column("expected_cash", Numeric(18, 2)),
    Column("counted_cash", Numeric(18, 2)),
    Column("difference", Numeric(18, 2)),
    Column("close_note", String(1000), nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('OPEN','CLOSED')", name="cash_session_status"),
    CheckConstraint("row_version IN (1,2)", name="cash_session_version"),
    CheckConstraint("opening_cash >= 0", name="cash_session_opening_nonnegative"),
    CheckConstraint(
        "(status='OPEN' AND closed_at IS NULL AND closed_by IS NULL "
        "AND expected_cash IS NULL AND counted_cash IS NULL AND difference IS NULL) OR "
        "(status='CLOSED' AND closed_at IS NOT NULL AND closed_by IS NOT NULL "
        "AND expected_cash IS NOT NULL AND counted_cash IS NOT NULL AND difference IS NOT NULL)",
        name="cash_session_close_shape",
    ),
)
Index(
    "uq_open_cash_session_location_v32",
    cash_sessions.c.premise_id,
    cash_sessions.c.electronic_device_id,
    unique=True,
    postgresql_where=cash_sessions.c.status == "OPEN",
)


invoices = Table(
    "invoices_v32",
    metadata,
    Column("invoice_uuid", UUID(as_uuid=True), primary_key=True),
    Column(
        "draft_uuid", UUID(as_uuid=True),
        ForeignKey("invoice_drafts_v32.draft_uuid"), nullable=True, unique=True,
    ),
    Column(
        "service_order_uuid", UUID(as_uuid=True),
        ForeignKey("service_orders_v32.sync_uuid"), nullable=True, unique=True,
    ),
    Column("source_type", String(20), nullable=False, server_default="SERVICE"),
    Column("direct_sale_uuid", UUID(as_uuid=True), unique=True),
    Column(
        "issue_operation_id", UUID(as_uuid=True),
        ForeignKey("accepted_operations_v32.operation_id"), nullable=False, unique=True,
    ),
    Column("invoice_number", String(130), nullable=False, unique=True),
    Column("fiscal_year", Integer, nullable=False),
    Column("fiscal_sequence", BigInteger, nullable=False),
    Column("premise_id", String(40), nullable=False),
    Column("electronic_device_id", String(40), nullable=False),
    Column(
        "cash_session_uuid", UUID(as_uuid=True),
        ForeignKey("cash_sessions_v32.session_uuid"), nullable=True,
    ),
    Column("cashier_id", String(200), nullable=False),
    Column("operator_tax_number", String(8)),
    Column(
        "billing_partner_uuid", UUID(as_uuid=True),
        ForeignKey("partners_v32.partner_uuid"),
    ),
    Column("billing_customer_json", JSONB, nullable=False),
    Column("status", String(40), nullable=False),
    Column("payment_status", String(20), nullable=False),
    Column("payments_json", JSONB, nullable=False),
    Column(
        "settlement_operation_id", UUID(as_uuid=True),
        ForeignKey("accepted_operations_v32.operation_id"), unique=True,
    ),
    Column("payment_received_at", DateTime(timezone=True)),
    Column("payment_received_method", String(20)),
    Column("fiscal_submission_deadline", Date),
    Column("total_amount", Numeric(18, 2), nullable=False),
    Column("currency", String(3), nullable=False, server_default="EUR"),
    Column("tax_mode", String(20), nullable=False),
    Column("tax_rate", Numeric(7, 4), nullable=False),
    Column("tax_note", String(500), nullable=False, server_default=""),
    Column("furs_zoi", String(64), nullable=False, server_default=""),
    Column("furs_eor", String(100), nullable=False, server_default=""),
    Column("snapshot_json", JSONB, nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("finalized_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "fiscal_year", "premise_id", "electronic_device_id", "fiscal_sequence",
        name="uq_invoice_fiscal_sequence",
    ),
    CheckConstraint(
        "status IN ('PENDING_FISCALIZATION','FISCAL_RECONCILIATION','ISSUED')",
        name="invoice_status",
    ),
    CheckConstraint("payment_status IN ('UNPAID','PAID')", name="invoice_payment_status"),
    CheckConstraint(
        "payment_received_method IS NULL OR payment_received_method IN "
        "('BANK_TRANSFER','CASH','CARD')",
        name="invoice_received_method",
    ),
    CheckConstraint("total_amount >= 0", name="invoice_total_nonnegative"),
    CheckConstraint("currency = 'EUR'", name="invoice_currency_eur"),
    CheckConstraint(
        "operator_tax_number IS NULL OR operator_tax_number ~ '^[0-9]{8}$'",
        name="invoice_operator_tax_number",
    ),
    CheckConstraint(
        "(source_type='SERVICE' AND draft_uuid IS NOT NULL "
        "AND service_order_uuid IS NOT NULL AND direct_sale_uuid IS NULL) OR "
        "(source_type='DIRECT_SALE' AND draft_uuid IS NULL "
        "AND service_order_uuid IS NULL AND direct_sale_uuid IS NOT NULL)",
        name="invoice_source_identity",
    ),
)


cash_journal_entries = Table(
    "cash_journal_entries_v32",
    metadata,
    Column("entry_uuid", UUID(as_uuid=True), primary_key=True),
    Column(
        "session_uuid", UUID(as_uuid=True),
        ForeignKey("cash_sessions_v32.session_uuid"), nullable=False,
    ),
    Column("entry_no", Integer, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("entry_type", String(30), nullable=False),
    Column("amount", Numeric(18, 2), nullable=False),
    Column("balance_after", Numeric(18, 2), nullable=False),
    Column("reference_type", String(40), nullable=False),
    Column("reference_uuid", UUID(as_uuid=True)),
    Column("reference_no", String(200), nullable=False, server_default=""),
    Column("description", String(500), nullable=False),
    Column("actor_user_uuid", UUID(as_uuid=True), ForeignKey("central_users_v32.user_uuid")),
    Column("actor_id", String(200), nullable=False),
    Column("workstation_uuid", UUID(as_uuid=True), ForeignKey("workstations_v32.workstation_uuid")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("session_uuid", "entry_no", name="uq_cash_journal_entry_no"),
    UniqueConstraint(
        "reference_type", "reference_uuid", "entry_type",
        name="uq_cash_journal_reference",
    ),
    CheckConstraint(
        "entry_type IN ('CASH_SALE','CASH_RECEIPT','CASH_EXPENSE','CASH_REFUND','CASH_BUYBACK')",
        name="cash_journal_entry_type",
    ),
    CheckConstraint("amount <> 0", name="cash_journal_amount_nonzero"),
    CheckConstraint("balance_after >= 0", name="cash_journal_balance_nonnegative"),
)


direct_sale_stock_allocations = Table(
    "direct_sale_stock_allocations_v32",
    metadata,
    Column(
        "invoice_uuid", UUID(as_uuid=True),
        ForeignKey("invoices_v32.invoice_uuid"), primary_key=True,
    ),
    Column("sale_line_uuid", UUID(as_uuid=True), primary_key=True),
    Column(
        "lot_uuid", UUID(as_uuid=True),
        ForeignKey("inventory_lots_v32.lot_uuid"), primary_key=True,
    ),
    Column("quantity", Numeric(18, 4), nullable=False),
    Column("landed_unit_cost", Numeric(18, 4), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("quantity > 0", name="direct_sale_allocation_quantity"),
    CheckConstraint("landed_unit_cost >= 0", name="direct_sale_allocation_cost"),
)


direct_sale_discount_audit = Table(
    "direct_sale_discount_audit_v32",
    metadata,
    Column(
        "invoice_uuid", UUID(as_uuid=True),
        ForeignKey("invoices_v32.invoice_uuid"), primary_key=True,
    ),
    Column("sale_line_uuid", UUID(as_uuid=True), primary_key=True),
    Column("requested_discount_percent", Numeric(7, 4), nullable=False),
    Column("applied_discount_percent", Numeric(7, 4), nullable=False),
    Column("decision", String(40), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "requested_discount_percent BETWEEN 0 AND 100",
        name="direct_sale_discount_requested_range",
    ),
    CheckConstraint(
        "applied_discount_percent BETWEEN 0 AND 100",
        name="direct_sale_discount_applied_range",
    ),
    CheckConstraint(
        "decision IN ('AS_REQUESTED','CLAMPED_TO_MINIMUM')",
        name="direct_sale_discount_decision",
    ),
)


service_tariffs = Table(
    "service_tariffs_v32",
    metadata,
    Column("tariff_uuid", UUID(as_uuid=True), primary_key=True),
    Column("tier", String(2), nullable=False),
    Column("tariff_version", String(80), nullable=False),
    Column("first_hour_rate", Numeric(18, 2), nullable=False),
    Column("extra_half_hour_rate", Numeric(18, 2), nullable=False),
    Column("minimum_line_price", Numeric(18, 2), nullable=False),
    Column("effective_from", Date, nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tier", "tariff_version", name="uq_service_tariff_version"),
    CheckConstraint("tier IN ('T1','T2')", name="service_tariff_tier"),
    CheckConstraint("first_hour_rate > 0", name="service_tariff_first_rate"),
    CheckConstraint("extra_half_hour_rate >= 0", name="service_tariff_extra_rate"),
    CheckConstraint("minimum_line_price > 0", name="service_tariff_minimum"),
)
Index(
    "uq_active_service_tariff_tier",
    service_tariffs.c.tier,
    unique=True,
    postgresql_where=service_tariffs.c.active.is_(True),
)


fiscal_outbox = Table(
    "fiscal_outbox_v32",
    metadata,
    Column("message_uuid", UUID(as_uuid=True), primary_key=True),
    Column(
        "invoice_uuid", UUID(as_uuid=True),
        ForeignKey("invoices_v32.invoice_uuid"), nullable=False, unique=True,
    ),
    Column("status", String(30), nullable=False),
    Column("request_sha256", String(64), nullable=False),
    Column("request_json", JSONB, nullable=False),
    Column("prepared_request_sha256", String(64), nullable=False, server_default=""),
    Column("prepared_request_json", JSONB, nullable=False, server_default="{}"),
    Column("prepared_zoi", String(64), nullable=False, server_default=""),
    Column("response_json", JSONB, nullable=False, server_default="{}"),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("last_error", String(1000)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN ('PENDING','SENDING','UNCERTAIN','CONFIRMED')",
        name="fiscal_outbox_status",
    ),
    CheckConstraint("attempt_count >= 0", name="fiscal_attempt_nonnegative"),
)
