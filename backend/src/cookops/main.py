from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.dummy_identities import DummyIdentityProvider
from cookops.application.human_authentication import HumanAuthenticationService
from cookops.config import Settings
from cookops.database import create_database_runtime
from cookops.http_auth import BrowserAuthenticationServices, create_auth_router

ReadinessProbe = Callable[[], Awaitable[bool]]


class ManagedDatabaseRuntime(Protocol):
    async def is_ready(self) -> bool: ...

    async def close(self) -> None: ...


DatabaseRuntimeFactory = Callable[[str], ManagedDatabaseRuntime]
BrowserAuthenticationFactory = Callable[[Settings, object], BrowserAuthenticationServices]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse()


@health_router.get("/ready", response_model=HealthResponse)
async def ready(request: Request) -> HealthResponse:
    if not await request.app.state.readiness_probe():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="application is not ready",
        )
    return HealthResponse()


async def not_ready() -> bool:
    return False


def create_browser_authentication_services(
    settings: Settings, session_factory: object
) -> BrowserAuthenticationServices:
    """Wire provider-independent sessions to the configured trusted adapter.

    The erased session-factory type keeps the application factory's test seam
    narrow; concrete services validate their actual SQLAlchemy dependency.
    """

    from sqlalchemy.ext.asyncio import async_sessionmaker

    if not isinstance(session_factory, async_sessionmaker):
        raise TypeError("database runtime must expose an async SQLAlchemy session factory")
    browser_sessions = BrowserSessionService(
        session_factory,
        encoded_hmac_key=settings.resolved_browser_session_hmac_key,
    )
    return BrowserAuthenticationServices(
        browser_sessions=browser_sessions,
        human_authentication=HumanAuthenticationService(
            session_factory,
            browser_sessions,
            session_lifetime=timedelta(seconds=settings.browser_session_lifetime_seconds),
        ),
        dummy_identities=DummyIdentityProvider(session_factory),
    )


def create_app(
    settings: Settings | None = None,
    readiness_probe: ReadinessProbe | None = None,
    database_runtime_factory: DatabaseRuntimeFactory = create_database_runtime,
    browser_authentication_factory: BrowserAuthenticationFactory = (
        create_browser_authentication_services
    ),
) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if readiness_probe is not None:
            yield
            return

        runtime = database_runtime_factory(str(app_settings.database_url))
        application.state.database = runtime
        session_factory = getattr(runtime, "session_factory", None)
        application.state.browser_authentication = browser_authentication_factory(
            app_settings, session_factory
        )
        application.state.readiness_probe = runtime.is_ready
        try:
            yield
        finally:
            await runtime.close()

    application = FastAPI(title="CookOps API", lifespan=lifespan)
    application.state.settings = app_settings
    application.state.readiness_probe = readiness_probe or not_ready
    application.include_router(health_router)
    application.include_router(create_auth_router(app_settings))
    return application


app = create_app()
