"""Cookie-authenticated ingredient copy preview."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.ingredient_copy import (
    IngredientCopyPreview,
    PreviewIngredientCopyCommand,
    preview_ingredient_copy,
)
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.config import Settings


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
        services = getattr(request.app.state, "ingredient_copy", None)
        if not isinstance(services, IngredientCopyHttpServices):
            raise HTTPException(status_code=503, detail="ingredient copy is not available")
        secret = request.cookies.get(settings.browser_session_cookie_name)
        if secret is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        authenticated = await services.browser_sessions.authenticate(secret)
        if authenticated is None:
            raise HTTPException(status_code=401, detail="not authenticated")
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

    return router


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
