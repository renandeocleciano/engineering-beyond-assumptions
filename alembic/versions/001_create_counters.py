"""Create counters table.

Revision ID: 001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "counters",
        sa.Column("name", sa.String(100), primary_key=True),
        sa.Column("value", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("counters")
