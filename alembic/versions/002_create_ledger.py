"""Create accounts, fundings, transfers, and immutable ledger entries.

Revision ID: 002
Revises: 001
"""

import sqlalchemy as sa

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('customer', 'treasury')", name="ck_accounts_kind"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_accounts_status"),
        sa.CheckConstraint("balance_minor >= 0 OR kind = 'treasury'", name="ck_accounts_balance"),
        sa.CheckConstraint("char_length(currency) = 3", name="ck_accounts_currency"),
    )
    op.create_index(
        "uq_accounts_treasury_currency",
        "accounts",
        ["currency"],
        unique=True,
        postgresql_where=sa.text("kind = 'treasury'"),
    )
    op.create_table(
        "fundings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("treasury_account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name="ck_fundings_amount"),
    )
    op.create_table(
        "transfers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column(
            "destination_account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name="ck_transfers_amount"),
        sa.CheckConstraint(
            "source_account_id <> destination_account_id", name="ck_transfers_distinct"
        ),
        sa.CheckConstraint("status IN ('completed')", name="ck_transfers_status"),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("transfer_id", sa.Uuid(), sa.ForeignKey("transfers.id"), nullable=True),
        sa.Column("funding_id", sa.Uuid(), sa.ForeignKey("fundings.id"), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor <> 0", name="ck_ledger_nonzero"),
        sa.CheckConstraint(
            "(transfer_id IS NOT NULL AND funding_id IS NULL) "
            "OR (transfer_id IS NULL AND funding_id IS NOT NULL)",
            name="ck_ledger_one_cause",
        ),
    )
    op.create_index(
        "ix_ledger_entries_account_created",
        "ledger_entries",
        ["account_id", "created_at", "id"],
    )
    op.execute(
        """
        CREATE FUNCTION ledger_entries_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'ledger entries are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER ledger_entries_no_mutation
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION ledger_entries_immutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_entries_no_mutation ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS ledger_entries_immutable()")
    op.drop_index("ix_ledger_entries_account_created", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("transfers")
    op.drop_table("fundings")
    op.drop_index("uq_accounts_treasury_currency", table_name="accounts")
    op.drop_table("accounts")
