"""widen users wallet_address to varchar(128)

Revision ID: 2225cbed80a6
Revises: acb0b86b0ff2
Create Date: 2026-05-01 07:55:18.075238

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2225cbed80a6'
down_revision: Union[str, None] = 'acb0b86b0ff2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PROD HOTFIX (2026-05-01): the dual-account merge in auth.py was writing
    # a 96-byte marker ("merged-into-<uuid>-<wallet>") into a VARCHAR(42)
    # column, blowing up with StringDataRightTruncation on every login that
    # triggered the merge path. We're widening the column AND shortening the
    # marker (in auth.py) — defense in depth.
    #
    # ALTER TYPE preserves the existing UNIQUE constraint and INDEX; Postgres
    # rebuilds the index automatically. USING is unnecessary because the new
    # type is a strict superset.
    op.alter_column(
        "users",
        "wallet_address",
        existing_type=sa.String(length=42),
        type_=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Down-migration is unsafe if any row exceeds 42 chars (e.g. existing
    # `deact_<32-hex>` markers — 38 bytes — would still fit, but any
    # legacy `merged-into-<uuid>-<wallet>` rows would not). Truncating
    # silently is worse than refusing to run, so leave the schema wide.
    op.alter_column(
        "users",
        "wallet_address",
        existing_type=sa.String(length=128),
        type_=sa.String(length=42),
        existing_nullable=False,
    )
