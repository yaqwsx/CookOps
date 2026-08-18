"""Cookie-authenticated recipe copy command adapter."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.browser_sessions import (
    AuthenticatedBrowserSession,
    BrowserSessionService,
)
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.application.recipe_copy import (
    CopyRecipeToOrganizationCommand,
    CopyRecipeToOrganizationResult,
    copy_recipe_to_organization,
)
from cookops.config import Settings


def _wire_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("must be a UUID string")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError("must be a UUID string") from error


WireUUID = Annotated[UUID, BeforeValidator(_wire_uuid)]


class RecipeCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_organization_id: WireUUID
    source_recipe_id: WireUUID
    source_current_recipe_version_id: WireUUID
    destination_recipe_id: WireUUID
    destination_recipe_version_id: WireUUID
    ingredient_version_mappings: dict[WireUUID, WireUUID]
    recipe_tag_mappings: dict[WireUUID, WireUUID]
    scaling_unit_mappings: dict[WireUUID, WireUUID]
    preferred_display_unit_mappings: dict[WireUUID, WireUUID]
    client_installation_id: WireUUID
    mutation_id: WireUUID
    client_wall_time: datetime
    logical_operation_id: WireUUID | None = None

    @field_validator(
        "source_organization_id",
        "source_recipe_id",
        "source_current_recipe_version_id",
        "destination_recipe_id",
        "destination_recipe_version_id",
        "client_installation_id",
        "mutation_id",
        "logical_operation_id",
        mode="after",
    )
    @classmethod
    def identifiers_must_not_be_zero(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("must not be the zero UUID")
        return value

    @field_validator(
        "ingredient_version_mappings",
        "recipe_tag_mappings",
        "scaling_unit_mappings",
        "preferred_display_unit_mappings",
    )
    @classmethod
    def mappings_must_not_contain_zero(cls, value: dict[UUID, UUID]) -> dict[UUID, UUID]:
        if any(item.int == 0 for pair in value.items() for item in pair):
            raise ValueError("must not contain the zero UUID")
        return value

    @field_validator("client_wall_time", mode="before")
    @classmethod
    def client_wall_time_must_be_a_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be an ISO timestamp string")
        return value

    @field_validator("client_wall_time")
    @classmethod
    def client_wall_time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value


class RecipeCopyResultResponse(BaseModel):
    mutation_id: UUID
    source_organization_id: UUID
    destination_organization_id: UUID
    source_recipe_id: UUID
    destination_recipe_id: UUID
    source_recipe_version_id: UUID
    destination_recipe_version_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool


class RecipeCopyHttpServices:
    def __init__(
        self,
        browser_sessions: BrowserSessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.browser_sessions = browser_sessions
        self.session_factory = session_factory


def create_recipe_copy_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/organizations", tags=["recipe copy"])

    @router.post(
        "/{destination_organization_id}/recipe-copy",
        response_model=RecipeCopyResultResponse,
    )
    async def copy(
        destination_organization_id: UUID,
        body: RecipeCopyRequest,
        request: Request,
    ) -> RecipeCopyResultResponse:
        if destination_organization_id.int == 0:
            raise HTTPException(status_code=422, detail="invalid destination organization")
        if (
            settings.browser_origin is None
            or request.headers.get("origin") != settings.browser_origin
        ):
            raise HTTPException(status_code=403, detail={"code": "forbidden"})
        services = _http_services(request)
        authenticated = await _authenticate(request, settings, services)
        try:
            result = await copy_recipe_to_organization(
                services.session_factory,
                ExecutionContext(authenticated.user_id, body.client_installation_id),
                CopyRecipeToOrganizationCommand(
                    source_organization_id=body.source_organization_id,
                    destination_organization_id=destination_organization_id,
                    source_recipe_id=body.source_recipe_id,
                    source_current_recipe_version_id=body.source_current_recipe_version_id,
                    destination_recipe_id=body.destination_recipe_id,
                    destination_recipe_version_id=body.destination_recipe_version_id,
                    ingredient_version_mappings=body.ingredient_version_mappings,
                    recipe_tag_mappings=body.recipe_tag_mappings,
                    scaling_unit_mappings=body.scaling_unit_mappings,
                    preferred_display_unit_mappings=body.preferred_display_unit_mappings,
                    mutation_id=body.mutation_id,
                    client_wall_time=body.client_wall_time,
                    logical_operation_id=body.logical_operation_id,
                ),
            )
        except ApplicationServiceError as error:
            raise _command_error(error) from error
        return _result_response(result)

    return router


def _http_services(request: Request) -> RecipeCopyHttpServices:
    services = getattr(request.app.state, "recipe_copy", None)
    if not isinstance(services, RecipeCopyHttpServices):
        raise HTTPException(status_code=503, detail="recipe copy is not available")
    return services


async def _authenticate(
    request: Request, settings: Settings, services: RecipeCopyHttpServices
) -> AuthenticatedBrowserSession:
    secret = request.cookies.get(settings.browser_session_cookie_name)
    if secret is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    authenticated = await services.browser_sessions.authenticate(secret)
    if authenticated is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return authenticated


def _command_error(error: ApplicationServiceError) -> HTTPException:
    if error.code == "forbidden":
        return HTTPException(status_code=404, detail={"code": "not_found"})
    status_code = (
        422
        if error.code == "validation_failed"
        else 409
        if error.code in {"stale_precondition", "idempotency_mismatch"}
        else 400
    )
    return HTTPException(
        status_code=status_code,
        detail={
            "code": error.code,
            "field_violations": [
                {"path": violation.path, "code": violation.code}
                for violation in error.field_violations
            ],
            "retry_same_identity": error.retry_same_identity,
        },
    )


def _result_response(value: CopyRecipeToOrganizationResult) -> RecipeCopyResultResponse:
    return RecipeCopyResultResponse(
        mutation_id=value.mutation_id,
        source_organization_id=value.source_organization_id,
        destination_organization_id=value.destination_organization_id,
        source_recipe_id=value.source_recipe_id,
        destination_recipe_id=value.destination_recipe_id,
        source_recipe_version_id=value.source_recipe_version_id,
        destination_recipe_version_id=value.destination_recipe_version_id,
        first_change_sequence=value.first_change_sequence,
        last_change_sequence=value.last_change_sequence,
        replayed=value.replayed,
    )
