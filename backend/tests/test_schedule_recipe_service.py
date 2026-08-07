import asyncio
import os
import random
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command as alembic_command
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.application.scheduled_recipe_moves import (
    MoveScheduledRecipeCommand,
    MoveScheduledRecipeResult,
    _prepare,
    move_scheduled_recipe,
)
from cookops.application.scheduled_recipe_overrides import (
    SetScheduledIngredientOverrideCommand,
    set_scheduled_ingredient_override,
)
from cookops.application.scheduled_recipes import (
    ScheduleRecipeCommand,
    _prepare_command,
    _retained_error,
    _ScheduleReferences,
    _suggested_scale,
    schedule_recipe,
)
from cookops.application.synchronization import SynchronizationQueryService
from cookops.persistence.models import (
    ClientInstallation,
    Event,
    EventArchiveSnapshot,
    EventDay,
    EventMealRole,
    FieldClock,
    Ingredient,
    IngredientVersion,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    Recipe,
    RecipeVersion,
    RecipeVersionIngredientLine,
    ScheduledIngredientOverride,
    ScheduledRecipe,
    UnitDefinition,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


@dataclass
class ServiceDatabase:
    sync_engine: Engine
    sessions: async_sessionmaker[AsyncSession]
    actor_id: UUID
    installation_id: UUID
    organization_id: UUID
    other_organization_id: UUID
    event_id: UUID
    event_day_id: UUID
    other_event_day_id: UUID
    event_role_id: UUID
    other_event_role_id: UUID
    recipe_id: UUID
    recipe_version_id: UUID
    person_recipe_id: UUID
    person_recipe_version_id: UUID
    no_capacity_recipe_id: UUID
    no_capacity_recipe_version_id: UUID
    recipe_ingredient_id: UUID
    recipe_ingredient_version_id: UUID
    recipe_line_key: UUID
    added_ingredient_id: UUID
    added_ingredient_version_id: UUID


@pytest.fixture
def service_database() -> Iterator[ServiceDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    sync_engine = create_engine(database_url)
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    actor_id, installation_id, organization_id, other_organization_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    event_id, other_event_id = uuid4(), uuid4()
    event_day_id, other_event_day_id = uuid4(), uuid4()
    event_role_id, other_event_role_id = uuid4(), uuid4()
    recipe_id, recipe_version_id = uuid4(), uuid4()
    person_recipe_id, person_recipe_version_id = uuid4(), uuid4()
    no_capacity_recipe_id, no_capacity_recipe_version_id = uuid4(), uuid4()
    recipe_ingredient_id, recipe_ingredient_version_id, recipe_line_key = uuid4(), uuid4(), uuid4()
    added_ingredient_id, added_ingredient_version_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    with sync_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Member",
                verified_email="member@example.test",
                normalized_email="member@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id, user_id=actor_id, installation_kind="browser"
            )
        )
        connection.execute(
            insert(Organization),
            [
                {"id": organization_id, "name": "Kitchen crew", "created_by_user_id": actor_id},
                {
                    "id": other_organization_id,
                    "name": "Other kitchen crew",
                    "created_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                user_id=actor_id,
                invited_email="member@example.test",
                role="member",
                state="active",
                invited_by_user_id=actor_id,
                claimed_at=now,
            )
        )
        person_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "person"
            )
        )
        tray_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "tray"
            )
        )
        gram_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        assert person_id is not None and tray_id is not None and gram_id is not None
        connection.execute(
            insert(Event),
            [
                {
                    "id": event_id,
                    "organization_id": organization_id,
                    "name": "Camp",
                    "start_date": date(2026, 7, 1),
                    "end_date": date(2026, 7, 1),
                    "base_expected_attendance": 42,
                    "budget_amount": Decimal("0"),
                    "currency": "CZK",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_event_id,
                    "organization_id": organization_id,
                    "name": "Other camp",
                    "start_date": date(2026, 7, 2),
                    "end_date": date(2026, 7, 2),
                    "base_expected_attendance": 10,
                    "budget_amount": Decimal("0"),
                    "currency": "CZK",
                    "created_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(
            insert(EventDay),
            [
                {
                    "id": event_day_id,
                    "event_id": event_id,
                    "calendar_date": date(2026, 7, 1),
                    "is_visible": True,
                    "provenance": "range_generated",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_event_day_id,
                    "event_id": other_event_id,
                    "calendar_date": date(2026, 7, 2),
                    "is_visible": True,
                    "provenance": "range_generated",
                    "created_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(
            insert(EventMealRole),
            [
                {
                    "id": event_role_id,
                    "event_id": event_id,
                    "built_in_translation_key": "meal_role.dinner",
                    "position_key": "a",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_event_role_id,
                    "event_id": other_event_id,
                    "built_in_translation_key": "meal_role.dinner",
                    "position_key": "a",
                    "created_by_user_id": actor_id,
                },
            ],
        )
        recipes = (
            (recipe_id, recipe_version_id, tray_id, Decimal("1"), Decimal("20"), True),
            (
                person_recipe_id,
                person_recipe_version_id,
                person_id,
                Decimal("10"),
                Decimal("8"),
                False,
            ),
            (
                no_capacity_recipe_id,
                no_capacity_recipe_version_id,
                tray_id,
                Decimal("3"),
                None,
                True,
            ),
        )
        connection.execute(
            insert(Recipe),
            [
                {
                    "id": recipe_id,
                    "organization_id": organization_id,
                    "current_version_id": version_id,
                    "created_by_user_id": actor_id,
                }
                for recipe_id, version_id, *_ in recipes
            ],
        )
        connection.execute(
            insert(Ingredient),
            [
                {
                    "id": recipe_ingredient_id,
                    "organization_id": organization_id,
                    "current_version_id": recipe_ingredient_version_id,
                    "created_by_user_id": actor_id,
                },
                {
                    "id": added_ingredient_id,
                    "organization_id": organization_id,
                    "current_version_id": added_ingredient_version_id,
                    "created_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(
            insert(IngredientVersion),
            [
                {
                    "id": recipe_ingredient_version_id,
                    "organization_id": organization_id,
                    "ingredient_id": recipe_ingredient_id,
                    "name": "Tomatoes",
                    "normalized_name": "tomatoes",
                    "canonical_unit_id": gram_id,
                    "mass_per_canonical_quantity": Decimal("1"),
                    "published_by_user_id": actor_id,
                },
                {
                    "id": added_ingredient_version_id,
                    "organization_id": organization_id,
                    "ingredient_id": added_ingredient_id,
                    "name": "Basil",
                    "normalized_name": "basil",
                    "canonical_unit_id": gram_id,
                    "mass_per_canonical_quantity": Decimal("1"),
                    "published_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(
            insert(RecipeVersionIngredientLine).values(
                id=uuid4(),
                organization_id=organization_id,
                recipe_id=recipe_id,
                recipe_version_id=recipe_version_id,
                line_key=recipe_line_key,
                ingredient_version_id=recipe_ingredient_version_id,
                base_quantity=Decimal("500"),
                position_key="a",
            )
        )
        connection.execute(
            insert(RecipeVersion),
            [
                {
                    "id": version_id,
                    "organization_id": organization_id,
                    "recipe_id": root_id,
                    "name": "Recipe",
                    "scaling_unit_id": unit_id,
                    "base_scaling_amount": base,
                    "estimated_diners_per_scaling_unit": capacity,
                    "round_suggestions_up": rounded,
                    "published_by_user_id": actor_id,
                }
                for root_id, version_id, unit_id, base, capacity, rounded in recipes
            ],
        )
    database = ServiceDatabase(
        sync_engine=sync_engine,
        sessions=async_sessionmaker(async_engine, expire_on_commit=False),
        actor_id=actor_id,
        installation_id=installation_id,
        organization_id=organization_id,
        other_organization_id=other_organization_id,
        event_id=event_id,
        event_day_id=event_day_id,
        other_event_day_id=other_event_day_id,
        event_role_id=event_role_id,
        other_event_role_id=other_event_role_id,
        recipe_id=recipe_id,
        recipe_version_id=recipe_version_id,
        person_recipe_id=person_recipe_id,
        person_recipe_version_id=person_recipe_version_id,
        no_capacity_recipe_id=no_capacity_recipe_id,
        no_capacity_recipe_version_id=no_capacity_recipe_version_id,
        recipe_ingredient_id=recipe_ingredient_id,
        recipe_ingredient_version_id=recipe_ingredient_version_id,
        recipe_line_key=recipe_line_key,
        added_ingredient_id=added_ingredient_id,
        added_ingredient_version_id=added_ingredient_version_id,
    )
    try:
        yield database
    finally:
        asyncio.run(async_engine.dispose())
        sync_engine.dispose()
        alembic_command.downgrade(configuration, "base")


def context(database: ServiceDatabase) -> ExecutionContext:
    return ExecutionContext(database.actor_id, database.installation_id)


def schedule_command(
    database: ServiceDatabase,
    *,
    mutation_id: UUID | None = None,
    scheduled_recipe_id: UUID | None = None,
    consumption_percentage: Decimal = Decimal("80"),
    position_key: str = "b",
    note: str | None = "  Prepare after lunch\r\n  ",
) -> ScheduleRecipeCommand:
    return ScheduleRecipeCommand(
        mutation_id=mutation_id or uuid4(),
        scheduled_recipe_id=scheduled_recipe_id or uuid4(),
        organization_id=database.organization_id,
        event_id=database.event_id,
        event_day_id=database.event_day_id,
        event_meal_role_id=database.event_role_id,
        recipe_id=database.recipe_id,
        recipe_version_id=database.recipe_version_id,
        consumption_percentage=consumption_percentage,
        position_key=position_key,
        note=note,
        client_wall_time=datetime.now(UTC),
    )


def schedule_then_override(database: ServiceDatabase) -> UUID:
    scheduled_recipe_id = uuid4()
    asyncio.run(
        schedule_recipe(
            database.sessions,
            context(database),
            schedule_command(database, scheduled_recipe_id=scheduled_recipe_id),
        )
    )
    return scheduled_recipe_id


def move_command(
    database: ServiceDatabase,
    scheduled_recipe_id: UUID,
    *,
    mutation_id: UUID | None = None,
    event_day_id: UUID | None = None,
    event_meal_role_id: UUID | None = None,
    position_key: str = "z",
    client_wall_time: datetime | None = None,
) -> MoveScheduledRecipeCommand:
    return MoveScheduledRecipeCommand(
        mutation_id=mutation_id or uuid4(),
        scheduled_recipe_id=scheduled_recipe_id,
        organization_id=database.organization_id,
        event_id=database.event_id,
        event_day_id=event_day_id or database.event_day_id,
        event_meal_role_id=event_meal_role_id or database.event_role_id,
        position_key=position_key,
        client_wall_time=client_wall_time or datetime.now(UTC),
    )


def override_command(
    database: ServiceDatabase,
    scheduled_recipe_id: UUID,
    *,
    mutation_id: UUID | None = None,
    override_id: UUID | None = None,
    operation: str = "set",
    override_kind: str = "replace",
    target_line_key: UUID | None = None,
    ingredient_id: UUID | None = None,
    ingredient_version_id: UUID | None = None,
    quantity: Decimal | None = Decimal("750"),
    include_in_portion_weight: bool | None = None,
    position_key: str | None = None,
    client_wall_time: datetime | None = None,
) -> SetScheduledIngredientOverrideCommand:
    return SetScheduledIngredientOverrideCommand(
        mutation_id=mutation_id or uuid4(),
        override_id=override_id or uuid4(),
        organization_id=database.organization_id,
        event_id=database.event_id,
        scheduled_recipe_id=scheduled_recipe_id,
        operation=cast(Literal["set", "clear"], operation),
        override_kind=cast(Literal["replace", "add"], override_kind),
        client_wall_time=client_wall_time or datetime.now(UTC),
        target_line_key=(
            target_line_key
            if override_kind == "add"
            else target_line_key or database.recipe_line_key
        ),
        ingredient_id=ingredient_id,
        ingredient_version_id=ingredient_version_id,
        quantity=quantity,
        include_in_portion_weight=include_in_portion_weight,
        note="Local note\r\n",
        position_key=position_key,
    )


def test_member_schedules_pinned_version_with_derived_default_and_atomic_feed(
    service_database: ServiceDatabase,
) -> None:
    command = schedule_command(service_database)
    result = asyncio.run(
        schedule_recipe(service_database.sessions, context(service_database), command)
    )

    assert result.replayed is False
    assert result.diner_count == 42
    assert result.attendance_mode == "follows_event"
    assert result.consumption_percentage == Decimal("80")
    assert result.selected_scale_amount == Decimal("2")
    assert result.scale_mode == "suggested"
    assert result.note == "  Prepare after lunch\n  "
    assert (result.first_change_sequence, result.last_change_sequence) == (1, 3)
    with service_database.sync_engine.connect() as connection:
        scheduled = connection.execute(
            select(
                ScheduledRecipe.event_id,
                ScheduledRecipe.event_day_id,
                ScheduledRecipe.event_meal_role_id,
                ScheduledRecipe.recipe_id,
                ScheduledRecipe.recipe_version_id,
                ScheduledRecipe.diner_count,
                ScheduledRecipe.attendance_mode,
                ScheduledRecipe.consumption_percentage,
                ScheduledRecipe.selected_scale_amount,
                ScheduledRecipe.scale_mode,
                ScheduledRecipe.note,
                ScheduledRecipe.position_key,
            ).where(ScheduledRecipe.id == command.scheduled_recipe_id)
        ).one()
        assert scheduled == (
            command.event_id,
            command.event_day_id,
            command.event_meal_role_id,
            command.recipe_id,
            command.recipe_version_id,
            42,
            "follows_event",
            Decimal("80"),
            Decimal("2"),
            "suggested",
            "  Prepare after lunch\n  ",
            "b",
        )
        mutation = connection.execute(
            select(Mutation.actor_role, Mutation.outcome, Mutation.first_change_sequence).where(
                Mutation.id == command.mutation_id
            )
        ).one()
        assert mutation == ("member", "accepted", 1)
        changes = connection.execute(
            select(
                OrganizationChange.entity_kind,
                OrganizationChange.entity_id,
                OrganizationChange.payload,
            )
            .where(OrganizationChange.mutation_id == command.mutation_id)
            .order_by(OrganizationChange.sequence)
        ).all()
        assert [change.entity_kind for change in changes] == [
            "scheduled_recipe",
            "event_ingredient_price_snapshot",
            "event_ingredient_price",
        ]
        assert changes[0].entity_id == command.scheduled_recipe_id
        assert changes[0].payload["record_schema_version"] == 1
        assert changes[0].payload["record"]["selected_scale_amount"] == "2"
        assert changes[0].payload["record"]["field_clocks"]["placement"][
            "winning_mutation_id"
        ] == str(command.mutation_id)
        assert changes[1].payload["record_schema_version"] == 1
        assert changes[2].payload["record_schema_version"] == 1


def test_member_sets_replaces_and_clears_pinned_recipe_ingredient(
    service_database: ServiceDatabase,
) -> None:
    scheduled_recipe_id = schedule_then_override(service_database)
    override_id = uuid4()
    set_command = override_command(service_database, scheduled_recipe_id, override_id=override_id)
    set_result = asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, context(service_database), set_command
        )
    )
    assert set_result.outcome == "accepted"
    assert set_result.ingredient_id == service_database.recipe_ingredient_id
    assert set_result.ingredient_version_id == service_database.recipe_ingredient_version_id
    assert set_result.quantity == Decimal("750")
    assert set_result.include_in_portion_weight is None
    assert set_result.note == "Local note\n"
    assert set_result.first_change_sequence == set_result.last_change_sequence

    clear_command = override_command(
        service_database,
        scheduled_recipe_id,
        override_id=override_id,
        operation="clear",
        quantity=None,
        client_wall_time=set_command.client_wall_time + timedelta(seconds=1),
    )
    clear_result = asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, context(service_database), clear_command
        )
    )
    assert clear_result.retired_at is not None
    with service_database.sync_engine.connect() as connection:
        stored = connection.execute(
            select(
                ScheduledIngredientOverride.quantity,
                ScheduledIngredientOverride.retired_at,
                Mutation.outcome,
                OrganizationChange.entity_kind,
            )
            .select_from(ScheduledIngredientOverride)
            .join(Mutation, Mutation.id == clear_command.mutation_id)
            .join(OrganizationChange, OrganizationChange.mutation_id == clear_command.mutation_id)
            .where(ScheduledIngredientOverride.id == override_id)
        ).one()
        assert stored.quantity == Decimal("750")
        assert stored.retired_at is not None
        assert stored.outcome == "accepted"
        assert stored.entity_kind == "scheduled_ingredient_override"


def test_member_adds_active_catalog_ingredient_and_replay_is_idempotent(
    service_database: ServiceDatabase,
) -> None:
    scheduled_recipe_id = schedule_then_override(service_database)
    command = override_command(
        service_database,
        scheduled_recipe_id,
        override_kind="add",
        target_line_key=None,
        ingredient_id=service_database.added_ingredient_id,
        ingredient_version_id=service_database.added_ingredient_version_id,
        quantity=Decimal("30"),
        include_in_portion_weight=True,
        position_key="z",
    )
    result = asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, context(service_database), command
        )
    )
    replay = asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, context(service_database), command
        )
    )
    assert result.ingredient_id == service_database.added_ingredient_id
    assert result.include_in_portion_weight is True
    assert replay.replayed is True
    assert replay.first_change_sequence == result.first_change_sequence


