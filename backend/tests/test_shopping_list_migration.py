import os
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect, select, update
from sqlalchemy.exc import DatabaseError, IntegrityError
from test_recipe_catalog_migration import insert_ingredient, publish_recipe, seed_catalog_context
from test_scheduled_recipe_migration import add_event_day_and_role, schedule_values

from alembic import command
from cookops.persistence.models import (
    AdHocShoppingItem,
    ClientInstallation,
    ScheduledRecipe,
    ShoppingContribution,
    ShoppingContributionSnapshot,
    ShoppingGenerationRevision,
    ShoppingIngredientRow,
    ShoppingList,
    ShoppingRevisionSource,
    StoreSection,
    UnitDefinition,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

SHOPPING_TABLES = {
    "ad_hoc_shopping_items",
    "shopping_contribution_snapshots",
    "shopping_contributions",
    "shopping_generation_revisions",
    "shopping_ingredient_rows",
    "shopping_lists",
    "shopping_revision_sources",
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


def _seed_scheduled_recipe(engine: Engine) -> tuple[UUID, UUID, UUID, UUID, UUID, UUID, UUID]:
    actor_id, organization_id, _, grams_id, person_id = seed_catalog_context(engine)
    ingredient_id, ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        grams_id=grams_id,
        name="Tomatoes",
    )
    recipe_id, recipe_version_id, _ = publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=ingredient_version_id,
    )
    event_id, day_id, role_id = add_event_day_and_role(
        engine, actor_id=actor_id, organization_id=organization_id, name="Camp"
    )
    with engine.begin() as connection:
        installation_id = connection.scalar(
            insert(ClientInstallation)
            .values(id=uuid4(), user_id=actor_id, installation_kind="browser")
            .returning(ClientInstallation.id)
        )
        scheduled_recipe_id = connection.scalar(
            insert(ScheduledRecipe)
            .values(
                **schedule_values(
                    actor_id=actor_id,
                    organization_id=organization_id,
                    event_id=event_id,
                    day_id=day_id,
                    role_id=role_id,
                    recipe_id=recipe_id,
                    recipe_version_id=recipe_version_id,
                )
            )
            .returning(ScheduledRecipe.id)
        )
    assert installation_id is not None
    assert scheduled_recipe_id is not None
    return (
        actor_id,
        organization_id,
        installation_id,
        event_id,
        ingredient_id,
        ingredient_version_id,
        scheduled_recipe_id,
    )


