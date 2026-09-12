"""Keep requested direct-sale discounts in an internal audit trail.

Revision ID: 20260912_14
Revises: 20260912_13
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_14"
down_revision: Union[str, Sequence[str], None] = "20260912_13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "direct_sale_discount_audit_v32",
        sa.Column(
            "invoice_uuid", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices_v32.invoice_uuid"), primary_key=True,
        ),
        sa.Column("sale_line_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("requested_discount_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("applied_discount_percent", sa.Numeric(7, 4), nullable=False),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "requested_discount_percent BETWEEN 0 AND 100",
            name="direct_sale_discount_requested_range",
        ),
        sa.CheckConstraint(
            "applied_discount_percent BETWEEN 0 AND 100",
            name="direct_sale_discount_applied_range",
        ),
        sa.CheckConstraint(
            "decision IN ('AS_REQUESTED','CLAMPED_TO_COST_FLOOR')",
            name="direct_sale_discount_decision",
        ),
    )


def downgrade() -> None:
    op.drop_table("direct_sale_discount_audit_v32")
