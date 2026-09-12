"""Allocate Service consumption to released receipt lots.

Revision ID: 20260912_11
Revises: 20260912_10
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_11"
down_revision: Union[str, Sequence[str], None] = "20260912_10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_stock_allocations_v32",
        sa.Column("service_line_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("service_part_consumptions_v32.line_uuid"), primary_key=True),
        sa.Column("lot_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("inventory_lots_v32.lot_uuid"), primary_key=True),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_service_stock_allocations_v32_service_stock_allocation_quantity")),
    )


def downgrade() -> None:
    op.drop_table("service_stock_allocations_v32")
