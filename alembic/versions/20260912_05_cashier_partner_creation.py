"""Record reviewed partner origin and safe tax-number identity.

Revision ID: 20260912_05
Revises: 20260912_04
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260912_05"
down_revision: Union[str, Sequence[str], None] = "20260912_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("partners_v32", sa.Column("country", sa.String(100), server_default="Slovenija", nullable=False))
    op.add_column("partners_v32", sa.Column("tax_number_normalized", sa.String(8)))
    op.add_column("partners_v32", sa.Column("registration_number", sa.String(30), server_default="", nullable=False))
    op.add_column("partners_v32", sa.Column("verification_source", sa.String(30), server_default="MANUAL_REVIEW", nullable=False))
    op.add_column("partners_v32", sa.Column("source_reference", sa.String(300), server_default="", nullable=False))
    op.create_unique_constraint(
        "uq_partners_v32_tax_number_normalized",
        "partners_v32", ["tax_number_normalized"],
    )
    op.create_check_constraint(
        "partner_verification_source", "partners_v32",
        "verification_source IN ('MANUAL_REVIEW','AJPES_REVIEW')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_partners_v32_partner_verification_source"), "partners_v32", type_="check")
    op.drop_constraint("uq_partners_v32_tax_number_normalized", "partners_v32", type_="unique")
    op.drop_column("partners_v32", "source_reference")
    op.drop_column("partners_v32", "verification_source")
    op.drop_column("partners_v32", "registration_number")
    op.drop_column("partners_v32", "tax_number_normalized")
    op.drop_column("partners_v32", "country")
