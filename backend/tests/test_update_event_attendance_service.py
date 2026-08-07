import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from test_create_event_service import (
    ServiceDatabase,
    context,
    event_command,
)
from test_create_event_service import (
    service_database as create_event_service_database,
)
from test_recipe_catalog_migration import insert_ingredient, publish_recipe

from cookops.application.events import (
    UpdateEventBaseAttendanceCommand,
    update_event_base_attendance,
)
from cookops.application.organizations import ApplicationServiceError
from cookops.persistence.models import (
    Event,
    EventArchiveSnapshot,
    EventDay,
    EventMealRole,
    FieldClock,
    Mutation,
    OrganizationChange,
    OrganizationMembership,
    ScheduledRecipe,
    UnitDefinition,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


@pytest.fixture
def service_database() -> Iterator[ServiceDatabase]:
    fixture_function = cast(
        Callable[[], Iterator[ServiceDatabase]],
        vars(create_event_service_database)["__wrapped__"],
    )
    yield from fixture_function()


def _create_event_and_scheduled_recipes(database: ServiceDatabase) -> tuple[UUID, UUID, UUID]:
    from cookops.application.events import create_event

    created = asyncio.run(
        create_event(database.sessions, context(database), event_command(database))
    )
    with database.sync_engine.connect() as connection:
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
        day_id = connection.scalar(select(EventDay.id).where(EventDay.event_id == created.event_id))
        role_id = connection.scalar(
            select(EventMealRole.id).where(EventMealRole.event_id == created.event_id)
        )
    assert (
        grams_id is not None
        and person_id is not None
        and day_id is not None
        and role_id is not None
    )
    _, ingredient_version_id = insert_ingredient(
        database.sync_engine,
        actor_id=database.actor_id,
        organization_id=database.organization_id,
        grams_id=grams_id,
        name="Tomatoes",
    )
    recipe_id, recipe_version_id, _ = publish_recipe(
        database.sync_engine,
        actor_id=database.actor_id,
        organization_id=database.organization_id,
        scaling_unit_id=person_id,
        preferred_display_unit_id=grams_id,
        ingredient_version_id=ingredient_version_id,
    )
    follows_id, manual_id, retired_id = uuid4(), uuid4(), uuid4()
    common = {
        "organization_id": database.organization_id,
        "event_id": created.event_id,
        "event_day_id": day_id,
        "event_meal_role_id": role_id,
        "recipe_id": recipe_id,
        "recipe_version_id": recipe_version_id,
        "consumption_percentage": Decimal("100"),
        "selected_scale_amount": Decimal("1"),
        "scale_mode": "suggested",
        "position_key": "a",
        "created_by_user_id": database.actor_id,
    }
    with database.sync_engine.begin() as connection:
        connection.execute(
            insert(ScheduledRecipe),
            [
                common
                | {
                    "id": follows_id,
                    "diner_count": 42,
                    "attendance_mode": "follows_event",
                    "retired_at": None,
                    "retired_by_user_id": None,
                },
                common
                | {
                    "id": manual_id,
                    "diner_count": 9,
                    "attendance_mode": "manual",
                    "position_key": "b",
                    "retired_at": None,
                    "retired_by_user_id": None,
                },
                common
                | {
                    "id": retired_id,
                    "diner_count": 42,
                    "attendance_mode": "follows_event",
                    "position_key": "c",
                    "retired_at": datetime.now(UTC),
                    "retired_by_user_id": database.actor_id,
                },
            ],
        )
    return created.event_id, follows_id, manual_id


def _command(
    database: ServiceDatabase, event_id: UUID, attendance: object
) -> UpdateEventBaseAttendanceCommand:
    return UpdateEventBaseAttendanceCommand(
        mutation_id=uuid4(),
        event_id=event_id,
        organization_id=database.organization_id,
        base_expected_attendance=cast(int, attendance),
        client_wall_time=datetime.now(UTC),
    )


def test_member_updates_event_attendance_and_only_following_recipes(
    service_database: ServiceDatabase,
) -> None:
    event_id, follows_id, manual_id = _create_event_and_scheduled_recipes(service_database)
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == service_database.organization_id,
                OrganizationMembership.user_id == service_database.actor_id,
            )
            .values(role="member")
        )
    command = _command(service_database, event_id, 57)

    result = asyncio.run(
        update_event_base_attendance(service_database.sessions, context(service_database), command)
    )

    assert result.replayed is False
    assert result.updated_scheduled_recipe_ids == (follows_id,)
    assert (result.first_change_sequence, result.last_change_sequence) == (8, 9)
    with service_database.sync_engine.connect() as connection:
        counts: dict[UUID, int] = {
            cast(UUID, row[0]): cast(int, row[1])
            for row in connection.execute(
                select(ScheduledRecipe.id, ScheduledRecipe.diner_count).where(
                    ScheduledRecipe.event_id == event_id
                )
            )
        }
        assert counts[follows_id] == 57
        assert counts[manual_id] == 9
        mutation = connection.execute(
            select(Mutation.actor_role, Mutation.outcome).where(Mutation.id == command.mutation_id)
        ).one()
        assert mutation == ("member", "accepted")
        changes = connection.execute(
            select(OrganizationChange.entity_kind, OrganizationChange.payload)
            .where(OrganizationChange.mutation_id == command.mutation_id)
            .order_by(OrganizationChange.sequence)
        ).all()
        assert [change.entity_kind for change in changes] == ["event", "scheduled_recipe"]
        assert changes[1].payload["record"]["diner_count"] == 57

    replayed = asyncio.run(
        update_event_base_attendance(service_database.sessions, context(service_database), command)
    )
    assert replayed.replayed is True
    assert replayed.updated_scheduled_recipe_ids == (follows_id,)


