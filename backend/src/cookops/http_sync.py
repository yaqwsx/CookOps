"""Cookie-authenticated adapters for the organization synchronization protocol."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.events import CreateEventCommand, UpdateEventBaseAttendanceCommand
from cookops.application.recipes import CreateRecipeCommand, RecipeIngredientLineInput
from cookops.application.scheduled_recipe_moves import MoveScheduledRecipeCommand
from cookops.application.scheduled_recipes import ScheduleRecipeCommand
from cookops.application.shopping_lists import (
    CreateShoppingListCommand,
    SetShoppingAvailableSupplyCommand,
    SetShoppingContributionFulfilmentCommand,
    SetShoppingManualPurchaseTargetCommand,
    SetShoppingRowFulfilmentCommand,
)
from cookops.application.synchronization import (
    MAX_COMMANDS_PER_PUSH,
    MAX_TRANSACTION_GROUPS_PER_PULL,
    BootstrapResult,
    InvalidSyncCursor,
    PullRequest,
    PullResult,
    PushCommandOutcome,
    PushRequest,
    SyncCommand,
    SynchronizationCommandService,
    SynchronizationQueryService,
    SyncPushDenied,
    SyncQueryDenied,
    UnsupportedSyncCommand,
)
from cookops.config import Settings


@dataclass(frozen=True, slots=True)
class SynchronizationHttpServices:
    browser_sessions: BrowserSessionService
    synchronization: SynchronizationQueryService
    commands: SynchronizationCommandService


MAX_PUSH_REQUEST_BYTES = 1024 * 1024


class PullChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    transaction_group_limit: int = Field(
        default=MAX_TRANSACTION_GROUPS_PER_PULL,
        ge=1,
        le=MAX_TRANSACTION_GROUPS_PER_PULL,
    )


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID


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


class BootstrapRecordResponse(BaseModel):
    organization_id: UUID
    entity_id: UUID
    entity_kind: str
    operation: Literal["upsert"]
    payload: dict[str, object]


class BootstrapResponse(BaseModel):
    sync_schema_version: Literal[1]
    server_time: datetime
    cursor: str
    records: tuple[BootstrapRecordResponse, ...]


class PushCommandRequest(BaseModel):
    """Forward-compatible command envelope; each known payload is validated below."""

    model_config = ConfigDict(extra="forbid")

    mutation_id: UUID
    command_kind: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.-]*$")
    command_schema_version: Literal[1]
    client_wall_time: datetime
    payload: dict[str, object]

    @field_validator("client_wall_time")
    @classmethod
    def client_wall_time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value


class PushChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    client_installation_id: UUID
    request_sent_at: datetime
    sync_schema_version: Literal[1]
    commands: tuple[PushCommandRequest, ...] = Field(max_length=MAX_COMMANDS_PER_PUSH)

    @field_validator("request_sent_at")
    @classmethod
    def request_sent_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value


class CreateEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    name: str
    start_date: date
    end_date: date
    base_expected_attendance: int
    budget_amount: Decimal
    location: str | None = None
    general_note: str | None = None
    logical_operation_id: UUID | None = None

    @field_validator("budget_amount", mode="before")
    @classmethod
    def budget_amount_must_be_decimal_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a decimal string")
        return value


class UpdateEventBaseAttendancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    base_expected_attendance: int
    logical_operation_id: UUID | None = None


class CreateShoppingListPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shopping_list_id: UUID
    generation_revision_id: UUID
    event_id: UUID
    name: str
    scheduled_recipe_ids: tuple[UUID, ...]
    logical_operation_id: UUID | None = None


class SetShoppingQuantityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shopping_list_id: UUID
    shopping_ingredient_row_id: UUID
    quantity: Decimal | None
    logical_operation_id: UUID | None = None

    @field_validator("quantity", mode="before")
    @classmethod
    def quantity_must_be_decimal_string_or_null(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("must be a decimal string or null")
        return value


class SetShoppingSupplyPayload(SetShoppingQuantityPayload):
    quantity: Decimal


class SetShoppingContributionFulfilmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shopping_list_id: UUID
    shopping_contribution_id: UUID
    fulfilled: StrictBool
    logical_operation_id: UUID | None = None


class SetShoppingRowFulfilmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shopping_list_id: UUID
    shopping_ingredient_row_id: UUID
    fulfilled: StrictBool
    logical_operation_id: UUID | None = None


class RecipeIngredientLinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    line_key: UUID
    ingredient_version_id: UUID
    base_quantity: Decimal
    position_key: str
    scaling_behavior: Literal["proportional", "fixed"] = "proportional"
    include_in_portion_weight: StrictBool = True
    preferred_display_unit_id: UUID | None = None
    note: str | None = None

    @field_validator("base_quantity", mode="before")
    @classmethod
    def base_quantity_must_be_decimal_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a decimal string")
        return value


class CreateRecipePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: UUID
    recipe_version_id: UUID
    name: str
    scaling_unit_id: UUID
    base_scaling_amount: Decimal
    ingredient_lines: tuple[RecipeIngredientLinePayload, ...]
    description: str | None = None
    recipe_tag_ids: tuple[UUID, ...] = ()
    estimated_diners_per_scaling_unit: Decimal | None = None
    round_suggestions_up: StrictBool = False
    logical_operation_id: UUID | None = None

    @field_validator("base_scaling_amount", "estimated_diners_per_scaling_unit", mode="before")
    @classmethod
    def decimal_values_must_be_decimal_strings(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("must be a decimal string or null")
        return value


class ScheduleRecipePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_recipe_id: UUID
    event_id: UUID
    event_day_id: UUID
    event_meal_role_id: UUID
    recipe_id: UUID
    recipe_version_id: UUID
    consumption_percentage: Decimal = Decimal("100")
    position_key: str = "a"
    note: str | None = None
    logical_operation_id: UUID | None = None

    @field_validator("consumption_percentage", mode="before")
    @classmethod
    def consumption_percentage_must_be_decimal_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a decimal string")
        return value


class MoveScheduledRecipePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_recipe_id: UUID
    event_id: UUID
    event_day_id: UUID
    event_meal_role_id: UUID
    position_key: str
    logical_operation_id: UUID | None = None


class PushCommandErrorResponse(BaseModel):
    code: str
    field_violations: tuple[dict[str, str], ...]
    retry_same_identity: bool


class PushCommandOutcomeResponse(BaseModel):
    mutation_id: UUID
    command_kind: str
    status: Literal["accepted", "partially_superseded", "rejected"]
    replayed: bool
    first_change_sequence: int | None
    last_change_sequence: int | None
    error: PushCommandErrorResponse | None


class ClockSkewWarningResponse(BaseModel):
    difference_seconds: int
    server_time: datetime


class PushChangesResponse(BaseModel):
    sync_schema_version: Literal[1]
    server_time: datetime
    clock_skew_warning: ClockSkewWarningResponse | None
    change_cursor: str
    outcomes: tuple[PushCommandOutcomeResponse, ...]


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


def _bootstrap_response(result: BootstrapResult) -> BootstrapResponse:
    return BootstrapResponse(
        sync_schema_version=result.sync_schema_version,
        server_time=result.server_time,
        cursor=result.cursor,
        records=tuple(
            BootstrapRecordResponse(
                organization_id=record.organization_id,
                entity_id=record.entity_id,
                entity_kind=record.entity_kind,
                operation="upsert",
                payload=record.payload,
            )
            for record in result.records
        ),
    )


def _push_command(command: PushCommandRequest, organization_id: UUID) -> SyncCommand:
    try:
        if command.command_kind == "event.create":
            payload = CreateEventPayload.model_validate(command.payload)
            return CreateEventCommand(
                mutation_id=command.mutation_id,
                event_id=payload.event_id,
                organization_id=organization_id,
                name=payload.name,
                start_date=payload.start_date,
                end_date=payload.end_date,
                base_expected_attendance=payload.base_expected_attendance,
                budget_amount=payload.budget_amount,
                client_wall_time=command.client_wall_time,
                location=payload.location,
                general_note=payload.general_note,
                logical_operation_id=payload.logical_operation_id,
            )
        if command.command_kind == "event.update_base_attendance":
            attendance_payload = UpdateEventBaseAttendancePayload.model_validate(command.payload)
            return UpdateEventBaseAttendanceCommand(
                mutation_id=command.mutation_id,
                event_id=attendance_payload.event_id,
                organization_id=organization_id,
                base_expected_attendance=attendance_payload.base_expected_attendance,
                client_wall_time=command.client_wall_time,
                logical_operation_id=attendance_payload.logical_operation_id,
            )
        if command.command_kind == "shopping_list.create":
            shopping_payload = CreateShoppingListPayload.model_validate(command.payload)
            return CreateShoppingListCommand(
                mutation_id=command.mutation_id,
                shopping_list_id=shopping_payload.shopping_list_id,
                generation_revision_id=shopping_payload.generation_revision_id,
                organization_id=organization_id,
                event_id=shopping_payload.event_id,
                name=shopping_payload.name,
                scheduled_recipe_ids=shopping_payload.scheduled_recipe_ids,
                client_wall_time=command.client_wall_time,
                logical_operation_id=shopping_payload.logical_operation_id,
            )
        if command.command_kind == "shopping_list.set_available_supply":
            supply_payload = SetShoppingSupplyPayload.model_validate(command.payload)
            return SetShoppingAvailableSupplyCommand(
                command.mutation_id,
                organization_id,
                supply_payload.shopping_list_id,
                supply_payload.shopping_ingredient_row_id,
                supply_payload.quantity,
                command.client_wall_time,
                supply_payload.logical_operation_id,
            )
        if command.command_kind == "shopping_list.set_manual_purchase_target":
            target_payload = SetShoppingQuantityPayload.model_validate(command.payload)
            return SetShoppingManualPurchaseTargetCommand(
                command.mutation_id,
                organization_id,
                target_payload.shopping_list_id,
                target_payload.shopping_ingredient_row_id,
                target_payload.quantity,
                command.client_wall_time,
                target_payload.logical_operation_id,
            )
        if command.command_kind == "shopping_list.set_contribution_fulfilment":
            fulfilment_payload = SetShoppingContributionFulfilmentPayload.model_validate(
                command.payload
            )
            return SetShoppingContributionFulfilmentCommand(
                command.mutation_id,
                organization_id,
                fulfilment_payload.shopping_list_id,
                fulfilment_payload.shopping_contribution_id,
                fulfilment_payload.fulfilled,
                command.client_wall_time,
                fulfilment_payload.logical_operation_id,
            )
        if command.command_kind == "shopping_list.set_row_fulfilment":
            row_fulfilment_payload = SetShoppingRowFulfilmentPayload.model_validate(command.payload)
            return SetShoppingRowFulfilmentCommand(
                command.mutation_id,
                organization_id,
                row_fulfilment_payload.shopping_list_id,
                row_fulfilment_payload.shopping_ingredient_row_id,
                row_fulfilment_payload.fulfilled,
                command.client_wall_time,
                row_fulfilment_payload.logical_operation_id,
            )
        if command.command_kind == "recipe.create":
            recipe_payload = CreateRecipePayload.model_validate(command.payload)
            return CreateRecipeCommand(
                mutation_id=command.mutation_id,
                recipe_id=recipe_payload.recipe_id,
                recipe_version_id=recipe_payload.recipe_version_id,
                organization_id=organization_id,
                name=recipe_payload.name,
                scaling_unit_id=recipe_payload.scaling_unit_id,
                base_scaling_amount=recipe_payload.base_scaling_amount,
                client_wall_time=command.client_wall_time,
                ingredient_lines=tuple(
                    RecipeIngredientLineInput(
                        id=line.id,
                        line_key=line.line_key,
                        ingredient_version_id=line.ingredient_version_id,
                        base_quantity=line.base_quantity,
                        position_key=line.position_key,
                        scaling_behavior=line.scaling_behavior,
                        include_in_portion_weight=line.include_in_portion_weight,
                        preferred_display_unit_id=line.preferred_display_unit_id,
                        note=line.note,
                    )
                    for line in recipe_payload.ingredient_lines
                ),
                description=recipe_payload.description,
                recipe_tag_ids=recipe_payload.recipe_tag_ids,
                estimated_diners_per_scaling_unit=(
                    recipe_payload.estimated_diners_per_scaling_unit
                ),
                round_suggestions_up=recipe_payload.round_suggestions_up,
                logical_operation_id=recipe_payload.logical_operation_id,
            )
        if command.command_kind == "scheduled_recipe.schedule":
            scheduled_recipe_payload = ScheduleRecipePayload.model_validate(command.payload)
            return ScheduleRecipeCommand(
                mutation_id=command.mutation_id,
                scheduled_recipe_id=scheduled_recipe_payload.scheduled_recipe_id,
                organization_id=organization_id,
                event_id=scheduled_recipe_payload.event_id,
                event_day_id=scheduled_recipe_payload.event_day_id,
                event_meal_role_id=scheduled_recipe_payload.event_meal_role_id,
                recipe_id=scheduled_recipe_payload.recipe_id,
                recipe_version_id=scheduled_recipe_payload.recipe_version_id,
                client_wall_time=command.client_wall_time,
                consumption_percentage=scheduled_recipe_payload.consumption_percentage,
                position_key=scheduled_recipe_payload.position_key,
                note=scheduled_recipe_payload.note,
                logical_operation_id=scheduled_recipe_payload.logical_operation_id,
            )
        if command.command_kind == "scheduled_recipe.move":
            move_payload = MoveScheduledRecipePayload.model_validate(command.payload)
            return MoveScheduledRecipeCommand(
                mutation_id=command.mutation_id,
                scheduled_recipe_id=move_payload.scheduled_recipe_id,
                organization_id=organization_id,
                event_id=move_payload.event_id,
                event_day_id=move_payload.event_day_id,
                event_meal_role_id=move_payload.event_meal_role_id,
                position_key=move_payload.position_key,
                client_wall_time=command.client_wall_time,
                logical_operation_id=move_payload.logical_operation_id,
            )
    except ValidationError:
        return UnsupportedSyncCommand(
            mutation_id=command.mutation_id,
            command_kind=command.command_kind,
            request_hash=_push_command_hash(command),
            client_wall_time=command.client_wall_time,
            rejection_code="validation_failed",
        )
    return UnsupportedSyncCommand(
        mutation_id=command.mutation_id,
        command_kind=command.command_kind,
        request_hash=_push_command_hash(command),
        client_wall_time=command.client_wall_time,
    )


def _push_command_hash(command: PushCommandRequest) -> bytes:
    return hashlib.sha256(
        json.dumps(
            command.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).digest()


def _push_outcome_response(outcome: PushCommandOutcome) -> PushCommandOutcomeResponse:
    error = None
    if outcome.error_code is not None:
        error = PushCommandErrorResponse(
            code=outcome.error_code,
            field_violations=tuple(
                {"path": path, "code": code} for path, code in outcome.field_violations
            ),
            retry_same_identity=outcome.retry_same_identity,
        )
    return PushCommandOutcomeResponse(
        mutation_id=outcome.mutation_id,
        command_kind=outcome.command_kind,
        status=outcome.status,
        replayed=outcome.replayed,
        first_change_sequence=outcome.first_change_sequence,
        last_change_sequence=outcome.last_change_sequence,
        error=error,
    )


async def _push_body(request: Request) -> PushChangesRequest:
    content_length = request.headers.get("content-length")
    if content_length is not None and (
        not content_length.isdigit() or int(content_length) > MAX_PUSH_REQUEST_BYTES
    ):
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="request too large"
        )
    raw = await request.body()
    if len(raw) > MAX_PUSH_REQUEST_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="request too large"
        )
    try:
        return PushChangesRequest.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid push request"
        ) from error


def create_sync_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/sync", tags=["synchronization"])

    @router.post("/bootstrap", response_model=BootstrapResponse)
    async def bootstrap(request: Request, body: BootstrapRequest) -> BootstrapResponse:
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
            return _bootstrap_response(
                await services.synchronization.bootstrap(
                    actor_user_id=authenticated.user_id, organization_id=body.organization_id
                )
            )
        except SyncQueryDenied as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="organization access denied"
            ) from error

    @router.post(
        "/push",
        response_model=PushChangesResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": PushChangesRequest.model_json_schema()}},
            }
        },
    )
    async def push_changes(request: Request) -> PushChangesResponse:
        body = await _push_body(request)
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
            result = await services.commands.push(
                actor_user_id=authenticated.user_id,
                request=PushRequest(
                    organization_id=body.organization_id,
                    client_installation_id=body.client_installation_id,
                    request_sent_at=body.request_sent_at,
                    commands=tuple(
                        _push_command(command, body.organization_id) for command in body.commands
                    ),
                ),
            )
        except SyncPushDenied as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization access denied",
            ) from error
        return PushChangesResponse(
            sync_schema_version=result.sync_schema_version,
            server_time=result.server_time,
            clock_skew_warning=(
                ClockSkewWarningResponse(
                    difference_seconds=result.clock_skew_seconds,
                    server_time=result.server_time,
                )
                if result.clock_skew_seconds is not None
                else None
            ),
            change_cursor=result.change_cursor,
            outcomes=tuple(_push_outcome_response(outcome) for outcome in result.outcomes),
        )

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
