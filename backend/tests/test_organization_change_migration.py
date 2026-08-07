import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event as ThreadEvent
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, insert, inspect, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from cookops.persistence.models import (
    ClientInstallation,
    Event,
    EventMealRole,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationChangeHead,
    OrganizationChangeTransaction,
    OrganizationMealRolePreset,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


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


def seed_identity(engine: Engine) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    actor_id = uuid4()
    installation_id = uuid4()
    organization_id = uuid4()
    other_organization_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Change-feed actor",
                verified_email="change-feed@example.test",
                normalized_email="change-feed@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id,
                user_id=actor_id,
                installation_kind="browser",
            )
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": organization_id,
                    "name": "Change-feed organization",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_organization_id,
                    "name": "Other change-feed organization",
                    "created_by_user_id": actor_id,
                },
            ],
        )
    return actor_id, installation_id, organization_id, other_organization_id, uuid4()


def mutation_values(
    *,
    mutation_id: UUID,
    organization_id: UUID,
    actor_id: UUID,
    installation_id: UUID,
    first_sequence: int,
    last_sequence: int,
) -> dict[str, object]:
    return {
        "id": mutation_id,
        "organization_id": organization_id,
        "is_system_administration_scope": False,
        "actor_user_id": actor_id,
        "actor_role": "member",
        "client_installation_id": installation_id,
        "client_wall_time": datetime.now(UTC),
        "command_schema_version": 1,
        "command_kind": "event.create",
        "target_identities": [{"entity_kind": "event", "entity_id": str(uuid4())}],
        "request_hash": b"c" * 32,
        "outcome": "accepted",
        "outcome_payload": {"result": "accepted"},
        "first_change_sequence": first_sequence,
        "last_change_sequence": last_sequence,
    }


def canonical_payload(**record: object) -> dict[str, object]:
    return {"record_schema_version": 1, "record": record}


def change_values(
    *,
    organization_id: UUID,
    sequence: int,
    mutation_id: UUID,
    entity_id: UUID | None = None,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "organization_id": organization_id,
        "sequence": sequence,
        "mutation_id": mutation_id,
        "entity_id": entity_id or uuid4(),
        "entity_kind": "event",
        "operation": "upsert",
        "payload": canonical_payload(id="canonical-event") if payload is None else payload,
    }


def reserve_range(
    connection: Connection, organization_id: UUID, mutation_id: UUID, count: int
) -> tuple[int, int]:
    result = connection.execute(
        text(
            "SELECT first_change_sequence, last_change_sequence "
            "FROM reserve_organization_change_transaction(:organization_id, :mutation_id, :count)"
        ),
        {"organization_id": organization_id, "mutation_id": mutation_id, "count": count},
    ).one()
    return result.first_change_sequence, result.last_change_sequence


def publish_change_transaction(
    connection: Connection,
    *,
    organization_id: UUID,
    mutation_id: UUID,
    actor_id: UUID,
    installation_id: UUID,
    count: int,
) -> tuple[int, int]:
    first_sequence, last_sequence = reserve_range(connection, organization_id, mutation_id, count)
    connection.execute(
        insert(Mutation).values(
            **mutation_values(
                mutation_id=mutation_id,
                organization_id=organization_id,
                actor_id=actor_id,
                installation_id=installation_id,
                first_sequence=first_sequence,
                last_sequence=last_sequence,
            )
        )
    )
    connection.execute(
        insert(OrganizationChange),
        [
            change_values(
                organization_id=organization_id,
                sequence=sequence,
                mutation_id=mutation_id,
            )
            for sequence in range(first_sequence, last_sequence + 1)
        ],
    )
    return first_sequence, last_sequence


