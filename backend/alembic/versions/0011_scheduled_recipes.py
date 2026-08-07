"""Add scheduled recipes and their event-local ingredient overrides."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_scheduled_recipes"
down_revision: str | None = "0010_recipe_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # These composite keys make the placement's event boundary enforceable by foreign keys.
    op.create_unique_constraint("uq_events_id_organization", "events", ["id", "organization_id"])
    op.create_unique_constraint("uq_event_days_id_event", "event_days", ["id", "event_id"])
    op.create_unique_constraint(
        "uq_event_meal_roles_id_event", "event_meal_roles", ["id", "event_id"]
    )

    op.create_table(
        "scheduled_recipes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_day_id", sa.Uuid(), nullable=False),
        sa.Column("event_meal_role_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_version_id", sa.Uuid(), nullable=False),
        sa.Column("diner_count", sa.Integer(), nullable=False),
        sa.Column("attendance_mode", sa.String(length=16), nullable=False),
        sa.Column("consumption_percentage", sa.Numeric(), nullable=False),
        sa.Column("selected_scale_amount", sa.Numeric(), nullable=False),
        sa.Column("scale_mode", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.CheckConstraint("diner_count >= 0", name="ck_scheduled_recipes_nonnegative_diners"),
        sa.CheckConstraint(
            "attendance_mode IN ('follows_event', 'manual')",
            name="ck_scheduled_recipes_attendance_mode",
        ),
        sa.CheckConstraint(
            "consumption_percentage >= 0 "
            "AND consumption_percentage::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_scheduled_recipes_nonnegative_consumption_percentage",
        ),
        sa.CheckConstraint(
            "selected_scale_amount >= 0 "
            "AND selected_scale_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_scheduled_recipes_nonnegative_selected_scale",
        ),
        sa.CheckConstraint(
            "scale_mode IN ('suggested', 'manual')", name="ck_scheduled_recipes_scale_mode"
        ),
        sa.CheckConstraint(
            "position_key ~ '^[0-9A-Za-z]+$'", name="ck_scheduled_recipes_position_key"
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_scheduled_recipes_retirement_attribution",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "organization_id"],
            ["events.id", "events.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_event_organization",
        ),
        sa.ForeignKeyConstraint(
            ["event_day_id", "event_id"],
            ["event_days.id", "event_days.event_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_day_event",
        ),
        sa.ForeignKeyConstraint(
            ["event_meal_role_id", "event_id"],
            ["event_meal_roles.id", "event_meal_roles.event_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_role_event",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id", "organization_id"],
            ["recipes.id", "recipes.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_recipe_organization",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_version_id", "organization_id"],
            ["recipe_versions.id", "recipe_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_recipe_version_organization",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_version_id", "recipe_id"],
            ["recipe_versions.id", "recipe_versions.recipe_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_recipe_version_recipe",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "event_id", "organization_id", name="uq_scheduled_recipes_id_event_organization"
        ),
    )
    op.create_index(
        "ix_scheduled_recipes_event_day_role_position",
        "scheduled_recipes",
        ["event_id", "event_day_id", "event_meal_role_id", "position_key"],
    )

    op.create_table(
        "scheduled_ingredient_overrides",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_recipe_id", sa.Uuid(), nullable=False),
        sa.Column("override_kind", sa.String(length=16), nullable=False),
        sa.Column("target_line_key", sa.Uuid(), nullable=True),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_version_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("include_in_portion_weight", sa.Boolean(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("position_key", sa.String(length=255, collation="C"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "last_modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_modified_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "override_kind IN ('replace', 'add')", name="ck_scheduled_overrides_kind"
        ),
        sa.CheckConstraint(
            "quantity >= 0 AND quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_scheduled_overrides_nonnegative_quantity",
        ),
        sa.CheckConstraint(
            "(override_kind = 'replace' AND target_line_key IS NOT NULL "
            "AND include_in_portion_weight IS NULL) OR "
            "(override_kind = 'add' AND target_line_key IS NULL "
            "AND include_in_portion_weight IS NOT NULL)",
            name="ck_scheduled_overrides_shape",
        ),
        sa.CheckConstraint(
            "position_key IS NULL OR position_key ~ '^[0-9A-Za-z]+$'",
            name="ck_scheduled_overrides_position_key",
        ),
        sa.CheckConstraint(
            "last_modified_at >= created_at", name="ck_scheduled_overrides_audit_order"
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_scheduled_overrides_retirement_attribution",
        ),
        sa.ForeignKeyConstraint(
            ["scheduled_recipe_id", "event_id", "organization_id"],
            [
                "scheduled_recipes.id",
                "scheduled_recipes.event_id",
                "scheduled_recipes.organization_id",
            ],
            ondelete="RESTRICT",
            name="fk_scheduled_overrides_scheduled_recipe",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id", "organization_id"],
            ["ingredients.id", "ingredients.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_overrides_ingredient_organization",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_version_id", "organization_id"],
            ["ingredient_versions.id", "ingredient_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_overrides_ingredient_version_organization",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_version_id", "ingredient_id"],
            ["ingredient_versions.id", "ingredient_versions.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_overrides_ingredient_version_ingredient",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["last_modified_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_scheduled_overrides_active_replacement",
        "scheduled_ingredient_overrides",
        ["scheduled_recipe_id", "target_line_key"],
        unique=True,
        postgresql_where=sa.text("override_kind = 'replace' AND retired_at IS NULL"),
    )

    op.execute(
        """
        CREATE FUNCTION cookops_validate_scheduled_ingredient_override()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            scheduled_recipe_version_id uuid;
            target_ingredient_id uuid;
        BEGIN
            IF NEW.retired_at IS NOT NULL THEN
                RETURN NULL;
            END IF;

            SELECT recipe_version_id
            INTO scheduled_recipe_version_id
            FROM scheduled_recipes
            WHERE id = NEW.scheduled_recipe_id
              AND event_id = NEW.event_id
              AND organization_id = NEW.organization_id;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'scheduled ingredient override requires its scheduled recipe';
            END IF;

            IF NEW.override_kind = 'replace' THEN
                SELECT ingredient_version.ingredient_id
                INTO target_ingredient_id
                FROM recipe_version_ingredient_lines AS line
                JOIN ingredient_versions AS ingredient_version
                  ON ingredient_version.id = line.ingredient_version_id
                WHERE line.recipe_version_id = scheduled_recipe_version_id
                  AND line.line_key = NEW.target_line_key;

                IF NOT FOUND OR target_ingredient_id <> NEW.ingredient_id THEN
                    RAISE EXCEPTION 'replacement override must target its pinned recipe line';
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM recipe_version_ingredient_lines AS line
                    WHERE line.recipe_version_id = scheduled_recipe_version_id
                      AND line.line_key = NEW.target_line_key
                      AND line.ingredient_version_id = NEW.ingredient_version_id
                ) THEN
                    RAISE EXCEPTION 'replacement override must retain pinned ingredient version';
                END IF;
            ELSIF EXISTS (
                SELECT 1
                FROM recipe_version_ingredient_lines AS line
                JOIN ingredient_versions AS ingredient_version
                  ON ingredient_version.id = line.ingredient_version_id
                WHERE line.recipe_version_id = scheduled_recipe_version_id
                  AND ingredient_version.ingredient_id = NEW.ingredient_id
            ) THEN
                RAISE EXCEPTION 'added override ingredient already exists in pinned recipe version';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER tr_scheduled_ingredient_override_validate
        AFTER INSERT OR UPDATE
        ON scheduled_ingredient_overrides
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION cookops_validate_scheduled_ingredient_override()
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_validate_scheduled_recipe_overrides()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM scheduled_ingredient_overrides AS override
                WHERE override.scheduled_recipe_id = NEW.id
                  AND override.event_id = NEW.event_id
                  AND override.organization_id = NEW.organization_id
                  AND override.retired_at IS NULL
                  AND (
                    (
                        override.override_kind = 'replace'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM recipe_version_ingredient_lines AS line
                            WHERE line.recipe_version_id = NEW.recipe_version_id
                              AND line.line_key = override.target_line_key
                              AND line.ingredient_version_id = override.ingredient_version_id
                        )
                    )
                    OR (
                        override.override_kind = 'add'
                        AND EXISTS (
                            SELECT 1
                            FROM recipe_version_ingredient_lines AS line
                            JOIN ingredient_versions AS ingredient_version
                              ON ingredient_version.id = line.ingredient_version_id
                            WHERE line.recipe_version_id = NEW.recipe_version_id
                              AND ingredient_version.ingredient_id = override.ingredient_id
                        )
                    )
                  )
            ) THEN
                RAISE EXCEPTION 'scheduled recipe version is incompatible with active overrides';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER tr_scheduled_recipe_override_validate
        AFTER UPDATE ON scheduled_recipes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION cookops_validate_scheduled_recipe_overrides()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tr_scheduled_recipe_override_validate ON scheduled_recipes")
    op.execute(
        "DROP TRIGGER IF EXISTS tr_scheduled_ingredient_override_validate "
        "ON scheduled_ingredient_overrides"
    )
    op.execute("DROP FUNCTION IF EXISTS cookops_validate_scheduled_recipe_overrides()")
    op.execute("DROP FUNCTION IF EXISTS cookops_validate_scheduled_ingredient_override()")
    op.drop_index("uq_scheduled_overrides_active_replacement", "scheduled_ingredient_overrides")
    op.drop_table("scheduled_ingredient_overrides")
    op.drop_index("ix_scheduled_recipes_event_day_role_position", "scheduled_recipes")
    op.drop_table("scheduled_recipes")
    op.drop_constraint("uq_event_meal_roles_id_event", "event_meal_roles", type_="unique")
    op.drop_constraint("uq_event_days_id_event", "event_days", type_="unique")
    op.drop_constraint("uq_events_id_organization", "events", type_="unique")
