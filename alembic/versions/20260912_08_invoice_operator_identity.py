"""Snapshot the centrally authenticated FURS operator identity.

Revision ID: 20260912_08
Revises: 20260912_07
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260912_08"
down_revision: Union[str, Sequence[str], None] = "20260912_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invoices_v32", sa.Column("operator_tax_number", sa.String(8)))
    op.create_check_constraint(
        "invoice_operator_tax_number", "invoices_v32",
        "operator_tax_number IS NULL OR operator_tax_number ~ '^[0-9]{8}$'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_invoices_v32_invoice_operator_tax_number"), "invoices_v32", type_="check")
    op.drop_column("invoices_v32", "operator_tax_number")
