"""Allow atomic central invoices for direct Cashier stock sales.

Revision ID: 20260912_13
Revises: 20260912_12
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_13"
down_revision: Union[str, Sequence[str], None] = "20260912_12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("invoices_v32", "draft_uuid", nullable=True)
    op.alter_column("invoices_v32", "service_order_uuid", nullable=True)
    op.add_column(
        "invoices_v32",
        sa.Column("source_type", sa.String(20), server_default="SERVICE", nullable=False),
    )
    op.add_column(
        "invoices_v32",
        sa.Column("direct_sale_uuid", postgresql.UUID(as_uuid=True)),
    )
    op.create_unique_constraint(
        "uq_invoices_v32_direct_sale_uuid", "invoices_v32", ["direct_sale_uuid"]
    )
    op.create_check_constraint(
        "invoice_source_identity", "invoices_v32",
        "(source_type='SERVICE' AND draft_uuid IS NOT NULL "
        "AND service_order_uuid IS NOT NULL AND direct_sale_uuid IS NULL) OR "
        "(source_type='DIRECT_SALE' AND draft_uuid IS NULL "
        "AND service_order_uuid IS NULL AND direct_sale_uuid IS NOT NULL)",
    )
    op.create_table(
        "direct_sale_stock_allocations_v32",
        sa.Column(
            "invoice_uuid", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices_v32.invoice_uuid"), primary_key=True,
        ),
        sa.Column("sale_line_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lot_uuid", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inventory_lots_v32.lot_uuid"), primary_key=True,
        ),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("landed_unit_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint("quantity > 0", name="direct_sale_allocation_quantity"),
        sa.CheckConstraint(
            "landed_unit_cost >= 0", name="direct_sale_allocation_cost"
        ),
    )


def downgrade() -> None:
    op.drop_table("direct_sale_stock_allocations_v32")
    op.drop_constraint(
        op.f("ck_invoices_v32_invoice_source_identity"),
        "invoices_v32", type_="check",
    )
    op.drop_constraint(
        "uq_invoices_v32_direct_sale_uuid", "invoices_v32", type_="unique"
    )
    op.drop_column("invoices_v32", "direct_sale_uuid")
    op.drop_column("invoices_v32", "source_type")
    op.alter_column("invoices_v32", "service_order_uuid", nullable=False)
    op.alter_column("invoices_v32", "draft_uuid", nullable=False)
