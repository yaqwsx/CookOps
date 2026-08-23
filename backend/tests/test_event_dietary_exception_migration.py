import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect, select

from alembic import command
from cookops.persistence.models import DietaryTag, Event, Organization, User

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


@dataclass
class MigrationDatabase:
    configuration: Config
    engine: Engine


@pytest.fixture
def migration_database() -> Iterator[MigrationDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.downgrade(configuration, "base")
    engine = create_engine(database_url)
    try:
        yield MigrationDatabase(configuration, engine)
    finally:
        engine.dispose()
        command.downgrade(configuration, "base")


def _seed_previous_schema(engine: Engine) -> tuple[UUID, UUID, UUID, UUID]:
    user_id, organization_id, event_id, tag_id = (uuid4() for _ in range(4))
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=user_id,
                display_name="Dietary migration tester",
                verified_email="dietary-migration@example.test",
                normalized_email="dietary-migration@example.test",
            )
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id,
                name="Dietary migration organization",
                created_by_user_id=user_id,
            )
        )
        connection.execute(
            insert(Event).values(
                id=event_id,
                organization_id=organization_id,
                name="Dietary migration event",
                start_date=date(2026, 8, 18),
                end_date=date(2026, 8, 18),
                base_expected_attendance=1,
                budget_amount=Decimal("0"),
                currency="CZK",
                created_by_user_id=user_id,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=tag_id,
                organization_id=organization_id,
                name="Vegan",
                normalized_name="vegan",
                color="#00AA00",
                created_by_user_id=user_id,
            )
        )
    return user_id, organization_id, event_id, tag_id


def test_0018_upgrades_from_0017_without_losing_existing_rows(
    migration_database: MigrationDatabase,
) -> None:
    configuration, engine = migration_database.configuration, migration_database.engine
    command.upgrade(configuration, "0017_media_source_identity")
    ids = _seed_previous_schema(engine)

    command.upgrade(configuration, "head")

    inspector = inspect(engine)
    assert {
        "event_dietary_exceptions",
        "event_dietary_exception_tags",
    } <= set(inspector.get_table_names())
    assert {
        check["name"] for check in inspector.get_check_constraints("event_dietary_exceptions")
    } >= {
        "ck_event_dietary_exceptions_name",
        "ck_event_dietary_exceptions_note",
        "ck_event_dietary_exceptions_retirement",
    }
    assert {
        check["name"] for check in inspector.get_check_constraints("event_dietary_exception_tags")
    } >= {"ck_event_dietary_exception_tags_retirement"}
    exception_indexes = {
        index["name"]: index for index in inspector.get_indexes("event_dietary_exceptions")
    }
    assert "ix_event_dietary_exceptions_event_active" in exception_indexes
    assert exception_indexes["ix_event_dietary_exceptions_event_active"]["column_names"] == [
        "event_id",
        "retired_at",
    ]
    assert exception_indexes["ix_event_dietary_exceptions_event_active"]["unique"] is False

    tag_indexes = {
        index["name"]: index for index in inspector.get_indexes("event_dietary_exception_tags")
    }
    active_pair = tag_indexes["uq_event_dietary_exception_tags_active_pair"]
    assert active_pair["column_names"] == ["exception_id", "dietary_tag_id"]
    assert active_pair["unique"] is True
    predicate = active_pair.get("dialect_options", {}).get("postgresql_where")
    assert predicate is not None
    assert str(predicate).strip().lower() == "retired_at is null"

    exception_uniques = {
        constraint["name"]: constraint
        for constraint in inspector.get_unique_constraints("event_dietary_exceptions")
    }
    assert exception_uniques["uq_event_dietary_exceptions_id_org"]["column_names"] == [
        "id",
        "organization_id",
    ]
    tag_uniques = {
        constraint["name"]: constraint
        for constraint in inspector.get_unique_constraints("event_dietary_exception_tags")
    }
    assert tag_uniques["uq_event_dietary_exception_tags_id_org"]["column_names"] == [
        "id",
        "organization_id",
    ]

    with engine.connect() as connection:
        assert connection.execute(select(User.id).where(User.id == ids[0])).scalar_one() == ids[0]
        assert (
            connection.execute(
                select(Organization.id).where(Organization.id == ids[1])
            ).scalar_one()
            == ids[1]
        )
        assert connection.execute(select(Event.id).where(Event.id == ids[2])).scalar_one() == ids[2]
        assert (
            connection.execute(select(DietaryTag.id).where(DietaryTag.id == ids[3])).scalar_one()
            == ids[3]
        )

    command.check(configuration)
    command.current(configuration, check_heads=True)
