import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
from cookops.application.events import CreateEventCommand, create_event
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.persistence.models import (
    ClientInstallation,
    Event,
    EventDay,
    EventMealRole,
    FieldClock,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMealRolePreset,
    OrganizationMembership,
    SystemRoleAssignment,
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


@pytest.fixture
def service_database() -> Iterator[ServiceDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    sync_engine = create_engine(database_url)
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    actor_id = uuid4()
    installation_id = uuid4()
    organization_id = uuid4()
    now = datetime.now(UTC)
    with sync_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Organization administrator",
                verified_email="admin@example.test",
                normalized_email="admin@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id, user_id=actor_id, installation_kind="browser"
            )
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id,
                name="Kitchen crew",
                default_currency="EUR",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                user_id=actor_id,
                invited_email="admin@example.test",
                role="organization_admin",
                state="active",
                invited_by_user_id=actor_id,
                claimed_at=now,
            )
        )
        connection.execute(
            insert(OrganizationMealRolePreset),
            [
                {
                    "organization_id": organization_id,
                    "built_in_translation_key": "meal_role.lunch",
                    "custom_name": None,
                    "normalized_custom_name": None,
                    "position_key": "b",
                    "created_by_user_id": actor_id,
                    "retired_at": None,
                    "retired_by_user_id": None,
                },
                {
                    "organization_id": organization_id,
                    "built_in_translation_key": None,
                    "custom_name": "  Late supper  ",
                    "normalized_custom_name": "late supper",
                    "position_key": "c",
                    "created_by_user_id": actor_id,
                    "retired_at": None,
                    "retired_by_user_id": None,
                },
                {
                    "organization_id": organization_id,
                    "built_in_translation_key": "meal_role.breakfast",
                    "custom_name": None,
                    "normalized_custom_name": None,
                    "position_key": "a",
                    "created_by_user_id": actor_id,
                    "retired_at": None,
                    "retired_by_user_id": None,
                },
                {
                    "organization_id": organization_id,
                    "built_in_translation_key": "meal_role.soup",
                    "custom_name": None,
                    "normalized_custom_name": None,
                    "position_key": "d",
                    "created_by_user_id": actor_id,
                    "retired_at": now,
                    "retired_by_user_id": actor_id,
                },
            ],
        )
    database = ServiceDatabase(
        sync_engine=sync_engine,
        sessions=async_sessionmaker(async_engine, expire_on_commit=False),
        actor_id=actor_id,
        installation_id=installation_id,
        organization_id=organization_id,
    )
    try:
        yield database
    finally:
        asyncio.run(async_engine.dispose())
        sync_engine.dispose()
        alembic_command.downgrade(configuration, "base")


def context(database: ServiceDatabase) -> ExecutionContext:
    return ExecutionContext(database.actor_id, database.installation_id)


def event_command(
    database: ServiceDatabase,
    *,
    mutation_id: UUID | None = None,
    event_id: UUID | None = None,
    name: str = "  Summer camp  ",
    start_date: date = date(2026, 7, 1),
    end_date: date = date(2026, 7, 3),
    attendance: int = 42,
    budget: Decimal = Decimal("1250.50"),
    location: str | None = "  Forest base  ",
    note: str | None = "First line\r\nSecond line",
    wall_time: datetime | None = None,
) -> CreateEventCommand:
    return CreateEventCommand(
        mutation_id=mutation_id or uuid4(),
        event_id=event_id or uuid4(),
        organization_id=database.organization_id,
        name=name,
        start_date=start_date,
        end_date=end_date,
        base_expected_attendance=attendance,
        budget_amount=budget,
        location=location,
        general_note=note,
        client_wall_time=wall_time or datetime.now(UTC),
    )


