import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from test_schedule_recipe_service import ServiceDatabase

from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.application.scheduled_recipes import ScheduleRecipeCommand, schedule_recipe
from cookops.application.shopping_lists import CreateShoppingListCommand, create_shopping_list
from cookops.persistence.models import (
    Mutation,
    OrganizationChange,
    ScheduledRecipe,
    ShoppingContributionSnapshot,
    ShoppingIngredientRow,
    ShoppingList,
    ShoppingRevisionSource,
)

pytest_plugins = ("test_schedule_recipe_service",)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


def _context(database: ServiceDatabase) -> ExecutionContext:
    return ExecutionContext(database.actor_id, database.installation_id)


async def _scheduled(database: ServiceDatabase):
    return await schedule_recipe(
        database.sessions,
        _context(database),
        ScheduleRecipeCommand(
            mutation_id=uuid4(),
            scheduled_recipe_id=uuid4(),
            organization_id=database.organization_id,
            event_id=database.event_id,
            event_day_id=database.event_day_id,
            event_meal_role_id=database.event_role_id,
            recipe_id=database.recipe_id,
            recipe_version_id=database.recipe_version_id,
            client_wall_time=datetime.now(UTC),
            position_key="a",
        ),
    )


def _command(database: ServiceDatabase, source_ids: tuple = ()) -> CreateShoppingListCommand:
    return CreateShoppingListCommand(
        mutation_id=uuid4(),
        shopping_list_id=uuid4(),
        generation_revision_id=uuid4(),
        organization_id=database.organization_id,
        event_id=database.event_id,
        name="  Main shopping  ",
        scheduled_recipe_ids=source_ids,
        client_wall_time=datetime.now(UTC),
    )


def test_create_shopping_list_materializes_snapshot_and_is_idempotent(
    service_database: ServiceDatabase,
) -> None:
    scheduled = asyncio.run(_scheduled(service_database))
    command = _command(service_database, (scheduled.scheduled_recipe_id,))
    result = asyncio.run(
        create_shopping_list(service_database.sessions, _context(service_database), command)
    )
    replay = asyncio.run(
        create_shopping_list(service_database.sessions, _context(service_database), command)
    )
    assert replay.replayed is True
    assert replay.shopping_list_id == result.shopping_list_id
    with service_database.sync_engine.connect() as connection:
        shopping_name = connection.scalar(
            select(ShoppingList.name).where(ShoppingList.id == result.shopping_list_id)
        )
        current_revision_id = connection.scalar(
            select(ShoppingList.current_generation_revision_id).where(
                ShoppingList.id == result.shopping_list_id
            )
        )
        row_supply = connection.scalar(
            select(ShoppingIngredientRow.available_supply_quantity).where(
                ShoppingIngredientRow.shopping_list_id == result.shopping_list_id
            )
        )
        source_ids = connection.scalars(
            select(ShoppingRevisionSource.scheduled_recipe_id).where(
                ShoppingRevisionSource.generation_revision_id == result.generation_revision_id
            )
        ).all()
        snapshots = connection.execute(
            select(
                ShoppingContributionSnapshot.generated_quantity,
                ShoppingContributionSnapshot.source_details,
            ).where(
                ShoppingContributionSnapshot.generation_revision_id == result.generation_revision_id
            )
        ).all()
        changes = connection.scalars(
            select(OrganizationChange).where(OrganizationChange.mutation_id == command.mutation_id)
        ).all()
    assert shopping_name == "Main shopping"
    assert current_revision_id == result.generation_revision_id
    assert len(result.ingredient_row_ids) == 1 and row_supply == Decimal("0")
    assert source_ids == [scheduled.scheduled_recipe_id]
    assert len(snapshots) == 1 and snapshots[0].generated_quantity == Decimal("1500")
    assert snapshots[0].source_details["recipe_name"] == "Recipe"
    assert len(changes) == 1


def test_create_shopping_list_rejects_invalid_or_archived_sources_atomically(
    service_database: ServiceDatabase,
) -> None:
    scheduled = asyncio.run(_scheduled(service_database))
    command = _command(service_database, (scheduled.scheduled_recipe_id, uuid4()))
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            create_shopping_list(service_database.sessions, _context(service_database), command)
        )
    assert error.value.code == "validation_failed"
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(ShoppingList.id).where(ShoppingList.id == command.shopping_list_id)
            )
            is None
        )
        assert (
            connection.scalar(select(Mutation.id).where(Mutation.id == command.mutation_id))
            == command.mutation_id
        )


@pytest.mark.parametrize(
    "scale", [Decimal("0"), Decimal("0.125"), Decimal("1"), Decimal("42.75"), Decimal("9999.999")]
)
def test_generation_quantity_scales_finitely_without_binary_rounding(
    service_database: ServiceDatabase, scale: Decimal
) -> None:
    scheduled = asyncio.run(_scheduled(service_database))
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(ScheduledRecipe)
            .where(ScheduledRecipe.id == scheduled.scheduled_recipe_id)
            .values(selected_scale_amount=scale)
        )
    result = asyncio.run(
        create_shopping_list(
            service_database.sessions,
            _context(service_database),
            _command(service_database, (scheduled.scheduled_recipe_id,)),
        )
    )
    with service_database.sync_engine.connect() as connection:
        quantity = connection.scalar(
            select(ShoppingContributionSnapshot.generated_quantity).where(
                ShoppingContributionSnapshot.generation_revision_id == result.generation_revision_id
            )
        )
    expected = Decimal("500") * scale
    assert quantity == (expected if expected > 0 else None)
