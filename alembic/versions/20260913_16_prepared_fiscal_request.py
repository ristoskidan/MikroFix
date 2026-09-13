"""Persist the exact prepared FURS request before network submission.

Revision ID: 20260913_16
Revises: 20260913_15
Create Date: 2026-09-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260913_16"
down_revision: Union[str, Sequence[str], None] = "20260913_15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "fiscal_outbox_v32",
        sa.Column("prepared_request_sha256", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "fiscal_outbox_v32",
        sa.Column(
            "prepared_request_json", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "fiscal_outbox_v32",
        sa.Column("prepared_zoi", sa.String(64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("fiscal_outbox_v32", "prepared_zoi")
    op.drop_column("fiscal_outbox_v32", "prepared_request_json")
    op.drop_column("fiscal_outbox_v32", "prepared_request_sha256")