def test_create_event_copies_current_presets_and_range_days(
    service_database: ServiceDatabase,
) -> None:
    command = event_command(service_database, name="  Výprava  ")
    result = asyncio.run(
        create_event(service_database.sessions, context(service_database), command)
    )

    assert result.replayed is False
    assert result.name == "Výprava"
    assert result.currency == "EUR"
    assert result.location == "Forest base"
    assert result.general_note == "First line\nSecond line"
    assert (result.first_change_sequence, result.last_change_sequence) == (1, 7)
    assert [day.calendar_date for day in result.days] == [
        date(2026, 7, 1),
        date(2026, 7, 2),
        date(2026, 7, 3),
    ]
    assert [role.position_key for role in result.meal_roles] == ["a", "b", "c"]
    assert [role.built_in_translation_key for role in result.meal_roles] == [
        "meal_role.breakfast",
        "meal_role.lunch",
        None,
    ]
    assert result.meal_roles[2].custom_name == "  Late supper  "

    with service_database.sync_engine.connect() as connection:
        event = connection.execute(
            select(
                Event.name,
                Event.base_expected_attendance,
                Event.budget_amount,
                Event.currency,
                Event.created_by_user_id,
                Event.created_at,
            ).where(Event.id == command.event_id)
        ).one()
        assert event[:5] == ("Výprava", 42, Decimal("1250.50"), "EUR", service_database.actor_id)
        event_created_at = event[5]
        attendance_clock = connection.execute(
            select(FieldClock.winning_client_wall_time, FieldClock.winning_mutation_id).where(
                FieldClock.organization_id == service_database.organization_id,
                FieldClock.entity_kind == "event",
                FieldClock.entity_id == command.event_id,
                FieldClock.field_name == "base_expected_attendance",
            )
        ).one()
        days = connection.execute(
            select(EventDay.calendar_date, EventDay.provenance, EventDay.is_visible)
            .where(EventDay.event_id == command.event_id)
            .order_by(EventDay.calendar_date)
        ).all()
        assert [(row.calendar_date, row.provenance, row.is_visible) for row in days] == [
            (date(2026, 7, 1), "range_generated", True),
            (date(2026, 7, 2), "range_generated", True),
            (date(2026, 7, 3), "range_generated", True),
        ]
        roles = connection.execute(
            select(
                EventMealRole.source_preset_id,
                EventMealRole.built_in_translation_key,
                EventMealRole.custom_name,
                EventMealRole.normalized_custom_name,
                EventMealRole.position_key,
                EventMealRole.created_by_user_id,
            )
            .where(EventMealRole.event_id == command.event_id)
            .order_by(EventMealRole.position_key)
        ).all()
        assert [
            (
                row.source_preset_id,
                row.built_in_translation_key,
                row.custom_name,
                row.normalized_custom_name,
                row.position_key,
                row.created_by_user_id,
            )
            for row in roles
        ] == [
            (
                result.meal_roles[0].source_preset_id,
                "meal_role.breakfast",
                None,
                None,
                "a",
                service_database.actor_id,
            ),
            (
                result.meal_roles[1].source_preset_id,
                "meal_role.lunch",
                None,
                None,
                "b",
                service_database.actor_id,
            ),
            (
                result.meal_roles[2].source_preset_id,
                None,
                "  Late supper  ",
                "late supper",
                "c",
                service_database.actor_id,
            ),
        ]
        mutation = connection.execute(
            select(
                Mutation.organization_id,
                Mutation.is_system_administration_scope,
                Mutation.actor_user_id,
                Mutation.actor_role,
                Mutation.client_installation_id,
                Mutation.command_kind,
                Mutation.outcome,
                Mutation.first_change_sequence,
                Mutation.last_change_sequence,
            ).where(Mutation.id == command.mutation_id)
        ).one()
        assert mutation == (
            service_database.organization_id,
            False,
            service_database.actor_id,
            "organization_admin",
            service_database.installation_id,
            "event.create",
            "accepted",
            1,
            7,
        )
        changes = connection.execute(
            select(
                OrganizationChange.sequence,
                OrganizationChange.mutation_id,
                OrganizationChange.entity_kind,
                OrganizationChange.entity_id,
                OrganizationChange.operation,
                OrganizationChange.payload,
            )
            .where(OrganizationChange.organization_id == service_database.organization_id)
            .order_by(OrganizationChange.sequence)
        ).all()
        assert [
            (
                change.sequence,
                change.mutation_id,
                change.entity_kind,
                change.entity_id,
                change.operation,
            )
            for change in changes
        ] == [
            (1, command.mutation_id, "event", command.event_id, "upsert"),
            *(
                (sequence, command.mutation_id, "event_day", day.id, "upsert")
                for sequence, day in enumerate(result.days, start=2)
            ),
            *(
                (sequence, command.mutation_id, "event_meal_role", role.id, "upsert")
                for sequence, role in enumerate(result.meal_roles, start=5)
            ),
        ]
        assert changes[0].payload == {
            "record_schema_version": 1,
            "record": {
                "id": str(command.event_id),
                "organization_id": str(service_database.organization_id),
                "name": "Výprava",
                "start_date": "2026-07-01",
                "end_date": "2026-07-03",
                "location": "Forest base",
                "general_note": "First line\nSecond line",
                "base_expected_attendance": 42,
                "budget_amount": "1250.5",
                "currency": "EUR",
                "created_at": event_created_at.isoformat(),
                "lifecycle": "active",
                "current_archive_snapshot_id": None,
                "archived_at": None,
                "archived_by_user_id": None,
                "created_by_user_id": str(service_database.actor_id),
                "field_clocks": {
                    "base_expected_attendance": {
                        "winning_client_wall_time": (
                            attendance_clock.winning_client_wall_time.isoformat()
                        ),
                        "winning_mutation_id": str(attendance_clock.winning_mutation_id),
                    }
                },
            },
        }
        day_record = changes[1].payload["record"]
        assert set(day_record) == {
            "id",
            "event_id",
            "calendar_date",
            "note",
            "is_visible",
            "provenance",
            "created_at",
            "created_by_user_id",
            "retired_at",
            "retired_by_user_id",
            "field_clocks",
        }
        assert day_record["id"] == str(result.days[0].id)
        day_created_at = connection.scalar(
            select(EventDay.created_at).where(EventDay.id == result.days[0].id)
        )
        assert day_created_at is not None
        assert day_record["created_at"] == day_created_at.isoformat()
        assert day_record["retired_by_user_id"] is None
        assert day_record["field_clocks"] == {"note": None, "is_visible": None}
        meal_role_record = changes[4].payload["record"]
        assert set(meal_role_record) == {
            "id",
            "event_id",
            "source_preset_id",
            "built_in_translation_key",
            "custom_name",
            "normalized_custom_name",
            "position_key",
            "created_at",
            "created_by_user_id",
            "retired_at",
            "retired_by_user_id",
            "field_clocks",
        }
        assert meal_role_record["id"] == str(result.meal_roles[0].id)
        meal_role_created_at = connection.scalar(
            select(EventMealRole.created_at).where(EventMealRole.id == result.meal_roles[0].id)
        )
        assert meal_role_created_at is not None
        assert meal_role_record["created_at"] == meal_role_created_at.isoformat()
        assert meal_role_record["retired_by_user_id"] is None
        assert meal_role_record["field_clocks"] == {"position_key": None}
    assert all(role.source_preset_id is not None for role in result.meal_roles)
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(OrganizationMealRolePreset)
            .where(OrganizationMealRolePreset.id == result.meal_roles[0].source_preset_id)
            .values(retired_at=datetime.now(UTC), retired_by_user_id=service_database.actor_id)
        )
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(EventMealRole.source_preset_id).where(
                    EventMealRole.id == result.meal_roles[0].id
                )
            )
            == result.meal_roles[0].source_preset_id
        )


