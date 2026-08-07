import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from test_create_event_service import ServiceDatabase, context, event_command
from test_create_event_service import service_database as create_event_service_database

from cookops.application.event_duplication import DuplicateEventCommand, duplicate_event
from cookops.application.event_lifecycle import SetEventLifecycleCommand, set_event_lifecycle
from cookops.application.organizations import ApplicationServiceError, FieldViolation
from cookops.persistence.models import (
    Event,
    EventDay,
    Mutation,
    OrganizationChange,
    OrganizationMembership,
    Receipt,
    ShoppingList,
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


def test_duplicate_archived_event_copies_plan_not_operational_history(
    service_database: ServiceDatabase,
) -> None:
    from cookops.application.events import create_event

    source = asyncio.run(
        create_event(
            service_database.sessions, context(service_database), event_command(service_database)
        )
    )
    archived = asyncio.run(
        set_event_lifecycle(
            service_database.sessions,
            context(service_database),
            SetEventLifecycleCommand(
                mutation_id=uuid4(),
                event_id=source.event_id,
                organization_id=service_database.organization_id,
                operation="archive",
                client_wall_time=datetime.now(UTC),
            ),
        )
    )
    assert archived.archive_snapshot_id is not None
    command = DuplicateEventCommand(
        mutation_id=uuid4(),
        event_id=uuid4(),
        organization_id=service_database.organization_id,
        source_event_id=source.event_id,
        source_archive_snapshot_id=archived.archive_snapshot_id,
        name="Copied event",
        client_wall_time=datetime.now(UTC),
    )
    result = asyncio.run(
        duplicate_event(service_database.sessions, context(service_database), command)
    )
    replay = asyncio.run(
        duplicate_event(service_database.sessions, context(service_database), command)
    )
    assert replay.replayed and replay.event_id == result.event_id
    with service_database.sync_engine.connect() as connection:
        copied = connection.execute(select(Event).where(Event.id == result.event_id)).one()
        assert copied.lifecycle == "active"
        assert copied.current_archive_snapshot_id is None
        assert connection.scalar(
            select(func.count()).select_from(EventDay).where(EventDay.event_id == result.event_id)
        ) == len(source.days)
        assert (
            connection.scalar(
                select(func.count())
                .select_from(ShoppingList)
                .where(ShoppingList.event_id == result.event_id)
            )
            == 0
        )
        assert (
            connection.scalar(
                select(func.count()).select_from(Receipt).where(Receipt.event_id == result.event_id)
            )
            == 0
        )
        assert connection.scalar(
            select(func.count())
            .select_from(OrganizationChange)
            .where(OrganizationChange.mutation_id == command.mutation_id)
        ) == 1 + len(source.days) + len(source.meal_roles)


def test_duplicate_requires_current_archived_snapshot(service_database: ServiceDatabase) -> None:
    command = DuplicateEventCommand(
        mutation_id=uuid4(),
        event_id=uuid4(),
        organization_id=service_database.organization_id,
        source_event_id=uuid4(),
        source_archive_snapshot_id=uuid4(),
        name="Copied event",
        client_wall_time=datetime.now(UTC),
    )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(duplicate_event(service_database.sessions, context(service_database), command))
    assert error.value.code == "validation_failed"


def test_duplicate_rejects_a_superseded_archive_snapshot(
    service_database: ServiceDatabase,
) -> None:
    from cookops.application.events import create_event

    source = asyncio.run(
        create_event(
            service_database.sessions, context(service_database), event_command(service_database)
        )
    )
    first = asyncio.run(
        set_event_lifecycle(
            service_database.sessions,
            context(service_database),
            SetEventLifecycleCommand(
                mutation_id=uuid4(),
                event_id=source.event_id,
                organization_id=service_database.organization_id,
                operation="archive",
                client_wall_time=datetime.now(UTC),
            ),
        )
    )
    asyncio.run(
        set_event_lifecycle(
            service_database.sessions,
            context(service_database),
            SetEventLifecycleCommand(
                mutation_id=uuid4(),
                event_id=source.event_id,
                organization_id=service_database.organization_id,
                operation="reactivate",
                client_wall_time=datetime.now(UTC),
            ),
        )
    )
    second = asyncio.run(
        set_event_lifecycle(
            service_database.sessions,
            context(service_database),
            SetEventLifecycleCommand(
                mutation_id=uuid4(),
                event_id=source.event_id,
                organization_id=service_database.organization_id,
                operation="archive",
                client_wall_time=datetime.now(UTC),
            ),
        )
    )
    assert first.archive_snapshot_id is not None
    assert second.archive_snapshot_id is not None
    assert first.archive_snapshot_id != second.archive_snapshot_id
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            duplicate_event(
                service_database.sessions,
                context(service_database),
                DuplicateEventCommand(
                    mutation_id=uuid4(),
                    event_id=uuid4(),
                    organization_id=service_database.organization_id,
                    source_event_id=source.event_id,
                    source_archive_snapshot_id=first.archive_snapshot_id,
                    name="Stale copy",
                    client_wall_time=datetime.now(UTC),
                ),
            )
        )
    assert error.value.code == "validation_failed"


