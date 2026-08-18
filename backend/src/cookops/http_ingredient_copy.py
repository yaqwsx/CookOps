"""Cookie-authenticated ingredient copy preview and execution."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.browser_sessions import (
    AuthenticatedBrowserSession,
    BrowserSessionService,
)
from cookops.application.ingredient_copy import (
    CopyIngredientToOrganizationCommand,
    CopyIngredientToOrganizationResult,
    IngredientCopyMapping,
    IngredientCopyPreview,
    PreviewIngredientCopyCommand,
    copy_ingredient_to_organization,
    preview_ingredient_copy,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
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


class MappingRequirementResponse(BaseModel):
    kind: str
    source_id: UUID
    seed_key: str | None = None


class IngredientCopyPreviewResponse(BaseModel):
    source_organization_id: UUID
    destination_organization_id: UUID
    source_ingredient_id: UUID
    source_version_id: UUID
    source_name: str
    canonical_unit_id: UUID
    default_store_section_id: UUID | None
    dietary_tag_ids: tuple[UUID, ...]
    precondition_fingerprint: str
    mapping_requirements: tuple[MappingRequirementResponse, ...]


class IngredientCopyMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["canonical_unit", "default_store_section", "dietary_tag"]
    source_id: WireUUID
    destination_id: WireUUID | None


class IngredientCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_organization_id: WireUUID
    ingredient_id: WireUUID
    client_installation_id: WireUUID
    precondition_fingerprint: str
    mappings: tuple[IngredientCopyMappingRequest, ...]
    mutation_id: WireUUID
    client_wall_time: datetime
    logical_operation_id: WireUUID | None = None

    @field_validator("client_installation_id", "mutation_id")
    @classmethod
    def identifiers_must_not_be_zero(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("must not be the zero UUID")
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


class IngredientCopyResultResponse(BaseModel):
    mutation_id: UUID
    source_organization_id: UUID
    destination_organization_id: UUID
    source_ingredient_id: UUID
    destination_ingredient_id: UUID
    source_version_id: UUID
    destination_version_id: UUID
    source_name: str
    canonical_unit_id: UUID
    default_store_section_id: UUID | None
    dietary_tag_ids: tuple[UUID, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool


class IngredientCopyHttpServices:
    def __init__(
        self,
        browser_sessions: BrowserSessionService,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.browser_sessions = browser_sessions
        self.session_factory = session_factory


def create_ingredient_copy_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/organizations", tags=["ingredient copy"])

    @router.get(
        "/{destination_organization_id}/ingredient-copy-preview/{source_organization_id}/{ingredient_id}",
        response_model=IngredientCopyPreviewResponse,
    )
    async def preview(
        destination_organization_id: UUID,
        source_organization_id: UUID,
        ingredient_id: UUID,
        request: Request,
    ) -> IngredientCopyPreviewResponse:
        services = _http_services(request)
        authenticated = await _authenticate(request, settings, services)
        try:
            result = await preview_ingredient_copy(
                services.session_factory,
                ExecutionContext(authenticated.user_id, UUID(int=0)),
                PreviewIngredientCopyCommand(
                    source_organization_id, destination_organization_id, ingredient_id
                ),
            )
        except ApplicationServiceError as error:
            if error.code == "forbidden":
                raise HTTPException(status_code=404, detail={"code": "not_found"}) from error
            if error.code == "stale_precondition":
                raise HTTPException(status_code=409, detail={"code": error.code}) from error
            raise HTTPException(status_code=400, detail={"code": error.code}) from error
        return _response(result)

    @router.post(
        "/{destination_organization_id}/ingredient-copy",
        response_model=IngredientCopyResultResponse,
    )
    async def copy(
        destination_organization_id: UUID,
        body: IngredientCopyRequest,
        request: Request,
    ) -> IngredientCopyResultResponse:
        if (
            settings.browser_origin is None
            or request.headers.get("origin") != settings.browser_origin
        ):
            raise HTTPException(status_code=403, detail={"code": "forbidden"})
        services = _http_services(request)
        authenticated = await _authenticate(request, settings, services)
        try:
            result = await copy_ingredient_to_organization(
                services.session_factory,
                ExecutionContext(authenticated.user_id, body.client_installation_id),
                CopyIngredientToOrganizationCommand(
                    source_organization_id=body.source_organization_id,
                    destination_organization_id=destination_organization_id,
                    ingredient_id=body.ingredient_id,
                    precondition_fingerprint=body.precondition_fingerprint,
                    mappings=tuple(
                        IngredientCopyMapping(
                            kind=item.kind,
                            source_id=item.source_id,
                            destination_id=item.destination_id,
                        )
                        for item in body.mappings
                    ),
                    mutation_id=body.mutation_id,
                    client_wall_time=body.client_wall_time,
                    logical_operation_id=body.logical_operation_id,
                ),
            )
        except ApplicationServiceError as error:
            raise _command_error(error) from error
        return _result_response(result)

    return router


def _http_services(request: Request) -> IngredientCopyHttpServices:
    services = getattr(request.app.state, "ingredient_copy", None)
    if not isinstance(services, IngredientCopyHttpServices):
        raise HTTPException(status_code=503, detail="ingredient copy is not available")
    return services


async def _authenticate(
    request: Request, settings: Settings, services: IngredientCopyHttpServices
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


def _response(value: IngredientCopyPreview) -> IngredientCopyPreviewResponse:
    return IngredientCopyPreviewResponse(
        source_organization_id=value.source_organization_id,
        destination_organization_id=value.destination_organization_id,
        source_ingredient_id=value.source_ingredient_id,
        source_version_id=value.source_version_id,
        source_name=value.source_name,
        canonical_unit_id=value.canonical_unit_id,
        default_store_section_id=value.default_store_section_id,
        dietary_tag_ids=value.dietary_tag_ids,
        precondition_fingerprint=value.precondition_fingerprint,
        mapping_requirements=tuple(
            MappingRequirementResponse(
                kind=item.kind, source_id=item.source_id, seed_key=item.seed_key
            )
            for item in value.mapping_requirements
        ),
    )


def _result_response(value: CopyIngredientToOrganizationResult) -> IngredientCopyResultResponse:
    return IngredientCopyResultResponse(
        mutation_id=value.mutation_id,
        source_organization_id=value.source_organization_id,
        destination_organization_id=value.destination_organization_id,
        source_ingredient_id=value.source_ingredient_id,
        destination_ingredient_id=value.destination_ingredient_id,
        source_version_id=value.source_version_id,
        destination_version_id=value.destination_version_id,
        source_name=value.source_name,
        canonical_unit_id=value.canonical_unit_id,
        default_store_section_id=value.default_store_section_id,
        dietary_tag_ids=value.dietary_tag_ids,
        first_change_sequence=value.first_change_sequence,
        last_change_sequence=value.last_change_sequence,
        replayed=value.replayed,
    )