@pytest.mark.parametrize("attendance", [-1, True, "57"])
def test_invalid_attendance_is_a_retained_rejection(
    service_database: ServiceDatabase, attendance: object
) -> None:
    event_id, _, _ = _create_event_and_scheduled_recipes(service_database)
    command = _command(service_database, event_id, attendance)

    with pytest.raises(ApplicationServiceError) as first_error:
        asyncio.run(
            update_event_base_attendance(
                service_database.sessions, context(service_database), command
            )
        )
    assert first_error.value.code == "validation_failed"
    assert first_error.value.field_violations[0].path == "base_expected_attendance"
    with pytest.raises(ApplicationServiceError) as replay_error:
        asyncio.run(
            update_event_base_attendance(
                service_database.sessions, context(service_database), command
            )
        )
    assert replay_error.value.code == "validation_failed"
    corrected = UpdateEventBaseAttendanceCommand(
        mutation_id=command.mutation_id,
        event_id=command.event_id,
        organization_id=command.organization_id,
        base_expected_attendance=0,
        client_wall_time=command.client_wall_time,
    )
    with pytest.raises(ApplicationServiceError, match="idempotency_mismatch"):
        asyncio.run(
            update_event_base_attendance(
                service_database.sessions, context(service_database), corrected
            )
        )
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == command.mutation_id))
            == "rejected"
        )


def test_concurrent_attendance_updates_are_lww_and_create_complete_groups(
    service_database: ServiceDatabase,
) -> None:
    event_id, follows_id, manual_id = _create_event_and_scheduled_recipes(service_database)
    first = _command(service_database, event_id, 51)
    second = _command(service_database, event_id, 73)

    async def run_concurrently() -> None:
        await asyncio.gather(
            update_event_base_attendance(
                service_database.sessions, context(service_database), first
            ),
            update_event_base_attendance(
                service_database.sessions, context(service_database), second
            ),
        )

    asyncio.run(run_concurrently())
    with service_database.sync_engine.connect() as connection:
        final_count = connection.scalar(
            select(ScheduledRecipe.diner_count).where(ScheduledRecipe.id == follows_id)
        )
        assert final_count == 73
        assert (
            connection.scalar(
                select(ScheduledRecipe.diner_count).where(ScheduledRecipe.id == manual_id)
            )
            == 9
        )
        changes_by_mutation: dict[UUID, int] = {
            cast(UUID, row[0]): cast(int, row[1])
            for row in connection.execute(
                select(OrganizationChange.mutation_id, func.count())
                .where(OrganizationChange.mutation_id.in_((first.mutation_id, second.mutation_id)))
                .group_by(OrganizationChange.mutation_id)
            )
        }
        assert changes_by_mutation[second.mutation_id] == 2
        assert changes_by_mutation[first.mutation_id] in (1, 2)