def test_override_lww_retains_newer_canonical_quantity_and_reports_supersession(
    service_database: ServiceDatabase,
) -> None:
    scheduled_recipe_id = schedule_then_override(service_database)
    override_id = uuid4()
    newer_time = datetime.now(UTC)
    newer = override_command(
        service_database,
        scheduled_recipe_id,
        override_id=override_id,
        quantity=Decimal("900"),
        client_wall_time=newer_time,
    )
    asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, context(service_database), newer
        )
    )
    older = override_command(
        service_database,
        scheduled_recipe_id,
        override_id=uuid4(),
        quantity=Decimal("100"),
        client_wall_time=newer_time - timedelta(seconds=1),
    )
    result = asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, context(service_database), older
        )
    )
    assert result.outcome == "partially_superseded"
    assert result.override_id == override_id
    assert result.quantity == Decimal("900")


def test_stale_set_after_clear_reconciles_the_retained_tombstone(
    service_database: ServiceDatabase,
) -> None:
    scheduled_recipe_id = schedule_then_override(service_database)
    override_id = uuid4()
    action_time = datetime.now(UTC)
    initial = override_command(
        service_database,
        scheduled_recipe_id,
        override_id=override_id,
        client_wall_time=action_time,
    )
    asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, context(service_database), initial
        )
    )
    clear = override_command(
        service_database,
        scheduled_recipe_id,
        override_id=override_id,
        operation="clear",
        quantity=None,
        client_wall_time=action_time + timedelta(seconds=2),
    )
    asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, context(service_database), clear
        )
    )
    stale = override_command(
        service_database,
        scheduled_recipe_id,
        override_id=uuid4(),
        quantity=Decimal("1"),
        client_wall_time=action_time + timedelta(seconds=1),
    )
    result = asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, context(service_database), stale
        )
    )
    assert result.outcome == "partially_superseded"
    assert result.override_id == override_id
    assert result.retired_at is not None


