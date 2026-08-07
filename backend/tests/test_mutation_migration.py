import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError

from alembic import command
from cookops.persistence.models import ClientInstallation, Mutation, Organization, User

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

MUTATION_TABLES = {"client_installations", "mutations"}


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


def seed_identity(engine: Engine) -> tuple[UUID, UUID, UUID, UUID]:
    actor_id = uuid4()
    other_user_id = uuid4()
    organization_id = uuid4()
    installation_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User),
            [
                {
                    "id": actor_id,
                    "display_name": "Mutation actor",
                    "verified_email": "actor@example.test",
                    "normalized_email": "actor@example.test",
                },
                {
                    "id": other_user_id,
                    "display_name": "Other user",
                    "verified_email": "other@example.test",
                    "normalized_email": "other@example.test",
                },
            ],
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id,
                name="Mutation organization",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id,
                user_id=actor_id,
                installation_kind="browser",
            )
        )
    return actor_id, other_user_id, organization_id, installation_id


def valid_mutation(
    *,
    actor_id: UUID,
    organization_id: UUID,
    installation_id: UUID,
    mutation_id: UUID | None = None,
) -> dict[str, object]:
    return {
        "id": mutation_id or uuid4(),
        "organization_id": organization_id,
        "is_system_administration_scope": False,
        "actor_user_id": actor_id,
        "actor_role": "member",
        "client_installation_id": installation_id,
        "client_wall_time": datetime.now(UTC),
        "command_schema_version": 1,
        "command_kind": "event.rename",
        "target_identities": [{"entity_kind": "event", "entity_id": str(uuid4())}],
        "request_hash": b"r" * 32,
        "outcome": "rejected",
        "outcome_payload": {"affected_ids": [str(uuid4())]},
        "first_change_sequence": None,
        "last_change_sequence": None,
    }


