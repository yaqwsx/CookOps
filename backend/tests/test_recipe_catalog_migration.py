import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, insert, inspect, select, text, update
from sqlalchemy.exc import DatabaseError

from alembic import command
from cookops.persistence.models import (
    Ingredient,
    IngredientVersion,
    Organization,
    Recipe,
    RecipeTag,
    RecipeVersion,
    RecipeVersionIngredientLine,
    RecipeVersionTag,
    UnitDefinition,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

RECIPE_TABLES = {
    "recipes",
    "recipe_versions",
    "recipe_version_tags",
    "recipe_version_ingredient_lines",
}


@dataclass
class MigrationDatabase:
    configuration: Config
    engine: Engine


@pytest.fixture
def migration_database() -> Iterator[MigrationDatabase]:
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    command.downgrade(configuration, "base")
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    try:
        yield MigrationDatabase(configuration, engine)
    finally:
        engine.dispose()
        command.downgrade(configuration, "base")


def seed_catalog_context(engine: Engine) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    actor_id = uuid4()
    organization_id = uuid4()
    other_organization_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Recipe editor",
                verified_email="recipe-editor@example.test",
                normalized_email="recipe-editor@example.test",
            )
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": organization_id,
                    "name": "Primary recipe organization",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_organization_id,
                    "name": "Other recipe organization",
                    "created_by_user_id": actor_id,
                },
            ],
        )
    with engine.connect() as connection:
        grams_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        person_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "person"
            )
        )
    assert grams_id is not None
    assert person_id is not None
    return actor_id, organization_id, other_organization_id, grams_id, person_id


def insert_ingredient(
    engine: Engine,
    *,
    actor_id: UUID,
    organization_id: UUID,
    grams_id: UUID,
    name: str,
) -> tuple[UUID, UUID]:
    ingredient_id = uuid4()
    version_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(Ingredient).values(
                id=ingredient_id,
                organization_id=organization_id,
                current_version_id=version_id,
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=version_id,
                organization_id=organization_id,
                ingredient_id=ingredient_id,
                name=name,
                normalized_name=name.lower(),
                canonical_unit_id=grams_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            )
        )
    return ingredient_id, version_id


def publish_recipe(
    engine: Engine,
    *,
    actor_id: UUID,
    organization_id: UUID,
    scaling_unit_id: UUID,
    preferred_display_unit_id: UUID,
    ingredient_version_id: UUID,
    recipe_id: UUID | None = None,
    version_id: UUID | None = None,
    based_on_version_id: UUID | None = None,
    line_key: UUID | None = None,
    recipe_tag_id: UUID | None = None,
    scaling_behavior: str = "proportional",
) -> tuple[UUID, UUID, UUID]:
    recipe_id = recipe_id or uuid4()
    version_id = version_id or uuid4()
    line_id = uuid4()
    with engine.begin() as connection:
        if based_on_version_id is None:
            connection.execute(
                insert(Recipe).values(
                    id=recipe_id,
                    organization_id=organization_id,
                    current_version_id=version_id,
                    created_by_user_id=actor_id,
                )
            )
        if recipe_tag_id is not None:
            connection.execute(
                insert(RecipeVersionTag).values(
                    recipe_version_id=version_id,
                    recipe_tag_id=recipe_tag_id,
                    organization_id=organization_id,
                )
            )
        connection.execute(
            insert(RecipeVersionIngredientLine).values(
                id=line_id,
                organization_id=organization_id,
                recipe_id=recipe_id,
                recipe_version_id=version_id,
                line_key=line_key or uuid4(),
                ingredient_version_id=ingredient_version_id,
                base_quantity=Decimal("500"),
                preferred_display_unit_id=preferred_display_unit_id,
                note="Finely chopped",
                position_key="a",
                scaling_behavior=scaling_behavior,
                include_in_portion_weight=True,
            )
        )
        connection.execute(
            insert(RecipeVersion).values(
                id=version_id,
                organization_id=organization_id,
                recipe_id=recipe_id,
                based_on_version_id=based_on_version_id,
                name="Tomato soup",
                description="[Cook slowly](https://example.test/recipe)",
                scaling_unit_id=scaling_unit_id,
                base_scaling_amount=Decimal("10"),
                estimated_diners_per_scaling_unit=Decimal("10"),
                round_suggestions_up=False,
                published_by_user_id=actor_id,
            )
        )
        if based_on_version_id is not None:
            connection.execute(
                update(Recipe).where(Recipe.id == recipe_id).values(current_version_id=version_id)
            )
    return recipe_id, version_id, line_id


