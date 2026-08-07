import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from cookops.persistence.models import (
    ClientInstallation,
    Event,
    EventArchiveSnapshot,
    FieldClock,
    Mutation,
    Organization,
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


def _seed(engine: Engine) -> tuple[UUID, UUID, UUID, UUID]:
    user_id, organization_id, installation_id, event_id = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=user_id,
                display_name="Archive tester",
                verified_email="archive@example.test",
                normalized_email="archive@example.test",
            )
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id, name="Archive organization", created_by_user_id=user_id
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id, user_id=user_id, installation_kind="browser"
            )
        )
        connection.execute(
            insert(Event).values(
                id=event_id,
                organization_id=organization_id,
                name="Archive event",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 1),
                base_expected_attendance=5,
                budget_amount=Decimal("0"),
                currency="CZK",
                created_by_user_id=user_id,
            )
        )
    return user_id, organization_id, installation_id, event_id


def _rejected_mutation(
    user_id: UUID, organization_id: UUID, installation_id: UUID
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "organization_id": organization_id,
        "is_system_administration_scope": False,
        "actor_user_id": user_id,
        "actor_role": "member",
        "client_installation_id": installation_id,
        "client_wall_time": datetime.now(UTC),
        "command_schema_version": 1,
        "command_kind": "event.update_base_attendance",
        "target_identities": [{"entity_kind": "event", "entity_id": str(uuid4())}],
        "request_hash": b"x" * 32,
        "outcome": "rejected",
        "outcome_payload": {"error": {"code": "validation_failed", "field_violations": []}},
    }


def test_archive_and_field_clock_schema_parity_and_downgrade(
    migration_database: MigrationDatabase,
) -> None:
    configuration, engine = migration_database.configuration, migration_database.engine
    command.upgrade(configuration, "head")
    assert {"event_archive_snapshots", "field_clocks"} <= set(inspect(engine).get_table_names())
    command.check(configuration)
    command.downgrade(configuration, "0011_scheduled_recipes")
    assert {"event_archive_snapshots", "field_clocks"}.isdisjoint(inspect(engine).get_table_names())


def test_archives_require_matching_immutable_snapshot_and_attribution(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    user_id, organization_id, _, event_id = _seed(migration_database.engine)
    other_event_id = uuid4()
    snapshot_id = uuid4()
    with migration_database.engine.begin() as connection:
        connection.execute(
            insert(Event).values(
                id=other_event_id,
                organization_id=organization_id,
                name="Other event",
                start_date=date(2026, 7, 2),
                end_date=date(2026, 7, 2),
                base_expected_attendance=0,
                budget_amount=Decimal("0"),
                currency="CZK",
                created_by_user_id=user_id,
            )
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(Event).where(Event.id == event_id).values(lifecycle="archived")
            )
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=snapshot_id,
                event_id=event_id,
                archive_schema_version=1,
                payload={"event": {}},
                content_hash=b"h" * 32,
                attachment_manifest=[],
                created_by_user_id=user_id,
            )
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                update(Event)
                .where(Event.id == other_event_id)
                .values(
                    lifecycle="archived",
                    current_archive_snapshot_id=snapshot_id,
                    archived_at=datetime.now(UTC),
                    archived_by_user_id=user_id,
                )
            )
            connection.exec_driver_sql(
                "SET CONSTRAINTS fk_events_current_archive_snapshot IMMEDIATE"
            )
        connection.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=snapshot_id,
                archived_at=datetime.now(UTC),
                archived_by_user_id=user_id,
            )
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(EventArchiveSnapshot).values(
                    event_id=other_event_id,
                    previous_snapshot_id=snapshot_id,
                    archive_schema_version=1,
                    payload={"event": {}},
                    content_hash=b"o" * 32,
                    attachment_manifest=[],
                    created_by_user_id=user_id,
                )
            )
            connection.exec_driver_sql(
                "SET CONSTRAINTS fk_event_archive_snapshots_previous_snapshot IMMEDIATE"
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(EventArchiveSnapshot)
                .where(EventArchiveSnapshot.id == snapshot_id)
                .values(payload={"event": {"mutated": True}})
            )


def test_field_clock_requires_an_accepted_scoped_winning_mutation(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    user_id, organization_id, installation_id, event_id = _seed(migration_database.engine)
    mutation = _rejected_mutation(user_id, organization_id, installation_id)
    with migration_database.engine.begin() as connection:
        connection.execute(insert(Mutation).values(**mutation))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(FieldClock).values(
                    organization_id=organization_id,
                    entity_kind="Event bad",
                    entity_id=uuid4(),
                    field_name="base_expected_attendance",
                    winning_client_wall_time=datetime.now(UTC),
                    winning_mutation_id=mutation["id"],
                )
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                insert(FieldClock).values(
                    organization_id=organization_id,
                    entity_kind="event",
                    entity_id=uuid4(),
                    field_name="base_expected_attendance",
                    winning_client_wall_time=datetime.now(UTC),
                    winning_mutation_id=uuid4(),
                )
            )
            connection.exec_driver_sql("SET CONSTRAINTS fk_field_clocks_winning_mutation IMMEDIATE")
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                insert(FieldClock).values(
                    organization_id=organization_id,
                    entity_kind="event",
                    entity_id=event_id,
                    field_name="base_expected_attendance",
                    winning_client_wall_time=mutation["client_wall_time"],
                    winning_mutation_id=mutation["id"],
                )
            )
            connection.exec_driver_sql(
                "SET CONSTRAINTS tr_field_clock_verify_winning_mutation IMMEDIATE"
            )
