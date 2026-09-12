"""Add central partners and immutable billing-customer invoice snapshot.

Revision ID: 20260912_04
Revises: 20260912_03
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_04"
down_revision: Union[str, Sequence[str], None] = "20260912_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "partners_v32",
        sa.Column("partner_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("address", sa.String(length=300), server_default="", nullable=False),
        sa.Column("post_city", sa.String(length=200), server_default="", nullable=False),
        sa.Column("tax_number", sa.String(length=20), server_default="", nullable=False),
        sa.Column("phone", sa.String(length=100), server_default="", nullable=False),
        sa.Column("email", sa.String(length=300), server_default="", nullable=False),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("verified_by", sa.String(length=200), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("row_version >= 1", name="partner_version_positive"),
        sa.PrimaryKeyConstraint("partner_uuid", name="pk_partners_v32"),
    )
    op.add_column(
        "invoices_v32",
        sa.Column("billing_partner_uuid", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "invoices_v32",
        sa.Column(
            "billing_customer_json", postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
    )
    op.alter_column("invoices_v32", "billing_customer_json", server_default=None)
    op.create_foreign_key(
        "fk_invoice_billing_partner",
        "invoices_v32", "partners_v32",
        ["billing_partner_uuid"], ["partner_uuid"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_invoice_billing_partner", "invoices_v32", type_="foreignkey")
    op.drop_column("invoices_v32", "billing_customer_json")
    op.drop_column("invoices_v32", "billing_partner_uuid")
    op.drop_table("partners_v32")
