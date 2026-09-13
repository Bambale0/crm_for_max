"""Add assignments, guarded revisions and employee progress notes.

Revision ID: 0003_task_progress
Revises: 0002_crm_intake
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_task_progress"
down_revision: str | None = "0002_crm_intake"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_employees_organization_id_id", "employees", ["organization_id", "id"]
    )
    op.add_column("requests", sa.Column("assignee_id", sa.Uuid(), nullable=True))
    op.add_column(
        "requests", sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_requests_nonnegative_revision"), "requests", "revision >= 0"
    )
    op.create_foreign_key(
        "fk_requests_organization_assignee",
        "requests",
        "employees",
        ["organization_id", "assignee_id"],
        ["organization_id", "id"],
    )
    op.create_index("ix_requests_assignee_created", "requests", ["assignee_id", "created_at", "id"])
    op.create_table(
        "task_progress",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("note", sa.String(2000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('in_progress', 'done', 'not_done', 'needs')",
            name=op.f("ck_task_progress_valid_state"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('not_done', 'needs') OR (note IS NOT NULL AND length(trim(note)) > 0)",
            name=op.f("ck_task_progress_required_note"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_task_progress_request_id_requests")
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name=op.f("fk_task_progress_actor_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_progress")),
    )
    op.create_index(
        "ix_task_progress_request_created", "task_progress", ["request_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_progress_request_created", table_name="task_progress")
    op.drop_table("task_progress")
    op.drop_index("ix_requests_assignee_created", table_name="requests")
    op.drop_constraint("fk_requests_organization_assignee", "requests", type_="foreignkey")
    op.drop_constraint(op.f("ck_requests_nonnegative_revision"), "requests", type_="check")
    op.drop_column("requests", "revision")
    op.drop_column("requests", "assignee_id")
    op.drop_constraint("uq_employees_organization_id_id", "employees", type_="unique")
