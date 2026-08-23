"""Cookie-authenticated online organization membership administration."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.memberships import (
    InviteMemberCommand,
    MembershipMutationResult,
    MembershipSummary,
    OrganizationAdminRoleCommand,
    RemoveMemberCommand,
    assign_organization_admin,
    invite_member,
    list_members,
    remove_member,
    revoke_organization_admin,
)
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.config import Settings


class InviteMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mutation_id: UUID
    client_installation_id: UUID
    client_wall_time: datetime
    invited_email: str = Field(min_length=1, max_length=320)

    @field_validator("client_wall_time")
    @classmethod
    def wall_time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value


class RemoveMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mutation_id: UUID
    client_installation_id: UUID
    client_wall_time: datetime

    @field_validator("client_wall_time")
    @classmethod
    def wall_time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value


class MembershipResponse(BaseModel):
    id: UUID
    invited_email: str
    role: str
    state: str


class MembershipListResponse(BaseModel):
    memberships: tuple[MembershipResponse, ...]


class MembershipMutationResponse(BaseModel):
    mutation_id: UUID
    membership_id: UUID
    state: str
    replayed: bool
    role: str | None = None


class MembershipHttpServices:
    def __init__(
        self,
        browser_sessions: BrowserSessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.browser_sessions = browser_sessions
        self.session_factory = session_factory


def _services(request: Request) -> MembershipHttpServices:
    services = getattr(request.app.state, "memberships", None)
    if not isinstance(services, MembershipHttpServices):
        raise HTTPException(status_code=503, detail="memberships are not available")
    return services


async def _actor_id(request: Request, settings: Settings, services: MembershipHttpServices) -> UUID:
    secret = request.cookies.get(settings.browser_session_cookie_name)
    if secret is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    authenticated = await services.browser_sessions.authenticate(secret)
    if authenticated is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return authenticated.user_id


def _context(actor_user_id: UUID, installation_id: UUID) -> ExecutionContext:
    return ExecutionContext(actor_user_id=actor_user_id, client_installation_id=installation_id)


def _membership_response(value: MembershipSummary) -> MembershipResponse:
    return MembershipResponse(
        id=value.id,
        invited_email=value.invited_email,
        role=value.role,
        state=value.state,
    )


def _mutation_response(value: MembershipMutationResult) -> MembershipMutationResponse:
    return MembershipMutationResponse(
        mutation_id=value.mutation_id,
        membership_id=value.membership_id,
        state=value.state,
        replayed=value.replayed,
    )


def _error(error: ApplicationServiceError) -> HTTPException:
    if error.code == "idempotency_mismatch":
        return HTTPException(status_code=409, detail={"code": error.code})
    if error.code == "forbidden":
        # Membership identifiers outside the current administrator's scope are
        # never distinguishable.
        return HTTPException(status_code=404, detail={"code": "not_found"})
    if error.code == "validation_failed":
        return HTTPException(status_code=422, detail={"code": error.code})
    if error.code == "client_time_too_far_ahead":
        return HTTPException(status_code=422, detail={"code": error.code})
    return HTTPException(status_code=400, detail={"code": error.code})


def create_memberships_router(settings: Settings) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/organizations/{organization_id}/members",
        tags=["organization membership"],
    )

    @router.get("", response_model=MembershipListResponse)
    async def members(organization_id: UUID, request: Request) -> MembershipListResponse:
        services = _services(request)
        actor_user_id = await _actor_id(request, settings, services)
        try:
            values = await list_members(services.session_factory, actor_user_id, organization_id)
        except ApplicationServiceError as error:
            raise _error(error) from error
        return MembershipListResponse(
            memberships=tuple(_membership_response(value) for value in values)
        )

    @router.post("/invitations", response_model=MembershipMutationResponse)
    async def invite(
        organization_id: UUID, body: InviteMemberRequest, request: Request
    ) -> MembershipMutationResponse:
        services = _services(request)
        actor_user_id = await _actor_id(request, settings, services)
        try:
            result = await invite_member(
                services.session_factory,
                _context(actor_user_id, body.client_installation_id),
                InviteMemberCommand(
                    mutation_id=body.mutation_id,
                    organization_id=organization_id,
                    invited_email=body.invited_email,
                    client_wall_time=body.client_wall_time,
                ),
            )
        except ApplicationServiceError as error:
            raise _error(error) from error
        return _mutation_response(result)

    @router.post("/{membership_id}/remove", response_model=MembershipMutationResponse)
    async def remove(
        organization_id: UUID,
        membership_id: UUID,
        body: RemoveMemberRequest,
        request: Request,
    ) -> MembershipMutationResponse:
        services = _services(request)
        actor_user_id = await _actor_id(request, settings, services)
        try:
            result = await remove_member(
                services.session_factory,
                _context(actor_user_id, body.client_installation_id),
                RemoveMemberCommand(
                    mutation_id=body.mutation_id,
                    organization_id=organization_id,
                    membership_id=membership_id,
                    client_wall_time=body.client_wall_time,
                ),
            )
        except ApplicationServiceError as error:
            raise _error(error) from error
        return _mutation_response(result)

    async def _change_role(
        organization_id: UUID,
        membership_id: UUID,
        body: RemoveMemberRequest,
        request: Request,
        assign: bool,
    ) -> MembershipMutationResponse:
        services = _services(request)
        actor_user_id = await _actor_id(request, settings, services)
        command = OrganizationAdminRoleCommand(
            mutation_id=body.mutation_id,
            organization_id=organization_id,
            membership_id=membership_id,
            client_wall_time=body.client_wall_time,
        )
        try:
            result = await (assign_organization_admin if assign else revoke_organization_admin)(
                services.session_factory,
                _context(actor_user_id, body.client_installation_id),
                command,
            )
        except ApplicationServiceError as error:
            raise _error(error) from error
        return MembershipMutationResponse(
            mutation_id=result.mutation_id,
            membership_id=result.membership_id,
            state="active",
            replayed=result.replayed,
            role=result.role,
        )

    @router.post(
        "/{membership_id}/assign-organization-admin", response_model=MembershipMutationResponse
    )
    async def assign_role(
        organization_id: UUID,
        membership_id: UUID,
        body: RemoveMemberRequest,
        request: Request,
    ) -> MembershipMutationResponse:
        return await _change_role(organization_id, membership_id, body, request, True)

    @router.post(
        "/{membership_id}/revoke-organization-admin", response_model=MembershipMutationResponse
    )
    async def revoke_role(
        organization_id: UUID,
        membership_id: UUID,
        body: RemoveMemberRequest,
        request: Request,
    ) -> MembershipMutationResponse:
        return await _change_role(organization_id, membership_id, body, request, False)

    return router
