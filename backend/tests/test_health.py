from contextlib import AsyncExitStack
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken

from cookops.application.events import EventSummary
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.http_auth import BrowserAuthenticationServices
from cookops.main import create_app

KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"
DETAILS_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWU"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_liveness_endpoint() -> None:
    app = create_app(
        Settings(environment=Environment.TEST, human_auth_provider=HumanAuthProvider.DUMMY)
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == httpx.codes.OK
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_readiness_endpoint_reports_successful_probe() -> None:
    async def ready() -> bool:
        return True

    app = create_app(
        Settings(environment=Environment.TEST, human_auth_provider=HumanAuthProvider.DUMMY),
        readiness_probe=ready,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == httpx.codes.OK
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_readiness_endpoint_is_safe_by_default() -> None:
    app = create_app(
        Settings(environment=Environment.TEST, human_auth_provider=HumanAuthProvider.DUMMY)
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == httpx.codes.SERVICE_UNAVAILABLE
    assert response.json() == {"detail": "application is not ready"}


@pytest.mark.anyio
async def test_mcp_is_mounted_only_after_runtime_start_and_requires_bearer_auth() -> None:
    class FakeRuntime:
        session_factory = object()

        async def is_ready(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    runtime = FakeRuntime()

    def create_browser_authentication(
        _settings: Settings, _session_factory: object
    ) -> BrowserAuthenticationServices:
        return MagicMock(spec=BrowserAuthenticationServices)

    app = create_app(
        Settings(
            environment=Environment.TEST,
            human_auth_provider=HumanAuthProvider.DUMMY,
            oauth_issuer="https://cookops.example/oauth",
            mcp_resource="https://cookops.example/mcp",
            oauth_introspection_url="http://oauth-server:3000/oauth/introspect",
            oauth_resource_server_secret="secret",
        ),
        database_runtime_factory=lambda _database_url: runtime,
        browser_authentication_factory=create_browser_authentication,
    )
    assert "/mcp" not in {getattr(route, "path", None) for route in app.routes}
    async with app.router.lifespan_context(app):
        assert "" in {getattr(route, "path", None) for route in app.routes}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://cookops.example"
        ) as client:
            response = await client.get("/mcp")
        assert response.status_code == httpx.codes.UNAUTHORIZED


@pytest.mark.anyio
async def test_mounted_mcp_runs_child_lifespan_and_serves_a_protected_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id, organization_id, event_id = uuid4(), uuid4(), uuid4()

    async def verify_token(_self: object, _token: str) -> AccessToken:
        return AccessToken(
            token="verified",
            client_id="client",
            scopes=["cookops:mcp"],
            expires_at=2_000_000_000,
            resource="https://cookops.example/mcp",
            subject=str(actor_id),
        )

    async def authorized_summary(*_args: object, **kwargs: object) -> EventSummary:
        assert kwargs["actor_user_id"] == actor_id
        assert kwargs["organization_id"] == organization_id
        assert kwargs["event_id"] == event_id
        return EventSummary(
            id=event_id,
            organization_id=organization_id,
            name="Harvest",
            start_date=date(2026, 8, 20),
            end_date=date(2026, 8, 21),
            base_expected_attendance=12,
            budget_amount=Decimal("10.50"),
            currency="CZK",
            lifecycle="active",
            archived_at=None,
            current_archive_snapshot_id=None,
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )

    monkeypatch.setattr("cookops.mcp_resource.McpIntrospectionVerifier.verify_token", verify_token)
    monkeypatch.setattr("cookops.mcp_resource.get_event_summary", authorized_summary)

    class FakeRuntime:
        session_factory = object()

        async def is_ready(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    app = create_app(
        Settings(
            environment=Environment.TEST,
            human_auth_provider=HumanAuthProvider.DUMMY,
            oauth_issuer="https://cookops.example/oauth",
            mcp_resource="https://cookops.example/mcp",
            oauth_introspection_url="http://oauth-server:3000/oauth/introspect",
            oauth_resource_server_secret="secret",
        ),
        database_runtime_factory=lambda _database_url: FakeRuntime(),
        browser_authentication_factory=lambda _settings, _session_factory: MagicMock(
            spec=BrowserAuthenticationServices
        ),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=transport,
                    base_url="https://cookops.example",
                    headers={"Authorization": "Bearer verified"},
                )
            )
            streams = await stack.enter_async_context(
                streamable_http_client("https://cookops.example/mcp", http_client=http)
            )
            client = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await client.initialize()
            result = await client.call_tool(
                "get_event_summary",
                {"organization_id": str(organization_id), "event_id": str(event_id)},
            )
            assert result.isError is False
            assert result.structuredContent["name"] == "Harvest"


def test_private_oauth_bridge_routes_require_complete_configuration() -> None:
    default_routes = set(
        create_app(
            Settings(environment=Environment.TEST, human_auth_provider=HumanAuthProvider.DUMMY)
        ).openapi()["paths"]
    )
    assert "/auth/mcp-interactions/{interaction_uid}" not in default_routes
    assert "/auth/mcp-grants" not in default_routes

    grants_only_routes = set(
        create_app(
            Settings(
                environment=Environment.TEST,
                human_auth_provider=HumanAuthProvider.DUMMY,
                oauth_grants_api_credential_base64url=KEY,
            )
        ).openapi()["paths"]
    )
    assert "/auth/mcp-interactions/{interaction_uid}" not in grants_only_routes
    assert "/auth/mcp-grants" not in grants_only_routes

    configured_routes = set(
        create_app(
            Settings(
                environment=Environment.TEST,
                human_auth_provider=HumanAuthProvider.DUMMY,
                browser_origin="https://test",
                oauth_interaction_origin="https://test",
                oauth_interaction_details_api_credential_base64url=DETAILS_KEY,
                oauth_interaction_approval_api_credential_base64url=KEY,
                oauth_grants_api_credential_base64url="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWc",
            )
        ).openapi()["paths"]
    )
    assert "/auth/mcp-interactions/{interaction_uid}" in configured_routes
    assert "/auth/mcp-grants" in configured_routes


@pytest.mark.anyio
async def test_database_runtime_is_created_and_closed_with_application_lifespan() -> None:
    class FakeRuntime:
        closed = False

        async def is_ready(self) -> bool:
            return True

        async def close(self) -> None:
            self.closed = True

    runtime = FakeRuntime()
    received_urls: list[str] = []

    def create_runtime(database_url: str) -> FakeRuntime:
        received_urls.append(database_url)
        return runtime

    def create_browser_authentication(
        _settings: Settings, _session_factory: object
    ) -> BrowserAuthenticationServices:
        return MagicMock(spec=BrowserAuthenticationServices)

    app = create_app(
        Settings(environment=Environment.TEST, human_auth_provider=HumanAuthProvider.DUMMY),
        database_runtime_factory=create_runtime,
        browser_authentication_factory=create_browser_authentication,
    )
    assert received_urls == []

    async with app.router.lifespan_context(app):
        assert received_urls == [str(app.state.settings.database_url)]
        assert await app.state.readiness_probe() is True
        assert runtime.closed is False

    assert runtime.closed is True
