import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from alembic import command
from cookops.persistence.models import Event, EventDay, EventMealRole, Organization, User

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

EVENT_TABLES = {"events", "event_days", "event_meal_roles"}


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


def seed_organizations(engine: Engine) -> tuple[UUID, UUID, UUID]:
    actor_id = uuid4()
    organization_id = uuid4()
    other_organization_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Event planner",
                verified_email="event-planner@example.test",
                normalized_email="event-planner@example.test",
            )
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": organization_id,
                    "name": "Primary organization",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_organization_id,
                    "name": "Other organization",
                    "created_by_user_id": actor_id,
                },
            ],
        )
    return actor_id, organization_id, other_organization_id


def event_values(actor_id: UUID, organization_id: UUID) -> dict[str, object]:
    return {
        "organization_id": organization_id,
        "name": "Summer camp",
        "start_date": date(2026, 7, 1),
        "end_date": date(2026, 7, 5),
        "base_expected_attendance": 42,
        "budget_amount": Decimal("12000.50"),
        "currency": "CZK",
        "created_by_user_id": actor_id,
    }


def test_event_lifecycle_migration_parity_and_downgrade(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine
    command.upgrade(configuration, "head")

    assert set(inspect(engine).get_table_names()) >= EVENT_TABLES
    command.check(configuration)

    actor_id, organization_id, _ = seed_organizations(engine)
    with engine.begin() as connection:
        event_id = connection.scalar(
            insert(Event).values(**event_values(actor_id, organization_id)).returning(Event.id)
        )
        assert event_id is not None
        connection.execute(
            insert(EventDay).values(
                event_id=event_id,
                calendar_date=date(2026, 7, 1),
                note="Arrival day",
                is_visible=True,
                provenance="range_generated",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(EventMealRole).values(
                event_id=event_id,
                built_in_translation_key="meal_role.breakfast",
                position_key="a",
                created_by_user_id=actor_id,
            )
        )

    command.downgrade(configuration, "0006_ingredient_catalog")
    assert EVENT_TABLES.isdisjoint(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(select(Organization.id).where(Organization.id == organization_id))


def test_event_constraints_and_lifecycle_attribution(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, _ = seed_organizations(engine)
    valid = event_values(actor_id, organization_id)
    with engine.begin() as connection:
        active_event_id = connection.scalar(insert(Event).values(**valid).returning(Event.id))
        assert active_event_id is not None

        invalid_statements = [
            insert(Event).values(valid | {"name": " "}),
            insert(Event).values(
                valid
                | {
                    "name": "Reverse dates",
                    "start_date": date(2026, 7, 2),
                    "end_date": date(2026, 7, 1),
                }
            ),
            insert(Event).values(
                valid | {"name": "Negative attendance", "base_expected_attendance": -1}
            ),
            insert(Event).values(
                valid | {"name": "Negative budget", "budget_amount": Decimal("-0.01")}
            ),
            insert(Event).values(valid | {"name": "Bad currency", "currency": "czk"}),
            insert(Event).values(
                valid | {"name": "Orphan organization", "organization_id": uuid4()}
            ),
        ]
        for statement in invalid_statements:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(statement)

        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(delete(Organization).where(Organization.id == organization_id))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(delete(User).where(User.id == actor_id))


def test_event_days_are_unique_visible_or_hidden_and_require_valid_provenance(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, _ = seed_organizations(engine)
    with engine.begin() as connection:
        event_id = connection.scalar(
            insert(Event).values(**event_values(actor_id, organization_id)).returning(Event.id)
        )
        assert event_id is not None
        generated_day_id = connection.scalar(
            insert(EventDay)
            .values(
                event_id=event_id,
                calendar_date=date(2026, 7, 1),
                note="",
                is_visible=False,
                provenance="range_generated",
                created_by_user_id=actor_id,
            )
            .returning(EventDay.id)
        )
        assert generated_day_id is not None
        manual_day_id = connection.scalar(
            insert(EventDay)
            .values(
                event_id=event_id,
                calendar_date=date(2026, 7, 6),
                provenance="manually_added",
                created_by_user_id=actor_id,
            )
            .returning(EventDay.id)
        )
        assert manual_day_id is not None

        retired_day_id = connection.scalar(
            insert(EventDay)
            .values(
                event_id=event_id,
                calendar_date=date(2026, 7, 1),
                provenance="range_generated",
                created_by_user_id=actor_id,
                retired_at=datetime.now(UTC),
                retired_by_user_id=actor_id,
            )
            .returning(EventDay.id)
        )
        assert retired_day_id is not None

        invalid_statements = [
            insert(EventDay).values(
                event_id=event_id,
                calendar_date=date(2026, 7, 1),
                provenance="range_generated",
                created_by_user_id=actor_id,
            ),
            insert(EventDay).values(
                event_id=event_id,
                calendar_date=date(2026, 7, 7),
                provenance="imported",
                created_by_user_id=actor_id,
            ),
            insert(EventDay).values(
                event_id=uuid4(),
                calendar_date=date(2026, 7, 7),
                provenance="manually_added",
                created_by_user_id=actor_id,
            ),
            insert(EventDay).values(
                event_id=event_id,
                calendar_date=date(2026, 7, 7),
                provenance="manually_added",
                created_by_user_id=actor_id,
                retired_at=datetime.now(UTC),
            ),
        ]
        for statement in invalid_statements:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(statement)


def test_event_owned_meal_roles_validate_identity_order_and_retirement(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, _ = seed_organizations(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        event_id = connection.scalar(
            insert(Event).values(**event_values(actor_id, organization_id)).returning(Event.id)
        )
        assert event_id is not None
        connection.execute(
            insert(EventMealRole).values(
                event_id=event_id,
                built_in_translation_key="meal_role.breakfast",
                position_key="a",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(EventMealRole).values(
                event_id=event_id,
                custom_name="Late supper",
                normalized_custom_name="late supper",
                position_key="b",
                created_by_user_id=actor_id,
                retired_at=now,
                retired_by_user_id=actor_id,
            )
        )

        invalid_statements = [
            insert(EventMealRole).values(
                event_id=event_id,
                built_in_translation_key="meal_role.breakfast",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(EventMealRole).values(
                event_id=event_id,
                custom_name="Late supper",
                normalized_custom_name="late supper",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(EventMealRole).values(
                event_id=event_id,
                built_in_translation_key="Meal Role",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(EventMealRole).values(
                event_id=event_id,
                custom_name=" ",
                normalized_custom_name="",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(EventMealRole).values(
                event_id=event_id,
                custom_name="Lunch",
                normalized_custom_name="LUNCH",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(EventMealRole).values(
                event_id=event_id,
                built_in_translation_key="meal_role.lunch",
                position_key="č",
                created_by_user_id=actor_id,
            ),
            insert(EventMealRole).values(
                event_id=event_id,
                built_in_translation_key="meal_role.lunch",
                position_key="c",
                created_by_user_id=actor_id,
                retired_at=now,
            ),
            insert(EventMealRole).values(
                event_id=uuid4(),
                built_in_translation_key="meal_role.lunch",
                position_key="c",
                created_by_user_id=actor_id,
            ),
        ]
        for statement in invalid_statements:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(statement)
