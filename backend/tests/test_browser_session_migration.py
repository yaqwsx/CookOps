import hashlib
import hmac
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError

from alembic import command
from cookops.persistence.models import BrowserSession, User

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


def create_user(engine: Engine, *, email: str = "session-user@example.test") -> UUID:
    user_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=user_id,
                display_name="Session user",
                verified_email=email,
                normalized_email=email,
            )
        )
    return user_id


def valid_session(user_id: UUID, *, now: datetime | None = None) -> dict[str, object]:
    created_at = now or datetime.now(UTC)
    return {
        "user_id": user_id,
        "secret_hmac": secrets.token_bytes(32),
        "created_at": created_at,
        "expires_at": created_at + timedelta(days=7),
    }


def test_migration_upgrades_empty_database_matches_metadata_and_downgrades(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine

    command.upgrade(configuration, "head")
    assert "browser_sessions" in inspect(engine).get_table_names()
    command.check(configuration)

    command.downgrade(configuration, "0004_client_mutations")
    assert "browser_sessions" not in inspect(engine).get_table_names()


def test_migration_upgrades_previous_schema_and_preserves_users_on_downgrade(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine
    command.upgrade(configuration, "0004_client_mutations")
    user_id = create_user(engine, email="existing-session-user@example.test")

    command.upgrade(configuration, "head")
    assert "browser_sessions" in inspect(engine).get_table_names()
    command.downgrade(configuration, "0004_client_mutations")

    assert "browser_sessions" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(select(User.id).where(User.id == user_id)) == user_id


def test_session_secret_is_stored_only_as_keyed_digest(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    user_id = create_user(engine)
    cookie_secret = secrets.token_bytes(32)
    deployment_key = secrets.token_bytes(32)
    secret_hmac = hmac.new(deployment_key, cookie_secret, hashlib.sha256).digest()
    values = valid_session(user_id)
    values["secret_hmac"] = secret_hmac

    with engine.begin() as connection:
        session_id = connection.scalar(
            insert(BrowserSession).values(**values).returning(BrowserSession.id)
        )
        stored = connection.execute(
            select(BrowserSession.secret_hmac).where(BrowserSession.id == session_id)
        ).scalar_one()

    assert stored == secret_hmac
    assert stored != cookie_secret
    column_names = {column["name"] for column in inspect(engine).get_columns("browser_sessions")}
    assert "secret" not in column_names
    assert "deployment_key" not in column_names


def test_session_lifecycle_constraints_and_user_references(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    user_id = create_user(engine)
    now = datetime.now(UTC)
    values = valid_session(user_id, now=now)

    with engine.begin() as connection:
        session_id = connection.scalar(
            insert(BrowserSession).values(**values).returning(BrowserSession.id)
        )
        assert session_id is not None

        invalid_sessions = [
            {**values, "secret_hmac": b"short"},
            {**values},
            {**values, "secret_hmac": secrets.token_bytes(32), "user_id": uuid4()},
            {
                **values,
                "secret_hmac": secrets.token_bytes(32),
                "expires_at": now,
            },
            {
                **values,
                "secret_hmac": secrets.token_bytes(32),
                "last_used_at": now - timedelta(microseconds=1),
            },
            {
                **values,
                "secret_hmac": secrets.token_bytes(32),
                "last_used_at": values["expires_at"],
            },
            {
                **values,
                "secret_hmac": secrets.token_bytes(32),
                "revoked_at": now,
            },
            {
                **values,
                "secret_hmac": secrets.token_bytes(32),
                "revoked_by_user_id": user_id,
            },
            {
                **values,
                "secret_hmac": secrets.token_bytes(32),
                "revoked_at": now,
                "revoked_by_user_id": uuid4(),
            },
            {
                **values,
                "secret_hmac": secrets.token_bytes(32),
                "revoked_at": now - timedelta(microseconds=1),
                "revoked_by_user_id": user_id,
            },
            {
                **values,
                "secret_hmac": secrets.token_bytes(32),
                "last_used_at": now + timedelta(minutes=2),
                "revoked_at": now + timedelta(minutes=1),
                "revoked_by_user_id": user_id,
            },
        ]
        for invalid in invalid_sessions:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(insert(BrowserSession).values(**invalid))

        last_used_at = now + timedelta(minutes=1)
        revoked_at = now + timedelta(minutes=2)
        connection.execute(
            update(BrowserSession)
            .where(BrowserSession.id == session_id)
            .values(
                last_used_at=last_used_at,
                revoked_at=revoked_at,
                revoked_by_user_id=user_id,
            )
        )
        assert connection.execute(
            select(
                BrowserSession.last_used_at,
                BrowserSession.revoked_at,
                BrowserSession.revoked_by_user_id,
            ).where(BrowserSession.id == session_id)
        ).one() == (last_used_at, revoked_at, user_id)

        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(delete(User).where(User.id == user_id))
