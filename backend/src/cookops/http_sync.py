"""Cookie-authenticated read adapter for the organization synchronization feed."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.synchronization import (
    MAX_TRANSACTION_GROUPS_PER_PULL,
    InvalidSyncCursor,
    PullRequest,
    PullResult,
    SynchronizationQueryService,
    SyncQueryDenied,
)
from cookops.config import Settings


@dataclass(frozen=True, slots=True)
class SynchronizationHttpServices:
    browser_sessions: BrowserSessionService
    synchronization: SynchronizationQueryService


class PullChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    transaction_group_limit: int = Field(
        default=MAX_TRANSACTION_GROUPS_PER_PULL,
        ge=1,
        le=MAX_TRANSACTION_GROUPS_PER_PULL,
    )


class SyncRecordResponse(BaseModel):
    organization_id: UUID
    sequence: int
    entity_id: UUID
    entity_kind: str
    operation: str
    payload: dict[str, object]


class SyncTransactionGroupResponse(BaseModel):
    mutation_id: UUID
    first_sequence: int
    last_sequence: int
    records: tuple[SyncRecordResponse, ...]


class PullChangesResponse(BaseModel):
    status: Literal["ok", "bootstrap_required"]
    sync_schema_version: Literal[1]
    server_time: datetime
    next_cursor: str | None
    transaction_groups: tuple[SyncTransactionGroupResponse, ...]
    oldest_available_at: datetime | None


def _services(request: Request) -> SynchronizationHttpServices:
    services = getattr(request.app.state, "synchronization", None)
    if not isinstance(services, SynchronizationHttpServices):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="synchronization is not available",
        )
    return services


def _result_response(result: PullResult) -> PullChangesResponse:
    return PullChangesResponse(
        status=result.status,
        sync_schema_version=result.sync_schema_version,
        server_time=result.server_time,
        next_cursor=result.next_cursor,
        transaction_groups=tuple(
            SyncTransactionGroupResponse(
                mutation_id=group.mutation_id,
                first_sequence=group.first_sequence,
                last_sequence=group.last_sequence,
                records=tuple(
                    SyncRecordResponse(
                        organization_id=record.organization_id,
                        sequence=record.sequence,
                        entity_id=record.entity_id,
                        entity_kind=record.entity_kind,
                        operation=record.operation,
                        payload=record.payload,
                    )
                    for record in group.records
                ),
            )
            for group in result.transaction_groups
        ),
        oldest_available_at=result.oldest_available_at,
    )


def create_sync_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/sync", tags=["synchronization"])

    @router.post("/pull", response_model=PullChangesResponse)
    async def pull_changes(body: PullChangesRequest, request: Request) -> PullChangesResponse:
        services = _services(request)
        secret = request.cookies.get(settings.browser_session_cookie_name)
        if secret is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated"
            )
        authenticated = await services.browser_sessions.authenticate(secret)
        if authenticated is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated"
            )
        try:
            result = await services.synchronization.pull(
                actor_user_id=authenticated.user_id,
                request=PullRequest(
                    organization_id=body.organization_id,
                    cursor=body.cursor,
                    transaction_group_limit=body.transaction_group_limit,
                ),
            )
        except SyncQueryDenied as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization access denied",
            ) from error
        except InvalidSyncCursor as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cursor"
            ) from error
        return _result_response(result)

    return router
