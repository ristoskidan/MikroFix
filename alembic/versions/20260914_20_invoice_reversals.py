"""Add central immutable invoice reversal documents.

Revision ID: 20260914_20
Revises: 20260914_19
Create Date: 2026-09-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260914_20"
down_revision: Union[str, Sequence[str], None] = "20260914_19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invoices_v32",
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "invoices_v32",
        sa.Column("reversal_of_invoice_uuid", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "invoices_v32", sa.Column("storno_reason", sa.String(length=500)),
    )
    op.create_foreign_key(
        op.f("fk_invoices_v32_reversal_of_invoice_uuid_invoices_v32"),
        "invoices_v32", "invoices_v32",
        ["reversal_of_invoice_uuid"], ["invoice_uuid"],
    )
    op.create_unique_constraint(
        op.f("uq_invoices_v32_reversal_of_invoice_uuid"),
        "invoices_v32", ["reversal_of_invoice_uuid"],
    )
    op.drop_constraint(
        op.f("ck_invoices_v32_invoice_status"), "invoices_v32", type_="check",
    )
    op.create_check_constraint(
        op.f("ck_invoices_v32_invoice_status"), "invoices_v32",
        "status IN ('PENDING_FISCALIZATION','FISCAL_RECONCILIATION',"
        "'ISSUED','REVERSAL_PENDING','REVERSED')",
    )
    op.drop_constraint(
        op.f("ck_invoices_v32_invoice_total_nonnegative"),
        "invoices_v32", type_="check",
    )
    op.create_check_constraint(
        op.f("ck_invoices_v32_invoice_total_sign"), "invoices_v32",
        "(source_type IN ('SERVICE','DIRECT_SALE') AND total_amount >= 0) OR "
        "(source_type='REVERSAL' AND total_amount <= 0)",
    )
    op.drop_constraint(
        op.f("ck_invoices_v32_invoice_source_identity"),
        "invoices_v32", type_="check",
    )
    op.create_check_constraint(
        op.f("ck_invoices_v32_invoice_source_identity"), "invoices_v32",
        "(source_type='SERVICE' AND draft_uuid IS NOT NULL "
        "AND service_order_uuid IS NOT NULL AND direct_sale_uuid IS NULL "
        "AND reversal_of_invoice_uuid IS NULL) OR "
        "(source_type='DIRECT_SALE' AND draft_uuid IS NULL "
        "AND service_order_uuid IS NULL AND direct_sale_uuid IS NOT NULL "
        "AND reversal_of_invoice_uuid IS NULL) OR "
        "(source_type='REVERSAL' AND draft_uuid IS NULL "
        "AND service_order_uuid IS NULL AND direct_sale_uuid IS NULL "
        "AND reversal_of_invoice_uuid IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_invoices_v32_invoice_version_positive"), "invoices_v32",
        "row_version >= 1",
    )


def downgrade() -> None:
    # A downgrade cannot represent reversals; refuse while any exist.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM invoices_v32 "
        "WHERE source_type='REVERSAL') THEN "
        "RAISE EXCEPTION 'cannot downgrade while invoice reversals exist'; "
        "END IF; END $$;"
    )
    op.drop_constraint(
        op.f("ck_invoices_v32_invoice_version_positive"),
        "invoices_v32", type_="check",
    )
    op.drop_constraint(
        op.f("ck_invoices_v32_invoice_source_identity"),
        "invoices_v32", type_="check",
    )
    op.create_check_constraint(
        op.f("ck_invoices_v32_invoice_source_identity"), "invoices_v32",
        "(source_type='SERVICE' AND draft_uuid IS NOT NULL "
        "AND service_order_uuid IS NOT NULL AND direct_sale_uuid IS NULL) OR "
        "(source_type='DIRECT_SALE' AND draft_uuid IS NULL "
        "AND service_order_uuid IS NULL AND direct_sale_uuid IS NOT NULL)",
    )
    op.drop_constraint(
        op.f("ck_invoices_v32_invoice_total_sign"), "invoices_v32", type_="check",
    )
    op.create_check_constraint(
        op.f("ck_invoices_v32_invoice_total_nonnegative"), "invoices_v32",
        "total_amount >= 0",
    )
    op.drop_constraint(
        op.f("ck_invoices_v32_invoice_status"), "invoices_v32", type_="check",
    )
    op.create_check_constraint(
        op.f("ck_invoices_v32_invoice_status"), "invoices_v32",
        "status IN ('PENDING_FISCALIZATION','FISCAL_RECONCILIATION','ISSUED')",
    )
    op.drop_constraint(
        op.f("uq_invoices_v32_reversal_of_invoice_uuid"),
        "invoices_v32", type_="unique",
    )
    op.drop_constraint(
        op.f("fk_invoices_v32_reversal_of_invoice_uuid_invoices_v32"),
        "invoices_v32", type_="foreignkey",
    )
    op.drop_column("invoices_v32", "storno_reason")
    op.drop_column("invoices_v32", "reversal_of_invoice_uuid")
    op.drop_column("invoices_v32", "row_version")
