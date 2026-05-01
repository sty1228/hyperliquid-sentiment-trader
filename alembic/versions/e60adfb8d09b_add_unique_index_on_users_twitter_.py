"""add unique index on users.twitter_username

Revision ID: e60adfb8d09b
Revises: 2225cbed80a6
Create Date: 2026-05-02 06:25:40.214835

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e60adfb8d09b'
down_revision: Union[str, None] = '2225cbed80a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The index was added by manual SQL on prod 2026-05-02 alongside a
    # one-time dedup of MomentumKevin / Ameliachenssmy duplicate user rows
    # (5 → 2). This migration captures the same DDL into alembic so fresh
    # environments cloning the repo and running `alembic upgrade head` get
    # the same schema. `IF NOT EXISTS` makes prod a no-op.
    #
    # Why a partial index: nullable twitter_username (users can connect a
    # wallet without supplying a Twitter handle). A regular UNIQUE would
    # reject every NULL row after the first.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_twitter_username
        ON users (twitter_username)
        WHERE twitter_username IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_users_twitter_username")
