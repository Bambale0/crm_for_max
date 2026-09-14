"""Add deduplicated group chat signals.

Revision ID: 0007_chat_signals
Revises: 0006_owner_admin
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_chat_signals"
down_revision: str | None = "0006_owner_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_analysis_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_max_user_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["chat_id"], ["bot_group_chats.chat_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key"),
    )
    op.create_index(
        "ix_chat_analysis_jobs_chat_id",
        "chat_analysis_jobs",
        ["chat_id"],
        unique=False,
    )
    op.create_index(
        "ix_chat_analysis_jobs_state",
        "chat_analysis_jobs",
        ["state"],
        unique=False,
    )

    op.create_table(
        "chat_signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_max_user_id", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("problem", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["chat_id"], ["bot_group_chats.chat_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key"),
    )
    op.create_index("ix_chat_signals_chat_id", "chat_signals", ["chat_id"], unique=False)
    op.create_index(
        "ix_chat_signals_chat_actor_created",
        "chat_signals",
        ["chat_id", "actor_max_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_chat_signals_fingerprint",
        "chat_signals",
        ["fingerprint"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_signals_fingerprint", table_name="chat_signals")
    op.drop_index("ix_chat_signals_chat_actor_created", table_name="chat_signals")
    op.drop_index("ix_chat_signals_chat_id", table_name="chat_signals")
    op.drop_table("chat_signals")
    op.drop_index("ix_chat_analysis_jobs_state", table_name="chat_analysis_jobs")
    op.drop_index("ix_chat_analysis_jobs_chat_id", table_name="chat_analysis_jobs")
    op.drop_table("chat_analysis_jobs")
