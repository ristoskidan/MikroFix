"""Separate service technical, billing and physical-custody state.

Revision ID: 20260920_23
Revises: 20260919_22
Create Date: 2026-09-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260920_23"
down_revision: Union[str, Sequence[str], None] = "20260919_22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "service_orders_v32",
        sa.Column("custody_status", sa.String(20), nullable=False, server_default="AT_MIKROFIX"),
    )
    op.add_column(
        "service_orders_v32",
        sa.Column("ready_for_pickup_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "service_orders_v32",
        sa.Column("picked_up_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "service_orders_v32",
        sa.Column("pickup_operation_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_service_orders_v32_pickup_operation",
        "service_orders_v32", "accepted_operations_v32",
        ["pickup_operation_id"], ["operation_id"],
    )
    op.create_unique_constraint(
        "uq_service_orders_v32_pickup_operation_id",
        "service_orders_v32", ["pickup_operation_id"],
    )
    op.execute(
        "UPDATE service_orders_v32 AS service "
        "SET ready_for_pickup_at=completion.completed_at "
        "FROM technical_completions_v32 AS completion "
        "WHERE completion.service_order_uuid=service.sync_uuid"
    )
    op.create_check_constraint(
        "service_order_custody_status", "service_orders_v32",
        "custody_status IN ('AT_MIKROFIX','RETURNED')",
    )
    op.create_check_constraint(
        "service_order_custody_shape", "service_orders_v32",
        "(custody_status='AT_MIKROFIX' AND picked_up_at IS NULL "
        "AND pickup_operation_id IS NULL) OR "
        "(custody_status='RETURNED' AND ready_for_pickup_at IS NOT NULL "
        "AND picked_up_at IS NOT NULL AND pickup_operation_id IS NOT NULL)",
    )
    op.create_index(
        "ix_service_orders_ready_pickup_v32", "service_orders_v32",
        ["ready_for_pickup_at", "sync_uuid"],
        postgresql_where=sa.text(
            "ready_for_pickup_at IS NOT NULL AND custody_status='AT_MIKROFIX'"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_service_orders_ready_pickup_v32", table_name="service_orders_v32")
    op.drop_constraint("service_order_custody_shape", "service_orders_v32", type_="check")
    op.drop_constraint("service_order_custody_status", "service_orders_v32", type_="check")
    op.drop_constraint(
        "uq_service_orders_v32_pickup_operation_id",
        "service_orders_v32", type_="unique",
    )
    op.drop_constraint(
        "fk_service_orders_v32_pickup_operation",
        "service_orders_v32", type_="foreignkey",
    )
    op.drop_column("service_orders_v32", "pickup_operation_id")
    op.drop_column("service_orders_v32", "picked_up_at")
    op.drop_column("service_orders_v32", "ready_for_pickup_at")
    op.drop_column("service_orders_v32", "custody_status")
