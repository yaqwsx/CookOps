import httpx
import pytest

from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.main import create_app


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

    app = create_app(
        Settings(environment=Environment.TEST, human_auth_provider=HumanAuthProvider.DUMMY),
        database_runtime_factory=create_runtime,
    )
    assert received_urls == []

    async with app.router.lifespan_context(app):
        assert received_urls == [str(app.state.settings.database_url)]
        assert await app.state.readiness_probe() is True
        assert runtime.closed is False

    assert runtime.closed is True
