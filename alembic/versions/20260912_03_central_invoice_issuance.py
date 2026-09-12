"""Add central idempotent invoice issuance and fiscal outbox.

Revision ID: 20260912_03
Revises: 20260912_02
Create Date: 2026-09-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260912_03"
down_revision: Union[str, Sequence[str], None] = "20260912_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_invoice_drafts_v32_invoice_draft_status"),
        "invoice_drafts_v32", type_="check",
    )
    op.create_check_constraint(
        "invoice_draft_status", "invoice_drafts_v32",
        "status IN ('READY','ISSUING','INVOICED')",
    )
    op.create_table(
        "invoice_number_sequences_v32",
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("premise_id", sa.String(length=40), nullable=False),
        sa.Column("electronic_device_id", sa.String(length=40), nullable=False),
        sa.Column("last_number", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("fiscal_year >= 2020", name="invoice_sequence_year"),
        sa.CheckConstraint("last_number >= 1", name="invoice_sequence_positive"),
        sa.PrimaryKeyConstraint(
            "fiscal_year", "premise_id", "electronic_device_id",
            name="pk_invoice_number_sequences_v32",
        ),
    )
    op.create_table(
        "invoices_v32",
        sa.Column("invoice_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_order_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issue_operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_number", sa.String(length=130), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_sequence", sa.BigInteger(), nullable=False),
        sa.Column("premise_id", sa.String(length=40), nullable=False),
        sa.Column("electronic_device_id", sa.String(length=40), nullable=False),
        sa.Column("cashier_id", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payment_status", sa.String(length=20), nullable=False),
        sa.Column("payments_json", postgresql.JSONB(), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="EUR", nullable=False),
        sa.Column("tax_mode", sa.String(length=20), nullable=False),
        sa.Column("tax_rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("tax_note", sa.String(length=500), server_default="", nullable=False),
        sa.Column("furs_zoi", sa.String(length=64), server_default="", nullable=False),
        sa.Column("furs_eor", sa.String(length=100), server_default="", nullable=False),
        sa.Column("snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('PENDING_FISCALIZATION','FISCAL_RECONCILIATION','ISSUED')", name="invoice_status"),
        sa.CheckConstraint("payment_status IN ('UNPAID','PAID')", name="invoice_payment_status"),
        sa.CheckConstraint("total_amount >= 0", name="invoice_total_nonnegative"),
        sa.CheckConstraint("currency = 'EUR'", name="invoice_currency_eur"),
        sa.ForeignKeyConstraint(["draft_uuid"], ["invoice_drafts_v32.draft_uuid"], name="fk_invoice_draft"),
        sa.ForeignKeyConstraint(["service_order_uuid"], ["service_orders_v32.sync_uuid"], name="fk_invoice_service_order"),
        sa.ForeignKeyConstraint(["issue_operation_id"], ["accepted_operations_v32.operation_id"], name="fk_invoice_issue_operation"),
        sa.PrimaryKeyConstraint("invoice_uuid", name="pk_invoices_v32"),
        sa.UniqueConstraint("draft_uuid", name="uq_invoices_v32_draft_uuid"),
        sa.UniqueConstraint("service_order_uuid", name="uq_invoices_v32_service_order_uuid"),
        sa.UniqueConstraint("issue_operation_id", name="uq_invoices_v32_issue_operation_id"),
        sa.UniqueConstraint("invoice_number", name="uq_invoices_v32_invoice_number"),
        sa.UniqueConstraint("fiscal_year", "premise_id", "electronic_device_id", "fiscal_sequence", name="uq_invoice_fiscal_sequence"),
    )
    op.create_table(
        "fiscal_outbox_v32",
        sa.Column("message_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_json", postgresql.JSONB(), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.String(length=1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('PENDING','SENDING','UNCERTAIN','CONFIRMED')", name="fiscal_outbox_status"),
        sa.CheckConstraint("attempt_count >= 0", name="fiscal_attempt_nonnegative"),
        sa.ForeignKeyConstraint(["invoice_uuid"], ["invoices_v32.invoice_uuid"], name="fk_fiscal_outbox_invoice"),
        sa.PrimaryKeyConstraint("message_uuid", name="pk_fiscal_outbox_v32"),
        sa.UniqueConstraint("invoice_uuid", name="uq_fiscal_outbox_v32_invoice_uuid"),
    )


def downgrade() -> None:
    op.drop_table("fiscal_outbox_v32")
    op.drop_table("invoices_v32")
    op.drop_table("invoice_number_sequences_v32")
    op.drop_constraint(
        op.f("ck_invoice_drafts_v32_invoice_draft_status"),
        "invoice_drafts_v32", type_="check",
    )
    op.create_check_constraint(
        "invoice_draft_status", "invoice_drafts_v32",
        "status IN ('READY','INVOICED')",
    )
