"""Add central technical completion, consumption, stock and invoice drafts.

Revision ID: 20260912_02
Revises: 20260911_01
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_02"
down_revision: Union[str, Sequence[str], None] = "20260911_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_items_v32",
        sa.Column("article_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("barcode", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("quantity_on_hand", sa.Numeric(18, 4), nullable=False),
        sa.Column("blocked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("blocked_reason", sa.String(length=500), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity_on_hand >= 0", name="inventory_quantity_nonnegative"),
        sa.CheckConstraint("row_version >= 1", name="inventory_version_positive"),
        sa.PrimaryKeyConstraint("article_uuid", name="pk_inventory_items_v32"),
        sa.UniqueConstraint("barcode", name="uq_inventory_items_v32_barcode"),
    )
    op.create_table(
        "technical_completions_v32",
        sa.Column("service_order_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["accepted_operations_v32.operation_id"],
            name="fk_tech_completion_operation",
        ),
        sa.ForeignKeyConstraint(
            ["service_order_uuid"], ["service_orders_v32.sync_uuid"],
            name="fk_tech_completion_order",
        ),
        sa.PrimaryKeyConstraint("service_order_uuid", name="pk_technical_completions_v32"),
        sa.UniqueConstraint("operation_id", name="uq_technical_completions_v32_operation_id"),
    )
    op.create_table(
        "service_part_consumptions_v32",
        sa.Column("line_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_order_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("article_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("barcode", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("lot_serial", sa.String(length=300), server_default="", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name="service_consumption_quantity_positive"),
        sa.ForeignKeyConstraint(
            ["article_uuid"], ["inventory_items_v32.article_uuid"],
            name="fk_service_consumption_article",
        ),
        sa.ForeignKeyConstraint(
            ["service_order_uuid"], ["service_orders_v32.sync_uuid"],
            name="fk_service_consumption_order",
        ),
        sa.PrimaryKeyConstraint("line_uuid", name="pk_service_part_consumptions_v32"),
        sa.UniqueConstraint(
            "service_order_uuid", "line_uuid", name="uq_service_consumption_line"
        ),
    )
    op.create_table(
        "invoice_drafts_v32",
        sa.Column("draft_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_order_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('READY','INVOICED')", name="invoice_draft_status"),
        sa.ForeignKeyConstraint(
            ["service_order_uuid"], ["service_orders_v32.sync_uuid"],
            name="fk_invoice_draft_order",
        ),
        sa.PrimaryKeyConstraint("draft_uuid", name="pk_invoice_drafts_v32"),
        sa.UniqueConstraint("service_order_uuid", name="uq_invoice_drafts_v32_service_order_uuid"),
    )


def downgrade() -> None:
    op.drop_table("invoice_drafts_v32")
    op.drop_table("service_part_consumptions_v32")
    op.drop_table("technical_completions_v32")
    op.drop_table("inventory_items_v32")
