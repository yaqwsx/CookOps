import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from test_create_event_service import ServiceDatabase, context, event_command
from test_create_event_service import service_database as create_event_service_database

from cookops.application.event_lifecycle import SetEventLifecycleCommand, set_event_lifecycle
from cookops.application.events import (
    UpdateEventBaseAttendanceCommand,
    update_event_base_attendance,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
)
from cookops.persistence.models import (
    ClientInstallation,
    DietaryTag,
    Event,
    EventArchiveSnapshot,
    EventDay,
    EventDietaryException,
    EventDietaryExceptionTag,
    EventMealRole,
    Mutation,
    OrganizationChange,
    OrganizationMembership,
    Recipe,
    RecipeVersion,
    RecipeVersionIngredientLine,
    ScheduledRecipe,
    UnitDefinition,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


@pytest.fixture
def service_database() -> Iterator[ServiceDatabase]:
    fixture = cast(
        Callable[[], Iterator[ServiceDatabase]], vars(create_event_service_database)["__wrapped__"]
    )
    yield from fixture()


def command(database: ServiceDatabase, event_id: UUID, operation: str) -> SetEventLifecycleCommand:
    return SetEventLifecycleCommand(
        mutation_id=uuid4(),
        event_id=event_id,
        organization_id=database.organization_id,
        operation=cast(Literal["archive", "reactivate"], operation),
        client_wall_time=datetime.now(UTC),
    )


def test_archive_materializes_history_emits_change_and_replays(
    service_database: ServiceDatabase,
) -> None:
    from cookops.application.events import create_event

    created = asyncio.run(
        create_event(
            service_database.sessions, context(service_database), event_command(service_database)
        )
    )
    historian_id = uuid4()
    historian_installation_id = uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=historian_id,
                display_name="Archived day editor",
                verified_email="history@example.test",
                normalized_email="history@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=historian_installation_id,
                user_id=historian_id,
                installation_kind="browser",
            )
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=service_database.organization_id,
                user_id=historian_id,
                invited_email="history@example.test",
                role="organization_admin",
                state="active",
                invited_by_user_id=service_database.actor_id,
                claimed_at=datetime.now(UTC),
            )
        )
        connection.execute(
            update(EventDay)
            .where(EventDay.event_id == created.event_id)
            .values(created_by_user_id=historian_id)
        )
    archived = command(service_database, created.event_id, "archive")
    result = asyncio.run(
        set_event_lifecycle(
            service_database.sessions,
            ExecutionContext(
                actor_user_id=historian_id,
                client_installation_id=historian_installation_id,
            ),
            archived,
        )
    )
    assert result.lifecycle == "archived"
    assert result.archive_snapshot_id is not None
    with service_database.sync_engine.connect() as connection:
        event = connection.execute(
            select(Event.lifecycle, Event.current_archive_snapshot_id).where(
                Event.id == created.event_id
            )
        ).one()
        assert event == ("archived", result.archive_snapshot_id)
        snapshot = connection.execute(
            select(EventArchiveSnapshot.payload, EventArchiveSnapshot.content_hash).where(
                EventArchiveSnapshot.id == result.archive_snapshot_id
            )
        ).one()
        assert snapshot.payload["schema_version"] == 1
        assert snapshot.payload["event"]["id"] == str(created.event_id)
        assert {user["display_name"] for user in snapshot.payload["attribution_users"]} >= {
            "Archived day editor"
        }
        assert len(snapshot.content_hash) == 32
        assert (
            connection.scalar(
                select(func.count())
                .select_from(OrganizationChange)
                .where(OrganizationChange.mutation_id == archived.mutation_id)
            )
            == 1
        )
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == archived.mutation_id))
            == "accepted"
        )
    replay = asyncio.run(
        set_event_lifecycle(
            service_database.sessions,
            ExecutionContext(
                actor_user_id=historian_id,
                client_installation_id=historian_installation_id,
            ),
            archived,
        )
    )
    assert replay.replayed is True
    assert replay.archive_snapshot_id == result.archive_snapshot_id


