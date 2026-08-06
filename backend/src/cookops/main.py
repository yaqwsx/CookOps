from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from cookops.config import Settings
from cookops.database import create_database_runtime

ReadinessProbe = Callable[[], Awaitable[bool]]


class ManagedDatabaseRuntime(Protocol):
    async def is_ready(self) -> bool: ...

    async def close(self) -> None: ...


DatabaseRuntimeFactory = Callable[[str], ManagedDatabaseRuntime]


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


def create_app(
    settings: Settings | None = None,
    readiness_probe: ReadinessProbe | None = None,
    database_runtime_factory: DatabaseRuntimeFactory = create_database_runtime,
) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if readiness_probe is not None:
            yield
            return

        runtime = database_runtime_factory(str(app_settings.database_url))
        application.state.database = runtime
        application.state.readiness_probe = runtime.is_ready
        try:
            yield
        finally:
            await runtime.close()

    application = FastAPI(title="CookOps API", lifespan=lifespan)
    application.state.settings = app_settings
    application.state.readiness_probe = readiness_probe or not_ready
    application.include_router(health_router)
    return application


app = create_app()
