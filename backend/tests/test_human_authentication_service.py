import asyncio
import base64
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, insert, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command as alembic_command
from cookops.application.browser_sessions import BrowserSessionService, IssuedBrowserSession
from cookops.application.human_authentication import (
    HumanAuthenticationDenied,
    HumanAuthenticationService,
    IdentityProvider,
    TrustedIdentityAssertion,
)
from cookops.persistence.models import (
    BrowserSession,
    ExternalIdentity,
    Organization,
    OrganizationMembership,
    SystemRoleAssignment,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


@dataclass
class AuthenticationDatabase:
    sync_engine: Engine
    sessions: async_sessionmaker[AsyncSession]
    users: dict[str, UUID]


@pytest.fixture
def authentication_database() -> Iterator[AuthenticationDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    sync_engine = create_engine(database_url)
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    users = {
        "actor": uuid4(),
        "google_member": uuid4(),
        "dummy_member": uuid4(),
        "system_admin": uuid4(),
        "no_access": uuid4(),
        "disabled_member": uuid4(),
    }
    organization_id = uuid4()
    with sync_engine.begin() as connection:
        connection.execute(
            insert(User),
            [
                {
                    "id": user_id,
                    "display_name": name.replace("_", " ").title(),
                    "verified_email": f"{name}@example.test",
                    "normalized_email": f"{name}@example.test",
                }
                for name, user_id in users.items()
            ],
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id,
                name="Test organization",
                created_by_user_id=users["actor"],
            )
        )
        connection.execute(
            insert(ExternalIdentity),
            [
                {
                    "user_id": users["google_member"],
                    "provider": "google",
                    "provider_subject": "google-member-subject",
                    "verified_email": "google_member@example.test",
                    "normalized_verified_email": "google_member@example.test",
                },
                {
                    "user_id": users["dummy_member"],
                    "provider": "dummy",
                    "provider_subject": "dummy-member-subject",
                    "verified_email": "dummy_member@example.test",
                    "normalized_verified_email": "dummy_member@example.test",
                },
                {
                    "user_id": users["system_admin"],
                    "provider": "google",
                    "provider_subject": "system-admin-subject",
                    "verified_email": "system_admin@example.test",
                    "normalized_verified_email": "system_admin@example.test",
                },
                {
                    "user_id": users["no_access"],
                    "provider": "google",
                    "provider_subject": "no-access-subject",
                    "verified_email": "no_access@example.test",
                    "normalized_verified_email": "no_access@example.test",
                },
                {
                    "user_id": users["disabled_member"],
                    "provider": "dummy",
                    "provider_subject": "disabled-member-subject",
                    "verified_email": "disabled_member@example.test",
                    "normalized_verified_email": "disabled_member@example.test",
                },
            ],
        )
        connection.execute(
            insert(OrganizationMembership),
            [
                {
                    "organization_id": organization_id,
                    "user_id": users[name],
                    "invited_email": f"{name}@example.test",
                    "state": "active",
                    "invited_by_user_id": users["actor"],
                    "claimed_at": NOW,
                }
                for name in ("google_member", "dummy_member", "disabled_member")
            ],
        )
        connection.execute(
            insert(SystemRoleAssignment).values(
                user_id=users["system_admin"],
                invited_email="system_admin@example.test",
                granted_by_user_id=users["actor"],
                claimed_at=NOW,
            )
        )
        connection.execute(
            update(User)
            .where(User.id == users["disabled_member"])
            .values(disabled_at=NOW, disabled_by_user_id=users["actor"])
        )
    database = AuthenticationDatabase(
        sync_engine=sync_engine,
        sessions=async_sessionmaker(async_engine, expire_on_commit=False),
        users=users,
    )
    try:
        yield database
    finally:
        asyncio.run(async_engine.dispose())
        sync_engine.dispose()
        alembic_command.downgrade(configuration, "base")


def service(database: AuthenticationDatabase) -> HumanAuthenticationService:
    browser_sessions = BrowserSessionService(
        database.sessions,
        encoded_hmac_key=KEY,
        clock=lambda: NOW,
    )
    return HumanAuthenticationService(
        database.sessions,
        browser_sessions,
        session_lifetime=timedelta(days=7),
        clock=lambda: NOW,
    )


def test_locale_update_requires_current_access_and_persists(
    authentication_database: AuthenticationDatabase,
) -> None:
    user_id = authentication_database.users["google_member"]
    auth = service(authentication_database)
    updated = asyncio.run(auth.set_current_identity_locale(user_id, "en"))
    assert updated is not None and updated.preferred_locale == "en"
    with authentication_database.sync_engine.begin() as connection:
        assert connection.scalar(select(User.preferred_locale).where(User.id == user_id)) == "en"
        connection.execute(
            update(OrganizationMembership)
            .where(OrganizationMembership.user_id == user_id)
            .values(
                state="removed",
                removed_at=NOW,
                removed_by_user_id=authentication_database.users["actor"],
            )
        )
    assert asyncio.run(auth.set_current_identity_locale(user_id, "cs")) is None
    with authentication_database.sync_engine.connect() as connection:
        assert connection.scalar(select(User.preferred_locale).where(User.id == user_id)) == "en"
    with pytest.raises(ValueError):
        asyncio.run(auth.set_current_identity_locale(user_id, "de"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("provider", "subject", "email", "user_name"),
    [
        ("google", "google-member-subject", "google_member@example.test", "google_member"),
        ("dummy", "dummy-member-subject", "dummy_member@example.test", "dummy_member"),
    ],
)
def test_dummy_and_google_complete_the_same_role_gate_and_session_flow(
    authentication_database: AuthenticationDatabase,
    provider: IdentityProvider,
    subject: str,
    email: str,
    user_name: str,
) -> None:
    assertion = TrustedIdentityAssertion(
        provider=provider,
        provider_subject=subject,
        verified_email=email,
    )

    completed = asyncio.run(service(authentication_database).complete(assertion))

    assert completed.user_id == authentication_database.users[user_name]
    assert completed.browser_session.user_id == completed.user_id
    assert completed.browser_session.expires_at == NOW + timedelta(days=7)
    authenticated = asyncio.run(
        BrowserSessionService(
            authentication_database.sessions,
            encoded_hmac_key=KEY,
            clock=lambda: NOW,
        ).authenticate(completed.browser_session.secret)
    )
    assert authenticated is not None
    assert authenticated.user_id == completed.user_id
    with authentication_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(User.last_successful_login_at).where(User.id == completed.user_id)
            )
            == NOW
        )
        assert (
            connection.scalar(
                select(ExternalIdentity.last_verified_at).where(
                    ExternalIdentity.provider == provider,
                    ExternalIdentity.provider_subject == subject,
                )
            )
            == NOW
        )