def test_override_rejects_pinned_line_and_added_catalog_errors(
    service_database: ServiceDatabase,
) -> None:
    scheduled_recipe_id = schedule_then_override(service_database)
    invalid_line = override_command(service_database, scheduled_recipe_id, target_line_key=uuid4())
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(
            set_scheduled_ingredient_override(
                service_database.sessions, context(service_database), invalid_line
            )
        )
    invalid_add = override_command(
        service_database,
        scheduled_recipe_id,
        override_kind="add",
        target_line_key=None,
        ingredient_id=service_database.recipe_ingredient_id,
        ingredient_version_id=service_database.recipe_ingredient_version_id,
        include_in_portion_weight=True,
        position_key="z",
    )
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(
            set_scheduled_ingredient_override(
                service_database.sessions, context(service_database), invalid_add
            )
        )


def test_override_decimal_validation_fuzzes_nonnegative_finite_inputs(
    service_database: ServiceDatabase,
) -> None:
    scheduled_recipe_id = schedule_then_override(service_database)
    randomizer = random.Random(841)
    for _ in range(80):
        quantity = Decimal(randomizer.randrange(0, 1_000_000)).scaleb(-randomizer.randrange(0, 6))
        command = override_command(
            service_database,
            scheduled_recipe_id,
            override_id=uuid4(),
            quantity=quantity,
            client_wall_time=datetime.now(UTC) + timedelta(microseconds=_),
        )
        result = asyncio.run(
            set_scheduled_ingredient_override(
                service_database.sessions, context(service_database), command
            )
        )
        assert result.quantity == quantity


