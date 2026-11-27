"""Create idempotency records for protected transfers.

Revision ID: 003
Revises: 002
"""

import sqlalchemy as sa

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("transfer_id", sa.Uuid(), sa.ForeignKey("transfers.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('reserved', 'completed')", name="ck_idempotency_status"
        ),
        sa.CheckConstraint(
            "(status = 'reserved' AND transfer_id IS NULL AND completed_at IS NULL) "
            "OR (status = 'completed' AND transfer_id IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_idempotency_status_shape",
        ),
        sa.UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
    )
    op.create_index(
        "ix_idempotency_records_transfer_id",
        "idempotency_records",
        ["transfer_id"],
        unique=True,
        postgresql_where=sa.text("transfer_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_records_transfer_id", table_name="idempotency_records")
    op.drop_table("idempotency_records")
