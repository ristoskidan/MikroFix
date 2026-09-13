"""Add versioned central T1/T2 tariffs for direct Cashier labor.

Revision ID: 20260913_15
Revises: 20260912_14
Create Date: 2026-09-13
"""

from datetime import date
from typing import Sequence, Union
from uuid import UUID

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260913_15"
down_revision: Union[str, Sequence[str], None] = "20260912_14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_direct_sale_discount_audit_v32_direct_sale_discount_decision"),
        "direct_sale_discount_audit_v32", type_="check",
    )
    op.create_check_constraint(
        "direct_sale_discount_decision", "direct_sale_discount_audit_v32",
        "decision IN ('AS_REQUESTED','CLAMPED_TO_MINIMUM')",
    )
    table = op.create_table(
        "service_tariffs_v32",
        sa.Column("tariff_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tier", sa.String(2), nullable=False),
        sa.Column("tariff_version", sa.String(80), nullable=False),
        sa.Column("first_hour_rate", sa.Numeric(18, 2), nullable=False),
        sa.Column("extra_half_hour_rate", sa.Numeric(18, 2), nullable=False),
        sa.Column("minimum_line_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tier", "tariff_version", name="uq_service_tariff_version"),
        sa.CheckConstraint("tier IN ('T1','T2')", name="service_tariff_tier"),
        sa.CheckConstraint("first_hour_rate > 0", name="service_tariff_first_rate"),
        sa.CheckConstraint("extra_half_hour_rate >= 0", name="service_tariff_extra_rate"),
        sa.CheckConstraint("minimum_line_price > 0", name="service_tariff_minimum"),
    )
    op.create_index(
        "uq_active_service_tariff_tier", "service_tariffs_v32", ["tier"],
        unique=True, postgresql_where=sa.text("active IS TRUE"),
    )
    op.bulk_insert(table, [
        {
            "tariff_uuid": UUID("10000000-0000-0000-0000-000000000001"),
            "tier": "T1", "tariff_version": "T1T2-v1-2026-09-04",
            "first_hour_rate": 30, "extra_half_hour_rate": 15,
            "minimum_line_price": 22, "effective_from": date(2026, 9, 4),
            "active": True,
        },
        {
            "tariff_uuid": UUID("20000000-0000-0000-0000-000000000001"),
            "tier": "T2", "tariff_version": "T1T2-v1-2026-09-04",
            "first_hour_rate": 90, "extra_half_hour_rate": 15,
            "minimum_line_price": 22, "effective_from": date(2026, 9, 4),
            "active": True,
        },
    ])


def downgrade() -> None:
    op.drop_index("uq_active_service_tariff_tier", table_name="service_tariffs_v32")
    op.drop_table("service_tariffs_v32")
    op.drop_constraint(
        op.f("ck_direct_sale_discount_audit_v32_direct_sale_discount_decision"),
        "direct_sale_discount_audit_v32", type_="check",
    )
    op.create_check_constraint(
        "direct_sale_discount_decision", "direct_sale_discount_audit_v32",
        "decision IN ('AS_REQUESTED','CLAMPED_TO_COST_FLOOR')",
    )