def test_archived_event_cannot_receive_a_scheduled_recipe(
    service_database: ServiceDatabase,
) -> None:
    snapshot_id = uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=snapshot_id,
                event_id=service_database.event_id,
                archive_schema_version=1,
                payload={"event": {}},
                content_hash=b"s" * 32,
                attachment_manifest=[],
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            update(Event)
            .where(Event.id == service_database.event_id)
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=snapshot_id,
                archived_at=datetime.now(UTC),
                archived_by_user_id=service_database.actor_id,
            )
        )

    command = schedule_command(service_database)
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(schedule_recipe(service_database.sessions, context(service_database), command))
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == command.mutation_id))
            == "rejected"
        )


def test_scale_suggestion_uses_person_capacity_and_base_fallback(
    service_database: ServiceDatabase,
) -> None:
    person = replace(
        schedule_command(service_database),
        recipe_id=service_database.person_recipe_id,
        recipe_version_id=service_database.person_recipe_version_id,
        consumption_percentage=Decimal("80"),
    )
    fallback = replace(
        schedule_command(service_database),
        recipe_id=service_database.no_capacity_recipe_id,
        recipe_version_id=service_database.no_capacity_recipe_version_id,
    )
    person_result = asyncio.run(
        schedule_recipe(service_database.sessions, context(service_database), person)
    )
    fallback_result = asyncio.run(
        schedule_recipe(service_database.sessions, context(service_database), fallback)
    )
    assert person_result.selected_scale_amount == Decimal("33.6")
    assert fallback_result.selected_scale_amount == Decimal("3")


