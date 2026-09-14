"""Allow auditable Office price decisions outside purchase receipts.

Revision ID: 20260914_19
Revises: 20260913_18
Create Date: 2026-09-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260914_19"
down_revision: Union[str, Sequence[str], None] = "20260913_18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "inventory_price_history_v32", "receipt_uuid",
        existing_type=postgresql.UUID(as_uuid=True), nullable=True,
    )
    op.add_column(
        "inventory_price_history_v32",
        sa.Column("price_change_operation_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "inventory_price_history_v32",
        sa.Column("change_reason", sa.String(length=500)),
    )
    op.create_foreign_key(
        op.f("fk_inventory_price_history_v32_price_change_operation_id_accepted_operations_v32"),
        "inventory_price_history_v32", "accepted_operations_v32",
        ["price_change_operation_id"], ["operation_id"],
    )
    op.create_unique_constraint(
        op.f("uq_inventory_price_history_v32_price_change_operation_id"),
        "inventory_price_history_v32", ["price_change_operation_id"],
    )
    op.create_check_constraint(
        op.f("ck_inventory_price_history_v32_inventory_price_history_one_source"),
        "inventory_price_history_v32",
        "(receipt_uuid IS NOT NULL AND price_change_operation_id IS NULL) OR "
        "(receipt_uuid IS NULL AND price_change_operation_id IS NOT NULL)",
    )


def downgrade() -> None:
    # Older schema can only represent receipt-origin price history.
    op.execute(
        "DELETE FROM inventory_price_history_v32 "
        "WHERE price_change_operation_id IS NOT NULL"
    )
    op.drop_constraint(
        op.f("ck_inventory_price_history_v32_inventory_price_history_one_source"),
        "inventory_price_history_v32", type_="check",
    )
    op.drop_constraint(
        op.f("uq_inventory_price_history_v32_price_change_operation_id"),
        "inventory_price_history_v32", type_="unique",
    )
    op.drop_constraint(
        op.f("fk_inventory_price_history_v32_price_change_operation_id_accepted_operations_v32"),
        "inventory_price_history_v32", type_="foreignkey",
    )
    op.drop_column("inventory_price_history_v32", "change_reason")
    op.drop_column("inventory_price_history_v32", "price_change_operation_id")
    op.alter_column(
        "inventory_price_history_v32", "receipt_uuid",
        existing_type=postgresql.UUID(as_uuid=True), nullable=False,
    )