def test_semantic_retry_is_replayed_and_changed_input_is_rejected(
    service_database: ServiceDatabase,
) -> None:
    command = event_command(service_database, name="Cafe\u0301", note="A\rB")
    first = asyncio.run(create_event(service_database.sessions, context(service_database), command))
    equivalent = event_command(
        service_database,
        mutation_id=command.mutation_id,
        event_id=command.event_id,
        name="Café",
        note="A\nB",
        budget=Decimal("1250.5000"),
        wall_time=command.client_wall_time,
    )
    replay = asyncio.run(
        create_event(service_database.sessions, context(service_database), equivalent)
    )
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.days == first.days
    changed = event_command(
        service_database,
        mutation_id=command.mutation_id,
        event_id=command.event_id,
        name="Changed",
        wall_time=command.client_wall_time,
    )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(create_event(service_database.sessions, context(service_database), changed))
    assert error.value.code == "idempotency_mismatch"
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Event)) == 1
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


def test_concurrent_same_mutation_creates_one_event(service_database: ServiceDatabase) -> None:
    command = event_command(service_database)

    async def run_concurrently() -> list[bool]:
        results = await asyncio.gather(
            *(
                create_event(service_database.sessions, context(service_database), command)
                for _ in range(6)
            )
        )
        return [result.replayed for result in results]

    replay_flags = asyncio.run(run_concurrently())
    assert replay_flags.count(False) == 1
    assert replay_flags.count(True) == 5
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Event)) == 1
        assert connection.scalar(select(func.count()).select_from(EventDay)) == 3
        assert connection.scalar(select(func.count()).select_from(EventMealRole)) == 3
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


