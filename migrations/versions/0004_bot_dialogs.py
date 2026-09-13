"""Persist native MAX dialogs, chat intake and outgoing replies

Revision ID: 0004_bot_dialogs
Revises: 0003_task_progress
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_bot_dialogs"
down_revision: str | None = "0003_task_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_conversations",
        sa.Column("max_user_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="menu", nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_timestamp_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("max_user_id", name=op.f("pk_bot_conversations")),
    )
    op.create_table(
        "bot_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("max_user_id", sa.BigInteger(), nullable=False),
        sa.Column("access_stamp", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("buttons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("callback_id", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_deliveries")),
        sa.UniqueConstraint("sequence", name=op.f("uq_bot_deliveries_sequence")),
    )
    op.create_index(op.f("ix_bot_deliveries_state"), "bot_deliveries", ["state"], unique=False)
    op.create_table(
        "bot_receipts",
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_key", name=op.f("pk_bot_receipts")),
    )
    op.create_table(
        "house_chats",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("house_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "category_id"],
            ["categories.organization_id", "categories.id"],
            name=op.f("fk_house_chats_organization_id_categories"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "house_id"],
            ["houses.organization_id", "houses.id"],
            name=op.f("fk_house_chats_organization_id_houses"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_house_chats_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("chat_id", name=op.f("pk_house_chats")),
    )
    op.create_table(
        "chat_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("important", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("dismissed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["house_chats.chat_id"],
            name=op.f("fk_chat_observations_chat_id_house_chats"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_chat_observations_request_id_requests")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_observations")),
        sa.UniqueConstraint("event_key", name=op.f("uq_chat_observations_event_key")),
    )


def downgrade() -> None:
    op.drop_table("chat_observations")
    op.drop_table("house_chats")
    op.drop_table("bot_receipts")
    op.drop_index(op.f("ix_bot_deliveries_state"), table_name="bot_deliveries")
    op.drop_table("bot_deliveries")
    op.drop_table("bot_conversations")