def test_retry_and_mismatch_are_idempotent(service_database: ServiceDatabase) -> None:
    command = schedule_command(service_database)
    first = asyncio.run(
        schedule_recipe(service_database.sessions, context(service_database), command)
    )
    replay = asyncio.run(
        schedule_recipe(service_database.sessions, context(service_database), command)
    )
    assert replay.replayed is True
    assert replay == replace(first, replayed=True)
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            schedule_recipe(
                service_database.sessions,
                context(service_database),
                replace(command, consumption_percentage=Decimal("81")),
            )
        )
    assert error.value.code == "idempotency_mismatch"


@pytest.mark.parametrize(
    ("invalid_field", "invalid_value", "corrected_value"),
    [
        ("consumption_percentage", Decimal("NaN"), Decimal("0")),
        ("note", 42, None),
    ],
)
def test_corrected_invalid_reuse_of_mutation_identity_is_not_replayed(
    service_database: ServiceDatabase,
    invalid_field: str,
    invalid_value: object,
    corrected_value: object,
) -> None:
    mutation_id = uuid4()
    base = schedule_command(service_database, mutation_id=mutation_id)
    if invalid_field == "consumption_percentage":
        invalid = replace(base, consumption_percentage=cast(Decimal, invalid_value))
        corrected = replace(base, consumption_percentage=cast(Decimal, corrected_value))
    else:
        invalid = replace(base, note=cast(str | None, invalid_value))
        corrected = replace(base, note=cast(str | None, corrected_value))
    with pytest.raises(ApplicationServiceError) as invalid_error:
        asyncio.run(schedule_recipe(service_database.sessions, context(service_database), invalid))
    assert invalid_error.value.code == "validation_failed"
    with pytest.raises(ApplicationServiceError) as corrected_error:
        asyncio.run(
            schedule_recipe(service_database.sessions, context(service_database), corrected)
        )
    assert corrected_error.value.code == "idempotency_mismatch"


