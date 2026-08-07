import asyncio
import base64
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command as alembic_command
from cookops.application.browser_sessions import (
    BrowserSessionConfigurationError,
    BrowserSessionService,
    decode_browser_session_hmac_key,
)
from cookops.persistence.models import BrowserSession, User

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()
ROTATED_KEY = base64.urlsafe_b64encode(b"fedcba9876543210fedcba9876543210").rstrip(b"=").decode()


@dataclass
class SessionDatabase:
    sync_engine: Engine
    sessions: async_sessionmaker[AsyncSession]
    user_id: UUID
    other_user_id: UUID


@pytest.fixture
def session_database() -> Iterator[SessionDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    sync_engine = create_engine(database_url)
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    user_id, other_user_id = uuid4(), uuid4()
    with sync_engine.begin() as connection:
        connection.execute(
            insert(User),
            [
                {
                    "id": user_id,
                    "display_name": "Session user",
                    "verified_email": "session@example.test",
                    "normalized_email": "session@example.test",
                },
                {
                    "id": other_user_id,
                    "display_name": "Other user",
                    "verified_email": "other@example.test",
                    "normalized_email": "other@example.test",
                },
            ],
        )
    database = SessionDatabase(
        sync_engine=sync_engine,
        sessions=async_sessionmaker(async_engine, expire_on_commit=False),
        user_id=user_id,
        other_user_id=other_user_id,
    )
    try:
        yield database
    finally:
        asyncio.run(async_engine.dispose())
        sync_engine.dispose()
        alembic_command.downgrade(configuration, "base")


def service(
    database: SessionDatabase,
    *,
    now: datetime,
    key: str = KEY,
) -> BrowserSessionService:
    return BrowserSessionService(database.sessions, encoded_hmac_key=key, clock=lambda: now)


def test_key_parser_is_strict_and_rotation_fails_closed(session_database: SessionDatabase) -> None:
    assert decode_browser_session_hmac_key(KEY) == b"0123456789abcdef0123456789abcdef"
    for bad_key in ("", "not-base64", KEY + "=", f" {KEY}", KEY[:-1]):
        with pytest.raises(BrowserSessionConfigurationError):
            decode_browser_session_hmac_key(bad_key)

    now = datetime(2026, 1, 1, tzinfo=UTC)
    issued = asyncio.run(
        service(session_database, now=now).issue(
            user_id=session_database.user_id,
            expires_at=now + timedelta(days=1),
        )
    )

    assert (
        asyncio.run(service(session_database, now=now, key=ROTATED_KEY).authenticate(issued.secret))
        is None
    )


def test_issue_persists_only_keyed_digest_and_authenticates_active_session(
    session_database: SessionDatabase,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session_service = service(session_database, now=now)
    issued = asyncio.run(
        session_service.issue(
            user_id=session_database.user_id,
            expires_at=now + timedelta(days=1),
        )
    )

    with session_database.sync_engine.connect() as connection:
        record = connection.execute(
            select(BrowserSession.secret_hmac, BrowserSession.last_used_at).where(
                BrowserSession.id == issued.id
            )
        ).one()
    assert record.secret_hmac != issued.secret.encode("ascii")
    assert len(record.secret_hmac) == 32
    assert issued.secret not in record.secret_hmac.decode("latin1")
    assert record.last_used_at is None

    authenticated = asyncio.run(session_service.authenticate(issued.secret))

    assert authenticated is not None
    assert authenticated.id == issued.id
    assert authenticated.user_id == session_database.user_id
    assert authenticated.last_used_at == now
    assert asyncio.run(session_service.authenticate("malformed\N{SNOWMAN}")) is None
    assert asyncio.run(session_service.authenticate("")) is None


def test_expiry_revocation_logout_and_disabled_users_are_rejected(
    session_database: SessionDatabase,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    issued = asyncio.run(
        service(session_database, now=now).issue(
            user_id=session_database.user_id,
            expires_at=now + timedelta(hours=1),
        )
    )
    assert (
        asyncio.run(
            service(session_database, now=now).logout(
                issued.secret, user_id=session_database.other_user_id
            )
        )
        is False
    )
    assert asyncio.run(service(session_database, now=now).authenticate(issued.secret)) is not None
    assert (
        asyncio.run(
            service(session_database, now=now).logout(
                issued.secret, user_id=session_database.user_id
            )
        )
        is True
    )
    assert asyncio.run(service(session_database, now=now).authenticate(issued.secret)) is None

    with session_database.sync_engine.connect() as connection:
        revoked = connection.execute(
            select(BrowserSession.revoked_at, BrowserSession.revoked_by_user_id).where(
                BrowserSession.id == issued.id
            )
        ).one()
    assert revoked == (now, session_database.user_id)

    expiring = asyncio.run(
        service(session_database, now=now).issue(
            user_id=session_database.other_user_id,
            expires_at=now + timedelta(minutes=1),
        )
    )
    assert (
        asyncio.run(
            service(session_database, now=now + timedelta(minutes=1)).authenticate(expiring.secret)
        )
        is None
    )

    with session_database.sync_engine.begin() as connection:
        connection.execute(
            update(User)
            .where(User.id == session_database.other_user_id)
            .values(disabled_at=now, disabled_by_user_id=session_database.user_id)
        )
    assert asyncio.run(service(session_database, now=now).authenticate(expiring.secret)) is None
    with pytest.raises(PermissionError):
        asyncio.run(
            service(session_database, now=now).issue(
                user_id=session_database.other_user_id,
                expires_at=now + timedelta(days=1),
            )
        )


def test_concurrent_authentication_keeps_last_used_monotonic(
    session_database: SessionDatabase,
) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    issued = asyncio.run(
        service(session_database, now=created_at).issue(
            user_id=session_database.user_id,
            expires_at=created_at + timedelta(days=1),
        )
    )
    early = created_at + timedelta(seconds=1)
    late = created_at + timedelta(seconds=2)

    async def authenticate_from_two_requests() -> tuple[object, object]:
        return await asyncio.gather(
            service(session_database, now=late).authenticate(issued.secret),
            service(session_database, now=early).authenticate(issued.secret),
        )

    results = asyncio.run(authenticate_from_two_requests())
    assert all(result is not None for result in results)
    final = asyncio.run(service(session_database, now=early).authenticate(issued.secret))
    assert final is not None
    assert final.last_used_at == late


def test_concurrent_revocation_is_atomic_and_retains_one_actor(
    session_database: SessionDatabase,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    issued = asyncio.run(
        service(session_database, now=now).issue(
            user_id=session_database.user_id,
            expires_at=now + timedelta(days=1),
        )
    )

    async def revoke_twice() -> tuple[bool, bool]:
        return await asyncio.gather(
            service(session_database, now=now).revoke(
                issued.secret, revoked_by_user_id=session_database.user_id
            ),
            service(session_database, now=now).revoke(
                issued.secret, revoked_by_user_id=session_database.other_user_id
            ),
        )

    outcomes = asyncio.run(revoke_twice())
    assert sorted(outcomes) == [False, True]
    with session_database.sync_engine.connect() as connection:
        revoked_at, revoked_by_user_id = connection.execute(
            select(BrowserSession.revoked_at, BrowserSession.revoked_by_user_id).where(
                BrowserSession.id == issued.id
            )
        ).one()
    assert revoked_at == now
    assert revoked_by_user_id in {session_database.user_id, session_database.other_user_id}


def test_authentication_serializes_with_concurrent_user_disable(
    session_database: SessionDatabase,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session_service = service(session_database, now=now)
    issued = asyncio.run(
        session_service.issue(
            user_id=session_database.user_id,
            expires_at=now + timedelta(days=1),
        )
    )

    async def disable_before_authentication_can_finish() -> object:
        async with session_database.sessions() as disabling_session, disabling_session.begin():
            user = await disabling_session.scalar(
                select(User).where(User.id == session_database.user_id).with_for_update(of=User)
            )
            assert user is not None
            authentication = asyncio.create_task(session_service.authenticate(issued.secret))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(authentication), timeout=0.1)
            user.disabled_at = now
            user.disabled_by_user_id = session_database.other_user_id
            await disabling_session.flush()
        return await authentication

    assert asyncio.run(disable_before_authentication_can_finish()) is None
