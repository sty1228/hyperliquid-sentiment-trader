"""add copy mode, manual trade overrides, and network events

Revision ID: 0ef77fc18230
Revises: 1b7e891fc2da
Create Date: 2026-04-28

Scope (intentionally narrow — autogenerate caught extensive pre-existing drift
between models and recorded migrations; that drift is NOT addressed here, only
the columns/tables newly required for the four 2026-04-28 product features):

  follows: copy_mode (text, default 'all'), remaining_copies (int, nullable)
  trades:  tp_override_pct, sl_override_pct (float, nullable),
           realized_pnl_usd (float, default 0)
  new table: network_events (BIGSERIAL id, user_id, type, payload JSONB, created_at)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0ef77fc18230"
down_revision: Union[str, None] = "1b7e891fc2da"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── follows: Copy Next mode ─────────────────────────────────
    op.add_column(
        "follows",
        sa.Column(
            "copy_mode",
            sa.String(length=10),
            nullable=False,
            server_default="all",
        ),
    )
    op.add_column(
        "follows",
        sa.Column("remaining_copies", sa.Integer(), nullable=True),
    )

    # ── trades: per-trade TP/SL overrides + realized partial PnL ─
    op.add_column("trades", sa.Column("tp_override_pct", sa.Float(), nullable=True))
    op.add_column("trades", sa.Column("sl_override_pct", sa.Float(), nullable=True))
    op.add_column(
        "trades",
        sa.Column(
            "realized_pnl_usd",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )

    # ── network_events: SSE event stream (cross-process via LISTEN/NOTIFY) ─
    op.create_table(
        "network_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_network_events_user_id", "network_events", ["user_id"], unique=False
    )
    op.create_index(
        "ix_network_events_user_id_id",
        "network_events",
        ["user_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_network_events_user_id_id", table_name="network_events")
    op.drop_index("ix_network_events_user_id", table_name="network_events")
    op.drop_table("network_events")

    op.drop_column("trades", "realized_pnl_usd")
    op.drop_column("trades", "sl_override_pct")
    op.drop_column("trades", "tp_override_pct")

    op.drop_column("follows", "remaining_copies")
    op.drop_column("follows", "copy_mode")
