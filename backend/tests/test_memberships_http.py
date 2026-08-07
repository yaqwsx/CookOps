import asyncio
import base64
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import PostgresDsn
from sqlalchemy import Engine, create_engine, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command as alembic_command
from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.human_authentication import (
    HumanAuthenticationDenied,
    HumanAuthenticationService,
    TrustedIdentityAssertion,
)
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.main import create_app
from cookops.persistence.models import (
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()


@dataclass
class MembershipDatabase:
    configuration: Config
    engine: Engine
    sessions: async_sessionmaker[AsyncSession]
    organization_id: UUID
    admin_membership_id: UUID
    ordinary_membership_id: UUID
    admin_id: UUID
    ordinary_id: UUID


@pytest.fixture
def membership_database() -> Iterator[MembershipDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    engine = create_engine(database_url)
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    admin_id, ordinary_id, member_id, creator_id = (uuid4(), uuid4(), uuid4(), uuid4())
    organization_id = uuid4()
    admin_membership_id, ordinary_membership_id, member_membership_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(User),
            [
                {
                    "id": user_id,
                    "display_name": name,
                    "verified_email": email,
                    "normalized_email": email,
                }
                for user_id, name, email in (
                    (admin_id, "Membership administrator", "admin@example.test"),
                    (ordinary_id, "Ordinary member", "ordinary@example.test"),
                    (member_id, "Limited member", "member@example.test"),
                    (creator_id, "Creator", "creator@example.test"),
                )
            ],
        )
        connection.execute(
            text(
                "INSERT INTO external_identities "
                "(user_id, provider, provider_subject, verified_email, normalized_verified_email) "
                "VALUES (:user_id, 'dummy', :subject, :email, :email)"
            ),
            [
                {"user_id": admin_id, "subject": "membership-admin", "email": "admin@example.test"},
                {
                    "user_id": ordinary_id,
                    "subject": "membership-ordinary",
                    "email": "ordinary@example.test",
                },
                {
                    "user_id": member_id,
                    "subject": "membership-member",
                    "email": "member@example.test",
                },
            ],
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id,
                name="Membership organization",
                default_currency="CZK",
                created_by_user_id=creator_id,
            )
        )
        connection.execute(
            insert(OrganizationMembership),
            [
                {
                    "id": membership_id,
                    "organization_id": organization_id,
                    "user_id": user_id,
                    "invited_email": email,
                    "role": role,
                    "state": "active",
                    "invited_by_user_id": creator_id,
                    "claimed_at": now,
                }
                for membership_id, user_id, email, role in (
                    (admin_membership_id, admin_id, "admin@example.test", "organization_admin"),
                    (ordinary_membership_id, ordinary_id, "ordinary@example.test", "member"),
                    (member_membership_id, member_id, "member@example.test", "member"),
                )
            ],
        )
    database = MembershipDatabase(
        configuration,
        engine,
        async_sessionmaker(async_engine, expire_on_commit=False),
        organization_id,
        admin_membership_id,
        ordinary_membership_id,
        admin_id,
        ordinary_id,
    )
    try:
        yield database
    finally:
        asyncio.run(async_engine.dispose())
        engine.dispose()
        alembic_command.downgrade(configuration, "base")


def _settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        human_auth_provider=HumanAuthProvider.DUMMY,
        database_url=PostgresDsn(os.environ["TEST_DATABASE_URL"]),
        browser_session_hmac_key=KEY,
    )


def _sign_in(client: TestClient, subject: str) -> None:
    assert client.post("/auth/dummy/session", json={"subject": subject}).status_code == 204


def _body() -> dict[str, str]:
    return {
        "mutation_id": str(uuid4()),
        "client_installation_id": str(uuid4()),
        "client_wall_time": datetime.now(UTC).isoformat(),
    }


