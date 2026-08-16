"""HTTP transport adapter for CookOps browser authentication.

The dummy routes are deliberately mounted only when trusted server configuration
selects the local development provider.  They convert a selection of an existing
dummy identity to the same completed authentication and opaque cookie used by the
future Google adapter; they do not bypass authorization.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.dummy_identities import DummyIdentityProvider
from cookops.application.google_identities import GoogleIdentityProvider
from cookops.application.human_authentication import (
    HumanAuthenticationDenied,
    HumanAuthenticationService,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    CreateOrganizationCommand,
    ExecutionContext,
    SetOrganizationLifecycleCommand,
    change_organization_lifecycle,
    create_organization,
    list_organizations_for_system_admin,
)
from cookops.config import Environment, Settings
from cookops.persistence.models import SystemRoleAssignment, User


@dataclass(frozen=True, slots=True)
class BrowserAuthenticationServices:
    """Application services required by browser-authentication transports."""

    browser_sessions: BrowserSessionService
    human_authentication: HumanAuthenticationService
    dummy_identities: DummyIdentityProvider | None
    google_identities: GoogleIdentityProvider | None


@dataclass(frozen=True, slots=True)
class OrganizationAdministrationHttpServices:
    browser_sessions: BrowserSessionService
    session_factory: async_sessionmaker[AsyncSession]


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


class CreateOrganizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mutation_id: UUID
    organization_id: UUID
    client_installation_id: UUID
    client_wall_time: datetime
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    default_currency: str = Field(default="CZK", min_length=3, max_length=3)

    @field_validator("name")
    @classmethod
    def canonical_name(cls, value: str) -> str:
        if not value.strip() or len(value.strip()) > 200:
            raise ValueError("must be nonblank and at most 200 characters")
        return value

    @field_validator("default_currency")
    @classmethod
    def iso_currency(cls, value: str) -> str:
        from iso4217 import Currency

        if value.strip().upper() not in Currency.__members__:
            raise ValueError("must be an ISO 4217 currency code")
        return value

    @field_validator("client_wall_time")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value


class CreatedOrganizationResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    default_currency: str


class SystemOrganizationResponse(CreatedOrganizationResponse):
    retired_at: datetime | None
    retired_by_user_id: UUID | None


class OrganizationLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["retire", "restore"]
    mutation_id: UUID
    client_installation_id: UUID
    client_wall_time: datetime

    @field_validator("client_wall_time")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value


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


def create_organization_administration_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system", tags=["system administration"])

    def services(request: Request) -> OrganizationAdministrationHttpServices:
        value = getattr(request.app.state, "organization_administration", None)
        if not isinstance(value, OrganizationAdministrationHttpServices):
            raise HTTPException(
                status_code=503, detail="organization administration is not available"
            )
        return value

    async def actor(request: Request, value: OrganizationAdministrationHttpServices) -> UUID:
        secret = request.cookies.get(settings.browser_session_cookie_name)
        authenticated = await value.browser_sessions.authenticate(secret) if secret else None
        if authenticated is None:
            raise _unauthenticated()
        return authenticated.user_id

    async def require_system_admin(
        actor_id: UUID, value: OrganizationAdministrationHttpServices
    ) -> None:
        async with value.session_factory() as session:
            allowed = await session.scalar(
                select(SystemRoleAssignment.id)
                .join(User, User.id == SystemRoleAssignment.user_id)
                .where(
                    SystemRoleAssignment.user_id == actor_id,
                    SystemRoleAssignment.role == "system_admin",
                    SystemRoleAssignment.revoked_at.is_(None),
                    User.disabled_at.is_(None),
                )
            )
        if allowed is None:
            raise HTTPException(status_code=403, detail={"code": "forbidden"})

    def application_error(error: ApplicationServiceError) -> HTTPException:
        if error.code == "forbidden":
            return HTTPException(status_code=403, detail={"code": error.code})
        if error.code == "validation_failed":
            return HTTPException(
                status_code=422,
                detail={
                    "code": error.code,
                    "field_violations": [
                        {"path": item.path, "code": item.code}
                        for item in error.field_violations
                    ],
                },
            )
        return HTTPException(status_code=409, detail={"code": error.code})

    @router.get("/organizations/access", status_code=204)
    async def access(request: Request) -> Response:
        value = services(request)
        actor_id = await actor(request, value)
        await require_system_admin(actor_id, value)
        return Response(status_code=204)

    @router.post(
        "/organizations", response_model=CreatedOrganizationResponse, status_code=201
    )
    async def create(
        body: CreateOrganizationRequest, request: Request
    ) -> CreatedOrganizationResponse:
        value = services(request)
        actor_id = await actor(request, value)
        try:
            result = await create_organization(
                value.session_factory,
                ExecutionContext(actor_id, body.client_installation_id),
                CreateOrganizationCommand(
                    mutation_id=body.mutation_id,
                    organization_id=body.organization_id,
                    name=body.name,
                    description=body.description,
                    default_currency=body.default_currency,
                    client_wall_time=body.client_wall_time,
                ),
            )
        except ApplicationServiceError as error:
            if error.code == "forbidden":
                raise HTTPException(status_code=403, detail={"code": "forbidden"}) from error
            if error.code == "validation_failed":
                raise HTTPException(
                    status_code=422,
                    detail={"code": error.code, "field_violations": [
                        {"path": item.path, "code": item.code} for item in error.field_violations
                    ]},
                ) from error
            raise HTTPException(status_code=409, detail={"code": error.code}) from error
        return CreatedOrganizationResponse(
            id=result.organization_id,
            name=result.name,
            description=result.description,
            default_currency=result.default_currency,
        )

    @router.get("/organizations", response_model=tuple[SystemOrganizationResponse, ...])
    async def list_all(request: Request) -> tuple[SystemOrganizationResponse, ...]:
        value = services(request)
        actor_id = await actor(request, value)
        try:
            result = await list_organizations_for_system_admin(
                value.session_factory, ExecutionContext(actor_id, UUID(int=0))
            )
        except ApplicationServiceError as error:
            raise application_error(error) from error
        return tuple(
            SystemOrganizationResponse(
                id=item.organization_id, name=item.name, description=item.description,
                default_currency=item.default_currency, retired_at=item.retired_at,
                retired_by_user_id=item.retired_by_user_id,
            ) for item in result
        )

    @router.post(
        "/organizations/{organization_id}/lifecycle", response_model=SystemOrganizationResponse
    )
    async def lifecycle(
        organization_id: UUID, body: OrganizationLifecycleRequest, request: Request
    ) -> SystemOrganizationResponse:
        value = services(request)
        actor_id = await actor(request, value)
        try:
            result = await change_organization_lifecycle(
                value.session_factory,
                ExecutionContext(actor_id, body.client_installation_id),
                SetOrganizationLifecycleCommand(
                    mutation_id=body.mutation_id,
                    organization_id=organization_id,
                    operation=body.operation,
                    client_wall_time=body.client_wall_time,
                ),
            )
        except ApplicationServiceError as error:
            raise application_error(error) from error
        return SystemOrganizationResponse(
            id=result.organization_id, name=result.name, description=result.description,
            default_currency=result.default_currency, retired_at=result.retired_at,
            retired_by_user_id=result.retired_by_user_id,
        )

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
