"""Add the durable full-inventory preparation barrier.

Revision ID: 20260919_22
Revises: 20260914_21
Create Date: 2026-09-19
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260919_22"
down_revision: Union[str, Sequence[str], None] = "20260914_21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "full_inventory_sessions_v32",
        sa.Column("session_uuid", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reference", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("open_slot", sa.Integer(), unique=True),
        sa.Column("barrier_sequence", sa.BigInteger(), nullable=False),
        sa.Column("inventory_epoch", sa.Integer(), nullable=False),
        sa.Column("preparation_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), nullable=False, unique=True),
        sa.Column("prepared_by_user_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("central_users_v32.user_uuid")),
        sa.Column("prepared_at_workstation_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("workstations_v32.workstation_uuid")),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activation_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), unique=True),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("closing_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), unique=True),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), unique=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_reason", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_full_inventory_sessions_v32_full_inventory_session_version")),
        sa.CheckConstraint("barrier_sequence >= 1", name=op.f("ck_full_inventory_sessions_v32_full_inventory_barrier_positive")),
        sa.CheckConstraint("inventory_epoch >= 0", name=op.f("ck_full_inventory_sessions_v32_full_inventory_epoch_nonnegative")),
        sa.CheckConstraint("status IN ('PREPARING','ACTIVE','CLOSED','CANCELLED')", name=op.f("ck_full_inventory_sessions_v32_full_inventory_session_status")),
        sa.CheckConstraint(
            "(status='PREPARING' AND activation_operation_id IS NULL "
            "AND closing_operation_id IS NULL AND cancellation_operation_id IS NULL "
            "AND activated_at IS NULL AND closed_at IS NULL AND cancelled_at IS NULL "
            "AND cancellation_reason IS NULL AND open_slot=1) OR "
            "(status='ACTIVE' AND activation_operation_id IS NOT NULL "
            "AND activated_at IS NOT NULL AND closing_operation_id IS NULL "
            "AND cancellation_operation_id IS NULL AND closed_at IS NULL "
            "AND cancelled_at IS NULL AND cancellation_reason IS NULL AND open_slot=1) OR "
            "(status='CLOSED' AND activation_operation_id IS NOT NULL "
            "AND closing_operation_id IS NOT NULL AND activated_at IS NOT NULL "
            "AND closed_at IS NOT NULL AND cancellation_operation_id IS NULL "
            "AND cancelled_at IS NULL AND cancellation_reason IS NULL AND open_slot IS NULL) OR "
            "(status='CANCELLED' AND activation_operation_id IS NULL "
            "AND closing_operation_id IS NULL AND cancellation_operation_id IS NOT NULL "
            "AND activated_at IS NULL AND closed_at IS NULL AND cancelled_at IS NOT NULL "
            "AND cancellation_reason IS NOT NULL AND open_slot IS NULL)",
            name=op.f("ck_full_inventory_sessions_v32_full_inventory_session_shape"),
        ),
    )
    op.create_table(
        "inventory_control_v32",
        sa.Column("control_id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("current_session_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("full_inventory_sessions_v32.session_uuid"), unique=True),
        sa.Column("current_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("control_id = 1", name=op.f("ck_inventory_control_v32_inventory_control_singleton")),
        sa.CheckConstraint("mode IN ('NORMAL','PREPARING','ACTIVE')", name=op.f("ck_inventory_control_v32_inventory_control_mode")),
        sa.CheckConstraint("current_epoch >= 0", name=op.f("ck_inventory_control_v32_inventory_control_epoch_nonnegative")),
        sa.CheckConstraint("row_version >= 1", name=op.f("ck_inventory_control_v32_inventory_control_version")),
        sa.CheckConstraint("(mode='NORMAL' AND current_session_uuid IS NULL) OR (mode IN ('PREPARING','ACTIVE') AND current_session_uuid IS NOT NULL)", name=op.f("ck_inventory_control_v32_inventory_control_shape")),
    )
    op.execute(
        "INSERT INTO inventory_control_v32 "
        "(control_id, mode, current_epoch, row_version) VALUES (1, 'NORMAL', 0, 1)"
    )
    op.create_table(
        "inventory_service_readiness_v32",
        sa.Column("session_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("full_inventory_sessions_v32.session_uuid"), primary_key=True),
        sa.Column("workstation_uuid", postgresql.UUID(as_uuid=True), sa.ForeignKey("workstations_v32.workstation_uuid"), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accepted_operations_v32.operation_id"), nullable=False, unique=True),
        sa.Column("pending_outbox_count", sa.Integer(), nullable=False),
        sa.Column("uncertain_outbox_count", sa.Integer(), nullable=False),
        sa.Column("sync_conflict_count", sa.Integer(), nullable=False),
        sa.Column("central_checkpoint", sa.BigInteger(), nullable=False),
        sa.Column("ack_device_sequence", sa.BigInteger(), nullable=False),
        sa.Column("local_write_lock_active", sa.Boolean(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("pending_outbox_count >= 0 AND uncertain_outbox_count >= 0 AND sync_conflict_count >= 0", name=op.f("ck_inventory_service_readiness_v32_inventory_readiness_counts_nonnegative")),
        sa.CheckConstraint("central_checkpoint >= 0", name=op.f("ck_inventory_service_readiness_v32_inventory_readiness_checkpoint_nonnegative")),
        sa.CheckConstraint("ack_device_sequence >= 1", name=op.f("ck_inventory_service_readiness_v32_inventory_readiness_sequence_positive")),
    )


def downgrade() -> None:
    op.drop_table("inventory_service_readiness_v32")
    op.drop_table("inventory_control_v32")
    op.drop_table("full_inventory_sessions_v32")
