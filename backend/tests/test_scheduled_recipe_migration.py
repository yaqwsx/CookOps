import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect, select, text, update
from sqlalchemy.exc import DatabaseError
from test_recipe_catalog_migration import insert_ingredient, publish_recipe, seed_catalog_context

from alembic import command
from cookops.persistence.models import (
    Event,
    EventDay,
    EventMealRole,
    RecipeVersionIngredientLine,
    ScheduledIngredientOverride,
    ScheduledRecipe,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

SCHEDULE_TABLES = {
    "scheduled_recipes",
    "scheduled_ingredient_overrides",
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


def event_values(actor_id: UUID, organization_id: UUID, *, name: str) -> dict[str, object]:
    return {
        "organization_id": organization_id,
        "name": name,
        "start_date": date(2026, 7, 1),
        "end_date": date(2026, 7, 2),
        "base_expected_attendance": 42,
        "budget_amount": Decimal("1000"),
        "currency": "CZK",
        "created_by_user_id": actor_id,
    }


def add_event_day_and_role(
    engine: Engine, *, actor_id: UUID, organization_id: UUID, name: str
) -> tuple[UUID, UUID, UUID]:
    with engine.begin() as connection:
        event_id = connection.scalar(
            insert(Event)
            .values(**event_values(actor_id, organization_id, name=name))
            .returning(Event.id)
        )
        assert event_id is not None
        day_id = connection.scalar(
            insert(EventDay)
            .values(
                event_id=event_id,
                calendar_date=date(2026, 7, 1),
                provenance="range_generated",
                created_by_user_id=actor_id,
            )
            .returning(EventDay.id)
        )
        role_id = connection.scalar(
            insert(EventMealRole)
            .values(
                event_id=event_id,
                built_in_translation_key="meal_role.dinner",
                position_key="a",
                created_by_user_id=actor_id,
            )
            .returning(EventMealRole.id)
        )
    assert day_id is not None
    assert role_id is not None
    return event_id, day_id, role_id


def schedule_values(
    *,
    actor_id: UUID,
    organization_id: UUID,
    event_id: UUID,
    day_id: UUID,
    role_id: UUID,
    recipe_id: UUID,
    recipe_version_id: UUID,
) -> dict[str, object]:
    return {
        "organization_id": organization_id,
        "event_id": event_id,
        "event_day_id": day_id,
        "event_meal_role_id": role_id,
        "recipe_id": recipe_id,
        "recipe_version_id": recipe_version_id,
        "diner_count": 42,
        "attendance_mode": "follows_event",
        "consumption_percentage": Decimal("80"),
        "selected_scale_amount": Decimal("3.5"),
        "scale_mode": "manual",
        "note": "Serve after the hike",
        "position_key": "a",
        "created_by_user_id": actor_id,
    }


def test_scheduled_recipe_migration_round_trips_from_recipe_catalog(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine
    command.upgrade(configuration, "0010_recipe_catalog")
    actor_id, organization_id, _, grams_id, person_id = seed_catalog_context(engine)
    _, ingredient_version_id = insert_ingredient(
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
        engine, actor_id=actor_id, organization_id=organization_id, name="Summer camp"
    )

    command.upgrade(configuration, "head")
    command.check(configuration)
    assert set(inspect(engine).get_table_names()) >= SCHEDULE_TABLES
    with engine.begin() as connection:
        scheduled_id = connection.scalar(
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
        assert scheduled_id is not None

    command.downgrade(configuration, "0010_recipe_catalog")
    assert SCHEDULE_TABLES.isdisjoint(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(select(Event.id).where(Event.id == event_id)) == event_id
    command.upgrade(configuration, "head")
    command.check(configuration)


def test_scheduled_recipe_and_override_database_boundaries(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, other_organization_id, grams_id, person_id = seed_catalog_context(
        engine
    )
    ingredient_id, ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        grams_id=grams_id,
        name="Tomatoes",
    )
    added_ingredient_id, added_ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        grams_id=grams_id,
        name="Basil",
    )
    _, other_ingredient_version_id = insert_ingredient(
        engine,
        actor_id=actor_id,
        organization_id=other_organization_id,
        grams_id=grams_id,
        name="Other tomatoes",
    )
    recipe_id, recipe_version_id, line_id = publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=ingredient_version_id,
    )
    _, incompatible_recipe_version_id, _ = publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=ingredient_version_id,
        recipe_id=recipe_id,
        based_on_version_id=recipe_version_id,
        line_key=uuid4(),
    )
    second_recipe_id, second_recipe_version_id, _ = publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=ingredient_version_id,
    )
    other_recipe_id, other_recipe_version_id, _ = publish_recipe(
        engine,
        actor_id=actor_id,
        organization_id=other_organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=other_ingredient_version_id,
    )
    event_id, day_id, role_id = add_event_day_and_role(
        engine, actor_id=actor_id, organization_id=organization_id, name="Primary event"
    )
    other_event_id, other_day_id, other_role_id = add_event_day_and_role(
        engine, actor_id=actor_id, organization_id=organization_id, name="Other event"
    )
    with engine.connect() as connection:
        line_key = connection.scalar(
            select(RecipeVersionIngredientLine.line_key).where(
                RecipeVersionIngredientLine.id == line_id
            )
        )
    assert line_key is not None

    values = schedule_values(
        actor_id=actor_id,
        organization_id=organization_id,
        event_id=event_id,
        day_id=day_id,
        role_id=role_id,
        recipe_id=recipe_id,
        recipe_version_id=recipe_version_id,
    )
    with engine.begin() as connection:
        scheduled_id = connection.scalar(
            insert(ScheduledRecipe).values(**values).returning(ScheduledRecipe.id)
        )
        assert scheduled_id is not None
        connection.execute(
            insert(ScheduledIngredientOverride).values(
                organization_id=organization_id,
                event_id=event_id,
                scheduled_recipe_id=scheduled_id,
                override_kind="replace",
                target_line_key=line_key,
                ingredient_id=ingredient_id,
                ingredient_version_id=ingredient_version_id,
                quantity=Decimal("0"),
                created_by_user_id=actor_id,
                last_modified_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(ScheduledIngredientOverride).values(
                organization_id=organization_id,
                event_id=event_id,
                scheduled_recipe_id=scheduled_id,
                override_kind="add",
                ingredient_id=added_ingredient_id,
                ingredient_version_id=added_ingredient_version_id,
                quantity=Decimal("20"),
                include_in_portion_weight=False,
                note="For garnish",
                position_key="z",
                created_by_user_id=actor_id,
                last_modified_by_user_id=actor_id,
            )
        )

        invalid_schedules = [
            values | {"event_id": other_event_id},
            values | {"event_day_id": other_day_id},
            values | {"event_meal_role_id": other_role_id},
            values | {"recipe_version_id": second_recipe_version_id},
            values | {"recipe_id": second_recipe_id, "recipe_version_id": recipe_version_id},
            values | {"recipe_id": other_recipe_id, "recipe_version_id": other_recipe_version_id},
            values | {"diner_count": -1},
            values | {"consumption_percentage": Decimal("-1")},
            values | {"consumption_percentage": Decimal("NaN")},
            values | {"consumption_percentage": Decimal("Infinity")},
            values | {"selected_scale_amount": Decimal("-1")},
            values | {"selected_scale_amount": Decimal("NaN")},
            values | {"selected_scale_amount": Decimal("Infinity")},
            values | {"attendance_mode": "derived"},
            values | {"scale_mode": "derived"},
        ]
        for statement_values in invalid_schedules:
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(insert(ScheduledRecipe).values(**statement_values))

        invalid_overrides = [
            {
                "override_kind": "replace",
                "target_line_key": line_key,
                "ingredient_id": ingredient_id,
                "ingredient_version_id": ingredient_version_id,
                "quantity": Decimal("10"),
                "include_in_portion_weight": True,
            },
            {
                "override_kind": "replace",
                "target_line_key": uuid4(),
                "ingredient_id": ingredient_id,
                "ingredient_version_id": ingredient_version_id,
                "quantity": Decimal("10"),
            },
            {
                "override_kind": "add",
                "ingredient_id": ingredient_id,
                "ingredient_version_id": ingredient_version_id,
                "quantity": Decimal("10"),
                "include_in_portion_weight": True,
            },
            {
                "override_kind": "add",
                "ingredient_id": added_ingredient_id,
                "ingredient_version_id": added_ingredient_version_id,
                "quantity": Decimal("-1"),
                "include_in_portion_weight": True,
            },
            {
                "override_kind": "add",
                "ingredient_id": added_ingredient_id,
                "ingredient_version_id": added_ingredient_version_id,
                "quantity": Decimal("NaN"),
                "include_in_portion_weight": True,
            },
            {
                "override_kind": "add",
                "ingredient_id": added_ingredient_id,
                "ingredient_version_id": other_ingredient_version_id,
                "quantity": Decimal("10"),
                "include_in_portion_weight": True,
            },
        ]
        for override_values in invalid_overrides:
            with pytest.raises(DatabaseError), connection.begin_nested():
                connection.execute(
                    insert(ScheduledIngredientOverride).values(
                        organization_id=organization_id,
                        event_id=event_id,
                        scheduled_recipe_id=scheduled_id,
                        created_by_user_id=actor_id,
                        last_modified_by_user_id=actor_id,
                        **override_values,
                    )
                )
                connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with pytest.raises(DatabaseError), engine.begin() as connection:
        connection.execute(
            update(ScheduledRecipe)
            .where(ScheduledRecipe.id == scheduled_id)
            .values(recipe_version_id=incompatible_recipe_version_id)
        )