def test_shopping_foundation_migration_round_trip_and_snapshot_immutability(
    migration_database: MigrationDatabase,
) -> None:
    configuration, engine = migration_database.configuration, migration_database.engine
    command.upgrade(configuration, "0012_event_archives_field_clocks")
    (
        actor_id,
        organization_id,
        installation_id,
        event_id,
        ingredient_id,
        ingredient_version_id,
        scheduled_recipe_id,
    ) = _seed_scheduled_recipe(engine)

    command.upgrade(configuration, "head")
    command.check(configuration)
    assert set(inspect(engine).get_table_names()) >= SHOPPING_TABLES
    with engine.connect() as connection:
        grams_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
    assert grams_id is not None
    list_id, revision_id, row_id, contribution_id = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        section_id = connection.scalar(
            insert(StoreSection)
            .values(
                id=uuid4(),
                organization_id=organization_id,
                name="Produce",
                normalized_name="produce",
                position_key="a",
                created_by_user_id=actor_id,
            )
            .returning(StoreSection.id)
        )
        connection.execute(
            insert(ShoppingList).values(
                id=list_id,
                organization_id=organization_id,
                event_id=event_id,
                name="First run",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(ShoppingGenerationRevision).values(
                id=revision_id,
                organization_id=organization_id,
                event_id=event_id,
                shopping_list_id=list_id,
                generated_by_user_id=actor_id,
            )
        )
        connection.execute(
            update(ShoppingList)
            .where(ShoppingList.id == list_id)
            .values(current_generation_revision_id=revision_id)
        )
        connection.execute(
            insert(ShoppingRevisionSource).values(
                generation_revision_id=revision_id,
                shopping_list_id=list_id,
                organization_id=organization_id,
                event_id=event_id,
                scheduled_recipe_id=scheduled_recipe_id,
            )
        )
        connection.execute(
            insert(ShoppingIngredientRow).values(
                id=row_id,
                organization_id=organization_id,
                event_id=event_id,
                shopping_list_id=list_id,
                ingredient_id=ingredient_id,
                ingredient_name="Tomatoes",
                calculation_unit_id=grams_id,
                default_store_section_id=section_id,
                default_store_section_name="Produce",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(ShoppingContribution).values(
                id=contribution_id,
                organization_id=organization_id,
                event_id=event_id,
                shopping_list_id=list_id,
                shopping_ingredient_row_id=row_id,
                ingredient_id=ingredient_id,
                scheduled_recipe_id=scheduled_recipe_id,
                fulfilment_updated_at=None,
                fulfilment_updated_by_user_id=None,
                fulfilment_updated_by_installation_id=None,
            )
        )
        connection.execute(
            insert(ShoppingContributionSnapshot).values(
                organization_id=organization_id,
                event_id=event_id,
                shopping_list_id=list_id,
                generation_revision_id=revision_id,
                shopping_contribution_id=contribution_id,
                ingredient_id=ingredient_id,
                active_in_revision=True,
                generated_quantity=Decimal("500"),
                ingredient_version_id=ingredient_version_id,
                ingredient_name="Tomatoes",
                source_details={"recipe_name": "Tomato soup", "line_notes": ["diced"]},
            )
        )
        connection.execute(
            insert(AdHocShoppingItem).values(
                organization_id=organization_id,
                event_id=event_id,
                shopping_list_id=list_id,
                name="Ice",
                target_amount=Decimal("2"),
                unit_id=grams_id,
                store_section_id=section_id,
                created_by_user_id=actor_id,
            )
        )
        with pytest.raises(DatabaseError), connection.begin_nested():
            connection.execute(
                update(ShoppingContributionSnapshot)
                .where(ShoppingContributionSnapshot.shopping_contribution_id == contribution_id)
                .values(generated_quantity=Decimal("600"))
            )
        with pytest.raises(DatabaseError), connection.begin_nested():
            connection.execute(
                update(ShoppingGenerationRevision)
                .where(ShoppingGenerationRevision.id == revision_id)
                .values(generated_by_user_id=actor_id)
            )
    assert installation_id

    command.downgrade(configuration, "0012_event_archives_field_clocks")
    assert SHOPPING_TABLES.isdisjoint(inspect(engine).get_table_names())
    command.upgrade(configuration, "head")
    command.check(configuration)


def test_shopping_scope_and_manual_target_boundaries(migration_database: MigrationDatabase) -> None:
    command.upgrade(migration_database.configuration, "head")
    (
        actor_id,
        organization_id,
        _,
        event_id,
        ingredient_id,
        _,
        scheduled_recipe_id,
    ) = _seed_scheduled_recipe(migration_database.engine)
    with migration_database.engine.connect() as connection:
        source_recipe_id, source_recipe_version_id = connection.execute(
            select(ScheduledRecipe.recipe_id, ScheduledRecipe.recipe_version_id).where(
                ScheduledRecipe.id == scheduled_recipe_id
            )
        ).one()
    other_event_id, other_day_id, other_role_id = add_event_day_and_role(
        migration_database.engine,
        actor_id=actor_id,
        organization_id=organization_id,
        name="Other camp",
    )
    other_scheduled_recipe_id = uuid4()
    with migration_database.engine.connect() as connection:
        grams_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
    assert grams_id is not None
    list_id, revision_id, row_id = uuid4(), uuid4(), uuid4()
    with migration_database.engine.begin() as connection:
        connection.execute(
            insert(ScheduledRecipe).values(
                id=other_scheduled_recipe_id,
                **schedule_values(
                    actor_id=actor_id,
                    organization_id=organization_id,
                    event_id=other_event_id,
                    day_id=other_day_id,
                    role_id=other_role_id,
                    recipe_id=source_recipe_id,
                    recipe_version_id=source_recipe_version_id,
                ),
            )
        )
        connection.execute(
            insert(ShoppingList).values(
                id=list_id,
                organization_id=organization_id,
                event_id=event_id,
                name="Scoped list",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(ShoppingGenerationRevision).values(
                id=revision_id,
                organization_id=organization_id,
                event_id=event_id,
                shopping_list_id=list_id,
                generated_by_user_id=actor_id,
            )
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(ShoppingIngredientRow).values(
                    id=row_id,
                    organization_id=organization_id,
                    event_id=event_id,
                    shopping_list_id=list_id,
                    ingredient_id=ingredient_id,
                    ingredient_name="Tomatoes",
                    calculation_unit_id=grams_id,
                    manual_purchase_target=Decimal("4"),
                    created_by_user_id=actor_id,
                )
            )
        connection.execute(
            insert(ShoppingIngredientRow).values(
                id=row_id,
                organization_id=organization_id,
                event_id=event_id,
                shopping_list_id=list_id,
                ingredient_id=ingredient_id,
                ingredient_name="Tomatoes",
                calculation_unit_id=grams_id,
                manual_purchase_target=Decimal("4"),
                manual_target_automatic_value=Decimal("3"),
                manual_target_generation_revision_id=revision_id,
                created_by_user_id=actor_id,
            )
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(ShoppingContribution).values(
                    organization_id=organization_id,
                    event_id=other_event_id,
                    shopping_list_id=list_id,
                    shopping_ingredient_row_id=row_id,
                    ingredient_id=ingredient_id,
                    scheduled_recipe_id=other_scheduled_recipe_id,
                )
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(ShoppingContribution).values(
                    organization_id=organization_id,
                    event_id=event_id,
                    shopping_list_id=list_id,
                    shopping_ingredient_row_id=row_id,
                    ingredient_id=ingredient_id,
                    scheduled_recipe_id=scheduled_recipe_id,
                    fulfilment_credit=Decimal("-1"),
                )
            )
