"""ERP outbox: approved invoices waiting to be posted as vendor bills.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "erp_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "invoice_id", sa.String(64), sa.ForeignKey("invoices.id"), nullable=False, unique=True
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.String(40), nullable=False),
        sa.Column("last_error", sa.String(2000)),
        sa.Column("erp_id", sa.Integer()),
        sa.Column("erp_name", sa.String(64)),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_erp_outbox_status", "erp_outbox", ["status"])


def downgrade() -> None:
    op.drop_index("ix_erp_outbox_status", table_name="erp_outbox")
    op.drop_table("erp_outbox")
