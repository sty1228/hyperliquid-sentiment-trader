"""add users last_seen_at

Revision ID: 1aadbe5e2594
Revises: 0ef77fc18230
Create Date: 2026-04-28 14:19:47.970932

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1aadbe5e2594'
down_revision: Union[str, None] = '0ef77fc18230'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_seen_at")