def test_corrected_malformed_uuid_reuse_of_mutation_identity_is_not_replayed(
    service_database: ServiceDatabase,
) -> None:
    base = schedule_command(service_database, mutation_id=uuid4())
    invalid = replace(base, event_day_id=cast(UUID, "not-a-uuid"))
    with pytest.raises(ApplicationServiceError) as invalid_error:
        asyncio.run(schedule_recipe(service_database.sessions, context(service_database), invalid))
    assert invalid_error.value.code == "validation_failed"
    with pytest.raises(ApplicationServiceError) as corrected_error:
        asyncio.run(schedule_recipe(service_database.sessions, context(service_database), base))
    assert corrected_error.value.code == "idempotency_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("consumption_percentage", Decimal("-0.1")),
        ("consumption_percentage", Decimal("NaN")),
        ("consumption_percentage", Decimal("Infinity")),
        ("position_key", "a-1"),
        ("position_key", ""),
        ("note", 42),
        ("note", "x\x00y"),
        pytest.param("note", "\x00" * 22_000, id="oversized_note"),
    ],
)
def test_invalid_command_is_deterministically_retained(
    service_database: ServiceDatabase, field: str, value: object
) -> None:
    base = schedule_command(service_database)
    if field == "consumption_percentage":
        command = replace(base, consumption_percentage=cast(Decimal, value))
    elif field == "position_key":
        command = replace(base, position_key=cast(str, value))
    else:
        assert field == "note"
        command = replace(base, note=cast(str | None, value))
    for _ in range(2):
        with pytest.raises(ApplicationServiceError) as error:
            asyncio.run(
                schedule_recipe(service_database.sessions, context(service_database), command)
            )
        assert error.value.code == "validation_failed"
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == command.mutation_id))
            == "rejected"
        )
        assert connection.scalar(select(func.count()).select_from(ScheduledRecipe)) == 0


def test_cross_event_or_catalog_reference_is_rejected_without_enumeration(
    service_database: ServiceDatabase,
) -> None:
    command = replace(
        schedule_command(service_database), event_day_id=service_database.other_event_day_id
    )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(schedule_recipe(service_database.sessions, context(service_database), command))
    assert error.value.code == "validation_failed"
    assert error.value.field_violations[0].path == "catalog_or_event_references"


