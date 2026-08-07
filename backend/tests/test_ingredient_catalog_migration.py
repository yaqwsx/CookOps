import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, insert, inspect, select, update
from sqlalchemy.exc import DatabaseError, IntegrityError

from alembic import command
from cookops.persistence.models import (
    DietaryTag,
    Ingredient,
    IngredientPriceEstimate,
    IngredientVersion,
    IngredientVersionDietaryTag,
    Organization,
    StoreSection,
    UnitDefinition,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

CATALOG_TABLES = {
    "ingredients",
    "ingredient_price_estimates",
    "ingredient_version_dietary_tags",
    "ingredient_versions",
    "unit_definitions",
}


@dataclass
class MigrationDatabase:
    configuration: Config
    engine: Engine


@pytest.fixture
def migration_database() -> Iterator[MigrationDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.downgrade(configuration, "base")
    engine = create_engine(database_url)
    try:
        yield MigrationDatabase(configuration, engine)
    finally:
        engine.dispose()
        command.downgrade(configuration, "base")


def seed_organizations(engine: Engine) -> tuple[UUID, UUID, UUID]:
    actor_id = uuid4()
    organization_id = uuid4()
    other_organization_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Catalog editor",
                verified_email="catalog@example.test",
                normalized_email="catalog@example.test",
            )
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": organization_id,
                    "name": "Primary organization",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_organization_id,
                    "name": "Other organization",
                    "created_by_user_id": actor_id,
                },
            ],
        )
    return actor_id, organization_id, other_organization_id


def system_unit_id(engine: Engine, code: str) -> UUID:
    with engine.connect() as connection:
        result = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == code
            )
        )
    assert result is not None
    return result


def insert_ingredient_with_version(
    engine: Engine,
    *,
    actor_id: UUID,
    organization_id: UUID,
    unit_id: UUID,
    name: str = "Tomatoes",
    ingredient_id: UUID | None = None,
    version_id: UUID | None = None,
    mass_per_canonical_quantity: Decimal = Decimal("1"),
    default_store_section_id: UUID | None = None,
    dietary_tag_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    ingredient_id = ingredient_id or uuid4()
    version_id = version_id or uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(Ingredient).values(
                id=ingredient_id,
                organization_id=organization_id,
                current_version_id=version_id,
                created_by_user_id=actor_id,
            )
        )
        if dietary_tag_id is not None:
            connection.execute(
                insert(IngredientVersionDietaryTag).values(
                    ingredient_version_id=version_id,
                    dietary_tag_id=dietary_tag_id,
                    organization_id=organization_id,
                )
            )
        connection.execute(
            insert(IngredientVersion).values(
                id=version_id,
                organization_id=organization_id,
                ingredient_id=ingredient_id,
                name=name,
                normalized_name=name.lower(),
                canonical_unit_id=unit_id,
                mass_per_canonical_quantity=mass_per_canonical_quantity,
                default_store_section_id=default_store_section_id,
                published_by_user_id=actor_id,
            )
        )
    return ingredient_id, version_id


