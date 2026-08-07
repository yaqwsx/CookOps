"""Add organization configuration records."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_organization_configuration"
down_revision: str | None = "0002_identity_organizations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common_configuration_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", sa.Uuid(), nullable=True),
    )


def _common_configuration_constraints() -> tuple[sa.ForeignKeyConstraint, ...]:
    return (
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
    )


def upgrade() -> None:
    op.create_table(
        "store_sections",
        *_common_configuration_columns(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("position_key", sa.String(length=255, collation="C"), nullable=False),
        sa.CheckConstraint(
            "btrim(name) <> '' AND normalized_name = lower(btrim(name))",
            name="ck_store_sections_normalized_name",
        ),
        sa.CheckConstraint(
            "position_key ~ '^[0-9A-Za-z]+$'", name="ck_store_sections_position_key"
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_store_sections_retirement_attribution",
        ),
        *_common_configuration_constraints(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "normalized_name", name="uq_store_sections_organization_name"
        ),
    )

    op.create_table(
        "organization_meal_role_presets",
        *_common_configuration_columns(),
        sa.Column("built_in_translation_key", sa.String(length=100), nullable=True),
        sa.Column("custom_name", sa.String(length=200), nullable=True),
        sa.Column("normalized_custom_name", sa.String(length=200), nullable=True),
        sa.Column("position_key", sa.String(length=255, collation="C"), nullable=False),
        sa.CheckConstraint(
            "(built_in_translation_key IS NOT NULL "
            "AND built_in_translation_key ~ '^[a-z][a-z0-9_.-]*$' "
            "AND custom_name IS NULL AND normalized_custom_name IS NULL) OR "
            "(built_in_translation_key IS NULL AND custom_name IS NOT NULL "
            "AND btrim(custom_name) <> '' "
            "AND normalized_custom_name IS NOT NULL "
            "AND normalized_custom_name = lower(btrim(custom_name)))",
            name="ck_meal_role_presets_display_identity",
        ),
        sa.CheckConstraint(
            "position_key ~ '^[0-9A-Za-z]+$'", name="ck_meal_role_presets_position_key"
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_meal_role_presets_retirement_attribution",
        ),
        *_common_configuration_constraints(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "built_in_translation_key",
            name="uq_meal_role_presets_builtin_key",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "normalized_custom_name",
            name="uq_meal_role_presets_custom_name",
        ),
    )

    op.create_table(
        "recipe_tags",
        *_common_configuration_columns(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=False),
        sa.CheckConstraint(
            "btrim(name) <> '' AND normalized_name = lower(btrim(name))",
            name="ck_recipe_tags_normalized_name",
        ),
        sa.CheckConstraint("color ~ '^#[0-9A-Fa-f]{6}$'", name="ck_recipe_tags_color"),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_recipe_tags_retirement_attribution",
        ),
        *_common_configuration_constraints(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "normalized_name", name="uq_recipe_tags_organization_name"
        ),
    )

    op.create_table(
        "dietary_tags",
        *_common_configuration_columns(),
        sa.Column("seed_key", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("normalized_name", sa.String(length=200), nullable=True),
        sa.Column("color", sa.String(length=7), nullable=True),
        sa.CheckConstraint(
            "seed_key IS NULL OR seed_key IN ('vegetarian', 'vegan', 'gluten', 'lactose')",
            name="ck_dietary_tags_seed_key",
        ),
        sa.CheckConstraint(
            "(name IS NULL AND normalized_name IS NULL AND seed_key IS NOT NULL) OR "
            "(name IS NOT NULL AND btrim(name) <> '' "
            "AND normalized_name IS NOT NULL "
            "AND normalized_name = lower(btrim(name)))",
            name="ck_dietary_tags_display_identity",
        ),
        sa.CheckConstraint(
            "color IS NULL OR color ~ '^#[0-9A-Fa-f]{6}$'", name="ck_dietary_tags_color"
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_dietary_tags_retirement_attribution",
        ),
        *_common_configuration_constraints(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "seed_key", name="uq_dietary_tags_seed_key"),
        sa.UniqueConstraint(
            "organization_id", "normalized_name", name="uq_dietary_tags_organization_name"
        ),
    )


def downgrade() -> None:
    op.drop_table("dietary_tags")
    op.drop_table("recipe_tags")
    op.drop_table("organization_meal_role_presets")
    op.drop_table("store_sections")