def test_system_administrator_needs_no_organization_membership(
    authentication_database: AuthenticationDatabase,
) -> None:
    completed = asyncio.run(
        service(authentication_database).complete(
            TrustedIdentityAssertion(
                provider="google",
                provider_subject="system-admin-subject",
                verified_email="system_admin@example.test",
            )
        )
    )

    assert completed.user_id == authentication_database.users["system_admin"]
    current = asyncio.run(
        service(authentication_database).current_identity(
            authentication_database.users["system_admin"]
        )
    )
    assert current is not None
    assert current.user_id == authentication_database.users["system_admin"]
    assert current.verified_email == "system_admin@example.test"


@pytest.mark.parametrize(
    ("table", "user_name", "assertion"),
    [
        (
            OrganizationMembership,
            "google_member",
            TrustedIdentityAssertion(
                provider="google",
                provider_subject="google-member-subject",
                verified_email="google_member@example.test",
            ),
        ),
        (
            SystemRoleAssignment,
            "system_admin",
            TrustedIdentityAssertion(
                provider="google",
                provider_subject="system-admin-subject",
                verified_email="system_admin@example.test",
            ),
        ),
    ],
)
def test_authority_row_must_match_the_assertion_email(
    authentication_database: AuthenticationDatabase,
    table: type[OrganizationMembership] | type[SystemRoleAssignment],
    user_name: str,
    assertion: TrustedIdentityAssertion,
) -> None:
    with authentication_database.sync_engine.begin() as connection:
        connection.execute(
            update(table)
            .where(table.user_id == authentication_database.users[user_name])
            .values(invited_email="other@example.test")
        )

    with pytest.raises(HumanAuthenticationDenied, match="authentication denied"):
        asyncio.run(service(authentication_database).complete(assertion))