def test_change_feed_migration_matches_orm_and_round_trips_from_0008(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine
    command.upgrade(configuration, "0008_event_role_provenance")
    actor_id, _, organization_id, _, _ = seed_identity(engine)
    with engine.begin() as connection:
        event_id = connection.scalar(
            insert(Event)
            .values(
                organization_id=organization_id,
                name="Existing event",
                start_date=datetime(2026, 7, 1, tzinfo=UTC).date(),
                end_date=datetime(2026, 7, 1, tzinfo=UTC).date(),
                base_expected_attendance=1,
                budget_amount=0,
                currency="CZK",
                created_by_user_id=actor_id,
            )
            .returning(Event.id)
        )
        assert event_id is not None

    command.downgrade(configuration, "0007_event_lifecycle")
    assert "source_preset_id" not in {
        column["name"] for column in inspect(engine).get_columns("event_meal_roles")
    }
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT to_regprocedure("
                    "'event_meal_role_validate_source_preset_organization()')"
                )
            )
            is None
        )

    command.upgrade(configuration, "head")
    assert {
        "organization_changes",
        "organization_change_heads",
        "organization_change_transactions",
    } <= set(inspect(engine).get_table_names())
    command.check(configuration)

    command.downgrade(configuration, "0008_event_role_provenance")
    assert {
        "organization_changes",
        "organization_change_heads",
        "organization_change_transactions",
    }.isdisjoint(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(select(Event.id).where(Event.id == event_id)) == event_id


def test_change_feed_allocates_contiguous_organization_local_ranges_atomically(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, installation_id, organization_id, _, first_mutation_id = seed_identity(engine)
    second_mutation_id = uuid4()
    with engine.begin() as connection:
        assert publish_change_transaction(
            connection,
            organization_id=organization_id,
            mutation_id=first_mutation_id,
            actor_id=actor_id,
            installation_id=installation_id,
            count=2,
        ) == (1, 2)
        assert publish_change_transaction(
            connection,
            organization_id=organization_id,
            mutation_id=second_mutation_id,
            actor_id=actor_id,
            installation_id=installation_id,
            count=1,
        ) == (3, 3)

    with engine.connect() as connection:
        records = list(
            connection.execute(
                select(OrganizationChange.sequence, OrganizationChange.mutation_id)
                .where(OrganizationChange.organization_id == organization_id)
                .order_by(OrganizationChange.sequence)
            ).tuples()
        )
        head = connection.scalar(
            select(OrganizationChangeHead.next_sequence).where(
                OrganizationChangeHead.organization_id == organization_id
            )
        )
    assert records == [(1, first_mutation_id), (2, first_mutation_id), (3, second_mutation_id)]
    assert head == 4


def test_concurrent_change_feed_reservations_are_serialized_without_gaps(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, installation_id, organization_id, _, first_mutation_id = seed_identity(engine)
    second_mutation_id = uuid4()
    first_reserved = ThreadEvent()
    release_first = ThreadEvent()
    second_reserved = ThreadEvent()

    def publish_first() -> tuple[int, int]:
        with engine.begin() as connection:
            result = publish_change_transaction(
                connection,
                organization_id=organization_id,
                mutation_id=first_mutation_id,
                actor_id=actor_id,
                installation_id=installation_id,
                count=2,
            )
            first_reserved.set()
            assert release_first.wait(timeout=5)
            return result

    def publish_second() -> tuple[int, int]:
        assert first_reserved.wait(timeout=5)
        with engine.begin() as connection:
            result = publish_change_transaction(
                connection,
                organization_id=organization_id,
                mutation_id=second_mutation_id,
                actor_id=actor_id,
                installation_id=installation_id,
                count=1,
            )
            second_reserved.set()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(publish_first)
        second = executor.submit(publish_second)
        assert first_reserved.wait(timeout=5)
        assert not second_reserved.wait(timeout=0.2)
        release_first.set()
        assert first.result(timeout=5) == (1, 2)
        assert second.result(timeout=5) == (3, 3)


def test_change_feed_enforces_range_completeness_scope_envelope_and_append_only(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, installation_id, organization_id, other_organization_id, mutation_id = seed_identity(
        engine
    )
    with engine.begin() as connection:
        publish_change_transaction(
            connection,
            organization_id=organization_id,
            mutation_id=mutation_id,
            actor_id=actor_id,
            installation_id=installation_id,
            count=1,
        )
        valid_change = change_values(
            organization_id=organization_id,
            sequence=1,
            mutation_id=mutation_id,
        )
        invalid_values = [
            {**valid_change, "sequence": 0},
            {**valid_change, "sequence": 2},
            {**valid_change, "entity_kind": "Event"},
            {**valid_change, "operation": "not an operation"},
            {**valid_change, "payload": {}},
            {**valid_change, "payload": {"record_schema_version": "1", "record": {}}},
            {**valid_change, "payload": {"record_schema_version": 1}},
            {**valid_change, "payload": {"record_schema_version": 1, "record": []}},
            {
                **valid_change,
                "payload": {"record_schema_version": 1, "record": {}, "extra": True},
            },
            {
                **valid_change,
                "payload": canonical_payload(note="data:image/png;base64,ZmFrZQ=="),
            },
            {**valid_change, "payload": canonical_payload(note="x" * 262144)},
            {**valid_change, "mutation_id": uuid4()},
            {**valid_change, "organization_id": other_organization_id},
            {**valid_change, "entity_id": None},
        ]
        for values in invalid_values:
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(insert(OrganizationChange).values(**values))

        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(OrganizationChange)
                .where(OrganizationChange.organization_id == organization_id)
                .values(operation="retire")
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                delete(OrganizationChange).where(
                    OrganizationChange.organization_id == organization_id
                )
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(text("TRUNCATE organization_changes"))
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(OrganizationChangeTransaction)
                .where(OrganizationChangeTransaction.organization_id == organization_id)
                .values(last_change_sequence=2)
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(OrganizationChangeHead)
                .where(OrganizationChangeHead.organization_id == organization_id)
                .values(next_sequence=100)
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(text("TRUNCATE organization_change_heads"))
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(Mutation)
                .where(Mutation.id == mutation_id)
                .values(first_change_sequence=2, last_change_sequence=2)
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(delete(Mutation).where(Mutation.id == mutation_id))

    incomplete_mutation_id = uuid4()
    with pytest.raises(DBAPIError), engine.begin() as connection:
        first_sequence, last_sequence = reserve_range(
            connection, organization_id, incomplete_mutation_id, 2
        )
        connection.execute(
            insert(Mutation).values(
                **mutation_values(
                    mutation_id=incomplete_mutation_id,
                    organization_id=organization_id,
                    actor_id=actor_id,
                    installation_id=installation_id,
                    first_sequence=first_sequence,
                    last_sequence=last_sequence,
                )
            )
        )
        connection.execute(
            insert(OrganizationChange).values(
                **change_values(
                    organization_id=organization_id,
                    sequence=first_sequence,
                    mutation_id=incomplete_mutation_id,
                )
            )
        )

    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(
            insert(Mutation).values(
                **mutation_values(
                    mutation_id=uuid4(),
                    organization_id=organization_id,
                    actor_id=actor_id,
                    installation_id=installation_id,
                    first_sequence=100,
                    last_sequence=100,
                )
            )
        )

    rejected_mutation_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(Mutation).values(
                **(
                    mutation_values(
                        mutation_id=rejected_mutation_id,
                        organization_id=organization_id,
                        actor_id=actor_id,
                        installation_id=installation_id,
                        first_sequence=1,
                        last_sequence=1,
                    )
                    | {
                        "outcome": "rejected",
                        "first_change_sequence": None,
                        "last_change_sequence": None,
                    }
                )
            )
        )
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(
            update(Mutation)
            .where(Mutation.id == rejected_mutation_id)
            .values(
                outcome="accepted",
                first_change_sequence=101,
                last_change_sequence=101,
            )
        )

    with engine.begin() as connection:
        system_mutation = mutation_values(
            mutation_id=uuid4(),
            organization_id=organization_id,
            actor_id=actor_id,
            installation_id=installation_id,
            first_sequence=1,
            last_sequence=1,
        ) | {
            "organization_id": None,
            "is_system_administration_scope": True,
            "actor_role": "system_admin",
            "command_kind": "organization.create",
            "first_change_sequence": None,
            "last_change_sequence": None,
        }
        connection.execute(insert(Mutation).values(**system_mutation))


def test_change_feed_rollback_releases_its_reserved_range(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, installation_id, organization_id, _, mutation_id = seed_identity(engine)
    with engine.connect() as connection:
        transaction = connection.begin()
        assert reserve_range(connection, organization_id, mutation_id, 1) == (1, 1)
        transaction.rollback()

    with engine.begin() as connection:
        published_mutation_id = uuid4()
        assert publish_change_transaction(
            connection,
            organization_id=organization_id,
            mutation_id=published_mutation_id,
            actor_id=actor_id,
            installation_id=installation_id,
            count=1,
        ) == (1, 1)


def test_event_meal_role_source_preset_must_belong_to_its_event_organization(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, _, organization_id, other_organization_id, _ = seed_identity(engine)
    with engine.begin() as connection:
        event_id = connection.scalar(
            insert(Event)
            .values(
                organization_id=organization_id,
                name="Primary event",
                start_date=datetime(2026, 7, 1, tzinfo=UTC).date(),
                end_date=datetime(2026, 7, 1, tzinfo=UTC).date(),
                base_expected_attendance=1,
                budget_amount=0,
                currency="CZK",
                created_by_user_id=actor_id,
            )
            .returning(Event.id)
        )
        local_preset_id = connection.scalar(
            insert(OrganizationMealRolePreset)
            .values(
                organization_id=organization_id,
                built_in_translation_key="meal_role.breakfast",
                position_key="a",
                created_by_user_id=actor_id,
            )
            .returning(OrganizationMealRolePreset.id)
        )
        foreign_preset_id = connection.scalar(
            insert(OrganizationMealRolePreset)
            .values(
                organization_id=other_organization_id,
                built_in_translation_key="meal_role.lunch",
                position_key="a",
                created_by_user_id=actor_id,
            )
            .returning(OrganizationMealRolePreset.id)
        )
        assert event_id is not None
        assert local_preset_id is not None
        assert foreign_preset_id is not None
        foreign_event_id = connection.scalar(
            insert(Event)
            .values(
                organization_id=other_organization_id,
                name="Other event",
                start_date=datetime(2026, 7, 1, tzinfo=UTC).date(),
                end_date=datetime(2026, 7, 1, tzinfo=UTC).date(),
                base_expected_attendance=1,
                budget_amount=0,
                currency="CZK",
                created_by_user_id=actor_id,
            )
            .returning(Event.id)
        )
        assert foreign_event_id is not None
        role_id = connection.scalar(
            insert(EventMealRole)
            .values(
                event_id=event_id,
                source_preset_id=local_preset_id,
                built_in_translation_key="meal_role.breakfast",
                position_key="a",
                created_by_user_id=actor_id,
            )
            .returning(EventMealRole.id)
        )
        assert role_id is not None
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(EventMealRole)
                .where(EventMealRole.id == role_id)
                .values(source_preset_id=foreign_preset_id)
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(EventMealRole)
                .where(EventMealRole.id == role_id)
                .values(event_id=foreign_event_id)
            )
