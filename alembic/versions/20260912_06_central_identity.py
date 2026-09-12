"""Add central users, workstation identities, sessions and audit trail.

Revision ID: 20260912_06
Revises: 20260912_05
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_06"
down_revision: Union[str, Sequence[str], None] = "20260912_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "central_users_v32",
        sa.Column("user_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("username_normalized", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("pin_salt", sa.LargeBinary(), nullable=False),
        sa.Column("pin_hash", sa.LargeBinary(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("must_change_pin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("operator_tax_number", sa.String(8)),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('ADMIN','SERVICE','OFFICE','BUYBACK','CASHIER')", name=op.f("ck_central_users_v32_central_user_role")),
        sa.CheckConstraint("failed_attempts >= 0", name=op.f("ck_central_users_v32_central_user_failed_attempts")),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_central_users_v32_central_user_version")),
        sa.CheckConstraint("operator_tax_number IS NULL OR operator_tax_number ~ '^[0-9]{8}$'", name=op.f("ck_central_users_v32_central_user_operator_tax_number")),
        sa.UniqueConstraint("username_normalized", name=op.f("uq_central_users_v32_username_normalized")),
    )
    op.create_table(
        "central_user_permissions_v32",
        sa.Column("user_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid"), primary_key=True),
        sa.Column("permission", sa.String(100), primary_key=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
    )
    op.create_table(
        "workstations_v32",
        sa.Column("workstation_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workstation_code", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("application_type", sa.String(20), nullable=False),
        sa.Column("credential_hash", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("application_type IN ('SERVICE','CASHIER','OFFICE')", name=op.f("ck_workstations_v32_workstation_application_type")),
        sa.UniqueConstraint("workstation_code", name=op.f("uq_workstations_v32_workstation_code")),
    )
    op.create_table(
        "central_sessions_v32",
        sa.Column("session_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid"), nullable=False),
        sa.Column("workstation_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("workstations_v32.workstation_uuid"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("token_hash", name=op.f("uq_central_sessions_v32_token_hash")),
    )
    op.create_table(
        "central_audit_events_v32",
        sa.Column("audit_sequence", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("actor_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
        sa.Column("workstation_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("workstations_v32.workstation_uuid")),
        sa.Column("entity_type", sa.String(100)),
        sa.Column("entity_id", sa.String(200)),
        sa.Column("details_json", postgresql.JSONB(), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    op.drop_table("central_audit_events_v32")
    op.drop_table("central_sessions_v32")
    op.drop_table("workstations_v32")
    op.drop_table("central_user_permissions_v32")
    op.drop_table("central_users_v32")
