import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from cookops.persistence.models import (
    ClientInstallation,
    Event,
    EventArchiveSnapshot,
    EventIngredientPrice,
    EventIngredientPriceSnapshot,
    Ingredient,
    IngredientPriceEstimate,
    IngredientVersion,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationChangeTransaction,
    UnitDefinition,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

PRICE_TABLES = {"event_ingredient_prices", "event_ingredient_price_snapshots"}
SHOPPING_PRICE_COLUMNS = {
    "event_price_snapshot_id",
    "price_amount",
    "priced_quantity",
    "priced_unit_id",
    "currency",
}


@dataclass
class MigrationDatabase:
    configuration: Config
    engine: Engine


@dataclass(frozen=True)
class Seed:
    actor_id: UUID
    organization_id: UUID
    installation_id: UUID
    event_id: UUID
    other_event_id: UUID
    ingredient_id: UUID
    other_ingredient_id: UUID
    kilograms_id: UUID
    source_estimate_id: UUID


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


def _seed(engine: Engine) -> Seed:
    actor_id, organization_id, installation_id = uuid4(), uuid4(), uuid4()
    event_id, other_event_id = uuid4(), uuid4()
    ingredient_id, version_id = uuid4(), uuid4()
    other_ingredient_id, other_version_id, source_estimate_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Price tester",
                verified_email="price@example.test",
                normalized_email="price@example.test",
            )
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id, name="Price organization", created_by_user_id=actor_id
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id, user_id=actor_id, installation_kind="browser"
            )
        )
        connection.execute(
            insert(Event),
            [
                {
                    "id": event_id,
                    "organization_id": organization_id,
                    "name": "Price event",
                    "start_date": date(2026, 8, 1),
                    "end_date": date(2026, 8, 1),
                    "base_expected_attendance": 1,
                    "budget_amount": Decimal("0"),
                    "currency": "CZK",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_event_id,
                    "organization_id": organization_id,
                    "name": "Other price event",
                    "start_date": date(2026, 8, 2),
                    "end_date": date(2026, 8, 2),
                    "base_expected_attendance": 1,
                    "budget_amount": Decimal("0"),
                    "currency": "CZK",
                    "created_by_user_id": actor_id,
                },
            ],
        )
    with engine.connect() as connection:
        grams_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        kilograms_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "kg"
            )
        )
    assert grams_id is not None and kilograms_id is not None
    with engine.begin() as connection:
        for ingredient_id_value, ingredient_version_id, name in (
            (ingredient_id, version_id, "Tomatoes"),
            (other_ingredient_id, other_version_id, "Onions"),
        ):
            connection.execute(
                insert(Ingredient).values(
                    id=ingredient_id_value,
                    organization_id=organization_id,
                    current_version_id=ingredient_version_id,
                    created_by_user_id=actor_id,
                )
            )
            connection.execute(
                insert(IngredientVersion).values(
                    id=ingredient_version_id,
                    organization_id=organization_id,
                    ingredient_id=ingredient_id_value,
                    name=name,
                    normalized_name=name.lower(),
                    canonical_unit_id=grams_id,
                    mass_per_canonical_quantity=Decimal("1"),
                    published_by_user_id=actor_id,
                )
            )
        connection.execute(
            insert(IngredientPriceEstimate).values(
                id=source_estimate_id,
                organization_id=organization_id,
                ingredient_id=ingredient_id,
                state="available",
                price_amount=Decimal("35"),
                priced_quantity=Decimal("1"),
                priced_unit_id=kilograms_id,
                currency="CZK",
                published_by_user_id=actor_id,
            )
        )
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == ingredient_id)
            .values(current_price_estimate_id=source_estimate_id)
        )
    return Seed(
        actor_id,
        organization_id,
        installation_id,
        event_id,
        other_event_id,
        ingredient_id,
        other_ingredient_id,
        kilograms_id,
        source_estimate_id,
    )