def test_recipe_catalog_migration_parity_upgrade_and_downgrade(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine

    command.upgrade(configuration, "0009_organization_change_feed")
    actor_id, organization_id, _, grams_id, person_id = seed_catalog_context(engine)
    _, ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        grams_id=grams_id,
        name="Tomatoes",
    )

    command.upgrade(configuration, "head")
    command.check(configuration)
    assert set(inspect(engine).get_table_names()) >= RECIPE_TABLES
    publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=ingredient_version_id,
    )

    command.downgrade(configuration, "0009_organization_change_feed")
    assert RECIPE_TABLES.isdisjoint(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(IngredientVersion.id).where(IngredientVersion.id == ingredient_version_id)
            )
            == ingredient_version_id
        )

    command.upgrade(configuration, "head")
    command.check(configuration)


def test_recipe_catalog_pins_catalog_versions_and_tag_membership(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, _, grams_id, person_id = seed_catalog_context(engine)
    _, ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        grams_id=grams_id,
        name="Tomatoes",
    )
    tag_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(RecipeTag).values(
                id=tag_id,
                organization_id=organization_id,
                name="Camp favorite",
                normalized_name="camp favorite",
                color="#12ab34",
                created_by_user_id=actor_id,
            )
        )

    recipe_id, version_id, line_id = publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=ingredient_version_id,
        recipe_tag_id=tag_id,
        scaling_behavior="fixed",
    )

    with engine.connect() as connection:
        assert (
            connection.scalar(select(Recipe.current_version_id).where(Recipe.id == recipe_id))
            == version_id
        )
        line = connection.execute(
            select(
                RecipeVersionIngredientLine.ingredient_version_id,
                RecipeVersionIngredientLine.base_quantity,
                RecipeVersionIngredientLine.note,
                RecipeVersionIngredientLine.include_in_portion_weight,
                RecipeVersionIngredientLine.scaling_behavior,
            ).where(RecipeVersionIngredientLine.id == line_id)
        ).one()
        assert line == (ingredient_version_id, Decimal("500"), "Finely chopped", True, "fixed")
        assert (
            connection.scalar(
                select(RecipeVersionTag.recipe_tag_id).where(
                    RecipeVersionTag.recipe_version_id == version_id
                )
            )
            == tag_id
        )


def test_recipe_catalog_database_constraints_and_immutability(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, other_organization_id, grams_id, person_id = seed_catalog_context(
        engine
    )
    _, ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        grams_id=grams_id,
        name="Tomatoes",
    )
    _, other_ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=other_organization_id,
        grams_id=grams_id,
        name="Other tomatoes",
    )
    tag_id = uuid4()
    other_tag_id = uuid4()
    other_scaling_unit_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(RecipeTag),
            [
                {
                    "id": tag_id,
                    "organization_id": organization_id,
                    "name": "Primary tag",
                    "normalized_name": "primary tag",
                    "color": "#123456",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_tag_id,
                    "organization_id": other_organization_id,
                    "name": "Other tag",
                    "normalized_name": "other tag",
                    "color": "#654321",
                    "created_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(
            insert(UnitDefinition).values(
                id=other_scaling_unit_id,
                organization_id=other_organization_id,
                code="other-batch",
                custom_name="Other batch",
                normalized_custom_name="other batch",
                dimension="custom",
                rounds_up_to_whole_unit=True,
                allows_ingredient_quantity=False,
                allows_recipe_scaling=True,
                created_by_user_id=actor_id,
            )
        )
    recipe_id, version_id, line_id = publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=ingredient_version_id,
        recipe_tag_id=tag_id,
    )

    with engine.begin() as connection:
        invalid_statements = [
            insert(RecipeVersion).values(
                id=uuid4(),
                organization_id=organization_id,
                recipe_id=recipe_id,
                name="Invalid scale unit",
                scaling_unit_id=other_scaling_unit_id,
                base_scaling_amount=Decimal("1"),
                published_by_user_id=actor_id,
            ),
            insert(RecipeVersion).values(
                id=uuid4(),
                organization_id=organization_id,
                recipe_id=recipe_id,
                name=" ",
                scaling_unit_id=grams_id,
                base_scaling_amount=Decimal("1"),
                published_by_user_id=actor_id,
            ),
            insert(RecipeVersion).values(
                id=uuid4(),
                organization_id=organization_id,
                recipe_id=recipe_id,
                name="Bad publication time",
                scaling_unit_id=person_id,
                base_scaling_amount=Decimal("1"),
                published_at=datetime(2000, 1, 1, tzinfo=UTC),
                published_by_user_id=actor_id,
            ),
            insert(RecipeVersionIngredientLine).values(
                id=uuid4(),
                organization_id=organization_id,
                recipe_id=recipe_id,
                recipe_version_id=uuid4(),
                line_key=uuid4(),
                ingredient_version_id=ingredient_version_id,
                base_quantity=Decimal("-1"),
                position_key="a",
            ),
            insert(RecipeVersionIngredientLine).values(
                id=uuid4(),
                organization_id=organization_id,
                recipe_id=recipe_id,
                recipe_version_id=uuid4(),
                line_key=uuid4(),
                ingredient_version_id=ingredient_version_id,
                base_quantity=Decimal("1"),
                position_key="a",
                scaling_behavior="invalid",
            ),
            insert(RecipeVersionIngredientLine).values(
                id=uuid4(),
                organization_id=organization_id,
                recipe_id=recipe_id,
                recipe_version_id=uuid4(),
                line_key=uuid4(),
                ingredient_version_id=other_ingredient_version_id,
                base_quantity=Decimal("1"),
                position_key="a",
            ),
            insert(RecipeVersionTag).values(
                recipe_version_id=version_id,
                recipe_tag_id=other_tag_id,
                organization_id=organization_id,
            ),
        ]
        for statement in invalid_statements:
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(statement)

        for immutable_statement in (
            update(RecipeVersion).where(RecipeVersion.id == version_id).values(name="Changed"),
            delete(RecipeVersionTag).where(RecipeVersionTag.recipe_version_id == version_id),
            delete(RecipeVersionIngredientLine).where(RecipeVersionIngredientLine.id == line_id),
            insert(RecipeVersionIngredientLine).values(
                id=uuid4(),
                organization_id=organization_id,
                recipe_id=recipe_id,
                recipe_version_id=version_id,
                line_key=uuid4(),
                ingredient_version_id=ingredient_version_id,
                base_quantity=Decimal("1"),
                position_key="b",
            ),
            delete(Recipe).where(Recipe.id == recipe_id),
            delete(RecipeTag).where(RecipeTag.id == tag_id),
        ):
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(immutable_statement)

        connection.execute(
            update(Recipe)
            .where(Recipe.id == recipe_id)
            .values(retired_at=datetime.now(UTC), retired_by_user_id=actor_id)
        )
        connection.execute(
            update(RecipeTag)
            .where(RecipeTag.id == tag_id)
            .values(retired_at=datetime.now(UTC), retired_by_user_id=actor_id)
        )
        with pytest.raises(DatabaseError), connection.begin_nested():
            connection.execute(text("TRUNCATE TABLE recipes CASCADE"))