def test_attendance_lww_uses_client_time_then_mutation_id(
    service_database: ServiceDatabase,
) -> None:
    event_id, follows_id, _ = _create_event_and_scheduled_recipes(service_database)
    action_time = datetime.now(UTC) + timedelta(seconds=1)
    winning = UpdateEventBaseAttendanceCommand(
        mutation_id=UUID(int=2),
        event_id=event_id,
        organization_id=service_database.organization_id,
        base_expected_attendance=73,
        client_wall_time=action_time,
    )
    losing = UpdateEventBaseAttendanceCommand(
        mutation_id=UUID(int=1),
        event_id=event_id,
        organization_id=service_database.organization_id,
        base_expected_attendance=51,
        client_wall_time=action_time,
    )

    winning_result = asyncio.run(
        update_event_base_attendance(service_database.sessions, context(service_database), winning)
    )
    losing_result = asyncio.run(
        update_event_base_attendance(service_database.sessions, context(service_database), losing)
    )

    assert winning_result.outcome == "accepted"
    assert losing_result.outcome == "partially_superseded"
    assert losing_result.base_expected_attendance == 73
    assert losing_result.updated_scheduled_recipe_ids == ()
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Event.base_expected_attendance).where(Event.id == event_id))
            == 73
        )
        assert (
            connection.scalar(
                select(ScheduledRecipe.diner_count).where(ScheduledRecipe.id == follows_id)
            )
            == 73
        )
        assert (
            connection.execute(
                select(Mutation.outcome).where(Mutation.id == losing.mutation_id)
            ).scalar_one()
            == "partially_superseded"
        )
        assert (
            connection.execute(
                select(FieldClock.winning_mutation_id).where(
                    FieldClock.organization_id == service_database.organization_id,
                    FieldClock.entity_kind == "event",
                    FieldClock.entity_id == event_id,
                    FieldClock.field_name == "base_expected_attendance",
                )
            ).scalar_one()
            == winning.mutation_id
        )


def test_future_attendance_command_is_retained_without_changing_the_event(
    service_database: ServiceDatabase,
) -> None:
    event_id, follows_id, _ = _create_event_and_scheduled_recipes(service_database)
    command = UpdateEventBaseAttendanceCommand(
        mutation_id=uuid4(),
        event_id=event_id,
        organization_id=service_database.organization_id,
        base_expected_attendance=73,
        client_wall_time=datetime.now(UTC) + timedelta(hours=24, seconds=1),
    )

    with pytest.raises(ApplicationServiceError, match="client_time_too_far_ahead"):
        asyncio.run(
            update_event_base_attendance(
                service_database.sessions, context(service_database), command
            )
        )

    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Event.base_expected_attendance).where(Event.id == event_id))
            == 42
        )
        assert (
            connection.scalar(
                select(ScheduledRecipe.diner_count).where(ScheduledRecipe.id == follows_id)
            )
            == 42
        )
        assert connection.execute(
            select(Mutation.outcome, Mutation.client_wall_time).where(
                Mutation.id == command.mutation_id
            )
        ).one() == ("rejected", command.client_wall_time)


def test_archived_event_rejects_attendance_mutation(
    service_database: ServiceDatabase,
) -> None:
    event_id, _, _ = _create_event_and_scheduled_recipes(service_database)
    snapshot_id = uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=snapshot_id,
                event_id=event_id,
                archive_schema_version=1,
                payload={"event": {}},
                content_hash=b"a" * 32,
                attachment_manifest=[],
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=snapshot_id,
                archived_at=datetime.now(UTC),
                archived_by_user_id=service_database.actor_id,
            )
        )
    command = _command(service_database, event_id, 73)

    with pytest.raises(ApplicationServiceError, match="archived_event"):
        asyncio.run(
            update_event_base_attendance(
                service_database.sessions, context(service_database), command
            )
        )

    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == command.mutation_id))
            == "rejected"
        )
