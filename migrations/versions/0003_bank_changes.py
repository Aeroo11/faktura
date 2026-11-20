"""Vendor bank-account change requests (request, call-back, approval).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bank_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vendor_id", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("old_bank_name", sa.String(128), nullable=False),
        sa.Column("old_bank_account", sa.String(64), nullable=False),
        sa.Column("new_bank_name", sa.String(128), nullable=False),
        sa.Column("new_bank_account", sa.String(64), nullable=False),
        sa.Column("source_invoice_id", sa.String(64), sa.ForeignKey("invoices.id")),
        sa.Column("channel", sa.String(500), nullable=False),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("callback", sa.JSON()),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("decision_note", sa.String(2000)),
        sa.Column("erp_status", sa.String(32)),
        sa.Column("erp_error", sa.String(2000)),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_bank_changes_vendor_id", "bank_changes", ["vendor_id"])
    op.create_index("ix_bank_changes_status", "bank_changes", ["status"])
    op.create_index("ix_bank_changes_erp_status", "bank_changes", ["erp_status"])


def downgrade() -> None:
    op.drop_index("ix_bank_changes_erp_status", table_name="bank_changes")
    op.drop_index("ix_bank_changes_status", table_name="bank_changes")
    op.drop_index("ix_bank_changes_vendor_id", table_name="bank_changes")
    op.drop_table("bank_changes")
