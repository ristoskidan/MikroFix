"""Preserve signed original actor for offline Service operations.

Revision ID: 20260912_07
Revises: 20260912_06
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_07"
down_revision: Union[str, Sequence[str], None] = "20260912_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workstations_v32", sa.Column("signing_public_key", sa.LargeBinary()))
    op.add_column("accepted_operations_v32", sa.Column("actor_user_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")))
    op.add_column("accepted_operations_v32", sa.Column("actor_authorization_version", sa.Integer(), server_default="0", nullable=False))
    op.add_column("accepted_operations_v32", sa.Column("transmitted_by_user_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")))
    op.add_column("accepted_operations_v32", sa.Column("transmitted_by_workstation_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("workstations_v32.workstation_uuid")))
    op.add_column("accepted_operations_v32", sa.Column("workstation_signature", sa.String(200), server_default="", nullable=False))
    op.create_check_constraint(
        "actor_authorization_version_nonnegative", "accepted_operations_v32",
        "actor_authorization_version >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_accepted_operations_v32_actor_authorization_version_nonnegative"), "accepted_operations_v32", type_="check")
    op.drop_column("accepted_operations_v32", "workstation_signature")
    op.drop_column("accepted_operations_v32", "transmitted_by_workstation_uuid")
    op.drop_column("accepted_operations_v32", "transmitted_by_user_uuid")
    op.drop_column("accepted_operations_v32", "actor_authorization_version")
    op.drop_column("accepted_operations_v32", "actor_user_uuid")
    op.drop_column("workstations_v32", "signing_public_key")
