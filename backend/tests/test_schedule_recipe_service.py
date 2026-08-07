import asyncio
import os
import random
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command as alembic_command
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.application.scheduled_recipes import (
    ScheduleRecipeCommand,
    _prepare_command,
    _retained_error,
    _ScheduleReferences,
    _suggested_scale,
    schedule_recipe,
)
from cookops.persistence.models import (
    ClientInstallation,
    Event,
    EventArchiveSnapshot,
    EventDay,
    EventMealRole,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    Recipe,
    RecipeVersion,
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
        assert person_id is not None and tray_id is not None
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
    assert (result.first_change_sequence, result.last_change_sequence) == (1, 1)
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
        change = connection.execute(
            select(
                OrganizationChange.entity_kind,
                OrganizationChange.entity_id,
                OrganizationChange.payload,
            ).where(OrganizationChange.mutation_id == command.mutation_id)
        ).one()
        assert change.entity_kind == "scheduled_recipe"
        assert change.entity_id == command.scheduled_recipe_id
        assert change.payload["record_schema_version"] == 1
        assert change.payload["record"]["selected_scale_amount"] == "2"


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
            == 1
        )


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
