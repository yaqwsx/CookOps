"""Add immutable versioned recipe catalog records."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_recipe_catalog"
down_revision: str | None = "0009_organization_change_feed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_recipe_tags_id_organization", "recipe_tags", ["id", "organization_id"]
    )

    op.create_table(
        "recipes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
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
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_recipes_retirement_attribution",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_recipes_id_organization"),
    )
    op.create_table(
        "recipe_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("based_on_version_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "scaling_model",
            sa.String(length=32),
            server_default=sa.text("'single_variable'"),
            nullable=False,
        ),
        sa.Column("scaling_unit_id", sa.Uuid(), nullable=False),
        sa.Column("base_scaling_amount", sa.Numeric(), nullable=False),
        sa.Column("estimated_diners_per_scaling_unit", sa.Numeric(), nullable=True),
        sa.Column(
            "round_suggestions_up", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_recipe_versions_name_not_empty"),
        sa.CheckConstraint(
            "scaling_model = 'single_variable'", name="ck_recipe_versions_scaling_model"
        ),
        sa.CheckConstraint(
            "base_scaling_amount > 0 "
            "AND base_scaling_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_recipe_versions_positive_base_scaling_amount",
        ),
        sa.CheckConstraint(
            "estimated_diners_per_scaling_unit IS NULL OR "
            "(estimated_diners_per_scaling_unit > 0 "
            "AND estimated_diners_per_scaling_unit::text NOT IN ('NaN', 'Infinity', '-Infinity'))",
            name="ck_recipe_versions_positive_estimated_diners",
        ),
        sa.CheckConstraint(
            "based_on_version_id IS NULL OR based_on_version_id <> id",
            name="ck_recipe_versions_nonrecursive_base",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id", "organization_id"],
            ["recipes.id", "recipes.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_versions_recipe_organization",
        ),
        sa.ForeignKeyConstraint(
            ["based_on_version_id", "recipe_id"],
            ["recipe_versions.id", "recipe_versions.recipe_id"],
            ondelete="RESTRICT",
            name="fk_recipe_versions_based_on_same_recipe",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(["scaling_unit_id"], ["unit_definitions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_recipe_versions_id_organization"),
        sa.UniqueConstraint("id", "recipe_id", name="uq_recipe_versions_id_recipe"),
    )
    op.create_index("ix_recipe_versions_recipe_id", "recipe_versions", ["recipe_id"])
    op.create_foreign_key(
        "fk_recipes_current_version",
        "recipes",
        "recipe_versions",
        ["current_version_id", "id"],
        ["id", "recipe_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "recipe_version_tags",
        sa.Column("recipe_version_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_tag_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["recipe_version_id", "organization_id"],
            ["recipe_versions.id", "recipe_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_tags_version_organization",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_tag_id", "organization_id"],
            ["recipe_tags.id", "recipe_tags.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_tags_tag_organization",
        ),
        sa.PrimaryKeyConstraint("recipe_version_id", "recipe_tag_id"),
    )
    op.create_table(
        "recipe_version_ingredient_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_version_id", sa.Uuid(), nullable=False),
        sa.Column("line_key", sa.Uuid(), nullable=False),
        sa.Column("ingredient_version_id", sa.Uuid(), nullable=False),
        sa.Column("base_quantity", sa.Numeric(), nullable=False),
        sa.Column("preferred_display_unit_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("position_key", sa.String(length=255, collation="C"), nullable=False),
        sa.Column(
            "scaling_behavior",
            sa.String(length=16),
            server_default=sa.text("'proportional'"),
            nullable=False,
        ),
        sa.Column(
            "include_in_portion_weight",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "base_quantity >= 0 AND base_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_recipe_version_lines_nonnegative_base_quantity",
        ),
        sa.CheckConstraint(
            "scaling_behavior IN ('proportional', 'fixed')",
            name="ck_recipe_version_lines_scaling_behavior",
        ),
        sa.CheckConstraint(
            "position_key ~ '^[0-9A-Za-z]+$'", name="ck_recipe_version_lines_position_key"
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id", "organization_id"],
            ["recipes.id", "recipes.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_lines_recipe_organization",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_version_id", "organization_id"],
            ["recipe_versions.id", "recipe_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_lines_version_organization",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_version_id", "recipe_id"],
            ["recipe_versions.id", "recipe_versions.recipe_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_lines_version_recipe",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_version_id", "organization_id"],
            ["ingredient_versions.id", "ingredient_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_lines_ingredient_version_organization",
        ),
        sa.ForeignKeyConstraint(
            ["preferred_display_unit_id"], ["unit_definitions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recipe_version_id", "line_key", name="uq_recipe_version_lines_version_line_key"
        ),
    )
    op.create_index(
        "ix_recipe_version_lines_recipe_id_line_key",
        "recipe_version_ingredient_lines",
        ["recipe_id", "line_key"],
    )

    op.execute(
        """
        CREATE FUNCTION cookops_validate_recipe_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            unit_organization_id uuid;
            unit_allows_recipe_scaling boolean;
            unit_retired_at timestamptz;
        BEGIN
            IF NEW.published_at IS DISTINCT FROM CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'recipe version publication time is server-owned';
            END IF;
            SELECT organization_id, allows_recipe_scaling, retired_at
            INTO unit_organization_id, unit_allows_recipe_scaling, unit_retired_at
            FROM unit_definitions WHERE id = NEW.scaling_unit_id;
            IF NOT FOUND OR NOT unit_allows_recipe_scaling OR unit_retired_at IS NOT NULL
                OR (unit_organization_id IS NOT NULL
                    AND unit_organization_id <> NEW.organization_id) THEN
                RAISE EXCEPTION 'recipe scaling unit is not available to this organization';
            END IF;
            PERFORM 1 FROM recipes
            WHERE id = NEW.recipe_id AND organization_id = NEW.organization_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'recipe version requires an organization-owned recipe';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_validate_recipe_version_tag()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM recipe_versions WHERE id = NEW.recipe_version_id
            ) THEN
                RAISE EXCEPTION 'recipe version tags must be published atomically';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_validate_recipe_version_ingredient_line()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            ingredient_id_value uuid;
            canonical_unit_id_value uuid;
            canonical_dimension text;
            display_unit_organization_id uuid;
            display_unit_dimension text;
            display_unit_allows_ingredient_quantity boolean;
            display_unit_retired_at timestamptz;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM recipe_versions WHERE id = NEW.recipe_version_id
            ) THEN
                RAISE EXCEPTION 'recipe ingredient lines must be published atomically';
            END IF;
            SELECT version.ingredient_id, version.canonical_unit_id, unit.dimension
            INTO ingredient_id_value, canonical_unit_id_value, canonical_dimension
            FROM ingredient_versions AS version
            JOIN unit_definitions AS unit ON unit.id = version.canonical_unit_id
            WHERE version.id = NEW.ingredient_version_id
              AND version.organization_id = NEW.organization_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'recipe ingredient version is not owned by the recipe organization';
            END IF;
            IF NEW.preferred_display_unit_id IS NOT NULL THEN
                SELECT organization_id, dimension, allows_ingredient_quantity, retired_at
                INTO display_unit_organization_id, display_unit_dimension,
                    display_unit_allows_ingredient_quantity, display_unit_retired_at
                FROM unit_definitions WHERE id = NEW.preferred_display_unit_id;
                IF NOT FOUND OR NOT display_unit_allows_ingredient_quantity
                    OR display_unit_retired_at IS NOT NULL
                    OR (display_unit_organization_id IS NOT NULL
                        AND display_unit_organization_id <> NEW.organization_id)
                    OR display_unit_dimension <> canonical_dimension
                    OR (canonical_dimension IN ('count', 'custom')
                        AND NEW.preferred_display_unit_id <> canonical_unit_id_value) THEN
                    RAISE EXCEPTION 'recipe preferred display unit is incompatible or unavailable';
                END IF;
            END IF;
            IF EXISTS (
                SELECT 1
                FROM recipe_version_ingredient_lines AS existing_line
                JOIN ingredient_versions AS existing_ingredient_version
                  ON existing_ingredient_version.id = existing_line.ingredient_version_id
                WHERE existing_line.recipe_id = NEW.recipe_id
                  AND existing_line.line_key = NEW.line_key
                  AND existing_ingredient_version.ingredient_id <> ingredient_id_value
            ) THEN
                RAISE EXCEPTION 'recipe line key cannot be reused for another ingredient';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_validate_recipe_current_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.current_version_id IS NULL THEN
                RAISE EXCEPTION 'recipe requires a current version';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_validate_recipe_version_ancestry()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                WITH RECURSIVE ancestry(id, based_on_version_id, path, has_cycle) AS (
                    SELECT id, based_on_version_id, ARRAY[id], false
                    FROM recipe_versions WHERE id = NEW.id
                    UNION ALL
                    SELECT parent.id, parent.based_on_version_id,
                        ancestry.path || parent.id,
                        parent.id = ANY(ancestry.path)
                    FROM recipe_versions AS parent
                    JOIN ancestry ON parent.id = ancestry.based_on_version_id
                    WHERE NOT ancestry.has_cycle
                )
                SELECT 1 FROM ancestry WHERE has_cycle
            ) THEN
                RAISE EXCEPTION 'recipe version ancestry cannot contain a cycle';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_prevent_recipe_deletion()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'recipes use reversible retirement';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_prevent_recipe_tag_deletion()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'recipe tags use reversible retirement';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_prevent_recipe_catalog_truncate()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'recipe catalog history cannot be truncated';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_recipe_versions_validate "
        "BEFORE INSERT ON recipe_versions "
        "FOR EACH ROW EXECUTE FUNCTION cookops_validate_recipe_version()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_recipe_versions_validate_ancestry "
        "AFTER INSERT ON recipe_versions DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION cookops_validate_recipe_version_ancestry()"
    )
    op.execute(
        "CREATE TRIGGER trg_recipe_version_tags_validate "
        "BEFORE INSERT ON recipe_version_tags "
        "FOR EACH ROW EXECUTE FUNCTION cookops_validate_recipe_version_tag()"
    )
    op.execute(
        "CREATE TRIGGER trg_recipe_version_ingredient_lines_validate "
        "BEFORE INSERT ON recipe_version_ingredient_lines "
        "FOR EACH ROW EXECUTE FUNCTION cookops_validate_recipe_version_ingredient_line()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_recipes_require_current_version "
        "AFTER INSERT OR UPDATE OF current_version_id ON recipes "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION cookops_validate_recipe_current_version()"
    )
    op.execute(
        "CREATE TRIGGER trg_recipes_no_delete BEFORE DELETE ON recipes "
        "FOR EACH ROW EXECUTE FUNCTION cookops_prevent_recipe_deletion()"
    )
    op.execute(
        "CREATE TRIGGER trg_recipe_tags_no_delete BEFORE DELETE ON recipe_tags "
        "FOR EACH ROW EXECUTE FUNCTION cookops_prevent_recipe_tag_deletion()"
    )
    for table_name in (
        "recipe_versions",
        "recipe_version_tags",
        "recipe_version_ingredient_lines",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_immutable "
            f"BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION cookops_prevent_immutable_catalog_mutation()"
        )
    for table_name in (
        "recipe_tags",
        "recipes",
        "recipe_versions",
        "recipe_version_tags",
        "recipe_version_ingredient_lines",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_no_truncate "
            f"BEFORE TRUNCATE ON {table_name} "
            "FOR EACH STATEMENT EXECUTE FUNCTION cookops_prevent_recipe_catalog_truncate()"
        )


