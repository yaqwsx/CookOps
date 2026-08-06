from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from cookops.config import Settings


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
    readiness_probe: Callable[[], Awaitable[bool]] = not_ready,
) -> FastAPI:
    app_settings = settings or Settings()
    application = FastAPI(title="CookOps API")
    application.state.settings = app_settings
    application.state.readiness_probe = readiness_probe
    application.include_router(health_router)
    return application


app = create_app()