def test_invalid_duplicate_is_retained_for_an_idempotent_retry(
    service_database: ServiceDatabase,
) -> None:
    command = DuplicateEventCommand(
        mutation_id=uuid4(),
        event_id=uuid4(),
        organization_id=service_database.organization_id,
        source_event_id=uuid4(),
        source_archive_snapshot_id=uuid4(),
        name="  ",
        client_wall_time=datetime.now(UTC),
    )
    for _ in range(2):
        with pytest.raises(ApplicationServiceError) as error:
            asyncio.run(
                duplicate_event(service_database.sessions, context(service_database), command)
            )
        assert error.value.code == "validation_failed"
        assert error.value.field_violations == (
            FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"),
        )
    with service_database.sync_engine.connect() as connection:
        outcome, outcome_payload = connection.execute(
            select(Mutation.outcome, Mutation.outcome_payload).where(
                Mutation.id == command.mutation_id
            )
        ).one()
    assert outcome == "rejected"
    assert outcome_payload == {
        "error": {
            "code": "validation_failed",
            "field_violations": [
                {"path": "name", "code": "must_be_nonblank_and_at_most_200_characters"}
            ],
        }
    }


def test_missing_archived_source_is_retained_for_an_idempotent_retry(
    service_database: ServiceDatabase,
) -> None:
    command = DuplicateEventCommand(
        mutation_id=uuid4(),
        event_id=uuid4(),
        organization_id=service_database.organization_id,
        source_event_id=uuid4(),
        source_archive_snapshot_id=uuid4(),
        name="Copy",
        client_wall_time=datetime.now(UTC),
    )
    for _ in range(2):
        with pytest.raises(ApplicationServiceError) as error:
            asyncio.run(
                duplicate_event(service_database.sessions, context(service_database), command)
            )
        assert error.value.field_violations == (
            FieldViolation("source_archive_snapshot_id", "not_found"),
        )
    with service_database.sync_engine.connect() as connection:
        outcome = connection.scalar(
            select(Mutation.outcome).where(Mutation.id == command.mutation_id)
        )
    assert outcome == "rejected"


def test_organization_member_can_duplicate_an_archived_event(
    service_database: ServiceDatabase,
) -> None:
    from cookops.application.events import create_event

    source = asyncio.run(
        create_event(
            service_database.sessions, context(service_database), event_command(service_database)
        )
    )
    archived = asyncio.run(
        set_event_lifecycle(
            service_database.sessions,
            context(service_database),
            SetEventLifecycleCommand(
                mutation_id=uuid4(),
                event_id=source.event_id,
                organization_id=service_database.organization_id,
                operation="archive",
                client_wall_time=datetime.now(UTC),
            ),
        )
    )
    assert archived.archive_snapshot_id is not None
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == service_database.organization_id,
                OrganizationMembership.user_id == service_database.actor_id,
            )
            .values(role="member")
        )
    result = asyncio.run(
        duplicate_event(
            service_database.sessions,
            context(service_database),
            DuplicateEventCommand(
                mutation_id=uuid4(),
                event_id=uuid4(),
                organization_id=service_database.organization_id,
                source_event_id=source.event_id,
                source_archive_snapshot_id=archived.archive_snapshot_id,
                name="Member copy",
                client_wall_time=datetime.now(UTC),
            ),
        )
    )
    assert result.event_id != source.event_id