def test_authority_lock_is_held_until_the_browser_session_is_inserted(
    authentication_database: AuthenticationDatabase,
) -> None:
    class LockCheckingSessionService(BrowserSessionService):
        async def issue_in_transaction(
            self,
            session: AsyncSession,
            *,
            user_id: UUID,
            expires_at: datetime,
        ) -> IssuedBrowserSession:
            async with authentication_database.sessions() as competing_session:
                await competing_session.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(OperationalError):
                    await competing_session.execute(
                        update(OrganizationMembership)
                        .where(OrganizationMembership.user_id == user_id)
                        .values(
                            state="removed",
                            removed_at=NOW,
                            removed_by_user_id=authentication_database.users["actor"],
                        )
                    )
                await competing_session.rollback()
            return await super().issue_in_transaction(
                session,
                user_id=user_id,
                expires_at=expires_at,
            )

    browser_sessions = LockCheckingSessionService(
        authentication_database.sessions,
        encoded_hmac_key=KEY,
        clock=lambda: NOW,
    )
    authentication = HumanAuthenticationService(
        authentication_database.sessions,
        browser_sessions,
        session_lifetime=timedelta(days=7),
        clock=lambda: NOW,
    )

    completed = asyncio.run(
        authentication.complete(
            TrustedIdentityAssertion(
                provider="google",
                provider_subject="google-member-subject",
                verified_email="google_member@example.test",
            )
        )
    )

    assert completed.user_id == authentication_database.users["google_member"]


@pytest.mark.parametrize(
    "assertion",
    [
        TrustedIdentityAssertion(
            provider="google",
            provider_subject="no-access-subject",
            verified_email="no_access@example.test",
        ),
        TrustedIdentityAssertion(
            provider="google",
            provider_subject="unknown-subject",
            verified_email="unknown@example.test",
        ),
        TrustedIdentityAssertion(
            provider="dummy",
            provider_subject="disabled-member-subject",
            verified_email="disabled_member@example.test",
        ),
        TrustedIdentityAssertion(
            provider="google",
            provider_subject="google-member-subject",
            verified_email="different@example.test",
        ),
    ],
)
def test_unrecognized_unauthorized_disabled_or_email_mismatched_identity_is_denied_without_creation(
    authentication_database: AuthenticationDatabase,
    assertion: TrustedIdentityAssertion,
) -> None:
    with authentication_database.sync_engine.connect() as connection:
        before = tuple(
            connection.scalar(select(func.count(table.id)))
            for table in (User, ExternalIdentity, OrganizationMembership, BrowserSession)
        )

    with pytest.raises(HumanAuthenticationDenied, match="authentication denied"):
        asyncio.run(service(authentication_database).complete(assertion))

    with authentication_database.sync_engine.connect() as connection:
        after = tuple(
            connection.scalar(select(func.count(table.id)))
            for table in (User, ExternalIdentity, OrganizationMembership, BrowserSession)
        )
    assert after == before


def test_trusted_assertion_and_session_lifetime_reject_invalid_input(
    authentication_database: AuthenticationDatabase,
) -> None:
    with pytest.raises(ValueError):
        TrustedIdentityAssertion(
            provider=cast(IdentityProvider, "password"),
            provider_subject="subject",
            verified_email="a@example.test",
        )
    with pytest.raises(ValueError):
        TrustedIdentityAssertion(
            provider="google",
            provider_subject=" subject",
            verified_email="a@example.test",
        )
    with pytest.raises(ValueError):
        TrustedIdentityAssertion(
            provider="google",
            provider_subject="subject",
            verified_email=" a@example.test",
        )

    browser_sessions = BrowserSessionService(
        authentication_database.sessions,
        encoded_hmac_key=KEY,
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="session_lifetime must be positive"):
        HumanAuthenticationService(
            authentication_database.sessions,
            browser_sessions,
            session_lifetime=timedelta(),
        )
