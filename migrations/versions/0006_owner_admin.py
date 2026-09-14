"""Add owner-managed bot settings and group chat registry.

Revision ID: 0006_owner_admin
Revises: 0005_resident_intake
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_owner_admin"
down_revision: str | None = "0005_resident_intake"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_organization_settings",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "group_analysis_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("organization_id"),
    )
    op.create_table(
        "bot_group_chats",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "analysis_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("chat_id"),
    )
    op.create_index(
        "ix_bot_group_chats_organization_id",
        "bot_group_chats",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_bot_group_chats_organization_id", table_name="bot_group_chats")
    op.drop_table("bot_group_chats")
    op.drop_table("bot_organization_settings")
