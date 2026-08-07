import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
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
    Event,
    EventArchiveSnapshot,
    EventDay,
    Mutation,
    OrganizationChange,
    OrganizationMembership,
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