def test_migration_upgrades_empty_database_matches_metadata_and_downgrades(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine

    command.upgrade(configuration, "head")
    assert set(inspect(engine).get_table_names()) >= MUTATION_TABLES
    command.check(configuration)

    command.downgrade(configuration, "0003_organization_configuration")
    assert MUTATION_TABLES.isdisjoint(inspect(engine).get_table_names())


def test_migration_upgrades_previous_schema_and_preserves_it_on_downgrade(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine
    command.upgrade(configuration, "0003_organization_configuration")
    actor_id = uuid4()
    organization_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Existing user",
                verified_email="existing@example.test",
                normalized_email="existing@example.test",
            )
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id,
                name="Existing organization",
                created_by_user_id=actor_id,
            )
        )

    command.upgrade(configuration, "head")
    assert set(inspect(engine).get_table_names()) >= MUTATION_TABLES
    command.downgrade(configuration, "0003_organization_configuration")

    assert MUTATION_TABLES.isdisjoint(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(select(Organization.id).where(Organization.id == organization_id))


def test_installation_and_mutation_constraints_and_idempotency_identity(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, other_user_id, organization_id, installation_id = seed_identity(engine)
    now = datetime.now(UTC)
    mutation_id = uuid4()

    with engine.begin() as connection:
        connection.execute(
            insert(ClientInstallation).values(
                user_id=actor_id,
                installation_kind="agent",
            )
        )
        invalid_installations = [
            insert(ClientInstallation).values(
                user_id=actor_id,
                installation_kind="desktop",
            ),
            insert(ClientInstallation).values(
                user_id=actor_id,
                installation_kind="browser",
                disabled_at=now,
            ),
            insert(ClientInstallation).values(
                user_id=actor_id,
                installation_kind="browser",
                created_at=now,
                disabled_at=now - timedelta(seconds=1),
                disabled_by_user_id=actor_id,
            ),
            insert(ClientInstallation).values(
                user_id=uuid4(),
                installation_kind="browser",
            ),
        ]
        for statement in invalid_installations:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(statement)

        mutation = valid_mutation(
            actor_id=actor_id,
            organization_id=organization_id,
            installation_id=installation_id,
            mutation_id=mutation_id,
        )
        connection.execute(insert(Mutation).values(**mutation))
        stored_hash = connection.scalar(
            select(Mutation.request_hash).where(Mutation.id == mutation_id)
        )
        assert stored_hash == mutation["request_hash"]

        system_mutation = valid_mutation(
            actor_id=actor_id,
            organization_id=organization_id,
            installation_id=installation_id,
        )
        system_mutation.update(
            organization_id=None,
            is_system_administration_scope=True,
            actor_role="system_admin",
            oauth_client_id="mcp-client",
            oauth_grant_id="mcp-grant",
            command_kind="organization.create",
            first_change_sequence=None,
            last_change_sequence=None,
        )
        connection.execute(insert(Mutation).values(**system_mutation))

        future_rejection = valid_mutation(
            actor_id=actor_id,
            organization_id=organization_id,
            installation_id=installation_id,
        )
        future_rejection.update(
            client_wall_time=now + timedelta(hours=25),
            outcome="rejected",
            outcome_payload={"error_code": "client_time_too_far_ahead"},
            first_change_sequence=None,
            last_change_sequence=None,
        )
        future_rejection_id = future_rejection["id"]
        connection.execute(insert(Mutation).values(**future_rejection))
        assert (
            connection.scalar(
                select(Mutation.client_wall_time).where(Mutation.id == future_rejection_id)
            )
            == future_rejection["client_wall_time"]
        )

        invalid_mutations = [
            {**mutation, "id": uuid4(), "actor_user_id": other_user_id},
            {**mutation, "id": uuid4(), "organization_id": None},
            {**mutation, "id": uuid4(), "is_system_administration_scope": True},
            {**mutation, "id": uuid4(), "actor_role": "owner"},
            {**mutation, "id": uuid4(), "command_schema_version": 0},
            {**mutation, "id": uuid4(), "command_kind": "Event Rename"},
            {**mutation, "id": uuid4(), "target_identities": []},
            {**mutation, "id": uuid4(), "target_identities": ["not-an-object"]},
            {**mutation, "id": uuid4(), "target_identities": [{}]},
            {
                **mutation,
                "id": uuid4(),
                "target_identities": [{"entity_kind": "event"}],
            },
            {
                **mutation,
                "id": uuid4(),
                "target_identities": [{"entity_kind": 42, "entity_id": str(uuid4())}],
            },
            {
                **mutation,
                "id": uuid4(),
                "target_identities": [{"entity_kind": "Event Name", "entity_id": str(uuid4())}],
            },
            {
                **mutation,
                "id": uuid4(),
                "target_identities": [{"entity_kind": "event", "entity_id": "not-a-uuid"}],
            },
            {
                **mutation,
                "id": uuid4(),
                "target_identities": [{"entity_kind": "event", "entity_id": str(uuid4()).upper()}],
            },
            {
                **mutation,
                "id": uuid4(),
                "target_identities": [
                    {
                        "entity_kind": "event",
                        "entity_id": str(uuid4()),
                        "note": "must not be persisted",
                    }
                ],
            },
            {**mutation, "id": uuid4(), "request_hash": b"short"},
            {**mutation, "id": uuid4(), "oauth_client_id": "mcp-client"},
            {**mutation, "id": uuid4(), "oauth_client_id": " mcp ", "oauth_grant_id": "grant"},
            {**mutation, "id": uuid4(), "outcome": "pending"},
            {**mutation, "id": uuid4(), "outcome_payload": ["not", "an", "object"]},
            {**mutation, "id": uuid4(), "first_change_sequence": 0},
            {
                **mutation,
                "id": uuid4(),
                "first_change_sequence": 4,
                "last_change_sequence": 3,
            },
            {
                **mutation,
                "id": uuid4(),
                "outcome": "rejected",
                "first_change_sequence": 1,
                "last_change_sequence": 1,
            },
            {
                **system_mutation,
                "id": uuid4(),
                "outcome": "partially_superseded",
            },
            {
                **system_mutation,
                "id": uuid4(),
                "actor_role": "organization_admin",
            },
        ]
        for values in invalid_mutations:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(insert(Mutation).values(**values))

        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(insert(Mutation).values(**{**mutation, "request_hash": b"m" * 32}))

        connection.execute(
            update(ClientInstallation)
            .where(ClientInstallation.id == installation_id)
            .values(disabled_at=now, disabled_by_user_id=actor_id)
        )
        disabled = connection.execute(
            select(ClientInstallation.disabled_at, ClientInstallation.disabled_by_user_id).where(
                ClientInstallation.id == installation_id
            )
        ).one()
        assert disabled == (now, actor_id)


def test_mutation_insert_rolls_back_with_its_domain_transaction(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, _, organization_id, installation_id = seed_identity(engine)
    mutation_id = uuid4()

    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            insert(Mutation).values(
                **valid_mutation(
                    actor_id=actor_id,
                    organization_id=organization_id,
                    installation_id=installation_id,
                    mutation_id=mutation_id,
                )
            )
        )
        transaction.rollback()

    with engine.connect() as connection:
        assert connection.scalar(select(Mutation.id).where(Mutation.id == mutation_id)) is None
