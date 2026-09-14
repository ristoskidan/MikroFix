"""Add quarantined physical stock return workflow.

Revision ID: 20260914_21
Revises: 20260914_20
Create Date: 2026-09-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260914_21"
down_revision: Union[str, Sequence[str], None] = "20260914_20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "physical_stock_returns_v32",
        sa.Column("return_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("invoice_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices_v32.invoice_uuid"), nullable=False),
        sa.Column("invoice_line_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lot_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("inventory_lots_v32.lot_uuid"), nullable=False),
        sa.Column("article_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("inventory_items_v32.article_uuid"), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("receipt_source", sa.String(30), nullable=False),
        sa.Column("condition_note", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("receipt_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), nullable=False, unique=True),
        sa.Column("received_by_user_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
        sa.Column("received_at_workstation_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("workstations_v32.workstation_uuid")),
        sa.Column("received_device_id", sa.String(200), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), unique=True),
        sa.Column("resolution_decision", sa.String(30)),
        sa.Column("resolution_reason", sa.String(500)),
        sa.Column("resolved_by_user_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
        sa.Column("resolved_at_workstation_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("workstations_v32.workstation_uuid")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_physical_stock_returns_v32_physical_return_version_positive")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_physical_stock_returns_v32_physical_return_quantity_positive")),
        sa.CheckConstraint("receipt_source IN ('CASHIER_RETURN','SERVICE_REMOVAL')", name=op.f("ck_physical_stock_returns_v32_physical_return_receipt_source")),
        sa.CheckConstraint("status IN ('QUARANTINED','RELEASED_TO_STOCK','DEFECTIVE','CLAIM_REVIEW_REQUIRED')", name=op.f("ck_physical_stock_returns_v32_physical_return_status")),
        sa.CheckConstraint("(status='QUARANTINED' AND resolution_operation_id IS NULL AND resolution_decision IS NULL AND resolved_at IS NULL) OR (status<>'QUARANTINED' AND resolution_operation_id IS NOT NULL AND resolution_decision IS NOT NULL AND resolved_at IS NOT NULL)", name=op.f("ck_physical_stock_returns_v32_physical_return_resolution_state")),
    )
    op.create_index(
        "ix_physical_stock_return_source",
        "physical_stock_returns_v32",
        ["invoice_uuid", "invoice_line_uuid", "lot_uuid"],
    )
    op.create_index(
        "ix_physical_stock_return_status",
        "physical_stock_returns_v32", ["status", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_physical_stock_return_status", table_name="physical_stock_returns_v32")
    op.drop_index("ix_physical_stock_return_source", table_name="physical_stock_returns_v32")
    op.drop_table("physical_stock_returns_v32")
