"""Create organization-scoped CRM catalog, memberships and manual requests.

Revision ID: 0002_crm_intake
Revises: 0001_identity
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_crm_intake"
down_revision: str | None = "0001_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("target_type", sa.String(40), nullable=True))
    op.add_column("audit_logs", sa.Column("target_id", sa.Uuid(), nullable=True))
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_categories_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_categories_organization_id")),
    )
    op.create_table(
        "districts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_districts_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_districts")),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_districts_organization_id")),
    )
    op.create_table(
        "request_statuses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("is_initial", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_request_statuses_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request_statuses")),
        sa.UniqueConstraint("organization_id", "code", name="uq_request_statuses_code"),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_request_statuses_organization_id")
        ),
    )
    op.create_index(
        "uq_request_statuses_initial",
        "request_statuses",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("is_initial"),
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(permissions) = 'array'", name=op.f("ck_roles_permissions_array")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_roles_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_roles_organization_id")),
    )
    op.create_table(
        "areas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("district_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "district_id"],
            ["districts.organization_id", "districts.id"],
            name="fk_areas_organization_district",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_areas_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_areas")),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_areas_organization_id")),
    )
    op.create_table(
        "employees",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("max_user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=True),
        sa.Column("all_houses", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("all_categories", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "house_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "category_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(category_ids) = 'array'", name=op.f("ck_employees_category_ids_array")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(house_ids) = 'array'", name=op.f("ck_employees_house_ids_array")
        ),
        sa.CheckConstraint("max_user_id > 0", name=op.f("ck_employees_positive_max_user_id")),
        sa.ForeignKeyConstraint(
            ["organization_id", "area_id"],
            ["areas.organization_id", "areas.id"],
            name="fk_employees_organization_area",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            name="fk_employees_organization_role",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_employees_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employees")),
        sa.UniqueConstraint(
            "organization_id", "max_user_id", name=op.f("uq_employees_organization_id")
        ),
    )
    op.create_table(
        "houses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "area_id"],
            ["areas.organization_id", "areas.id"],
            name="fk_houses_organization_area",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_houses_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_houses")),
        sa.UniqueConstraint("organization_id", "id", name=op.f("uq_houses_organization_id")),
    )
    op.create_index(
        "ix_houses_organization_area", "houses", ["organization_id", "area_id"], unique=False
    )
    op.create_table(
        "entrances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("house_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "house_id"],
            ["houses.organization_id", "houses.id"],
            name="fk_entrances_organization_house",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_entrances_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entrances")),
        sa.UniqueConstraint(
            "organization_id", "house_id", "id", name=op.f("uq_entrances_organization_id")
        ),
        sa.UniqueConstraint("organization_id", "house_id", "number", name="uq_entrances_number"),
    )
    op.create_table(
        "requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("house_id", sa.Uuid(), nullable=False),
        sa.Column("entrance_id", sa.Uuid(), nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("status_id", sa.Uuid(), nullable=False),
        sa.Column("applicant_name", sa.String(length=200), nullable=True),
        sa.Column("applicant_phone", sa.String(length=50), nullable=True),
        sa.Column("apartment", sa.String(length=30), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="manual", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_requests_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "category_id"],
            ["categories.organization_id", "categories.id"],
            name="fk_requests_organization_category",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "house_id", "entrance_id"],
            ["entrances.organization_id", "entrances.house_id", "entrances.id"],
            name="fk_requests_organization_house_entrance",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "house_id"],
            ["houses.organization_id", "houses.id"],
            name="fk_requests_organization_house",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "status_id"],
            ["request_statuses.organization_id", "request_statuses.id"],
            name="fk_requests_organization_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_requests_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requests")),
        sa.UniqueConstraint("number", name=op.f("uq_requests_number")),
        sa.UniqueConstraint(
            "organization_id", "created_by", "idempotency_key", name="uq_requests_idempotency"
        ),
    )
    op.create_index(
        "ix_requests_organization_created",
        "requests",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "request_status_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("from_status_id", sa.Uuid(), nullable=True),
        sa.Column("to_status_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name=op.f("fk_request_status_history_actor_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["from_status_id"],
            ["request_statuses.id"],
            name=op.f("fk_request_status_history_from_status_id_request_statuses"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["requests.id"],
            name=op.f("fk_request_status_history_request_id_requests"),
        ),
        sa.ForeignKeyConstraint(
            ["to_status_id"],
            ["request_statuses.id"],
            name=op.f("fk_request_status_history_to_status_id_request_statuses"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request_status_history")),
    )
    op.create_index(
        op.f("ix_request_status_history_request_id"),
        "request_status_history",
        ["request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_request_status_history_request_id"), table_name="request_status_history")
    op.drop_table("request_status_history")
    op.drop_index("ix_requests_organization_created", table_name="requests")
    op.drop_table("requests")
    op.drop_table("entrances")
    op.drop_index("ix_houses_organization_area", table_name="houses")
    op.drop_table("houses")
    op.drop_table("employees")
    op.drop_table("areas")
    op.drop_table("roles")
    op.drop_index(
        "uq_request_statuses_initial",
        table_name="request_statuses",
        postgresql_where=sa.text("is_initial"),
    )
    op.drop_table("request_statuses")
    op.drop_table("districts")
    op.drop_table("categories")
    op.drop_table("organizations")
    op.drop_column("audit_logs", "target_id")
    op.drop_column("audit_logs", "target_type")
