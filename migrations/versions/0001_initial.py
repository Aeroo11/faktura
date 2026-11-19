"""Initial schema: invoices, append-only audit trail with hash chain.

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Postgres refuses any UPDATE or DELETE on audit rows, so history cannot be rewritten
# through the application or a careless query. The hash chain still detects edits made
# by someone who disables the trigger.
AUDIT_GUARD = """
CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_no_update_delete
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
"""


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False, unique=True),
        sa.Column("pdf_path", sa.String(512), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("reading", sa.JSON(), nullable=False),
        sa.Column("model_reading", sa.JSON(), nullable=False),
        sa.Column("human_verified", sa.JSON(), nullable=False),
        sa.Column("makers", sa.JSON(), nullable=False),
        sa.Column("vendor_id", sa.String(32), nullable=True),
        sa.Column("total", sa.BigInteger(), nullable=True),
        sa.Column("auto_approved", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_invoices_seq", "invoices", ["seq"])
    op.create_index("ix_invoices_state", "invoices", ["state"])
    op.create_index("ix_invoices_decision", "invoices", ["decision"])
    op.create_index("ix_invoices_vendor_id", "invoices", ["vendor_id"])

    op.create_table(
        "audit_events",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("invoice_id", sa.String(64), sa.ForeignKey("invoices.id"), nullable=True),
        sa.Column("at", sa.String(40), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_audit_events_invoice_id", "audit_events", ["invoice_id"])

    op.create_table(
        "audit_head",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("last_hash", sa.String(64), nullable=False),
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(AUDIT_GUARD)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_update_delete ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
    op.drop_table("audit_head")
    op.drop_table("audit_events")
    op.drop_table("invoices")
