"""Keep versioned authorization evidence for delayed offline operations.

Revision ID: 20260912_09
Revises: 20260912_08
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_09"
down_revision: Union[str, Sequence[str], None] = "20260912_08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "central_user_permission_history_v32",
        sa.Column("user_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid"), primary_key=True),
        sa.Column("permission", sa.String(100), primary_key=True),
        sa.Column("valid_from_version", sa.Integer(), primary_key=True),
        sa.Column("valid_until_version", sa.Integer()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("recorded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
        sa.CheckConstraint("valid_from_version >= 1", name=op.f("ck_central_user_permission_history_v32_permission_history_from_version")),
        sa.CheckConstraint("valid_until_version IS NULL OR valid_until_version >= valid_from_version", name=op.f("ck_central_user_permission_history_v32_permission_history_version_range")),
    )
    op.execute(
        """
        INSERT INTO central_user_permission_history_v32(
            user_uuid, permission, valid_from_version, valid_until_version
        )
        SELECT user_uuid, permission, 1, NULL
        FROM central_user_permissions_v32
        """
    )


def downgrade() -> None:
    op.drop_table("central_user_permission_history_v32")