def _accepted_mutation(
    engine: Engine, seed: Seed, event_id: UUID
) -> tuple[UUID, datetime, datetime]:
    mutation_id = uuid4()
    client_time = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(Mutation).values(
                id=mutation_id,
                organization_id=seed.organization_id,
                is_system_administration_scope=False,
                actor_user_id=seed.actor_id,
                actor_role="member",
                client_installation_id=seed.installation_id,
                client_wall_time=client_time,
                command_schema_version=1,
                command_kind="event.update_price_estimates",
                target_identities=[{"entity_kind": "event", "entity_id": str(event_id)}],
                request_hash=bytes(32),
                outcome="accepted",
                outcome_payload={"result": "ok"},
                first_change_sequence=1,
                last_change_sequence=1,
            )
        )
        connection.execute(
            insert(OrganizationChangeTransaction).values(
                organization_id=seed.organization_id,
                mutation_id=mutation_id,
                first_change_sequence=1,
                last_change_sequence=1,
            )
        )
        connection.execute(
            insert(OrganizationChange).values(
                organization_id=seed.organization_id,
                sequence=1,
                mutation_id=mutation_id,
                entity_id=event_id,
                entity_kind="event",
                operation="updated",
                payload={"record_schema_version": 1, "record": {}},
            )
        )
        server_time = connection.scalar(
            select(Mutation.server_received_at).where(Mutation.id == mutation_id)
        )
    assert server_time is not None
    return mutation_id, client_time, server_time


def _snapshot_values(
    seed: Seed, price_id: UUID, mutation_id: UUID, client_time: datetime, server_time: datetime
) -> dict[str, object]:
    return {
        "organization_id": seed.organization_id,
        "event_id": seed.event_id,
        "ingredient_id": seed.ingredient_id,
        "event_ingredient_price_id": price_id,
        "source_ingredient_price_estimate_id": seed.source_estimate_id,
        "state": "available",
        "price_amount": Decimal("35"),
        "priced_quantity": Decimal("1"),
        "priced_unit_id": seed.kilograms_id,
        "currency": "CZK",
        "captured_by_user_id": seed.actor_id,
        "effective_client_action_time": client_time,
        "server_received_at": server_time,
        "originating_mutation_id": mutation_id,
    }


def test_event_price_schema_parity_and_downgrade(migration_database: MigrationDatabase) -> None:
    configuration, engine = migration_database.configuration, migration_database.engine
    command.upgrade(configuration, "head")
    assert set(inspect(engine).get_table_names()) >= PRICE_TABLES
    shopping_columns = {
        column["name"] for column in inspect(engine).get_columns("shopping_contribution_snapshots")
    }
    checks = {
        check["name"]
        for check in inspect(engine).get_check_constraints("shopping_contribution_snapshots")
    }
    foreign_keys = {
        key["name"] for key in inspect(engine).get_foreign_keys("shopping_contribution_snapshots")
    }
    assert shopping_columns >= SHOPPING_PRICE_COLUMNS
    assert "ck_shopping_contribution_snapshots_price_shape" in checks
    assert "fk_shopping_contribution_snapshots_event_price" in foreign_keys
    command.check(configuration)
    command.downgrade(configuration, "0014_receipts_media")
    assert PRICE_TABLES.isdisjoint(inspect(engine).get_table_names())