def test_archive_persists_resolved_dietary_warnings_after_live_changes(
    service_database: ServiceDatabase,
) -> None:
    from cookops.application.events import create_event
    from cookops.application.ingredients import CreateIngredientCommand, create_ingredient

    created = asyncio.run(
        create_event(
            service_database.sessions, context(service_database), event_command(service_database)
        )
    )
    ingredient_id, ingredient_version_id = uuid4(), uuid4()
    recipe_id, recipe_version_id, line_key = uuid4(), uuid4(), uuid4()
    exception_id, association_id, custom_association_id = uuid4(), uuid4(), uuid4()
    custom_tag_id = uuid4()
    connection = service_database.sync_engine.connect()
    try:
        day_id = connection.scalar(select(EventDay.id).where(EventDay.event_id == created.event_id))
        role_id = connection.scalar(
            select(EventMealRole.id).where(EventMealRole.event_id == created.event_id)
        )
        unit_id = uuid4()
        tag_id = uuid4()
        assert day_id and role_id and unit_id and tag_id
        connection.execute(
            insert(UnitDefinition).values(
                id=unit_id,
                organization_id=service_database.organization_id,
                code="portion_test",
                custom_name="Portion test",
                normalized_custom_name="portion test",
                dimension="count",
                base_unit_factor=None,
                rounds_up_to_whole_unit=False,
                allows_ingredient_quantity=True,
                allows_recipe_scaling=True,
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=tag_id,
                organization_id=service_database.organization_id,
                seed_key="vegan",
                name=None,
                normalized_name=None,
                color=None,
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=custom_tag_id,
                organization_id=service_database.organization_id,
                seed_key=None,
                name="Nut-free",
                normalized_name="nut-free",
                color=None,
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.commit()
        ingredient = asyncio.run(
            create_ingredient(
                service_database.sessions,
                context(service_database),
                CreateIngredientCommand(
                    mutation_id=uuid4(),
                    ingredient_id=ingredient_id,
                    ingredient_version_id=ingredient_version_id,
                    organization_id=service_database.organization_id,
                    name="Tofu",
                    canonical_unit_id=unit_id,
                    mass_per_canonical_quantity=Decimal("1"),
                    client_wall_time=datetime.now(UTC),
                    dietary_tag_ids=(tag_id, custom_tag_id),
                ),
            )
        )
        ingredient_id = ingredient.ingredient_id
        ingredient_version_id = ingredient.ingredient_version_id
        connection.execute(
            insert(Recipe).values(
                id=recipe_id,
                organization_id=service_database.organization_id,
                current_version_id=recipe_version_id,
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(RecipeVersionIngredientLine).values(
                id=uuid4(),
                organization_id=service_database.organization_id,
                recipe_id=recipe_id,
                recipe_version_id=recipe_version_id,
                line_key=line_key,
                ingredient_version_id=ingredient_version_id,
                base_quantity=Decimal("1"),
                preferred_display_unit_id=None,
                note=None,
                position_key="a",
                scaling_behavior="proportional",
                include_in_portion_weight=True,
            )
        )
        connection.execute(
            insert(RecipeVersion).values(
                id=recipe_version_id,
                organization_id=service_database.organization_id,
                recipe_id=recipe_id,
                based_on_version_id=None,
                name="Tofu soup",
                description=None,
                scaling_model="single_variable",
                scaling_unit_id=unit_id,
                base_scaling_amount=Decimal("1"),
                estimated_diners_per_scaling_unit=None,
                round_suggestions_up=False,
                published_by_user_id=service_database.actor_id,
            )
        )
        scheduled_id = uuid4()
        connection.execute(
            insert(ScheduledRecipe).values(
                id=scheduled_id,
                organization_id=service_database.organization_id,
                event_id=created.event_id,
                event_day_id=day_id,
                event_meal_role_id=role_id,
                recipe_id=recipe_id,
                recipe_version_id=recipe_version_id,
                diner_count=2,
                attendance_mode="follows_event",
                consumption_percentage=Decimal("100"),
                selected_scale_amount=Decimal("2"),
                scale_mode="manual",
                note=None,
                position_key="a",
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(EventDietaryException).values(
                id=exception_id,
                organization_id=service_database.organization_id,
                event_id=created.event_id,
                name="Alex",
                note=None,
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(EventDietaryExceptionTag).values(
                id=association_id,
                organization_id=service_database.organization_id,
                exception_id=exception_id,
                dietary_tag_id=tag_id,
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(EventDietaryExceptionTag).values(
                id=custom_association_id,
                organization_id=service_database.organization_id,
                exception_id=exception_id,
                dietary_tag_id=custom_tag_id,
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.commit()
    finally:
        connection.close()
    archived = command(service_database, created.event_id, "archive")
    result = asyncio.run(
        set_event_lifecycle(service_database.sessions, context(service_database), archived)
    )
    with service_database.sync_engine.begin() as connection:
        before = connection.scalar(
            select(EventArchiveSnapshot.payload).where(
                EventArchiveSnapshot.id == result.archive_snapshot_id
            )
        )
        assert before is not None
        expected_descriptors = sorted(
            [
                {"id": str(tag_id), "seed_key": "vegan", "name": None},
                {"id": str(custom_tag_id), "seed_key": None, "name": "Nut-free"},
            ],
            key=lambda item: cast(str, item["id"]),
        )
        assert before["resolved_dietary_warnings"] == [
            {
                "id": str(scheduled_id),
                "event_id": str(created.event_id),
                "organization_id": str(service_database.organization_id),
                "scheduled_recipe_id": str(scheduled_id),
                "warnings": [
                    {
                        "exception_name": "Alex",
                        "tag_descriptors": expected_descriptors,
                        "ingredient_names": ["Tofu"],
                    }
                ],
                "retired_at": None,
            }
        ]
        connection.execute(
            update(EventDietaryException)
            .where(EventDietaryException.id == exception_id)
            .values(name="Changed")
        )
        connection.execute(
            update(DietaryTag)
            .where(DietaryTag.id == tag_id)
            .values(retired_at=datetime.now(UTC), retired_by_user_id=service_database.actor_id)
        )
        connection.execute(
            update(DietaryTag)
            .where(DietaryTag.id == custom_tag_id)
            .values(
                name="Changed",
                normalized_name="changed",
                retired_at=datetime.now(UTC),
                retired_by_user_id=service_database.actor_id,
            )
        )
        after = connection.scalar(
            select(EventArchiveSnapshot.payload).where(
                EventArchiveSnapshot.id == result.archive_snapshot_id
            )
        )
    assert after is not None
    assert after["resolved_dietary_warnings"] == before["resolved_dietary_warnings"]


def test_only_administrator_can_archive_and_reactivate(service_database: ServiceDatabase) -> None:
    from cookops.application.events import create_event
    from cookops.persistence.models import OrganizationMembership

    created = asyncio.run(
        create_event(
            service_database.sessions, context(service_database), event_command(service_database)
        )
    )
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == service_database.organization_id,
                OrganizationMembership.user_id == service_database.actor_id,
            )
            .values(role="member")
        )
    with pytest.raises(ApplicationServiceError, match="forbidden"):
        asyncio.run(
            set_event_lifecycle(
                service_database.sessions,
                context(service_database),
                command(service_database, created.event_id, "archive"),
            )
        )


def test_invalid_transition_is_retained_and_replays_its_error(
    service_database: ServiceDatabase,
) -> None:
    from cookops.application.events import create_event

    created = asyncio.run(
        create_event(
            service_database.sessions, context(service_database), event_command(service_database)
        )
    )
    invalid = command(service_database, created.event_id, "reactivate")
    for _ in range(2):
        with pytest.raises(ApplicationServiceError) as error:
            asyncio.run(
                set_event_lifecycle(service_database.sessions, context(service_database), invalid)
            )
        assert error.value.code == "validation_failed"
        assert error.value.field_violations == (
            FieldViolation("operation", "invalid_for_lifecycle"),
        )
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == invalid.mutation_id))
            == "rejected"
        )


def test_archive_keeps_attendance_clock_in_event_change(service_database: ServiceDatabase) -> None:
    from cookops.application.events import create_event

    created = asyncio.run(
        create_event(
            service_database.sessions, context(service_database), event_command(service_database)
        )
    )
    attendance = UpdateEventBaseAttendanceCommand(
        mutation_id=uuid4(),
        event_id=created.event_id,
        organization_id=service_database.organization_id,
        base_expected_attendance=8,
        client_wall_time=datetime.now(UTC),
    )
    asyncio.run(
        update_event_base_attendance(
            service_database.sessions, context(service_database), attendance
        )
    )
    archived = command(service_database, created.event_id, "archive")
    asyncio.run(set_event_lifecycle(service_database.sessions, context(service_database), archived))
    with service_database.sync_engine.connect() as connection:
        payload = connection.scalar(
            select(OrganizationChange.payload).where(
                OrganizationChange.mutation_id == archived.mutation_id
            )
        )
    assert isinstance(payload, dict)
    record = payload["record"]
    assert isinstance(record, dict)
    assert record["field_clocks"]["base_expected_attendance"]["winning_mutation_id"] == str(
        attendance.mutation_id
    )


def test_concurrent_archive_and_reactivation_leave_complete_change_groups(
    service_database: ServiceDatabase,
) -> None:
    from cookops.application.events import create_event

    created = asyncio.run(
        create_event(
            service_database.sessions, context(service_database), event_command(service_database)
        )
    )
    archive = command(service_database, created.event_id, "archive")
    asyncio.run(set_event_lifecycle(service_database.sessions, context(service_database), archive))
    first, second = (
        command(service_database, created.event_id, "reactivate"),
        command(service_database, created.event_id, "reactivate"),
    )

    async def concurrently() -> tuple[object, object]:
        return await asyncio.gather(
            set_event_lifecycle(service_database.sessions, context(service_database), first),
            set_event_lifecycle(service_database.sessions, context(service_database), second),
            return_exceptions=True,
        )

    outcomes = asyncio.run(concurrently())
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1, outcomes
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Event.lifecycle).where(Event.id == created.event_id))
            == "active"
        )
