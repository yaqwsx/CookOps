"""Add the event lifecycle planning core."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_event_lifecycle"
down_revision: str | None = "0006_ingredient_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("general_note", sa.Text(), nullable=True),
        sa.Column("base_expected_attendance", sa.Integer(), nullable=False),
        sa.Column("budget_amount", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_events_name_not_empty"),
        sa.CheckConstraint("end_date >= start_date", name="ck_events_date_range"),
        sa.CheckConstraint(
            "base_expected_attendance >= 0", name="ck_events_nonnegative_base_attendance"
        ),
        sa.CheckConstraint(
            "budget_amount >= 0 AND budget_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_events_nonnegative_budget",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_events_currency"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "event_days",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_visible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "provenance IN ('range_generated', 'manually_added')",
            name="ck_event_days_provenance",
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_event_days_retirement_attribution",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_event_days_active_event_date",
        "event_days",
        ["event_id", "calendar_date"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_table(
        "event_meal_roles",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("built_in_translation_key", sa.String(length=100), nullable=True),
        sa.Column("custom_name", sa.String(length=200), nullable=True),
        sa.Column("normalized_custom_name", sa.String(length=200), nullable=True),
        sa.Column("position_key", sa.String(length=255, collation="C"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(built_in_translation_key IS NOT NULL "
            "AND built_in_translation_key ~ '^[a-z][a-z0-9_.-]*$' "
            "AND custom_name IS NULL AND normalized_custom_name IS NULL) OR "
            "(built_in_translation_key IS NULL AND custom_name IS NOT NULL "
            "AND btrim(custom_name) <> '' "
            "AND normalized_custom_name IS NOT NULL "
            "AND normalized_custom_name = lower(btrim(custom_name)))",
            name="ck_event_meal_roles_display_identity",
        ),
        sa.CheckConstraint(
            "position_key ~ '^[0-9A-Za-z]+$'", name="ck_event_meal_roles_position_key"
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_event_meal_roles_retirement_attribution",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "built_in_translation_key", name="uq_event_meal_roles_builtin_key"
        ),
        sa.UniqueConstraint(
            "event_id", "normalized_custom_name", name="uq_event_meal_roles_custom_name"
        ),
    )


def downgrade() -> None:
    op.drop_table("event_meal_roles")
    op.drop_table("event_days")
    op.drop_table("events")
