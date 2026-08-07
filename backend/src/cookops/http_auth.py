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
from cookops.application.google_identities import GoogleIdentityProvider
from cookops.application.human_authentication import (
    HumanAuthenticationDenied,
    HumanAuthenticationService,
)
from cookops.config import Environment, Settings


@dataclass(frozen=True, slots=True)
class BrowserAuthenticationServices:
    """Application services required by browser-authentication transports."""

    browser_sessions: BrowserSessionService
    human_authentication: HumanAuthenticationService
    dummy_identities: DummyIdentityProvider | None
    google_identities: GoogleIdentityProvider | None


class DummyIdentityResponse(BaseModel):
    subject: str
    display_name: str


class DummyIdentityListResponse(BaseModel):
    identities: tuple[DummyIdentityResponse, ...]


class SelectDummyIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    subject: str = Field(min_length=1, max_length=255)


class GoogleIdTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id_token: str = Field(min_length=1, max_length=16_384)


class CurrentIdentityResponse(BaseModel):
    id: UUID
    display_name: str
    verified_email: str


class AvailableOrganizationResponse(BaseModel):
    id: UUID
    name: str


class AvailableOrganizationListResponse(BaseModel):
    organizations: tuple[AvailableOrganizationResponse, ...]


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


def _set_session_cookie(response: Response, settings: Settings, secret: str) -> None:
    response.set_cookie(
        settings.browser_session_cookie_name,
        secret,
        httponly=True,
        max_age=settings.browser_session_lifetime_seconds,
        path="/",
        samesite=settings.browser_session_cookie_samesite,
        secure=settings.browser_session_cookie_secure,
    )


def _unauthenticated() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")


def _require_google_token_transport(request: Request, settings: Settings) -> None:
    """Reject production token presentation unless ASGI reports HTTPS.

    The endpoint does not read an untrusted ``X-Forwarded-Proto`` header itself.
    A reverse proxy deployment must instead be explicitly configured to convey a
    trusted HTTPS ASGI scope.  Failing closed protects the Google ID token when
    that deployment boundary is incomplete or misconfigured.
    """

    if settings.environment is Environment.PRODUCTION and request.url.scheme != "https":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="secure transport required",
        )


def create_auth_router(settings: Settings) -> APIRouter:
    """Create the minimal common browser-session endpoints.

    The selected provider creates the same opaque browser session; current-session
    lookup and logout are provider-independent.
    """

    router = APIRouter(prefix="/auth", tags=["authentication"])

    if settings.human_auth_provider == "dummy":

        @router.get("/dummy/identities", response_model=DummyIdentityListResponse)
        async def list_dummy_identities(request: Request) -> DummyIdentityListResponse:
            identities_provider = _services(request).dummy_identities
            if identities_provider is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="authentication is not available",
                )
            identities = await identities_provider.list_selectable()
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
                identities_provider = services.dummy_identities
                if identities_provider is None:
                    raise RuntimeError("dummy identity provider is not configured")
                assertion = await identities_provider.assertion_for_subject(selection.subject)
                completed = await services.human_authentication.complete(assertion)
            except HumanAuthenticationDenied as error:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="authentication denied",
                ) from error
            _set_session_cookie(response, settings, completed.browser_session.secret)
            response.status_code = status.HTTP_204_NO_CONTENT
            return response

    if settings.human_auth_provider == "google":

        @router.post("/google/session", status_code=status.HTTP_204_NO_CONTENT)
        async def create_google_session(
            presentation: GoogleIdTokenRequest,
            request: Request,
            response: Response,
        ) -> Response:
            _require_google_token_transport(request, settings)
            google_provider = _services(request).google_identities
            if google_provider is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="authentication is not available",
                )
            try:
                completed = await google_provider.complete_id_token(presentation.id_token)
            except HumanAuthenticationDenied as error:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="authentication denied",
                ) from error
            _set_session_cookie(response, settings, completed.browser_session.secret)
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


def create_organization_access_router(settings: Settings) -> APIRouter:
    """Create the cookie-authenticated organization-switcher query endpoint."""

    router = APIRouter(prefix="/api/v1", tags=["organizations"])

    @router.get("/organizations", response_model=AvailableOrganizationListResponse)
    async def available_organizations(request: Request) -> AvailableOrganizationListResponse:
        services = _services(request)
        secret = request.cookies.get(settings.browser_session_cookie_name)
        if secret is None:
            raise _unauthenticated()
        session = await services.browser_sessions.authenticate(secret)
        if session is None:
            raise _unauthenticated()
        organizations = await services.human_authentication.available_organizations(session.user_id)
        if organizations is None:
            raise _unauthenticated()
        return AvailableOrganizationListResponse(
            organizations=tuple(
                AvailableOrganizationResponse(
                    id=organization.organization_id, name=organization.name
                )
                for organization in organizations
            )
        )

    return router