def test_foreign_scheduled_recipe_identity_is_not_enumerated(
    service_database: ServiceDatabase,
) -> None:
    foreign_event_id, foreign_day_id, foreign_role_id = uuid4(), uuid4(), uuid4()
    foreign_recipe_id, foreign_recipe_version_id, foreign_scheduled_recipe_id = (
        uuid4(),
        uuid4(),
        uuid4(),
    )
    with service_database.sync_engine.begin() as connection:
        scaling_unit_id = connection.scalar(
            select(RecipeVersion.scaling_unit_id).where(
                RecipeVersion.id == service_database.recipe_version_id
            )
        )
        assert scaling_unit_id is not None
        connection.execute(
            insert(Event).values(
                id=foreign_event_id,
                organization_id=service_database.other_organization_id,
                name="Foreign camp",
                start_date=date(2026, 7, 3),
                end_date=date(2026, 7, 3),
                base_expected_attendance=1,
                budget_amount=Decimal("0"),
                currency="CZK",
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(EventDay).values(
                id=foreign_day_id,
                event_id=foreign_event_id,
                calendar_date=date(2026, 7, 3),
                is_visible=True,
                provenance="range_generated",
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(EventMealRole).values(
                id=foreign_role_id,
                event_id=foreign_event_id,
                built_in_translation_key="meal_role.dinner",
                position_key="a",
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(Recipe).values(
                id=foreign_recipe_id,
                organization_id=service_database.other_organization_id,
                current_version_id=foreign_recipe_version_id,
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(RecipeVersion).values(
                id=foreign_recipe_version_id,
                organization_id=service_database.other_organization_id,
                recipe_id=foreign_recipe_id,
                name="Foreign recipe",
                scaling_unit_id=scaling_unit_id,
                base_scaling_amount=Decimal("1"),
                round_suggestions_up=False,
                published_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(ScheduledRecipe).values(
                id=foreign_scheduled_recipe_id,
                organization_id=service_database.other_organization_id,
                event_id=foreign_event_id,
                event_day_id=foreign_day_id,
                event_meal_role_id=foreign_role_id,
                recipe_id=foreign_recipe_id,
                recipe_version_id=foreign_recipe_version_id,
                diner_count=1,
                attendance_mode="follows_event",
                consumption_percentage=Decimal("100"),
                selected_scale_amount=Decimal("1"),
                scale_mode="suggested",
                position_key="a",
                created_by_user_id=service_database.actor_id,
            )
        )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            schedule_recipe(
                service_database.sessions,
                context(service_database),
                replace(
                    schedule_command(service_database),
                    scheduled_recipe_id=foreign_scheduled_recipe_id,
                ),
            )
        )
    assert error.value.code == "validation_failed"
    assert [(item.path, item.code) for item in error.value.field_violations] == [
        ("catalog_or_event_references", "must_be_active_and_consistent")
    ]


def test_historical_recipe_version_cannot_be_newly_scheduled(
    service_database: ServiceDatabase,
) -> None:
    current_version_id = uuid4()
    with service_database.sync_engine.begin() as connection:
        scaling_unit_id = connection.scalar(
            select(RecipeVersion.scaling_unit_id).where(
                RecipeVersion.id == service_database.recipe_version_id
            )
        )
        assert scaling_unit_id is not None
        connection.execute(
            insert(RecipeVersion).values(
                id=current_version_id,
                organization_id=service_database.organization_id,
                recipe_id=service_database.recipe_id,
                based_on_version_id=service_database.recipe_version_id,
                name="Updated recipe",
                scaling_unit_id=scaling_unit_id,
                base_scaling_amount=Decimal("1"),
                estimated_diners_per_scaling_unit=Decimal("20"),
                round_suggestions_up=True,
                published_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            update(Recipe)
            .where(Recipe.id == service_database.recipe_id)
            .values(current_version_id=current_version_id)
        )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            schedule_recipe(
                service_database.sessions,
                context(service_database),
                schedule_command(service_database),
            )
        )
    assert error.value.code == "validation_failed"
    assert error.value.field_violations[0].path == "catalog_or_event_references"


def test_corrupted_retained_rejection_is_not_silently_accepted() -> None:
    mutation = Mutation(
        id=uuid4(),
        outcome_payload={
            "error": {
                "code": "validation_failed",
                "field_violations": [{"path": "note", "code": "invalid"}, "invalid"],
            }
        },
    )
    with pytest.raises(RuntimeError):
        _retained_error(mutation)


def test_same_scheduled_identity_concurrently_creates_once(
    service_database: ServiceDatabase,
) -> None:
    scheduled_recipe_id = uuid4()
    first = schedule_command(service_database, scheduled_recipe_id=scheduled_recipe_id)
    second = replace(first, mutation_id=uuid4())

    async def schedule_both() -> tuple[object, object]:
        return await asyncio.gather(
            schedule_recipe(service_database.sessions, context(service_database), first),
            schedule_recipe(service_database.sessions, context(service_database), second),
            return_exceptions=True,
        )

    results = asyncio.run(schedule_both())
    assert sum(result.__class__.__name__ == "ScheduleRecipeResult" for result in results) == 1
    errors = [result for result in results if isinstance(result, ApplicationServiceError)]
    assert len(errors) == 1
    assert errors[0].code == "validation_failed"
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(ScheduledRecipe)
                .where(ScheduledRecipe.id == scheduled_recipe_id)
            )
            == 1
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(OrganizationChange)
                .where(OrganizationChange.mutation_id.in_((first.mutation_id, second.mutation_id)))
            )
            == 3
        )


def test_member_moves_a_scheduled_recipe_with_lww_placement_and_feed(
    service_database: ServiceDatabase,
) -> None:
    scheduled_recipe_id = schedule_then_override(service_database)
    command = move_command(service_database, scheduled_recipe_id, position_key="z9")
    result = asyncio.run(
        move_scheduled_recipe(service_database.sessions, context(service_database), command)
    )

    assert result.outcome == "accepted"
    assert result.position_key == "z9"
    assert result.first_change_sequence == result.last_change_sequence
    with service_database.sync_engine.connect() as connection:
        assert connection.execute(
            select(
                ScheduledRecipe.event_day_id,
                ScheduledRecipe.event_meal_role_id,
                ScheduledRecipe.position_key,
            ).where(ScheduledRecipe.id == scheduled_recipe_id)
        ).one() == (service_database.event_day_id, service_database.event_role_id, "z9")
        clock = connection.execute(
            select(FieldClock.winning_client_wall_time, FieldClock.winning_mutation_id).where(
                FieldClock.organization_id == service_database.organization_id,
                FieldClock.entity_kind == "scheduled_recipe",
                FieldClock.entity_id == scheduled_recipe_id,
                FieldClock.field_name == "placement",
            )
        ).one()
        assert clock == (command.client_wall_time, command.mutation_id)
        record = connection.scalar(
            select(OrganizationChange.payload).where(
                OrganizationChange.mutation_id == command.mutation_id
            )
        )
        assert isinstance(record, dict)
        canonical = record.get("record")
        assert isinstance(canonical, dict)
        clocks = canonical.get("field_clocks")
        assert isinstance(clocks, dict)
        placement = clocks.get("placement")
        assert isinstance(placement, dict)
        assert placement["winning_mutation_id"] == str(command.mutation_id)
    bootstrap = asyncio.run(
        SynchronizationQueryService(
            service_database.sessions,
            encoded_cursor_hmac_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
        ).bootstrap(
            actor_user_id=service_database.actor_id,
            organization_id=service_database.organization_id,
        )
    )
    bootstrap_record = next(
        item for item in bootstrap.records if item.entity_id == scheduled_recipe_id
    )
    bootstrap_canonical = bootstrap_record.payload.get("record")
    assert isinstance(bootstrap_canonical, dict)
    bootstrap_clocks = bootstrap_canonical.get("field_clocks")
    assert isinstance(bootstrap_clocks, dict)
    bootstrap_placement = bootstrap_clocks.get("placement")
    assert isinstance(bootstrap_placement, dict)
    assert bootstrap_placement["winning_mutation_id"] == str(command.mutation_id)


def test_move_lww_concurrent_actions_converge_to_newer_placement(
    service_database: ServiceDatabase,
) -> None:
    scheduled_recipe_id = schedule_then_override(service_database)
    newest = move_command(
        service_database,
        scheduled_recipe_id,
        position_key="z",
        client_wall_time=datetime.now(UTC),
    )
    older = move_command(
        service_database,
        scheduled_recipe_id,
        position_key="a",
        client_wall_time=newest.client_wall_time - timedelta(seconds=1),
    )

    async def move_both() -> tuple[MoveScheduledRecipeResult, MoveScheduledRecipeResult]:
        return await asyncio.gather(
            move_scheduled_recipe(service_database.sessions, context(service_database), newest),
            move_scheduled_recipe(service_database.sessions, context(service_database), older),
        )

    outcomes = asyncio.run(move_both())
    assert {outcome.outcome for outcome in outcomes} == {"accepted", "partially_superseded"}
    loser = next(outcome for outcome in outcomes if outcome.outcome == "partially_superseded")
    assert loser.position_key == "z"
    replay = asyncio.run(
        move_scheduled_recipe(
            service_database.sessions,
            context(service_database),
            older,
        )
    )
    assert replay.replayed is True
    assert replay.position_key == "z"
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(ScheduledRecipe.position_key).where(
                    ScheduledRecipe.id == scheduled_recipe_id
                )
            )
            == "z"
        )


