"""Add a free-form address for resident requests.

Revision ID: 0005_resident_intake
Revises: 0004_bot_dialogs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_resident_intake"
down_revision: str | None = "0004_bot_dialogs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("requests", sa.Column("applicant_address", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("requests", "applicant_address")
