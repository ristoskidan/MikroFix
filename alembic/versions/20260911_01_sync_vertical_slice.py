"""Create the first central synchronization tables.

Revision ID: 20260911_01
Revises:
Create Date: 2026-09-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260911_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_orders_v32",
        sa.Column("sync_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("origin_device_id", sa.String(length=200), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("row_version >= 1", name="row_version_positive"),
        sa.PrimaryKeyConstraint("sync_uuid", name="pk_service_orders_v32"),
    )
    op.create_table(
        "change_feed_v32",
        sa.Column(
            "central_sequence",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("aggregate_version >= 1", name="aggregate_version_positive"),
        sa.PrimaryKeyConstraint("central_sequence", name="pk_change_feed_v32"),
        sa.UniqueConstraint("event_id", name="uq_change_feed_v32_event_id"),
    )
    op.create_table(
        "accepted_operations_v32",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("device_id", sa.String(length=200), nullable=False),
        sa.Column("device_sequence", sa.BigInteger(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_code", sa.String(length=40), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column("central_sequence", sa.BigInteger(), nullable=True),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("device_sequence >= 1", name="device_sequence_positive"),
        sa.CheckConstraint("expected_version >= 0", name="expected_version_nonnegative"),
        sa.PrimaryKeyConstraint("operation_id", name="pk_accepted_operations_v32"),
        sa.UniqueConstraint("device_id", "device_sequence", name="uq_operation_device_sequence"),
    )


def downgrade() -> None:
    op.drop_table("accepted_operations_v32")
    op.drop_table("change_feed_v32")
    op.drop_table("service_orders_v32")