def test_invitation_is_prelogin_idempotent_and_never_exports_email_to_sync(
    membership_database: MembershipDatabase,
) -> None:
    path = f"/api/v1/organizations/{membership_database.organization_id}/members"
    body = {**_body(), "invited_email": " Future@Example.test "}
    with TestClient(create_app(_settings()), base_url="https://testserver") as anonymous:
        assert anonymous.post(f"{path}/invitations", json=body).status_code == 401
    with TestClient(create_app(_settings()), base_url="https://testserver") as limited:
        _sign_in(limited, "membership-member")
        assert limited.get(path).json() == {"detail": {"code": "not_found"}}
        assert limited.post(f"{path}/invitations", json=body).status_code == 404
    with TestClient(create_app(_settings()), base_url="https://testserver") as admin:
        _sign_in(admin, "membership-admin")
        created = admin.post(f"{path}/invitations", json=body)
        assert created.status_code == 200
        assert created.json()["state"] == "invited"
        membership_id = UUID(created.json()["membership_id"])
        replayed = admin.post(f"{path}/invitations", json=body)
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True
        assert (
            admin.post(
                f"{path}/invitations", json={**body, "invited_email": "different@example.test"}
            ).status_code
            == 409
        )
        bootstrap = admin.post(
            "/api/v1/sync/bootstrap",
            json={"organization_id": str(membership_database.organization_id)},
        )
        assert bootstrap.status_code == 200
        assert "future@example.test" not in str(bootstrap.json())

    with membership_database.engine.connect() as connection:
        invited = connection.execute(
            select(
                OrganizationMembership.user_id,
                OrganizationMembership.invited_email,
                OrganizationMembership.role,
                OrganizationMembership.state,
            ).where(OrganizationMembership.id == membership_id)
        ).one()
        assert invited == (None, "future@example.test", "member", "invited")
        mutation = connection.execute(
            select(Mutation.first_change_sequence, Mutation.last_change_sequence).where(
                Mutation.id == UUID(body["mutation_id"])
            )
        ).one()
        assert mutation[0] == mutation[1]
        change = connection.execute(
            select(OrganizationChange.entity_kind, OrganizationChange.payload).where(
                OrganizationChange.mutation_id == UUID(body["mutation_id"])
            )
        ).one()
        assert change[0] == "organization"
        assert "future@example.test" not in str(change[1])

    authentication = HumanAuthenticationService(
        membership_database.sessions,
        BrowserSessionService(membership_database.sessions, encoded_hmac_key=KEY),
        session_lifetime=timedelta(days=7),
    )
    claimed = asyncio.run(
        authentication.complete(
            TrustedIdentityAssertion(
                provider="google",
                provider_subject="future-google-subject",
                verified_email="Future@Example.test",
            )
        )
    )
    with membership_database.engine.connect() as connection:
        claimed_membership = connection.execute(
            select(OrganizationMembership.user_id, OrganizationMembership.state).where(
                OrganizationMembership.id == membership_id
            )
        ).one()
    assert claimed_membership == (claimed.user_id, "active")
    with pytest.raises(HumanAuthenticationDenied):
        asyncio.run(
            authentication.complete(
                TrustedIdentityAssertion(
                    provider="google",
                    provider_subject="uninvited-google-subject",
                    verified_email="other@example.test",
                )
            )
        )


def test_only_current_administrator_can_remove_an_ordinary_member(
    membership_database: MembershipDatabase,
) -> None:
    path = f"/api/v1/organizations/{membership_database.organization_id}/members"
    with TestClient(create_app(_settings()), base_url="https://testserver") as limited:
        _sign_in(limited, "membership-member")
        assert (
            limited.post(
                f"{path}/{membership_database.ordinary_membership_id}/remove", json=_body()
            ).status_code
            == 404
        )
    with TestClient(create_app(_settings()), base_url="https://testserver") as ordinary:
        _sign_in(ordinary, "membership-ordinary")
        with TestClient(create_app(_settings()), base_url="https://testserver") as admin:
            _sign_in(admin, "membership-admin")
            assert (
                admin.post(
                    f"{path}/{membership_database.admin_membership_id}/remove", json=_body()
                ).status_code
                == 404
            )
            body = _body()
            removed = admin.post(
                f"{path}/{membership_database.ordinary_membership_id}/remove", json=body
            )
            assert removed.status_code == 200
            assert removed.json()["state"] == "removed"
            assert (
                admin.post(
                    f"{path}/{membership_database.ordinary_membership_id}/remove", json=body
                ).json()["replayed"]
                is True
            )
        assert ordinary.get("/auth/session").status_code == 401

    with membership_database.engine.connect() as connection:
        row = connection.execute(
            select(
                OrganizationMembership.state,
                OrganizationMembership.removed_at,
                OrganizationMembership.removed_by_user_id,
            ).where(OrganizationMembership.id == membership_database.ordinary_membership_id)
        ).one()
    assert row[0] == "removed"
    assert row[1] is not None
    assert row[2] == membership_database.admin_id
