"""Add auditable late payment state to central invoices.

Revision ID: 20260913_18
Revises: 20260913_17
Create Date: 2026-09-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260913_18"
down_revision: Union[str, Sequence[str], None] = "20260913_17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invoices_v32",
        sa.Column("settlement_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "invoices_v32",
        sa.Column("payment_received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "invoices_v32",
        sa.Column("payment_received_method", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "invoices_v32",
        sa.Column("fiscal_submission_deadline", sa.Date(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_invoices_v32_settlement_operation_id_accepted_operations_v32"),
        "invoices_v32", "accepted_operations_v32",
        ["settlement_operation_id"], ["operation_id"],
    )
    op.create_unique_constraint(
        op.f("uq_invoices_v32_settlement_operation_id"),
        "invoices_v32", ["settlement_operation_id"],
    )
    op.create_check_constraint(
        op.f("ck_invoices_v32_invoice_received_method"),
        "invoices_v32",
        "payment_received_method IS NULL OR payment_received_method IN "
        "('BANK_TRANSFER','CASH','CARD')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_invoices_v32_invoice_received_method"),
        "invoices_v32", type_="check",
    )
    op.drop_constraint(
        op.f("uq_invoices_v32_settlement_operation_id"),
        "invoices_v32", type_="unique",
    )
    op.drop_constraint(
        op.f("fk_invoices_v32_settlement_operation_id_accepted_operations_v32"),
        "invoices_v32", type_="foreignkey",
    )
    op.drop_column("invoices_v32", "fiscal_submission_deadline")
    op.drop_column("invoices_v32", "payment_received_method")
    op.drop_column("invoices_v32", "payment_received_at")
    op.drop_column("invoices_v32", "settlement_operation_id")