def test_catalog_migration_parity_seeds_units_and_downgrades(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine

    command.upgrade(configuration, "head")
    assert set(inspect(engine).get_table_names()) >= CATALOG_TABLES
    command.check(configuration)

    with engine.connect() as connection:
        units = connection.execute(
            select(
                UnitDefinition.code,
                UnitDefinition.dimension,
                UnitDefinition.base_unit_factor,
                UnitDefinition.allows_ingredient_quantity,
                UnitDefinition.allows_recipe_scaling,
            ).where(UnitDefinition.organization_id.is_(None))
        ).all()
    assert {(row.code, row.dimension) for row in units} == {
        ("g", "mass"),
        ("kg", "mass"),
        ("ml", "volume"),
        ("cl", "volume"),
        ("dl", "volume"),
        ("l", "volume"),
        ("tsp", "volume"),
        ("tbsp", "volume"),
        ("piece", "count"),
        ("package", "count"),
        ("bunch", "count"),
        ("person", "custom"),
        ("tray", "custom"),
        ("batch", "custom"),
        ("pot", "custom"),
        ("loaf", "custom"),
    }
    unit_by_code = {row.code: row for row in units}
    assert unit_by_code["kg"].base_unit_factor == Decimal("1000")
    assert unit_by_code["tsp"].base_unit_factor == Decimal("5")
    assert unit_by_code["piece"].base_unit_factor is None
    assert unit_by_code["person"].allows_recipe_scaling is True
    assert unit_by_code["person"].allows_ingredient_quantity is False

    command.downgrade(configuration, "0005_browser_sessions")
    assert CATALOG_TABLES.isdisjoint(inspect(engine).get_table_names())


def test_catalog_constraints_organization_boundaries_and_immutability(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, other_organization_id = seed_organizations(engine)
    grams_id = system_unit_id(engine, "g")
    kilograms_id = system_unit_id(engine, "kg")
    milliliters_id = system_unit_id(engine, "ml")
    now = datetime.now(UTC)
    section_id = uuid4()
    dietary_tag_id = uuid4()

    with engine.begin() as connection:
        connection.execute(
            insert(StoreSection).values(
                id=section_id,
                organization_id=organization_id,
                name="Produce",
                normalized_name="produce",
                position_key="a",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=dietary_tag_id,
                organization_id=organization_id,
                name="Nightshade",
                normalized_name="nightshade",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(UnitDefinition).values(
                organization_id=organization_id,
                code="scoop",
                custom_name="Scoop",
                normalized_custom_name="scoop",
                dimension="custom",
                rounds_up_to_whole_unit=True,
                allows_ingredient_quantity=True,
                allows_recipe_scaling=False,
                created_by_user_id=actor_id,
            )
        )

    ingredient_id, version_id = insert_ingredient_with_version(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        unit_id=grams_id,
        default_store_section_id=section_id,
        dietary_tag_id=dietary_tag_id,
    )
    price_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(IngredientPriceEstimate).values(
                id=price_id,
                organization_id=organization_id,
                ingredient_id=ingredient_id,
                state="available",
                price_amount=Decimal("35.50"),
                priced_quantity=Decimal("1"),
                priced_unit_id=kilograms_id,
                currency="CZK",
                published_by_user_id=actor_id,
            )
        )
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == ingredient_id)
            .values(current_price_estimate_id=price_id)
        )

    with engine.connect() as connection:
        custom_unit_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id == organization_id,
                UnitDefinition.code == "scoop",
            )
        )
    assert custom_unit_id is not None

    with engine.connect() as connection:
        current = connection.execute(
            select(Ingredient.current_version_id, Ingredient.current_price_estimate_id).where(
                Ingredient.id == ingredient_id
            )
        ).one()
        assert current == (version_id, price_id)
        assert (
            connection.scalar(
                select(IngredientVersionDietaryTag.dietary_tag_id).where(
                    IngredientVersionDietaryTag.ingredient_version_id == version_id
                )
            )
            == dietary_tag_id
        )

    other_ingredient_id, other_version_id = insert_ingredient_with_version(
        engine,
        actor_id=actor_id,
        organization_id=other_organization_id,
        unit_id=grams_id,
        name="Other tomatoes",
    )
    invalid_statements = [
        insert(UnitDefinition).values(
            organization_id=organization_id,
            code="Scoop",
            custom_name="Bad code",
            normalized_custom_name="bad code",
            dimension="custom",
            rounds_up_to_whole_unit=False,
            allows_ingredient_quantity=True,
            allows_recipe_scaling=False,
            created_by_user_id=actor_id,
        ),
        insert(UnitDefinition).values(
            organization_id=organization_id,
            code="empty",
            custom_name="Empty",
            normalized_custom_name="empty",
            dimension="custom",
            rounds_up_to_whole_unit=False,
            allows_ingredient_quantity=False,
            allows_recipe_scaling=False,
            created_by_user_id=actor_id,
        ),
        insert(IngredientVersion).values(
            id=uuid4(),
            organization_id=organization_id,
            ingredient_id=ingredient_id,
            name="No mass",
            normalized_name="no mass",
            canonical_unit_id=grams_id,
            mass_per_canonical_quantity=Decimal("0"),
            published_by_user_id=actor_id,
        ),
        insert(IngredientVersion).values(
            id=uuid4(),
            organization_id=organization_id,
            ingredient_id=ingredient_id,
            based_on_version_id=other_version_id,
            name="Cross ingredient base",
            normalized_name="cross ingredient base",
            canonical_unit_id=grams_id,
            mass_per_canonical_quantity=Decimal("1"),
            published_by_user_id=actor_id,
        ),
        insert(IngredientVersionDietaryTag).values(
            ingredient_version_id=version_id,
            dietary_tag_id=dietary_tag_id,
            organization_id=other_organization_id,
        ),
        insert(IngredientPriceEstimate).values(
            id=uuid4(),
            organization_id=organization_id,
            ingredient_id=ingredient_id,
            state="available",
            price_amount=Decimal("1"),
            priced_quantity=Decimal("0"),
            priced_unit_id=grams_id,
            currency="CZK",
            published_by_user_id=actor_id,
        ),
        insert(IngredientPriceEstimate).values(
            id=uuid4(),
            organization_id=organization_id,
            ingredient_id=ingredient_id,
            state="available",
            price_amount=Decimal("1"),
            priced_quantity=Decimal("1"),
            priced_unit_id=grams_id,
            currency=None,
            published_by_user_id=actor_id,
        ),
        insert(IngredientPriceEstimate).values(
            id=uuid4(),
            organization_id=organization_id,
            ingredient_id=ingredient_id,
            state="available",
            price_amount=Decimal("1"),
            priced_quantity=Decimal("1"),
            priced_unit_id=grams_id,
            currency="EUR",
            published_by_user_id=actor_id,
        ),
        insert(IngredientPriceEstimate).values(
            id=uuid4(),
            organization_id=organization_id,
            ingredient_id=ingredient_id,
            state="unavailable",
            price_amount=Decimal("1"),
            published_by_user_id=actor_id,
        ),
    ]
    with engine.begin() as connection:
        for statement in invalid_statements:
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(statement)

        with pytest.raises(DatabaseError), connection.begin_nested():
            connection.execute(
                update(UnitDefinition)
                .where(UnitDefinition.id == grams_id)
                .values(rounds_up_to_whole_unit=True)
            )

        with pytest.raises(DatabaseError), connection.begin_nested():
            connection.execute(
                insert(UnitDefinition).values(
                    code="untrusted-system-unit",
                    dimension="custom",
                    rounds_up_to_whole_unit=False,
                    allows_ingredient_quantity=True,
                    allows_recipe_scaling=False,
                )
            )

        # A small deterministic boundary sweep protects the positive-conversion
        # invariant from ordinary decimal edge values without depending on floats.
        for bad_mass in (Decimal("0"), Decimal("-0.0000001"), Decimal("-1")):
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(
                    insert(IngredientVersion).values(
                        id=uuid4(),
                        organization_id=organization_id,
                        ingredient_id=ingredient_id,
                        name=f"Invalid mass {bad_mass}",
                        normalized_name=f"invalid mass {bad_mass}",
                        canonical_unit_id=grams_id,
                        mass_per_canonical_quantity=bad_mass,
                        published_by_user_id=actor_id,
                    )
                )

    # These checks need catalog context and are deliberately enforced by database
    # triggers rather than trusting a future application-service caller.
    with engine.begin() as connection:
        for unit_update in (
            insert(IngredientVersion).values(
                id=uuid4(),
                organization_id=organization_id,
                ingredient_id=ingredient_id,
                name="Wrong kilogram conversion",
                normalized_name="wrong kilogram conversion",
                canonical_unit_id=kilograms_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            ),
            insert(IngredientVersion).values(
                id=uuid4(),
                organization_id=organization_id,
                ingredient_id=ingredient_id,
                name="Incompatible volume version",
                normalized_name="incompatible volume version",
                canonical_unit_id=milliliters_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            ),
            insert(IngredientVersion).values(
                id=uuid4(),
                organization_id=other_organization_id,
                ingredient_id=other_ingredient_id,
                name="Foreign custom unit",
                normalized_name="foreign custom unit",
                canonical_unit_id=custom_unit_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            ),
            insert(IngredientPriceEstimate).values(
                id=uuid4(),
                organization_id=organization_id,
                ingredient_id=ingredient_id,
                state="available",
                price_amount=Decimal("1"),
                priced_quantity=Decimal("1"),
                priced_unit_id=custom_unit_id,
                currency="CZK",
                published_by_user_id=actor_id,
            ),
            insert(IngredientPriceEstimate).values(
                id=uuid4(),
                organization_id=organization_id,
                ingredient_id=ingredient_id,
                state="available",
                price_amount=Decimal("NaN"),
                priced_quantity=Decimal("1"),
                priced_unit_id=grams_id,
                currency="CZK",
                published_by_user_id=actor_id,
            ),
        ):
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(unit_update)

    # The pointer constraints are intentionally deferred so one transaction can
    # create a root, initial immutable version, and pointer atomically.
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == ingredient_id)
            .values(current_version_id=other_version_id)
        )

    with pytest.raises(DatabaseError), engine.begin() as connection:
        connection.execute(
            insert(Ingredient).values(
                organization_id=organization_id,
                created_by_user_id=actor_id,
            )
        )

    with pytest.raises(DatabaseError), engine.begin() as connection:
        connection.execute(
            insert(IngredientVersionDietaryTag).values(
                ingredient_version_id=version_id,
                dietary_tag_id=dietary_tag_id,
                organization_id=organization_id,
            )
        )

    initial_graph_ingredient_id = uuid4()
    initial_graph_grams_version_id = uuid4()
    with pytest.raises(DatabaseError), engine.begin() as connection:
        connection.execute(
            insert(Ingredient).values(
                id=initial_graph_ingredient_id,
                organization_id=organization_id,
                current_version_id=initial_graph_grams_version_id,
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=uuid4(),
                organization_id=organization_id,
                ingredient_id=initial_graph_ingredient_id,
                name="Initial graph volume",
                normalized_name="initial graph volume",
                canonical_unit_id=milliliters_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=initial_graph_grams_version_id,
                organization_id=organization_id,
                ingredient_id=initial_graph_ingredient_id,
                name="Initial graph mass",
                normalized_name="initial graph mass",
                canonical_unit_id=grams_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            )
        )

    custom_ingredient_id, _ = insert_ingredient_with_version(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        unit_id=custom_unit_id,
        name="Custom scoop ingredient",
        mass_per_canonical_quantity=Decimal("50"),
    )
    with engine.begin() as connection:
        for custom_unit_update in (
            update(UnitDefinition)
            .where(UnitDefinition.id == custom_unit_id)
            .values(organization_id=other_organization_id),
            update(UnitDefinition)
            .where(UnitDefinition.id == custom_unit_id)
            .values(dimension="count"),
        ):
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(custom_unit_update)
        connection.execute(
            update(UnitDefinition)
            .where(UnitDefinition.id == custom_unit_id)
            .values(retired_at=now, retired_by_user_id=actor_id)
        )
    with pytest.raises(DatabaseError), engine.begin() as connection:
        connection.execute(
            insert(IngredientVersion).values(
                id=uuid4(),
                organization_id=organization_id,
                ingredient_id=custom_ingredient_id,
                name="Retired custom unit reuse",
                normalized_name="retired custom unit reuse",
                canonical_unit_id=custom_unit_id,
                mass_per_canonical_quantity=Decimal("50"),
                published_by_user_id=actor_id,
            )
        )

    with engine.begin() as connection:
        for immutable_statement in (
            update(IngredientVersion)
            .where(IngredientVersion.id == version_id)
            .values(name="Changed"),
            delete(IngredientVersionDietaryTag).where(
                IngredientVersionDietaryTag.ingredient_version_id == version_id
            ),
            update(IngredientPriceEstimate)
            .where(IngredientPriceEstimate.id == price_id)
            .values(price_amount=Decimal("99")),
        ):
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(immutable_statement)

        connection.execute(
            update(UnitDefinition)
            .where(UnitDefinition.organization_id == organization_id)
            .where(UnitDefinition.code == "scoop")
            .values(retired_at=now, retired_by_user_id=actor_id)
        )
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == ingredient_id)
            .values(retired_at=now, retired_by_user_id=actor_id)
        )

    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(UnitDefinition.retired_at).where(
                    UnitDefinition.organization_id == organization_id,
                    UnitDefinition.code == "scoop",
                )
            )
            == now
        )
        assert (
            connection.scalar(select(Ingredient.retired_at).where(Ingredient.id == ingredient_id))
            == now
        )
        assert connection.scalar(select(Ingredient.id).where(Ingredient.id == other_ingredient_id))
