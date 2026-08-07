import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError

from alembic import command
from cookops.persistence.models import (
    ExternalIdentity,
    Organization,
    OrganizationMembership,
    SystemRoleAssignment,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

IDENTITY_TABLES = {
    "external_identities",
    "organization_memberships",
    "organizations",
    "system_role_assignments",
    "users",
}


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


def test_migration_upgrades_empty_database_and_downgrades_cleanly(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine

    command.upgrade(configuration, "head")
    assert set(inspect(engine).get_table_names()) >= IDENTITY_TABLES
    command.check(configuration)

    command.downgrade(configuration, "0001_baseline")
    assert IDENTITY_TABLES.isdisjoint(inspect(engine).get_table_names())


def test_migration_upgrades_previous_schema(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine

    command.upgrade(configuration, "0001_baseline")
    assert IDENTITY_TABLES.isdisjoint(inspect(engine).get_table_names())
    command.upgrade(configuration, "head")
    assert set(inspect(engine).get_table_names()) >= IDENTITY_TABLES


def test_identity_constraints_and_transaction_rollback(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id = uuid4()
    member_id = uuid4()
    organization_id = uuid4()
    membership_id = uuid4()
    system_role_id = uuid4()
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            insert(User),
            [
                {
                    "id": actor_id,
                    "display_name": "System Admin",
                    "verified_email": "Admin@Example.test",
                    "normalized_email": "admin@example.test",
                },
                {
                    "id": member_id,
                    "display_name": "Member",
                    "verified_email": "member@example.test",
                    "normalized_email": "member@example.test",
                },
            ],
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id,
                name="Test organization",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(ExternalIdentity).values(
                user_id=member_id,
                provider="google",
                provider_subject="google-member",
                verified_email="member@example.test",
                normalized_verified_email="member@example.test",
            )
        )

        invalid_statements = [
            insert(User).values(
                display_name="Duplicate email",
                verified_email="MEMBER@example.test",
                normalized_email="member@example.test",
            ),
            insert(ExternalIdentity).values(
                user_id=member_id,
                provider="password",
                provider_subject="subject",
                verified_email="member@example.test",
                normalized_verified_email="member@example.test",
            ),
            insert(ExternalIdentity).values(
                user_id=actor_id,
                provider="google",
                provider_subject="google-member",
                verified_email="admin@example.test",
                normalized_verified_email="admin@example.test",
            ),
            insert(Organization).values(
                name="Invalid currency",
                default_currency="czk",
                created_by_user_id=actor_id,
            ),
            insert(Organization).values(
                name="Invalid retirement",
                created_by_user_id=actor_id,
                retired_at=now,
            ),
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                invited_email="Member@Example.test",
                invited_by_user_id=actor_id,
            ),
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                user_id=member_id,
                invited_email="member@example.test",
                role="owner",
                state="active",
                invited_by_user_id=actor_id,
                claimed_at=now,
            ),
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                user_id=member_id,
                invited_email="member@example.test",
                state="pending",
                invited_by_user_id=actor_id,
                claimed_at=now,
            ),
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                user_id=member_id,
                invited_email="claim-without-time@example.test",
                state="active",
                invited_by_user_id=actor_id,
            ),
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                invited_email="removed-without-actor@example.test",
                state="removed",
                invited_by_user_id=actor_id,
                removed_at=now,
            ),
            insert(SystemRoleAssignment).values(
                user_id=member_id,
                invited_email="member@example.test",
                role="root",
                granted_by_user_id=actor_id,
                claimed_at=now,
            ),
            insert(SystemRoleAssignment).values(
                invited_email="revoked-without-actor@example.test",
                granted_by_user_id=actor_id,
                revoked_at=now,
            ),
            insert(OrganizationMembership).values(
                organization_id=uuid4(),
                invited_email="unowned@example.test",
                invited_by_user_id=actor_id,
            ),
        ]
        for statement in invalid_statements:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(statement)

        connection.execute(
            insert(OrganizationMembership).values(
                id=membership_id,
                organization_id=organization_id,
                user_id=member_id,
                invited_email="member@example.test",
                state="active",
                invited_by_user_id=actor_id,
                claimed_at=now,
            )
        )
        duplicate_membership = insert(OrganizationMembership).values(
            organization_id=organization_id,
            user_id=member_id,
            invited_email="member-alias@example.test",
            state="active",
            invited_by_user_id=actor_id,
            claimed_at=now,
        )
        duplicate_invitation = insert(OrganizationMembership).values(
            organization_id=organization_id,
            invited_email="member@example.test",
            invited_by_user_id=actor_id,
        )
        for statement in (duplicate_membership, duplicate_invitation):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(statement)

        connection.execute(
            update(OrganizationMembership)
            .where(OrganizationMembership.id == membership_id)
            .values(state="removed", removed_at=now, removed_by_user_id=actor_id)
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                user_id=member_id,
                invited_email="member@example.test",
                state="active",
                invited_by_user_id=actor_id,
                claimed_at=now,
            )
        )

        connection.execute(
            insert(SystemRoleAssignment).values(
                id=system_role_id,
                user_id=member_id,
                invited_email="member@example.test",
                granted_by_user_id=actor_id,
                claimed_at=now,
            )
        )
        duplicate_system_role_user = insert(SystemRoleAssignment).values(
            user_id=member_id,
            invited_email="member-admin@example.test",
            granted_by_user_id=actor_id,
            claimed_at=now,
        )
        duplicate_system_role_email = insert(SystemRoleAssignment).values(
            invited_email="member@example.test",
            granted_by_user_id=actor_id,
        )
        for statement in (duplicate_system_role_user, duplicate_system_role_email):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(statement)

        connection.execute(
            update(SystemRoleAssignment)
            .where(SystemRoleAssignment.id == system_role_id)
            .values(revoked_at=now, revoked_by_user_id=actor_id)
        )
        connection.execute(
            insert(SystemRoleAssignment).values(
                user_id=member_id,
                invited_email="member@example.test",
                granted_by_user_id=actor_id,
                claimed_at=now,
            )
        )

    rollback_organization_id = uuid4()
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            insert(Organization).values(
                id=rollback_organization_id,
                name="Rolled back",
                created_by_user_id=actor_id,
            )
        )
        transaction.rollback()

    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(Organization.id).where(Organization.id == rollback_organization_id)
            )
            is None
        )
