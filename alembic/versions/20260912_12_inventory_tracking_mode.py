"""Distinguish quantity lots from globally unique serial identities.

Revision ID: 20260912_12
Revises: 20260912_11
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260912_12"
down_revision: Union[str, Sequence[str], None] = "20260912_11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_items_v32",
        sa.Column("track_mode", sa.String(20), server_default="QUANTITY", nullable=False),
    )
    op.create_check_constraint(
        "inventory_track_mode", "inventory_items_v32",
        "track_mode IN ('QUANTITY','SERIAL')",
    )
    op.add_column(
        "inventory_lots_v32", sa.Column("serial_unique_key", sa.String(300))
    )
    op.create_unique_constraint(
        "uq_inventory_lots_v32_serial_unique_key",
        "inventory_lots_v32", ["serial_unique_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_inventory_lots_v32_serial_unique_key",
        "inventory_lots_v32", type_="unique",
    )
    op.drop_column("inventory_lots_v32", "serial_unique_key")
    op.drop_constraint(
        op.f("ck_inventory_items_v32_inventory_track_mode"),
        "inventory_items_v32", type_="check",
    )
    op.drop_column("inventory_items_v32", "track_mode")
