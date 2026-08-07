"""Add immutable organization ingredient catalog records."""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa

from alembic import op

revision: str = "0006_ingredient_catalog"
down_revision: str | None = "0005_browser_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _system_unit(
    *,
    identifier: str,
    code: str,
    dimension: str,
    base_unit_factor: str | None,
    rounds_up_to_whole_unit: bool,
    allows_ingredient_quantity: bool,
    allows_recipe_scaling: bool,
) -> dict[str, object]:
    return {
        "id": UUID(identifier),
        "code": code,
        "dimension": dimension,
        "base_unit_factor": base_unit_factor,
        "rounds_up_to_whole_unit": rounds_up_to_whole_unit,
        "allows_ingredient_quantity": allows_ingredient_quantity,
        "allows_recipe_scaling": allows_recipe_scaling,
    }


SYSTEM_UNITS = (
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000001",
        code="g",
        dimension="mass",
        base_unit_factor="1",
        rounds_up_to_whole_unit=False,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=False,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000002",
        code="kg",
        dimension="mass",
        base_unit_factor="1000",
        rounds_up_to_whole_unit=False,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=False,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000003",
        code="ml",
        dimension="volume",
        base_unit_factor="1",
        rounds_up_to_whole_unit=False,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=False,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000004",
        code="cl",
        dimension="volume",
        base_unit_factor="10",
        rounds_up_to_whole_unit=False,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=False,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000005",
        code="dl",
        dimension="volume",
        base_unit_factor="100",
        rounds_up_to_whole_unit=False,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=False,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000006",
        code="l",
        dimension="volume",
        base_unit_factor="1000",
        rounds_up_to_whole_unit=False,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=True,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000007",
        code="tsp",
        dimension="volume",
        base_unit_factor="5",
        rounds_up_to_whole_unit=False,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=False,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000008",
        code="tbsp",
        dimension="volume",
        base_unit_factor="15",
        rounds_up_to_whole_unit=False,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=False,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000009",
        code="piece",
        dimension="count",
        base_unit_factor=None,
        rounds_up_to_whole_unit=True,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=True,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000010",
        code="package",
        dimension="count",
        base_unit_factor=None,
        rounds_up_to_whole_unit=True,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=False,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000011",
        code="bunch",
        dimension="count",
        base_unit_factor=None,
        rounds_up_to_whole_unit=True,
        allows_ingredient_quantity=True,
        allows_recipe_scaling=False,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000012",
        code="person",
        dimension="custom",
        base_unit_factor=None,
        rounds_up_to_whole_unit=False,
        allows_ingredient_quantity=False,
        allows_recipe_scaling=True,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000013",
        code="tray",
        dimension="custom",
        base_unit_factor=None,
        rounds_up_to_whole_unit=True,
        allows_ingredient_quantity=False,
        allows_recipe_scaling=True,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000014",
        code="batch",
        dimension="custom",
        base_unit_factor=None,
        rounds_up_to_whole_unit=True,
        allows_ingredient_quantity=False,
        allows_recipe_scaling=True,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000015",
        code="pot",
        dimension="custom",
        base_unit_factor=None,
        rounds_up_to_whole_unit=True,
        allows_ingredient_quantity=False,
        allows_recipe_scaling=True,
    ),
    _system_unit(
        identifier="00000000-0000-4000-8000-000000000016",
        code="loaf",
        dimension="custom",
        base_unit_factor=None,
        rounds_up_to_whole_unit=True,
        allows_ingredient_quantity=False,
        allows_recipe_scaling=True,
    ),
)


