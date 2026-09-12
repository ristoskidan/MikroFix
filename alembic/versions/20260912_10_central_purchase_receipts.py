"""Add the central Office purchase-receipt and stock-lot model.

Revision ID: 20260912_10
Revises: 20260912_09
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_10"
down_revision: Union[str, Sequence[str], None] = "20260912_09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_items_v32",
        sa.Column("current_sale_price", sa.Numeric(18, 2), server_default="0", nullable=False),
    )
    op.create_table(
        "receipt_document_types_v32",
        sa.Column("type_code", sa.String(5), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("type_code ~ '^[A-Z]{2,5}$'", name=op.f("ck_receipt_document_types_v32_receipt_document_type_code")),
    )
    op.create_table(
        "receipt_number_sequences_v32",
        sa.Column("calendar_year", sa.Integer(), primary_key=True),
        sa.Column("series_code", sa.String(5), primary_key=True),
        sa.Column("last_number", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("calendar_year >= 2020", name=op.f("ck_receipt_number_sequences_v32_receipt_sequence_year")),
        sa.CheckConstraint("last_number >= 1", name=op.f("ck_receipt_number_sequences_v32_receipt_sequence_positive")),
    )
    op.create_table(
        "purchase_receipts_v32",
        sa.Column("receipt_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("receipt_number", sa.String(40), nullable=False, unique=True),
        sa.Column("document_type", sa.String(10), nullable=False),
        sa.Column("series_code", sa.String(5), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("supplier_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("partners_v32.partner_uuid"), nullable=False),
        sa.Column("supplier_version", sa.Integer(), nullable=False),
        sa.Column("supplier_snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column("external_document_no", sa.String(200), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="EUR", nullable=False),
        sa.Column("document_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("shipping_total", sa.Numeric(18, 4), nullable=False),
        sa.Column("invoice_receipt_number", sa.String(40), unique=True),
        sa.Column("invoice_external_no", sa.String(200)),
        sa.Column("invoice_date", sa.Date()),
        sa.Column("invoice_due_date", sa.Date()),
        sa.Column("invoice_payment_status", sa.String(20)),
        sa.Column("confirmed_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), nullable=False, unique=True),
        sa.Column("release_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), unique=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("supplier_uuid", "document_type", "external_document_no", name="uq_purchase_receipt_supplier_document"),
        sa.UniqueConstraint("supplier_uuid", "invoice_external_no", name="uq_purchase_receipt_supplier_invoice"),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_purchase_receipts_v32_purchase_receipt_version")),
        sa.CheckConstraint("document_type IN ('DOB','DRA','CUSTOM')", name=op.f("ck_purchase_receipts_v32_purchase_receipt_document_type")),
        sa.CheckConstraint("status IN ('PENDING_INVOICE','RELEASED')", name=op.f("ck_purchase_receipts_v32_purchase_receipt_status")),
        sa.CheckConstraint("currency = 'EUR'", name=op.f("ck_purchase_receipts_v32_purchase_receipt_currency")),
        sa.CheckConstraint("document_total >= 0 AND shipping_total >= 0", name=op.f("ck_purchase_receipts_v32_purchase_receipt_totals")),
    )
    op.create_table(
        "purchase_receipt_revisions_v32",
        sa.Column("receipt_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_receipts_v32.receipt_uuid"), primary_key=True),
        sa.Column("row_version", sa.Integer(), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), nullable=False, unique=True),
        sa.Column("snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column("actor_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
        sa.Column("workstation_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("workstations_v32.workstation_uuid")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_purchase_receipt_revisions_v32_purchase_receipt_revision_version")),
    )
    op.create_table(
        "purchase_receipt_lines_v32",
        sa.Column("line_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("receipt_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_receipts_v32.receipt_uuid"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("article_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("inventory_items_v32.article_uuid"), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("net_unit_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("discount_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("vat_rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("vat_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("calculated_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("supplier_line_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("allocated_shipping", sa.Numeric(18, 4), nullable=False),
        sa.Column("landed_unit_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("confirmed_sale_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("supplier_barcode", sa.String(200), nullable=False),
        sa.Column("lot_serial", sa.String(300), nullable=False),
        sa.UniqueConstraint("receipt_uuid", "line_no", name="uq_purchase_receipt_line_no"),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_purchase_receipt_lines_v32_purchase_receipt_line_quantity")),
    )
    op.create_table(
        "inventory_identities_v32",
        sa.Column("identity_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("article_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("inventory_items_v32.article_uuid"), nullable=False),
        sa.Column("supplier_barcode", sa.String(200), nullable=False, unique=True),
        sa.Column("created_from_receipt_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_receipts_v32.receipt_uuid"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "inventory_lots_v32",
        sa.Column("lot_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("receipt_line_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_receipt_lines_v32.line_uuid"), nullable=False, unique=True),
        sa.Column("identity_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("inventory_identities_v32.identity_uuid"), nullable=False),
        sa.Column("article_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("inventory_items_v32.article_uuid"), nullable=False),
        sa.Column("lot_serial", sa.String(300), nullable=False),
        sa.Column("received_quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("available_quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("landed_unit_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("sale_price_snapshot", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("received_quantity > 0", name=op.f("ck_inventory_lots_v32_inventory_lot_received_quantity")),
        sa.CheckConstraint("available_quantity >= 0", name=op.f("ck_inventory_lots_v32_inventory_lot_available_quantity")),
        sa.CheckConstraint("status IN ('PENDING','AVAILABLE','CONSUMED')", name=op.f("ck_inventory_lots_v32_inventory_lot_status")),
    )
    op.create_table(
        "inventory_price_history_v32",
        sa.Column("price_history_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("article_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("inventory_items_v32.article_uuid"), nullable=False),
        sa.Column("receipt_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_receipts_v32.receipt_uuid"), nullable=False),
        sa.Column("previous_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("confirmed_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("highest_available_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
    )
    op.create_table(
        "receipt_attachments_v32",
        sa.Column("attachment_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("receipt_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_receipts_v32.receipt_uuid"), nullable=False),
        sa.Column("receipt_version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("original_name", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
        sa.UniqueConstraint("receipt_uuid", "sha256", name="uq_receipt_attachment_digest"),
        sa.CheckConstraint("size_bytes BETWEEN 1 AND 26214400", name=op.f("ck_receipt_attachments_v32_receipt_attachment_size")),
    )


def downgrade() -> None:
    op.drop_table("receipt_attachments_v32")
    op.drop_table("inventory_price_history_v32")
    op.drop_table("inventory_lots_v32")
    op.drop_table("inventory_identities_v32")
    op.drop_table("purchase_receipt_lines_v32")
    op.drop_table("purchase_receipt_revisions_v32")
    op.drop_table("purchase_receipts_v32")
    op.drop_table("receipt_number_sequences_v32")
    op.drop_table("receipt_document_types_v32")
    op.drop_column("inventory_items_v32", "current_sale_price")
