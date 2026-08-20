from unittest.mock import MagicMock

import httpx
import pytest

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


def test_mcp_is_unmounted_even_when_its_private_verifier_is_configured() -> None:
    app = create_app(
        Settings(
            environment=Environment.TEST,
            human_auth_provider=HumanAuthProvider.DUMMY,
            oauth_issuer="https://cookops.example/oauth",
            mcp_resource="https://cookops.example/mcp",
            oauth_introspection_url="http://oauth-server:3000/oauth/introspect",
            oauth_resource_server_secret="secret",
        )
    )
    assert "/mcp" not in {getattr(route, "path", None) for route in app.routes}


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