def upgrade() -> None:
    op.create_table(
        "unit_definitions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("custom_name", sa.String(length=200), nullable=True),
        sa.Column("normalized_custom_name", sa.String(length=200), nullable=True),
        sa.Column("dimension", sa.String(length=16), nullable=False),
        sa.Column("base_unit_factor", sa.Numeric(), nullable=True),
        sa.Column("rounds_up_to_whole_unit", sa.Boolean(), nullable=False),
        sa.Column("allows_ingredient_quantity", sa.Boolean(), nullable=False),
        sa.Column("allows_recipe_scaling", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(organization_id IS NULL AND custom_name IS NULL "
            "AND normalized_custom_name IS NULL AND created_by_user_id IS NULL "
            "AND retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(organization_id IS NOT NULL AND custom_name IS NOT NULL "
            "AND btrim(custom_name) <> '' AND normalized_custom_name IS NOT NULL "
            "AND normalized_custom_name = lower(btrim(custom_name)) "
            "AND created_by_user_id IS NOT NULL)",
            name="ck_unit_definitions_scope_and_display",
        ),
        sa.CheckConstraint("code ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="ck_unit_definitions_code"),
        sa.CheckConstraint(
            "dimension IN ('mass', 'volume', 'count', 'custom')",
            name="ck_unit_definitions_dimension",
        ),
        sa.CheckConstraint(
            "(dimension IN ('mass', 'volume') "
            "AND base_unit_factor IS NOT NULL AND base_unit_factor > 0 "
            "AND base_unit_factor::text NOT IN ('NaN', 'Infinity', '-Infinity')) OR "
            "(dimension IN ('count', 'custom') AND base_unit_factor IS NULL)",
            name="ck_unit_definitions_base_factor",
        ),
        sa.CheckConstraint(
            "allows_ingredient_quantity OR allows_recipe_scaling",
            name="ck_unit_definitions_permitted_context",
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_unit_definitions_retirement_attribution",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_unit_definitions_id_organization"),
    )
    op.create_index(
        "uq_unit_definitions_system_code",
        "unit_definitions",
        ["code"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NULL"),
    )
    op.create_index(
        "uq_unit_definitions_organization_code",
        "unit_definitions",
        ["organization_id", "code"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )
    op.create_index(
        "uq_unit_definitions_organization_name",
        "unit_definitions",
        ["organization_id", "normalized_custom_name"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )
    units = sa.table(
        "unit_definitions",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("dimension", sa.String()),
        sa.column("base_unit_factor", sa.Numeric()),
        sa.column("rounds_up_to_whole_unit", sa.Boolean()),
        sa.column("allows_ingredient_quantity", sa.Boolean()),
        sa.column("allows_recipe_scaling", sa.Boolean()),
    )
    op.bulk_insert(units, list(SYSTEM_UNITS))

    # Composite keys make every organization boundary explicit in later catalog rows.
    op.create_unique_constraint(
        "uq_dietary_tags_id_organization", "dietary_tags", ["id", "organization_id"]
    )
    op.create_unique_constraint(
        "uq_store_sections_id_organization", "store_sections", ["id", "organization_id"]
    )

    op.create_table(
        "ingredients",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("current_price_estimate_id", sa.Uuid(), nullable=True),
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
            name="ck_ingredients_retirement_attribution",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_ingredients_id_organization"),
    )
    op.create_table(
        "ingredient_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("based_on_version_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("canonical_unit_id", sa.Uuid(), nullable=False),
        sa.Column("mass_per_canonical_quantity", sa.Numeric(), nullable=False),
        sa.Column("default_store_section_id", sa.Uuid(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "btrim(name) <> '' AND normalized_name = lower(btrim(name))",
            name="ck_ingredient_versions_normalized_name",
        ),
        sa.CheckConstraint(
            "mass_per_canonical_quantity > 0 "
            "AND mass_per_canonical_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_ingredient_versions_positive_mass_conversion",
        ),
        sa.CheckConstraint(
            "based_on_version_id IS NULL OR based_on_version_id <> id",
            name="ck_ingredient_versions_nonrecursive_base",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id", "organization_id"],
            ["ingredients.id", "ingredients.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_versions_ingredient_organization",
        ),
        sa.ForeignKeyConstraint(
            ["based_on_version_id", "ingredient_id"],
            ["ingredient_versions.id", "ingredient_versions.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_versions_based_on_same_ingredient",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_unit_id"], ["unit_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["default_store_section_id", "organization_id"],
            ["store_sections.id", "store_sections.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_versions_store_section_organization",
        ),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_ingredient_versions_id_organization"),
        sa.UniqueConstraint("id", "ingredient_id", name="uq_ingredient_versions_id_ingredient"),
    )
    op.create_table(
        "ingredient_version_dietary_tags",
        sa.Column("ingredient_version_id", sa.Uuid(), nullable=False),
        sa.Column("dietary_tag_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ingredient_version_id", "organization_id"],
            ["ingredient_versions.id", "ingredient_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_version_tags_version_organization",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["dietary_tag_id", "organization_id"],
            ["dietary_tags.id", "dietary_tags.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_version_tags_tag_organization",
        ),
        sa.PrimaryKeyConstraint("ingredient_version_id", "dietary_tag_id"),
    )
    op.create_index(
        "ix_ingredient_versions_ingredient_id", "ingredient_versions", ["ingredient_id"]
    )
    op.create_table(
        "ingredient_price_estimates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("based_on_estimate_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("price_amount", sa.Numeric(), nullable=True),
        sa.Column("priced_quantity", sa.Numeric(), nullable=True),
        sa.Column("priced_unit_id", sa.Uuid(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "state IN ('available', 'unavailable')",
            name="ck_ingredient_price_estimates_state",
        ),
        sa.CheckConstraint(
            "(state = 'available' AND price_amount IS NOT NULL "
            "AND price_amount >= 0 "
            "AND price_amount::text NOT IN ('NaN', 'Infinity', '-Infinity') "
            "AND priced_quantity IS NOT NULL AND priced_quantity > 0 "
            "AND priced_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity') "
            "AND priced_unit_id IS NOT NULL "
            "AND currency IS NOT NULL AND currency ~ '^[A-Z]{3}$') OR "
            "(state = 'unavailable' AND price_amount IS NULL "
            "AND priced_quantity IS NULL AND priced_unit_id IS NULL AND currency IS NULL)",
            name="ck_ingredient_price_estimates_value_shape",
        ),
        sa.CheckConstraint(
            "based_on_estimate_id IS NULL OR based_on_estimate_id <> id",
            name="ck_ingredient_price_estimates_nonrecursive_base",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id", "organization_id"],
            ["ingredients.id", "ingredients.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_price_estimates_ingredient_organization",
        ),
        sa.ForeignKeyConstraint(
            ["based_on_estimate_id", "ingredient_id"],
            ["ingredient_price_estimates.id", "ingredient_price_estimates.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_price_estimates_based_on_same_ingredient",
        ),
        sa.ForeignKeyConstraint(["priced_unit_id"], ["unit_definitions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "ingredient_id", name="uq_ingredient_price_estimates_id_ingredient"
        ),
    )
    op.create_foreign_key(
        "fk_ingredients_current_version",
        "ingredients",
        "ingredient_versions",
        ["current_version_id", "id"],
        ["id", "ingredient_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_ingredients_current_price_estimate",
        "ingredients",
        "ingredient_price_estimates",
        ["current_price_estimate_id", "id"],
        ["id", "ingredient_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.execute(
        """
        CREATE FUNCTION cookops_validate_unit_definition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND NEW.organization_id IS NULL THEN
                RAISE EXCEPTION 'system unit definitions are migration seeds only';
            END IF;
            IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.organization_id IS NULL THEN
                RAISE EXCEPTION 'system unit definitions are read-only';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'organization unit definitions use reversible retirement';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
                    OR NEW.code IS DISTINCT FROM OLD.code THEN
                    RAISE EXCEPTION 'unit scope and code are immutable';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM ingredient_versions WHERE canonical_unit_id = OLD.id
                    UNION ALL
                    SELECT 1 FROM ingredient_price_estimates WHERE priced_unit_id = OLD.id
                ) AND (
                    NEW.dimension IS DISTINCT FROM OLD.dimension
                    OR NEW.base_unit_factor IS DISTINCT FROM OLD.base_unit_factor
                    OR NEW.rounds_up_to_whole_unit IS DISTINCT FROM OLD.rounds_up_to_whole_unit
                    OR NEW.allows_ingredient_quantity <> OLD.allows_ingredient_quantity
                    OR NEW.allows_recipe_scaling <> OLD.allows_recipe_scaling
                ) THEN
                    RAISE EXCEPTION 'used unit semantics are immutable';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_validate_ingredient_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            unit_organization_id uuid;
            unit_dimension text;
            unit_base_factor numeric;
            unit_allows_ingredient_quantity boolean;
            unit_retired_at timestamptz;
        BEGIN
            IF NEW.published_at IS DISTINCT FROM CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'ingredient version publication time is server-owned';
            END IF;
            SELECT organization_id, dimension, base_unit_factor, allows_ingredient_quantity,
                retired_at
            INTO unit_organization_id, unit_dimension, unit_base_factor,
                unit_allows_ingredient_quantity, unit_retired_at
            FROM unit_definitions
            WHERE id = NEW.canonical_unit_id;

            IF NOT FOUND OR NOT unit_allows_ingredient_quantity OR unit_retired_at IS NOT NULL
                OR (unit_organization_id IS NOT NULL
                    AND unit_organization_id <> NEW.organization_id) THEN
                RAISE EXCEPTION 'ingredient canonical unit is not available to this organization';
            END IF;
            IF unit_dimension = 'mass'
                AND NEW.mass_per_canonical_quantity <> unit_base_factor THEN
                RAISE EXCEPTION 'mass unit must use its built-in mass conversion';
            END IF;
            PERFORM 1 FROM ingredients
            WHERE id = NEW.ingredient_id AND organization_id = NEW.organization_id
            FOR UPDATE;
            IF EXISTS (
                SELECT 1
                FROM ingredient_versions AS existing_version
                JOIN unit_definitions AS existing_unit
                    ON existing_unit.id = existing_version.canonical_unit_id
                WHERE existing_version.ingredient_id = NEW.ingredient_id
                    AND (existing_unit.dimension <> unit_dimension
                        OR (unit_dimension IN ('count', 'custom')
                            AND existing_version.canonical_unit_id <> NEW.canonical_unit_id))
            ) THEN
                RAISE EXCEPTION 'ingredient versions require compatible quantity semantics';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_validate_ingredient_price_estimate()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            price_unit_organization_id uuid;
            price_unit_dimension text;
            price_unit_allows_ingredient_quantity boolean;
            price_unit_retired_at timestamptz;
            canonical_unit_id uuid;
            canonical_dimension text;
            organization_currency text;
        BEGIN
            IF NEW.state = 'unavailable' THEN
                RETURN NEW;
            END IF;
            SELECT organization_id, dimension, allows_ingredient_quantity, retired_at
            INTO price_unit_organization_id, price_unit_dimension,
                price_unit_allows_ingredient_quantity, price_unit_retired_at
            FROM unit_definitions
            WHERE id = NEW.priced_unit_id;
            IF NOT FOUND OR NOT price_unit_allows_ingredient_quantity
                OR price_unit_retired_at IS NOT NULL
                OR (price_unit_organization_id IS NOT NULL
                    AND price_unit_organization_id <> NEW.organization_id) THEN
                RAISE EXCEPTION 'price unit is not available to this organization';
            END IF;
            SELECT default_currency INTO organization_currency
            FROM organizations WHERE id = NEW.organization_id;
            IF NEW.currency <> organization_currency THEN
                RAISE EXCEPTION 'price currency must match organization default currency';
            END IF;
            SELECT version.canonical_unit_id INTO canonical_unit_id
            FROM ingredients AS ingredient
            JOIN ingredient_versions AS version
                ON version.id = ingredient.current_version_id
            WHERE ingredient.id = NEW.ingredient_id
                AND ingredient.organization_id = NEW.organization_id;
            IF canonical_unit_id IS NULL THEN
                RAISE EXCEPTION 'ingredient requires a current version before pricing';
            END IF;
            SELECT dimension INTO canonical_dimension
            FROM unit_definitions
            WHERE id = canonical_unit_id;
            IF canonical_dimension <> price_unit_dimension
                OR (canonical_dimension IN ('count', 'custom')
                    AND canonical_unit_id <> NEW.priced_unit_id) THEN
                RAISE EXCEPTION 'price unit is incompatible with ingredient canonical unit';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_validate_ingredient_version_tag()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
        BEGIN
            PERFORM 1
            FROM ingredient_versions WHERE id = NEW.ingredient_version_id;
            IF FOUND THEN
                RAISE EXCEPTION 'ingredient version dietary tags must be published atomically';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_validate_ingredient_current_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.current_version_id IS NULL THEN
                RAISE EXCEPTION 'ingredient requires a current version';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_prevent_ingredient_deletion()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'ingredients use reversible retirement';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION cookops_prevent_immutable_catalog_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'immutable CookOps catalog records cannot be modified';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_unit_definitions_validate "
        "BEFORE INSERT OR UPDATE OR DELETE ON unit_definitions "
        "FOR EACH ROW EXECUTE FUNCTION cookops_validate_unit_definition()"
    )
    op.execute(
        "CREATE TRIGGER trg_ingredient_versions_validate "
        "BEFORE INSERT ON ingredient_versions "
        "FOR EACH ROW EXECUTE FUNCTION cookops_validate_ingredient_version()"
    )
    op.execute(
        "CREATE TRIGGER trg_ingredient_version_tags_validate "
        "BEFORE INSERT ON ingredient_version_dietary_tags "
        "FOR EACH ROW EXECUTE FUNCTION cookops_validate_ingredient_version_tag()"
    )
    op.execute(
        "CREATE TRIGGER trg_ingredient_price_estimates_validate "
        "BEFORE INSERT ON ingredient_price_estimates "
        "FOR EACH ROW EXECUTE FUNCTION cookops_validate_ingredient_price_estimate()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_ingredients_require_current_version "
        "AFTER INSERT OR UPDATE OF current_version_id ON ingredients "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION cookops_validate_ingredient_current_version()"
    )
    op.execute(
        "CREATE TRIGGER trg_ingredients_no_delete BEFORE DELETE ON ingredients "
        "FOR EACH ROW EXECUTE FUNCTION cookops_prevent_ingredient_deletion()"
    )
    for table_name in (
        "ingredient_versions",
        "ingredient_version_dietary_tags",
        "ingredient_price_estimates",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_immutable "
            f"BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION cookops_prevent_immutable_catalog_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_ingredients_no_delete ON ingredients")
    op.execute("DROP TRIGGER trg_ingredients_require_current_version ON ingredients")
    op.execute("DROP TRIGGER trg_ingredient_price_estimates_validate ON ingredient_price_estimates")
    op.execute(
        "DROP TRIGGER trg_ingredient_version_tags_validate ON ingredient_version_dietary_tags"
    )
    op.execute("DROP TRIGGER trg_ingredient_versions_validate ON ingredient_versions")
    op.execute("DROP TRIGGER trg_unit_definitions_validate ON unit_definitions")
    for table_name in (
        "ingredient_price_estimates",
        "ingredient_version_dietary_tags",
        "ingredient_versions",
    ):
        op.execute(f"DROP TRIGGER trg_{table_name}_immutable ON {table_name}")
    op.execute("DROP FUNCTION cookops_prevent_immutable_catalog_mutation()")
    op.execute("DROP FUNCTION cookops_validate_ingredient_price_estimate()")
    op.execute("DROP FUNCTION cookops_validate_ingredient_version_tag()")
    op.execute("DROP FUNCTION cookops_validate_ingredient_current_version()")
    op.execute("DROP FUNCTION cookops_prevent_ingredient_deletion()")
    op.execute("DROP FUNCTION cookops_validate_ingredient_version()")
    op.execute("DROP FUNCTION cookops_validate_unit_definition()")
    op.drop_constraint("fk_ingredients_current_price_estimate", "ingredients", type_="foreignkey")
    op.drop_constraint("fk_ingredients_current_version", "ingredients", type_="foreignkey")
    op.drop_table("ingredient_price_estimates")
    op.drop_table("ingredient_version_dietary_tags")
    op.drop_index("ix_ingredient_versions_ingredient_id", table_name="ingredient_versions")
    op.drop_table("ingredient_versions")
    op.drop_table("ingredients")
    op.drop_constraint("uq_store_sections_id_organization", "store_sections", type_="unique")
    op.drop_constraint("uq_dietary_tags_id_organization", "dietary_tags", type_="unique")
    op.drop_index("uq_unit_definitions_organization_name", table_name="unit_definitions")
    op.drop_index("uq_unit_definitions_organization_code", table_name="unit_definitions")
    op.drop_index("uq_unit_definitions_system_code", table_name="unit_definitions")
    op.drop_table("unit_definitions")