def test_recipe_line_key_cannot_change_logical_ingredient_between_versions(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, _, grams_id, person_id = seed_catalog_context(engine)
    _, first_ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        grams_id=grams_id,
        name="Tomatoes",
    )
    _, replacement_ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        grams_id=grams_id,
        name="Peppers",
    )
    line_key = uuid4()
    recipe_id, _, _ = publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=first_ingredient_version_id,
        line_key=line_key,
    )

    with engine.begin() as connection:
        with pytest.raises(DatabaseError), connection.begin_nested():
            connection.execute(
                insert(RecipeVersionIngredientLine).values(
                    id=uuid4(),
                    organization_id=organization_id,
                    recipe_id=recipe_id,
                    recipe_version_id=uuid4(),
                    line_key=line_key,
                    ingredient_version_id=replacement_ingredient_version_id,
                    base_quantity=Decimal("300"),
                    position_key="a",
                )
            )
        with pytest.raises(DatabaseError), connection.begin_nested():
            connection.execute(
                insert(RecipeVersion).values(
                    id=uuid4(),
                    organization_id=organization_id,
                    recipe_id=recipe_id,
                    based_on_version_id=uuid4(),
                    name="Unknown previous version",
                    scaling_unit_id=person_id,
                    base_scaling_amount=Decimal("1"),
                    published_by_user_id=actor_id,
                )
            )
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_recipe_version_ancestry_rejects_a_multi_row_cycle(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, _, grams_id, person_id = seed_catalog_context(engine)
    _, ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        grams_id=grams_id,
        name="Tomatoes",
    )
    recipe_id, _, _ = publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=ingredient_version_id,
    )
    first_version_id = uuid4()
    second_version_id = uuid4()

    with engine.begin() as connection, pytest.raises(DatabaseError), connection.begin_nested():
        connection.execute(
            insert(RecipeVersion),
            [
                {
                    "id": first_version_id,
                    "organization_id": organization_id,
                    "recipe_id": recipe_id,
                    "based_on_version_id": second_version_id,
                    "name": "Cycle one",
                    "scaling_unit_id": person_id,
                    "base_scaling_amount": Decimal("1"),
                    "published_by_user_id": actor_id,
                },
                {
                    "id": second_version_id,
                    "organization_id": organization_id,
                    "recipe_id": recipe_id,
                    "based_on_version_id": first_version_id,
                    "name": "Cycle two",
                    "scaling_unit_id": person_id,
                    "base_scaling_amount": Decimal("1"),
                    "published_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