def test_move_rejects_archived_or_cross_event_placement(service_database: ServiceDatabase) -> None:
    scheduled_recipe_id = schedule_then_override(service_database)
    cross_event = move_command(
        service_database,
        scheduled_recipe_id,
        event_day_id=service_database.other_event_day_id,
        event_meal_role_id=service_database.other_event_role_id,
    )
    with pytest.raises(ApplicationServiceError) as rejected:
        asyncio.run(
            move_scheduled_recipe(service_database.sessions, context(service_database), cross_event)
        )
    assert rejected.value.field_violations[0].path == "placement"
    with service_database.sync_engine.begin() as connection:
        snapshot_id = uuid4()
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=snapshot_id,
                event_id=service_database.event_id,
                archive_schema_version=1,
                payload={},
                content_hash=b"0" * 32,
                attachment_manifest=[],
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            update(Event)
            .where(Event.id == service_database.event_id)
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=snapshot_id,
                archived_at=datetime.now(UTC),
                archived_by_user_id=service_database.actor_id,
            )
        )
    with pytest.raises(ApplicationServiceError) as archived:
        asyncio.run(
            move_scheduled_recipe(
                service_database.sessions,
                context(service_database),
                move_command(service_database, scheduled_recipe_id),
            )
        )
    assert archived.value.code == "archived_event"


def test_fuzz_move_position_keys_are_validated_without_normalizing_identity(
    service_database: ServiceDatabase,
) -> None:
    random_source = random.Random(20260807)
    scheduled_recipe_id = uuid4()
    for _ in range(200):
        candidate = "".join(chr(random_source.randrange(0, 128)) for _ in range(12))
        prepared = _prepare(
            move_command(service_database, scheduled_recipe_id, position_key=candidate)
        )
        expected = (
            bool(candidate.strip()) and candidate.strip().isascii() and candidate.strip().isalnum()
        )
        assert bool(prepared.violations) is not expected


def test_fuzz_numeric_suggestion_is_nonnegative_or_rejected(
    service_database: ServiceDatabase,
) -> None:
    random_source = random.Random(20260807)
    for _ in range(200):
        value = Decimal(random_source.randrange(-10_000, 10_001)).scaleb(
            random_source.randrange(-8, 8)
        )
        prepared = _prepare_command(
            replace(schedule_command(service_database), consumption_percentage=value)
        )
        if value < 0:
            assert any(
                violation.path == "consumption_percentage" for violation in prepared.violations
            )
        else:
            suggestion = _suggested_scale(
                prepared,
                _ScheduleReferences(
                    diner_count=42,
                    base_scaling_amount=Decimal("1"),
                    estimated_diners_per_scaling_unit=Decimal("20"),
                    round_suggestions_up=True,
                    scaling_unit_code="tray",
                ),
            )
            assert suggestion >= 0