def test_event_prices_are_scoped_retained_and_snapshots_are_immutable(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    seed = _seed(migration_database.engine)
    mutation_id, client_time, server_time = _accepted_mutation(
        migration_database.engine, seed, seed.event_id
    )
    price_id, other_price_id, snapshot_id, next_snapshot_id = uuid4(), uuid4(), uuid4(), uuid4()
    with migration_database.engine.begin() as connection:
        connection.execute(
            insert(EventIngredientPrice).values(
                id=price_id,
                organization_id=seed.organization_id,
                event_id=seed.event_id,
                ingredient_id=seed.ingredient_id,
                created_by_user_id=seed.actor_id,
            )
        )
        connection.execute(
            insert(EventIngredientPriceSnapshot).values(
                id=snapshot_id,
                **_snapshot_values(seed, price_id, mutation_id, client_time, server_time),
            )
        )
        connection.execute(
            update(EventIngredientPrice)
            .where(EventIngredientPrice.id == price_id)
            .values(current_snapshot_id=snapshot_id)
        )
        connection.execute(
            insert(EventIngredientPriceSnapshot).values(
                id=next_snapshot_id,
                **(
                    _snapshot_values(seed, price_id, mutation_id, client_time, server_time)
                    | {"previous_snapshot_id": snapshot_id}
                ),
            )
        )
        connection.execute(
            update(EventIngredientPrice)
            .where(EventIngredientPrice.id == price_id)
            .values(current_snapshot_id=next_snapshot_id)
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                insert(EventIngredientPriceSnapshot).values(
                    **(
                        _snapshot_values(seed, price_id, mutation_id, client_time, server_time)
                        | {"previous_snapshot_id": snapshot_id}
                    )
                )
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(EventIngredientPrice)
                .where(EventIngredientPrice.id == price_id)
                .values(current_snapshot_id=snapshot_id)
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(EventIngredientPrice).values(
                    organization_id=seed.organization_id,
                    event_id=seed.event_id,
                    ingredient_id=seed.ingredient_id,
                    created_by_user_id=seed.actor_id,
                )
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(EventIngredientPriceSnapshot)
                .where(EventIngredientPriceSnapshot.id == snapshot_id)
                .values(price_amount=Decimal("36"))
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(EventIngredientPrice)
                .where(EventIngredientPrice.id == price_id)
                .values(ingredient_id=seed.other_ingredient_id)
            )
        connection.execute(
            insert(EventIngredientPrice).values(
                id=other_price_id,
                organization_id=seed.organization_id,
                event_id=seed.other_event_id,
                ingredient_id=seed.ingredient_id,
                created_by_user_id=seed.actor_id,
            )
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(EventIngredientPrice)
                .where(EventIngredientPrice.id == other_price_id)
                .values(current_snapshot_id=snapshot_id)
            )
            connection.exec_driver_sql(
                "SET CONSTRAINTS fk_event_ingredient_prices_current_snapshot IMMEDIATE"
            )
        archive_snapshot_id = uuid4()
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=archive_snapshot_id,
                event_id=seed.event_id,
                archive_schema_version=1,
                payload={"event": {}},
                content_hash=bytes(32),
                attachment_manifest=[],
                created_by_user_id=seed.actor_id,
            )
        )
        connection.execute(
            update(Event)
            .where(Event.id == seed.event_id)
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=archive_snapshot_id,
                archived_at=datetime.now(UTC),
                archived_by_user_id=seed.actor_id,
            )
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                insert(EventIngredientPrice).values(
                    organization_id=seed.organization_id,
                    event_id=seed.event_id,
                    ingredient_id=seed.other_ingredient_id,
                    created_by_user_id=seed.actor_id,
                )
            )


def test_snapshots_copy_a_compatible_source_or_remain_explicitly_unavailable(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    seed = _seed(migration_database.engine)
    mutation_id, client_time, server_time = _accepted_mutation(
        migration_database.engine, seed, seed.event_id
    )
    price_id = uuid4()
    with migration_database.engine.begin() as connection:
        connection.execute(
            insert(EventIngredientPrice).values(
                id=price_id,
                organization_id=seed.organization_id,
                event_id=seed.event_id,
                ingredient_id=seed.ingredient_id,
                created_by_user_id=seed.actor_id,
            )
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                insert(EventIngredientPriceSnapshot).values(
                    **(
                        _snapshot_values(seed, price_id, mutation_id, client_time, server_time)
                        | {"price_amount": Decimal("99")}
                    )
                )
            )
            connection.exec_driver_sql(
                "SET CONSTRAINTS tr_event_ingredient_price_snapshot_verify_capture IMMEDIATE"
            )
        connection.execute(update(Organization).values(default_currency="EUR"))
        foreign_source_id = uuid4()
        connection.execute(
            insert(IngredientPriceEstimate).values(
                id=foreign_source_id,
                organization_id=seed.organization_id,
                ingredient_id=seed.ingredient_id,
                based_on_estimate_id=seed.source_estimate_id,
                state="available",
                price_amount=Decimal("2"),
                priced_quantity=Decimal("1"),
                priced_unit_id=seed.kilograms_id,
                currency="EUR",
                published_by_user_id=seed.actor_id,
            )
        )
        connection.execute(
            insert(EventIngredientPriceSnapshot).values(
                **(
                    _snapshot_values(seed, price_id, mutation_id, client_time, server_time)
                    | {
                        "source_ingredient_price_estimate_id": foreign_source_id,
                        "state": "unavailable",
                        "price_amount": None,
                        "priced_quantity": None,
                        "priced_unit_id": None,
                        "currency": None,
                    }
                )
            )
        )
