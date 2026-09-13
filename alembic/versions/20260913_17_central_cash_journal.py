"""Add central cash sessions and immutable journal entries.

Revision ID: 20260913_17
Revises: 20260913_16
Create Date: 2026-09-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260913_17"
down_revision: Union[str, Sequence[str], None] = "20260913_16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cash_session_number_sequences_v32",
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("last_number", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("fiscal_year >= 2020", name=op.f("ck_cash_session_number_sequences_v32_cash_session_sequence_year")),
        sa.CheckConstraint("last_number >= 1", name=op.f("ck_cash_session_number_sequences_v32_cash_session_sequence_positive")),
        sa.PrimaryKeyConstraint("fiscal_year", name=op.f("pk_cash_session_number_sequences_v32")),
    )
    op.create_table(
        "cash_sessions_v32",
        sa.Column("session_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_no", sa.String(length=40), nullable=False),
        sa.Column("premise_id", sa.String(length=40), nullable=False),
        sa.Column("electronic_device_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_by_user_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("opened_by", sa.String(length=200), nullable=False),
        sa.Column("opened_by_workstation_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("opening_cash", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by_user_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("closed_by", sa.String(length=200), nullable=True),
        sa.Column("closed_by_workstation_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expected_cash", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("counted_cash", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("difference", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("close_note", sa.String(length=1000), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(status='OPEN' AND closed_at IS NULL AND closed_by IS NULL AND expected_cash IS NULL AND counted_cash IS NULL AND difference IS NULL) OR (status='CLOSED' AND closed_at IS NOT NULL AND closed_by IS NOT NULL AND expected_cash IS NOT NULL AND counted_cash IS NOT NULL AND difference IS NOT NULL)",
            name=op.f("ck_cash_sessions_v32_cash_session_close_shape"),
        ),
        sa.CheckConstraint("opening_cash >= 0", name=op.f("ck_cash_sessions_v32_cash_session_opening_nonnegative")),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name=op.f("ck_cash_sessions_v32_cash_session_status")),
        sa.CheckConstraint("row_version IN (1,2)", name=op.f("ck_cash_sessions_v32_cash_session_version")),
        sa.ForeignKeyConstraint(["closed_by_user_uuid"], ["central_users_v32.user_uuid"], name=op.f("fk_cash_sessions_v32_closed_by_user_uuid_central_users_v32")),
        sa.ForeignKeyConstraint(["closed_by_workstation_uuid"], ["workstations_v32.workstation_uuid"], name=op.f("fk_cash_sessions_v32_closed_by_workstation_uuid_workstations_v32")),
        sa.ForeignKeyConstraint(["opened_by_user_uuid"], ["central_users_v32.user_uuid"], name=op.f("fk_cash_sessions_v32_opened_by_user_uuid_central_users_v32")),
        sa.ForeignKeyConstraint(["opened_by_workstation_uuid"], ["workstations_v32.workstation_uuid"], name=op.f("fk_cash_sessions_v32_opened_by_workstation_uuid_workstations_v32")),
        sa.PrimaryKeyConstraint("session_uuid", name=op.f("pk_cash_sessions_v32")),
        sa.UniqueConstraint("session_no", name=op.f("uq_cash_sessions_v32_session_no")),
    )
    op.create_index(
        "uq_open_cash_session_location_v32", "cash_sessions_v32",
        ["premise_id", "electronic_device_id"], unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.add_column(
        "invoices_v32",
        sa.Column("cash_session_uuid", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_invoices_v32_cash_session_uuid_cash_sessions_v32"),
        "invoices_v32", "cash_sessions_v32", ["cash_session_uuid"], ["session_uuid"],
    )
    op.create_table(
        "cash_journal_entries_v32",
        sa.Column("entry_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_no", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_type", sa.String(length=30), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("balance_after", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("reference_type", sa.String(length=40), nullable=False),
        sa.Column("reference_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_no", sa.String(length=200), server_default="", nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("actor_user_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("workstation_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_cash_journal_entries_v32_cash_journal_amount_nonzero")),
        sa.CheckConstraint("balance_after >= 0", name=op.f("ck_cash_journal_entries_v32_cash_journal_balance_nonnegative")),
        sa.CheckConstraint("entry_type IN ('CASH_SALE','CASH_RECEIPT','CASH_EXPENSE','CASH_REFUND','CASH_BUYBACK')", name=op.f("ck_cash_journal_entries_v32_cash_journal_entry_type")),
        sa.ForeignKeyConstraint(["actor_user_uuid"], ["central_users_v32.user_uuid"], name=op.f("fk_cash_journal_entries_v32_actor_user_uuid_central_users_v32")),
        sa.ForeignKeyConstraint(["session_uuid"], ["cash_sessions_v32.session_uuid"], name=op.f("fk_cash_journal_entries_v32_session_uuid_cash_sessions_v32")),
        sa.ForeignKeyConstraint(["workstation_uuid"], ["workstations_v32.workstation_uuid"], name=op.f("fk_cash_journal_entries_v32_workstation_uuid_workstations_v32")),
        sa.PrimaryKeyConstraint("entry_uuid", name=op.f("pk_cash_journal_entries_v32")),
        sa.UniqueConstraint("reference_type", "reference_uuid", "entry_type", name="uq_cash_journal_reference"),
        sa.UniqueConstraint("session_uuid", "entry_no", name="uq_cash_journal_entry_no"),
    )


def downgrade() -> None:
    op.drop_table("cash_journal_entries_v32")
    op.drop_constraint(
        op.f("fk_invoices_v32_cash_session_uuid_cash_sessions_v32"),
        "invoices_v32", type_="foreignkey",
    )
    op.drop_column("invoices_v32", "cash_session_uuid")
    op.drop_index("uq_open_cash_session_location_v32", table_name="cash_sessions_v32")
    op.drop_table("cash_sessions_v32")
    op.drop_table("cash_session_number_sequences_v32")
