"""drop trades realized_pnl_usd; PnL is HL-authoritative now

Revision ID: acb0b86b0ff2
Revises: 1aadbe5e2594
Create Date: 2026-05-01 05:13:06.167035

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'acb0b86b0ff2'
down_revision: Union[str, None] = '1aadbe5e2594'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop is idempotent — IF EXISTS guards against prod where the column may
    # have been removed manually or never applied. Mirrors the recovery
    # guidance in CLAUDE.md §8 ("Recovering from migration state drift").
    op.execute("ALTER TABLE trades DROP COLUMN IF EXISTS realized_pnl_usd")


def downgrade() -> None:
    op.add_column(
        "trades",
        sa.Column(
            "realized_pnl_usd",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )
