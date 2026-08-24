import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update
from test_create_recipe_service import ServiceDatabase, context, recipe_command
from test_create_recipe_service import service_database as create_recipe_service_database

from cookops.application.organizations import ApplicationServiceError
from cookops.application.recipe_copy import (
    CopyRecipeToOrganizationCommand,
    copy_recipe_to_organization,
)
from cookops.application.recipes import create_recipe
from cookops.persistence.models import (
    Ingredient,
    IngredientVersion,
    Mutation,
    OrganizationChange,
    OrganizationMembership,
    Recipe,
    RecipeTag,
    RecipeVersion,
    RecipeVersionIngredientLine,
    RecipeVersionTag,
)

pytest_plugins = ("test_create_recipe_service",)


@pytest.fixture
def recipe_copy_database() -> Iterator[ServiceDatabase]:
    """Use the recipe fixture without colliding with other plugin fixtures."""
    yield from create_recipe_service_database.__wrapped__()


def test_copy_current_recipe_to_destination_admin(
    recipe_copy_database: ServiceDatabase,
) -> None:
    database = recipe_copy_database
    destination_ingredient_id, destination_version_id = uuid4(), uuid4()
    destination_tag_id = uuid4()
    with database.sync_engine.begin() as connection:
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=database.other_organization_id,
                user_id=database.actor_id,
                invited_email="member@example.test",
                role="organization_admin",
                state="active",
                invited_by_user_id=database.actor_id,
                claimed_at=datetime.now(UTC),
            )
        )
        connection.execute(
            insert(RecipeTag).values(
                id=destination_tag_id,
                organization_id=database.other_organization_id,
                name="Vegetarian",
                normalized_name="vegetarian",
                color="#228B22",
                created_by_user_id=database.actor_id,
            )
        )
        connection.execute(
            insert(Ingredient).values(
                id=destination_ingredient_id,
                organization_id=database.other_organization_id,
                current_version_id=destination_version_id,
                created_by_user_id=database.actor_id,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=destination_version_id,
                organization_id=database.other_organization_id,
                ingredient_id=destination_ingredient_id,
                name="Tomatoes",
                normalized_name="tomatoes",
                canonical_unit_id=database.grams_id,
                mass_per_canonical_quantity=1,
                published_by_user_id=database.actor_id,
            )
        )
    source = asyncio.run(
        create_recipe(database.sessions, context(database), recipe_command(database))
    )
    destination_recipe_id, destination_recipe_version_id = uuid4(), uuid4()
    command = CopyRecipeToOrganizationCommand(
        source_organization_id=database.organization_id,
        destination_organization_id=database.other_organization_id,
        source_recipe_id=source.recipe_id,
        source_current_recipe_version_id=source.recipe_version_id,
        destination_recipe_id=destination_recipe_id,
        destination_recipe_version_id=destination_recipe_version_id,
        ingredient_version_mappings={database.ingredient_version_id: destination_version_id},
        recipe_tag_mappings={database.tag_id: destination_tag_id},
        scaling_unit_mappings={database.person_id: database.person_id},
        preferred_display_unit_mappings={database.grams_id: database.grams_id},
        client_wall_time=datetime.now(UTC),
    )
    result = asyncio.run(copy_recipe_to_organization(database.sessions, context(database), command))
    assert result.replayed is False
    with database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Recipe.id).where(Recipe.id == destination_recipe_id))
            == destination_recipe_id
        )
        copied_line = (
            connection.execute(
                select(RecipeVersionIngredientLine.ingredient_version_id).where(
                    RecipeVersionIngredientLine.recipe_version_id == destination_recipe_version_id
                )
            )
            .scalars()
            .all()
        )
        assert copied_line == [destination_version_id, destination_version_id]
    replay = asyncio.run(copy_recipe_to_organization(database.sessions, context(database), command))
    assert replay.replayed is True
    assert (replay.first_change_sequence, replay.last_change_sequence) == (
        result.first_change_sequence,
        result.last_change_sequence,
    )
    with database.sync_engine.connect() as connection:
        stored_sequences = connection.execute(
            select(Mutation.first_change_sequence, Mutation.last_change_sequence).where(
                Mutation.id == command.mutation_id
            )
        ).one()
    assert stored_sequences == (result.first_change_sequence, result.last_change_sequence)


def test_same_organization_rejection_is_retained(
    recipe_copy_database: ServiceDatabase,
) -> None:
    database = recipe_copy_database
    with database.sync_engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == database.organization_id,
                OrganizationMembership.user_id == database.actor_id,
            )
            .values(role="organization_admin")
        )
    source = asyncio.run(
        create_recipe(database.sessions, context(database), recipe_command(database))
    )
    command = CopyRecipeToOrganizationCommand(
        source_organization_id=database.organization_id,
        destination_organization_id=database.organization_id,
        source_recipe_id=source.recipe_id,
        source_current_recipe_version_id=source.recipe_version_id,
        destination_recipe_id=uuid4(),
        destination_recipe_version_id=uuid4(),
    )
    with pytest.raises(ApplicationServiceError) as first:
        asyncio.run(copy_recipe_to_organization(database.sessions, context(database), command))
    assert first.value.code == "validation_failed"
    with pytest.raises(ApplicationServiceError) as replay:
        asyncio.run(copy_recipe_to_organization(database.sessions, context(database), command))
    assert replay.value.code == "validation_failed"
    changed = replace(command, destination_recipe_id=uuid4())
    with pytest.raises(ApplicationServiceError) as mismatch:
        asyncio.run(copy_recipe_to_organization(database.sessions, context(database), changed))
    assert mismatch.value.code == "idempotency_mismatch"


def test_stale_source_current_version_rejects_without_target_graph(
    recipe_copy_database: ServiceDatabase,
) -> None:
    database = recipe_copy_database
    source = asyncio.run(
        create_recipe(database.sessions, context(database), recipe_command(database))
    )
    stale_pointer = uuid4()
    with database.sync_engine.begin() as connection:
        current = connection.execute(
            select(
                RecipeVersion.organization_id,
                RecipeVersion.recipe_id,
                RecipeVersion.based_on_version_id,
                RecipeVersion.name,
                RecipeVersion.description,
                RecipeVersion.scaling_model,
                RecipeVersion.scaling_unit_id,
                RecipeVersion.base_scaling_amount,
                RecipeVersion.estimated_diners_per_scaling_unit,
                RecipeVersion.round_suggestions_up,
                RecipeVersion.published_by_user_id,
            ).where(RecipeVersion.id == source.recipe_version_id)
        ).one()
        connection.execute(
            insert(RecipeVersion).values(
                id=stale_pointer,
                organization_id=current.organization_id,
                recipe_id=current.recipe_id,
                based_on_version_id=source.recipe_version_id,
                name=current.name,
                description=current.description,
                scaling_model=current.scaling_model,
                scaling_unit_id=current.scaling_unit_id,
                base_scaling_amount=current.base_scaling_amount,
                estimated_diners_per_scaling_unit=current.estimated_diners_per_scaling_unit,
                round_suggestions_up=current.round_suggestions_up,
                published_by_user_id=current.published_by_user_id,
            )
        )
        connection.execute(
            update(Recipe)
            .where(Recipe.id == source.recipe_id)
            .values(current_version_id=stale_pointer)
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=database.other_organization_id,
                user_id=database.actor_id,
                invited_email="member@example.test",
                role="organization_admin",
                state="active",
                invited_by_user_id=database.actor_id,
                claimed_at=datetime.now(UTC),
            )
        )
    destination_recipe_id, destination_version_id = uuid4(), uuid4()
    command = CopyRecipeToOrganizationCommand(
        source_organization_id=database.organization_id,
        destination_organization_id=database.other_organization_id,
        source_recipe_id=source.recipe_id,
        source_current_recipe_version_id=source.recipe_version_id,
        destination_recipe_id=destination_recipe_id,
        destination_recipe_version_id=destination_version_id,
    )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(copy_recipe_to_organization(database.sessions, context(database), command))
    assert error.value.code == "stale_precondition"
    with database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Recipe.id).where(Recipe.id == destination_recipe_id)) is None
        )
        assert (
            connection.scalar(
                select(RecipeVersion.id).where(RecipeVersion.id == destination_version_id)
            )
            is None
        )
        assert (
            connection.scalar(
                select(RecipeVersionIngredientLine.id).where(
                    RecipeVersionIngredientLine.recipe_version_id == destination_version_id
                )
            )
            is None
        )
        assert (
            connection.scalar(
                select(RecipeVersionTag.recipe_version_id).where(
                    RecipeVersionTag.recipe_version_id == destination_version_id
                )
            )
            is None
        )
        assert (
            connection.scalar(
                select(OrganizationChange.sequence).where(
                    OrganizationChange.mutation_id == command.mutation_id
                )
            )
            is None
        )


def test_missing_mapping_rejects_replays_without_target_rows(
    recipe_copy_database: ServiceDatabase,
) -> None:
    database = recipe_copy_database
    source = asyncio.run(
        create_recipe(database.sessions, context(database), recipe_command(database))
    )
    with database.sync_engine.begin() as connection:
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=database.other_organization_id,
                user_id=database.actor_id,
                invited_email="member@example.test",
                role="organization_admin",
                state="active",
                invited_by_user_id=database.actor_id,
                claimed_at=datetime.now(UTC),
            )
        )
    command = CopyRecipeToOrganizationCommand(
        source_organization_id=database.organization_id,
        destination_organization_id=database.other_organization_id,
        source_recipe_id=source.recipe_id,
        source_current_recipe_version_id=source.recipe_version_id,
        destination_recipe_id=uuid4(),
        destination_recipe_version_id=uuid4(),
        recipe_tag_mappings={database.tag_id: uuid4()},
        scaling_unit_mappings={database.person_id: database.person_id},
        preferred_display_unit_mappings={database.grams_id: database.grams_id},
    )
    with pytest.raises(ApplicationServiceError) as first:
        asyncio.run(copy_recipe_to_organization(database.sessions, context(database), command))
    assert first.value.code == "validation_failed"
    with pytest.raises(ApplicationServiceError) as replay:
        asyncio.run(copy_recipe_to_organization(database.sessions, context(database), command))
    assert replay.value.code == "validation_failed"
    with database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Recipe.id).where(Recipe.id == command.destination_recipe_id))
            is None
        )
        assert (
            connection.scalar(
                select(RecipeVersion.id).where(
                    RecipeVersion.id == command.destination_recipe_version_id
                )
            )
            is None
        )
        assert (
            connection.scalar(
                select(RecipeVersionIngredientLine.id).where(
                    RecipeVersionIngredientLine.recipe_id == command.destination_recipe_id
                )
            )
            is None
        )
        assert (
            connection.scalar(
                select(RecipeVersionTag.recipe_version_id).where(
                    RecipeVersionTag.recipe_version_id == command.destination_recipe_version_id
                )
            )
            is None
        )
        assert (
            connection.scalar(
                select(OrganizationChange.sequence).where(
                    OrganizationChange.mutation_id == command.mutation_id
                )
            )
            is None
        )