@pytest.mark.parametrize(
    ("start_date", "end_date", "attendance", "budget", "wall_time", "expected_path"),
    [
        (date(2026, 7, 3), date(2026, 7, 1), 1, Decimal("1"), datetime.now(UTC), "end_date"),
        (
            date(2026, 7, 1),
            date(2026, 7, 1),
            -1,
            Decimal("1"),
            datetime.now(UTC),
            "base_expected_attendance",
        ),
        (date(2026, 7, 1), date(2026, 7, 1), 1, Decimal("NaN"), datetime.now(UTC), "budget_amount"),
        (date(2026, 7, 1), date(2026, 7, 1), 1, Decimal("1"), datetime.now(), "client_wall_time"),
    ],
)
def test_invalid_input_is_retained_rejection(
    service_database: ServiceDatabase,
    start_date: date,
    end_date: date,
    attendance: int,
    budget: Decimal,
    wall_time: datetime,
    expected_path: str,
) -> None:
    command = event_command(
        service_database,
        start_date=start_date,
        end_date=end_date,
        attendance=attendance,
        budget=budget,
        wall_time=wall_time,
    )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(create_event(service_database.sessions, context(service_database), command))
    assert error.value.code == "validation_failed"
    assert expected_path in {violation.path for violation in error.value.field_violations}
    with pytest.raises(ApplicationServiceError) as replay:
        asyncio.run(create_event(service_database.sessions, context(service_database), command))
    assert replay.value.code == "validation_failed"
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Event)) == 0
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


def test_date_range_is_bounded(service_database: ServiceDatabase) -> None:
    command = event_command(
        service_database,
        end_date=date(2026, 7, 1) + timedelta(days=366),
    )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(create_event(service_database.sessions, context(service_database), command))
    assert error.value.code == "validation_failed"
    assert {violation.path for violation in error.value.field_violations} == {"end_date"}


def test_malformed_command_is_a_retained_validation_rejection(
    service_database: ServiceDatabase,
) -> None:
    command = CreateEventCommand(
        mutation_id=cast(UUID, "not-a-uuid"),
        event_id=cast(UUID, "not-a-uuid"),
        organization_id=service_database.organization_id,
        name=cast(str, 42),
        start_date=cast(date, "not-a-date"),
        end_date=date(2026, 7, 1),
        base_expected_attendance=cast(int, True),
        budget_amount=cast(Decimal, "not-a-decimal"),
        client_wall_time=cast(datetime, "not-a-timestamp"),
        location=cast(str | None, 42),
        general_note=cast(str | None, 42),
    )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(create_event(service_database.sessions, context(service_database), command))
    assert error.value.code == "validation_failed"
    assert {violation.path for violation in error.value.field_violations} == {
        "base_expected_attendance",
        "budget_amount",
        "client_wall_time",
        "event_id",
        "general_note",
        "location",
        "mutation_id",
        "name",
        "start_date",
    }


def test_member_is_forbidden_but_system_admin_can_create(service_database: ServiceDatabase) -> None:
    member_id = uuid4()
    member_installation_id = uuid4()
    system_admin_id = uuid4()
    system_installation_id = uuid4()
    now = datetime.now(UTC)
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(User),
            [
                {
                    "id": member_id,
                    "display_name": "Member",
                    "verified_email": "member@example.test",
                    "normalized_email": "member@example.test",
                },
                {
                    "id": system_admin_id,
                    "display_name": "System administrator",
                    "verified_email": "system@example.test",
                    "normalized_email": "system@example.test",
                },
            ],
        )
        connection.execute(
            insert(ClientInstallation),
            [
                {
                    "id": member_installation_id,
                    "user_id": member_id,
                    "installation_kind": "browser",
                },
                {
                    "id": system_installation_id,
                    "user_id": system_admin_id,
                    "installation_kind": "browser",
                },
            ],
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=service_database.organization_id,
                user_id=member_id,
                invited_email="member@example.test",
                role="member",
                state="active",
                invited_by_user_id=service_database.actor_id,
                claimed_at=now,
            )
        )
        connection.execute(
            insert(SystemRoleAssignment).values(
                user_id=system_admin_id,
                invited_email="system@example.test",
                granted_by_user_id=service_database.actor_id,
                claimed_at=now,
            )
        )
    with pytest.raises(ApplicationServiceError) as member_error:
        asyncio.run(
            create_event(
                service_database.sessions,
                ExecutionContext(member_id, member_installation_id),
                event_command(service_database),
            )
        )
    assert member_error.value.code == "forbidden"
    command = event_command(service_database)
    result = asyncio.run(
        create_event(
            service_database.sessions,
            ExecutionContext(system_admin_id, system_installation_id),
            command,
        )
    )
    assert result.replayed is False
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(
            select(Mutation.actor_role).where(Mutation.id == command.mutation_id)
        ) == ("system_admin")