def downgrade() -> None:
    for table_name in (
        "recipe_version_ingredient_lines",
        "recipe_version_tags",
        "recipe_versions",
        "recipes",
        "recipe_tags",
    ):
        op.execute(f"DROP TRIGGER trg_{table_name}_no_truncate ON {table_name}")
    op.execute("DROP TRIGGER trg_recipe_tags_no_delete ON recipe_tags")
    op.execute("DROP TRIGGER trg_recipes_no_delete ON recipes")
    op.execute("DROP TRIGGER trg_recipes_require_current_version ON recipes")
    op.execute(
        "DROP TRIGGER trg_recipe_version_ingredient_lines_validate "
        "ON recipe_version_ingredient_lines"
    )
    op.execute("DROP TRIGGER trg_recipe_version_tags_validate ON recipe_version_tags")
    op.execute("DROP TRIGGER trg_recipe_versions_validate ON recipe_versions")
    op.execute("DROP TRIGGER trg_recipe_versions_validate_ancestry ON recipe_versions")
    for table_name in (
        "recipe_version_ingredient_lines",
        "recipe_version_tags",
        "recipe_versions",
    ):
        op.execute(f"DROP TRIGGER trg_{table_name}_immutable ON {table_name}")
    op.execute("DROP FUNCTION cookops_prevent_recipe_deletion()")
    op.execute("DROP FUNCTION cookops_prevent_recipe_tag_deletion()")
    op.execute("DROP FUNCTION cookops_prevent_recipe_catalog_truncate()")
    op.execute("DROP FUNCTION cookops_validate_recipe_current_version()")
    op.execute("DROP FUNCTION cookops_validate_recipe_version_ancestry()")
    op.execute("DROP FUNCTION cookops_validate_recipe_version_ingredient_line()")
    op.execute("DROP FUNCTION cookops_validate_recipe_version_tag()")
    op.execute("DROP FUNCTION cookops_validate_recipe_version()")
    op.drop_index(
        "ix_recipe_version_lines_recipe_id_line_key",
        table_name="recipe_version_ingredient_lines",
    )
    op.drop_table("recipe_version_ingredient_lines")
    op.drop_table("recipe_version_tags")
    op.drop_constraint("fk_recipes_current_version", "recipes", type_="foreignkey")
    op.drop_index("ix_recipe_versions_recipe_id", table_name="recipe_versions")
    op.drop_table("recipe_versions")
    op.drop_table("recipes")
    op.drop_constraint("uq_recipe_tags_id_organization", "recipe_tags", type_="unique")
