import base64
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import PostgresDsn
from sqlalchemy import Engine, create_engine, insert, select, update

from alembic import command as alembic_command
from cookops.application.google_identities import GoogleIdentityProvider
from cookops.application.human_authentication import HumanAuthenticationDenied
from cookops.application.oauth_interactions import (
    OAuthInteractionApprovalService,
    OAuthInteractionDetails,
)
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.http_auth import BrowserAuthenticationServices, create_auth_router
from cookops.http_oauth_interactions import OAuthInteractionHttpServices
from cookops.main import create_app, create_browser_authentication_services
from cookops.persistence.models import (
    BrowserSession,
    ExternalIdentity,
    Organization,
    OrganizationMembership,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()


@dataclass
class DummyAuthDatabase:
    sync_engine: Engine
    actor_id: UUID
    authorized_user_id: UUID
    organization_id: UUID


@pytest.fixture
def dummy_auth_database() -> Iterator[DummyAuthDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    sync_engine = create_engine(database_url)
    actor_id, authorized_user_id, no_access_user_id, disabled_user_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    organization_id = uuid4()
    now = datetime.now(UTC)
    with sync_engine.begin() as connection:
        connection.execute(
            insert(User),
            [
                {
                    "id": actor_id,
                    "display_name": "Development actor",
                    "verified_email": "actor@example.test",
                    "normalized_email": "actor@example.test",
                },
                {
                    "id": authorized_user_id,
                    "display_name": "Alice Member",
                    "verified_email": "alice@example.test",
                    "normalized_email": "alice@example.test",
                },
                {
                    "id": no_access_user_id,
                    "display_name": "Zoe No Access",
                    "verified_email": "zoe@example.test",
                    "normalized_email": "zoe@example.test",
                },
            ],
        )
        connection.execute(
            insert(User).values(
                id=disabled_user_id,
                display_name="Disabled Dummy",
                verified_email="disabled@example.test",
                normalized_email="disabled@example.test",
                disabled_at=now,
                disabled_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(ExternalIdentity),
            [
                {
                    "user_id": authorized_user_id,
                    "provider": "dummy",
                    "provider_subject": "dummy-alice",
                    "verified_email": "alice@example.test",
                    "normalized_verified_email": "alice@example.test",
                },
                {
                    "user_id": authorized_user_id,
                    "provider": "google",
                    "provider_subject": "google-alice",
                    "verified_email": "alice@example.test",
                    "normalized_verified_email": "alice@example.test",
                },
                {
                    "user_id": no_access_user_id,
                    "provider": "dummy",
                    "provider_subject": "dummy-zoe",
                    "verified_email": "zoe@example.test",
                    "normalized_verified_email": "zoe@example.test",
                },
                {
                    "user_id": disabled_user_id,
                    "provider": "dummy",
                    "provider_subject": "dummy-disabled",
                    "verified_email": "disabled@example.test",
                    "normalized_verified_email": "disabled@example.test",
                },
            ],
        )
        connection.execute(
            insert(Organization).values(
                id=organization_id,
                name="Development organization",
                default_currency="CZK",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                user_id=authorized_user_id,
                invited_email="alice@example.test",
                role="member",
                state="active",
                invited_by_user_id=actor_id,
                claimed_at=now,
            )
        )
    database = DummyAuthDatabase(
        sync_engine=sync_engine,
        actor_id=actor_id,
        authorized_user_id=authorized_user_id,
        organization_id=organization_id,
    )
    try:
        yield database
    finally:
        sync_engine.dispose()
        alembic_command.downgrade(configuration, "base")


def settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        human_auth_provider=HumanAuthProvider.DUMMY,
        database_url=PostgresDsn(os.environ["TEST_DATABASE_URL"]),
        browser_session_hmac_key=KEY,
    )


def test_dummy_authentication_only_selects_existing_identities_and_issues_a_secure_session(
    dummy_auth_database: DummyAuthDatabase,
) -> None:
    app = create_app(settings())
    with TestClient(app, base_url="https://testserver") as client:
        listed = client.get("/auth/dummy/identities")
        assert listed.status_code == 200
        assert listed.json() == {
            "identities": [
                {"subject": "dummy-alice", "display_name": "Alice Member"},
                {"subject": "dummy-zoe", "display_name": "Zoe No Access"},
            ]
        }

        rejected = client.post("/auth/dummy/session", json={"subject": "dummy-zoe"})
        assert rejected.status_code == 403
        assert rejected.json() == {"detail": "authentication denied"}
        assert "set-cookie" not in rejected.headers

        arbitrary = client.post(
            "/auth/dummy/session",
            json={"subject": "not-an-existing-identity"},
        )
        assert arbitrary.status_code == 403
        assert arbitrary.json() == rejected.json()

        malformed = client.post(
            "/auth/dummy/session",
            json={"subject": "dummy-alice", "email": "admin@example.test"},
        )
        assert malformed.status_code == 422

        created = client.post("/auth/dummy/session", json={"subject": "dummy-alice"})
        assert created.status_code == 204
        assert created.content == b""
        cookie = created.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "Max-Age=604800" in cookie
        assert "Path=/" in cookie
        assert "SameSite=lax" in cookie
        assert "Secure" in cookie

        current = client.get("/auth/session")
        assert current.status_code == 200
        assert current.json() == {
            "id": str(dummy_auth_database.authorized_user_id),
            "display_name": "Alice Member",
            "verified_email": "alice@example.test",
        }
        organizations = client.get("/api/v1/organizations")
        assert organizations.status_code == 200
        assert organizations.json() == {
            "organizations": [
                {
                    "id": str(dummy_auth_database.organization_id),
                    "name": "Development organization",
                }
            ]
        }

        with dummy_auth_database.sync_engine.begin() as connection:
            connection.execute(
                update(OrganizationMembership)
                .where(OrganizationMembership.user_id == dummy_auth_database.authorized_user_id)
                .values(
                    state="removed",
                    removed_at=datetime.now(UTC),
                    removed_by_user_id=dummy_auth_database.actor_id,
                )
            )
        assert client.get("/auth/session").status_code == 401
        assert client.get("/api/v1/organizations").status_code == 401

        logged_out = client.post("/auth/session/logout")
        assert logged_out.status_code == 204
        assert "Max-Age=0" in logged_out.headers["set-cookie"]
        assert client.get("/auth/session").status_code == 401

    with dummy_auth_database.sync_engine.connect() as connection:
        revoked = connection.execute(
            select(BrowserSession.revoked_at, BrowserSession.revoked_by_user_id)
        ).one()
    assert revoked.revoked_at is not None
    assert revoked.revoked_by_user_id == dummy_auth_database.authorized_user_id


def test_dummy_routes_are_not_mounted_when_google_is_selected(
    dummy_auth_database: DummyAuthDatabase,
) -> None:
    app_settings = settings().model_copy(
        update={
            "human_auth_provider": HumanAuthProvider.GOOGLE,
            "google_client_id": "test-client.apps.googleusercontent.com",
        }
    )
    with TestClient(create_app(app_settings), base_url="https://testserver") as client:
        assert client.get("/auth/dummy/identities").status_code == 404
        assert (
            client.post("/auth/dummy/session", json={"subject": "dummy-alice"}).status_code == 404
        )


def test_google_route_issues_the_same_cookie_and_does_not_mount_dummy_routes() -> None:
    app_settings = Settings(
        environment=Environment.PRODUCTION,
        human_auth_provider=HumanAuthProvider.GOOGLE,
        google_client_id="test-client.apps.googleusercontent.com",
        browser_session_hmac_key=KEY,
        browser_origin="https://testserver",
        oauth_interaction_details_api_credential_base64url=KEY[:-1] + "U",
        oauth_interaction_approval_api_credential_base64url=KEY,
        oauth_interaction_origin="https://testserver",
    )
    google_provider = MagicMock()
    google_provider.complete_id_token = AsyncMock(
        return_value=MagicMock(browser_session=MagicMock(secret="google-session-secret"))
    )

    def authentication_factory(
        _settings: Settings, _session_factory: object
    ) -> BrowserAuthenticationServices:
        return BrowserAuthenticationServices(
            browser_sessions=MagicMock(),
            human_authentication=MagicMock(),
            dummy_identities=None,
            google_identities=google_provider,
        )

    class Runtime:
        session_factory = MagicMock()

        async def is_ready(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    with TestClient(
        create_app(
            app_settings,
            database_runtime_factory=lambda _url: Runtime(),
            browser_authentication_factory=authentication_factory,
        ),
        base_url="https://testserver",
    ) as client:
        created = client.post("/auth/google/session", json={"id_token": "opaque-google-token"})

        assert created.status_code == 204
        assert "google-session-secret" in created.headers["set-cookie"]
        assert "HttpOnly" in created.headers["set-cookie"]
        assert client.get("/auth/dummy/identities").status_code == 404
        assert client.post("/auth/google/session", json={}).status_code == 422

        google_provider.complete_id_token.side_effect = HumanAuthenticationDenied(
            "authentication denied"
        )
        denied = client.post("/auth/google/session", json={"id_token": "invalid-token"})
        assert denied.status_code == 403
        assert denied.json() == {"detail": "authentication denied"}
        assert "set-cookie" not in denied.headers

    assert google_provider.complete_id_token.await_args_list[0].args == ("opaque-google-token",)


def test_production_google_route_rejects_plain_http_before_token_verification() -> None:
    settings = Settings(
        environment=Environment.PRODUCTION,
        human_auth_provider=HumanAuthProvider.GOOGLE,
        google_client_id="test-client.apps.googleusercontent.com",
        browser_session_hmac_key=KEY,
        browser_origin="https://testserver",
        oauth_interaction_details_api_credential_base64url=KEY[:-1] + "U",
        oauth_interaction_approval_api_credential_base64url=KEY,
        oauth_interaction_origin="https://testserver",
    )
    google_provider = MagicMock()
    app = FastAPI()
    app.state.browser_authentication = BrowserAuthenticationServices(
        browser_sessions=MagicMock(),
        human_authentication=MagicMock(),
        dummy_identities=None,
        google_identities=google_provider,
    )
    app.include_router(create_auth_router(settings))

    with TestClient(app, base_url="http://testserver") as client:
        response = client.post("/auth/google/session", json={"id_token": "opaque-google-token"})

    assert response.status_code == 400
    assert response.json() == {"detail": "secure transport required"}
    google_provider.complete_id_token.assert_not_called()


@pytest.mark.parametrize("provider", [HumanAuthProvider.DUMMY, HumanAuthProvider.GOOGLE])
def test_identity_http_sessions_reach_cookie_bound_interaction_bridge(
    dummy_auth_database: DummyAuthDatabase, provider: HumanAuthProvider
) -> None:
    app_settings = settings().model_copy(
        update={
            "environment": (
                Environment.PRODUCTION
                if provider is HumanAuthProvider.GOOGLE
                else Environment.TEST
            ),
            "human_auth_provider": provider,
            "google_client_id": "test-client.apps.googleusercontent.com",
            "browser_origin": "https://testserver",
            "oauth_interaction_origin": "https://testserver",
            "oauth_interaction_details_api_credential_base64url": KEY,
            "oauth_interaction_approval_api_credential_base64url": KEY,
        }
    )

    def authentication_factory(
        configured: Settings, session_factory: object
    ) -> BrowserAuthenticationServices:
        services = create_browser_authentication_services(configured, session_factory)
        if provider is HumanAuthProvider.GOOGLE:
            services = BrowserAuthenticationServices(
                browser_sessions=services.browser_sessions,
                human_authentication=services.human_authentication,
                dummy_identities=None,
                google_identities=GoogleIdentityProvider(
                    services.human_authentication,
                    configured.google_client_id or "",
                    token_verifier=lambda raw, audience: {
                        "iss": "https://accounts.google.com",
                        "aud": audience,
                        "email_verified": True,
                        "sub": "google-alice",
                        "email": "alice@example.test",
                    }
                    if raw == "opaque-google-token"
                    else {},
                ),
            )
        return services

    private_client = AsyncMock()
    private_client.interaction_details.return_value = OAuthInteractionDetails(
        interaction_uid="N9E_oxk7dD9t7rR10dj-3",
        client_name="Trusted agent",
        resource="https://cookops.example/mcp",
        scopes=("cookops:mcp",),
    )
    private_client.record_approval.return_value = True
    app = create_app(app_settings, browser_authentication_factory=authentication_factory)
    with TestClient(app, base_url="https://testserver") as client:
        endpoint, payload = (
            ("/auth/dummy/session", {"subject": "dummy-alice"})
            if provider is HumanAuthProvider.DUMMY
            else ("/auth/google/session", {"id_token": "opaque-google-token"})
        )
        created = client.post(endpoint, json=payload)
        assert created.status_code == 204
        cookie = created.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie

        app.state.oauth_interactions = OAuthInteractionHttpServices(
            OAuthInteractionApprovalService(
                app.state.browser_authentication.browser_sessions,
                app.state.browser_authentication.human_authentication,
                private_client,
            )
        )
        uid = "N9E_oxk7dD9t7rR10dj-3"
        consent_page = client.get(f"/auth/mcp-interactions/{uid}")
        assert consent_page.status_code == 200
        csrf_token = re.search(r"csrfToken:'([0-9a-f]{64})'", consent_page.text)
        assert csrf_token is not None
        assert (
            client.post(
                f"/auth/mcp-interactions/{uid}",
                json={"decision": "approve", "csrfToken": csrf_token.group(1)},
                headers={"origin": "https://testserver"},
            ).status_code
            == 204
        )
        call = private_client.record_approval.await_args.kwargs
        assert set(call) == {"interaction_uid", "subject", "decision"}
        assert call == {
            "interaction_uid": uid,
            "subject": dummy_auth_database.authorized_user_id,
            "decision": "approve",
        }

        private_client.reset_mock()
        client.cookies.clear()
        assert client.get(f"/auth/mcp-interactions/{uid}").status_code == 401
        assert (
            client.post(
                f"/auth/mcp-interactions/{uid}",
                json={"decision": "approve"},
                headers={"origin": "https://testserver"},
            ).status_code
            == 401
        )
        private_client.record_approval.assert_not_awaited()
