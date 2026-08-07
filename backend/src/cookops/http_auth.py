"""HTTP transport adapter for CookOps browser authentication.

The dummy routes are deliberately mounted only when trusted server configuration
selects the local development provider.  They convert a selection of an existing
dummy identity to the same completed authentication and opaque cookie used by the
future Google adapter; they do not bypass authorization.
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.dummy_identities import DummyIdentityProvider
from cookops.application.human_authentication import (
    HumanAuthenticationDenied,
    HumanAuthenticationService,
)
from cookops.config import Settings


@dataclass(frozen=True, slots=True)
class BrowserAuthenticationServices:
    """Application services required by browser-authentication transports."""

    browser_sessions: BrowserSessionService
    human_authentication: HumanAuthenticationService
    dummy_identities: DummyIdentityProvider


class DummyIdentityResponse(BaseModel):
    subject: str
    display_name: str


class DummyIdentityListResponse(BaseModel):
    identities: tuple[DummyIdentityResponse, ...]


class SelectDummyIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    subject: str = Field(min_length=1, max_length=255)


class CurrentIdentityResponse(BaseModel):
    id: UUID
    display_name: str
    verified_email: str


def _services(request: Request) -> BrowserAuthenticationServices:
    services = getattr(request.app.state, "browser_authentication", None)
    if not isinstance(services, BrowserAuthenticationServices):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication is not available",
        )
    return services


def _delete_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.browser_session_cookie_name,
        httponly=True,
        path="/",
        samesite=settings.browser_session_cookie_samesite,
        secure=settings.browser_session_cookie_secure,
    )


def _unauthenticated() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")


def create_auth_router(settings: Settings) -> APIRouter:
    """Create the minimal common browser-session endpoints.

    Development-only identity selection is included only for the selected dummy
    adapter.  Google will later add a provider-specific completion endpoint while
    retaining the current-session and logout routes unchanged.
    """

    router = APIRouter(prefix="/auth", tags=["authentication"])

    if settings.human_auth_provider == "dummy":

        @router.get("/dummy/identities", response_model=DummyIdentityListResponse)
        async def list_dummy_identities(request: Request) -> DummyIdentityListResponse:
            identities = await _services(request).dummy_identities.list_selectable()
            return DummyIdentityListResponse(
                identities=tuple(
                    DummyIdentityResponse(
                        subject=identity.subject, display_name=identity.display_name
                    )
                    for identity in identities
                )
            )

        @router.post("/dummy/session", status_code=status.HTTP_204_NO_CONTENT)
        async def create_dummy_session(
            selection: SelectDummyIdentityRequest,
            request: Request,
            response: Response,
        ) -> Response:
            services = _services(request)
            try:
                assertion = await services.dummy_identities.assertion_for_subject(selection.subject)
                completed = await services.human_authentication.complete(assertion)
            except HumanAuthenticationDenied as error:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="authentication denied",
                ) from error
            response.set_cookie(
                settings.browser_session_cookie_name,
                completed.browser_session.secret,
                httponly=True,
                max_age=settings.browser_session_lifetime_seconds,
                path="/",
                samesite=settings.browser_session_cookie_samesite,
                secure=settings.browser_session_cookie_secure,
            )
            response.status_code = status.HTTP_204_NO_CONTENT
            return response

    @router.get("/session", response_model=CurrentIdentityResponse)
    async def current_identity(request: Request) -> CurrentIdentityResponse:
        services = _services(request)
        secret = request.cookies.get(settings.browser_session_cookie_name)
        if secret is None:
            raise _unauthenticated()
        session = await services.browser_sessions.authenticate(secret)
        if session is None:
            raise _unauthenticated()
        identity = await services.human_authentication.current_identity(session.user_id)
        if identity is None:
            raise _unauthenticated()
        return CurrentIdentityResponse(
            id=identity.user_id,
            display_name=identity.display_name,
            verified_email=identity.verified_email,
        )

    @router.post("/session/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request, response: Response) -> Response:
        services = _services(request)
        secret = request.cookies.get(settings.browser_session_cookie_name)
        if secret is not None:
            session = await services.browser_sessions.authenticate(secret)
            if session is not None:
                await services.browser_sessions.logout(secret, user_id=session.user_id)
        _delete_cookie(response, settings)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    return router
