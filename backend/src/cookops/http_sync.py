"""Cookie-authenticated adapters for the organization synchronization protocol."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.catalog_configuration import CatalogConfigurationCommand
from cookops.application.event_duplication import DuplicateEventCommand
from cookops.application.event_lifecycle import SetEventLifecycleCommand
from cookops.application.event_prices import UpdateEventPriceEstimatesCommand
from cookops.application.events import CreateEventCommand, UpdateEventBaseAttendanceCommand
from cookops.application.ingredients import CreateIngredientCommand, InitialPrice
from cookops.application.receipts import (
    CreateReceiptCommand,
    SetReceiptLifecycleCommand,
    UpdateReceiptCommand,
)
from cookops.application.recipes import (
    CreateRecipeCommand,
    PublishRecipeVersionCommand,
    RecipeIngredientLineInput,
)
from cookops.application.scheduled_recipe_attendance import SetScheduledRecipeAttendanceCommand
from cookops.application.scheduled_recipe_context import SetScheduledRecipeContextCommand
from cookops.application.scheduled_recipe_moves import MoveScheduledRecipeCommand
from cookops.application.scheduled_recipe_overrides import SetScheduledIngredientOverrideCommand
from cookops.application.scheduled_recipes import ScheduleRecipeCommand
from cookops.application.shopping_lists import (
    CreateAdHocShoppingItemCommand,
    CreateShoppingListCommand,
    RefreshShoppingListCommand,
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
    SyncHintsDenied,
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
MAX_HINT_ORGANIZATIONS = 20
HINT_POLL_SECONDS = 2


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


class HintSubscriptionRequest(BaseModel):
    """Small client message; organization authorization is never trusted from it."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["subscribe"]
    organization_ids: tuple[UUID, ...] = Field(min_length=1, max_length=MAX_HINT_ORGANIZATIONS)

    @field_validator("organization_ids")
    @classmethod
    def organization_ids_must_be_unique(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("organization IDs must be unique")
        return value


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


class ScheduledRecipeContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_recipe_id: UUID
    event_id: UUID
    consumption_percentage: Decimal
    operation: Literal["set_manual", "use_suggestion"]
    selected_scale_amount: Decimal | None = None
    logical_operation_id: UUID | None = None

    @field_validator("consumption_percentage", "selected_scale_amount", mode="before")
    @classmethod
    def decimals_must_be_strings(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("must be a decimal string or null")
        return value


class EventLifecyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    operation: Literal["archive", "reactivate"]
    logical_operation_id: UUID | None = None


class DuplicateEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    source_event_id: UUID
    source_archive_snapshot_id: UUID
    name: str
    logical_operation_id: UUID | None = None


class UpdateEventPriceEstimatesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    logical_operation_id: UUID | None = None


class CreateShoppingListPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shopping_list_id: UUID
    generation_revision_id: UUID
    event_id: UUID
    name: str
    scheduled_recipe_ids: tuple[UUID, ...]
    logical_operation_id: UUID | None = None


class RefreshShoppingListPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_revision_id: UUID
    shopping_list_id: UUID
    parent_generation_revision_id: UUID
    scheduled_recipe_ids: tuple[UUID, ...]
    logical_operation_id: UUID | None = None

    @field_validator("scheduled_recipe_ids")
    @classmethod
    def scheduled_recipe_ids_must_be_unique(
        cls, value: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("scheduled recipe IDs must be unique")
        return value


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


class CreateAdHocShoppingItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shopping_list_id: UUID
    ad_hoc_shopping_item_id: UUID
    name: str = Field(min_length=1, max_length=200)
    target_amount: Decimal
    unit_id: UUID
    store_section_id: UUID
    note: str | None = Field(default=None, max_length=4000)
    logical_operation_id: UUID | None = None

    @field_validator("target_amount", mode="before")
    @classmethod
    def target_amount_must_be_decimal_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a decimal string")
        return value


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


class PublishRecipeVersionPayload(CreateRecipePayload):
    based_on_version_id: UUID


class InitialIngredientPricePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    amount: Decimal
    quantity: Decimal
    unit_id: UUID
    currency: str

    @field_validator("amount", "quantity", mode="before")
    @classmethod
    def decimal_values_must_be_decimal_strings(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a decimal string")
        return value


class CreateIngredientPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient_id: UUID
    ingredient_version_id: UUID
    name: str
    canonical_unit_id: UUID
    mass_per_canonical_quantity: Decimal
    dietary_tag_ids: tuple[UUID, ...] = ()
    default_store_section_id: UUID | None = None
    initial_price: InitialIngredientPricePayload | None = None
    logical_operation_id: UUID | None = None

    @field_validator("mass_per_canonical_quantity", mode="before")
    @classmethod
    def mass_must_be_decimal_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a decimal string")
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


class ScheduledRecipeAttendancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_recipe_id: UUID
    event_id: UUID
    operation: Literal["set_manual", "follow_event"]
    diner_count: StrictInt | None = None
    logical_operation_id: UUID | None = None


class _ScheduledIngredientOverridePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    override_id: UUID
    event_id: UUID
    scheduled_recipe_id: UUID
    operation: Literal["set"]
    quantity: Decimal
    note: str | None = None
    logical_operation_id: UUID | None = None

    @field_validator("quantity", mode="before")
    @classmethod
    def quantity_must_be_decimal_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a decimal string")
        return value


class ReplacementScheduledIngredientOverridePayload(_ScheduledIngredientOverridePayload):
    override_kind: Literal["replace"]
    target_line_key: UUID


class AddedScheduledIngredientOverridePayload(_ScheduledIngredientOverridePayload):
    override_kind: Literal["add"]
    ingredient_id: UUID
    ingredient_version_id: UUID
    include_in_portion_weight: StrictBool
    position_key: str


ScheduledIngredientOverridePayload = Annotated[
    ReplacementScheduledIngredientOverridePayload
    | AddedScheduledIngredientOverridePayload,
    Field(discriminator="override_kind"),
]
scheduled_ingredient_override_payload_adapter = TypeAdapter(ScheduledIngredientOverridePayload)


class ReceiptMetadataPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: UUID
    event_id: UUID
    title: str
    total_amount: Decimal
    receipt_date: date | None = None
    note: str | None = None
    logical_operation_id: UUID | None = None

    @field_validator("total_amount", mode="before")
    @classmethod
    def total_amount_must_be_decimal_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("must be a decimal string")
        return value


class ReceiptLifecyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: UUID
    event_id: UUID
    operation: Literal["retire", "restore"]
    logical_operation_id: UUID | None = None


class CatalogConfigurationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    entity_kind: Literal["store_section", "recipe_tag", "dietary_tag", "unit_definition"]
    operation: Literal["create", "update", "retire", "restore"]
    name: str | None = None
    color: str | None = None
    position_key: str | None = None
    allows_ingredient_quantity: StrictBool | None = None
    allows_recipe_scaling: StrictBool | None = None
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


def _services(request: Request | WebSocket) -> SynchronizationHttpServices:
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
        if command.command_kind == "event.lifecycle":
            event_lifecycle_payload = EventLifecyclePayload.model_validate(command.payload)
            return SetEventLifecycleCommand(
                mutation_id=command.mutation_id,
                event_id=event_lifecycle_payload.event_id,
                organization_id=organization_id,
                operation=event_lifecycle_payload.operation,
                client_wall_time=command.client_wall_time,
                logical_operation_id=event_lifecycle_payload.logical_operation_id,
            )
        if command.command_kind == "event.duplicate":
            duplicate_payload = DuplicateEventPayload.model_validate(command.payload)
            return DuplicateEventCommand(
                mutation_id=command.mutation_id,
                event_id=duplicate_payload.event_id,
                organization_id=organization_id,
                source_event_id=duplicate_payload.source_event_id,
                source_archive_snapshot_id=duplicate_payload.source_archive_snapshot_id,
                name=duplicate_payload.name,
                client_wall_time=command.client_wall_time,
                logical_operation_id=duplicate_payload.logical_operation_id,
            )
        if command.command_kind == "event.update_price_estimates":
            price_payload = UpdateEventPriceEstimatesPayload.model_validate(command.payload)
            return UpdateEventPriceEstimatesCommand(
                mutation_id=command.mutation_id,
                organization_id=organization_id,
                event_id=price_payload.event_id,
                client_wall_time=command.client_wall_time,
                logical_operation_id=price_payload.logical_operation_id,
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
        if command.command_kind == "shopping_list.refresh":
            refresh_payload = RefreshShoppingListPayload.model_validate(command.payload)
            return RefreshShoppingListCommand(
                mutation_id=command.mutation_id,
                generation_revision_id=refresh_payload.generation_revision_id,
                organization_id=organization_id,
                shopping_list_id=refresh_payload.shopping_list_id,
                parent_generation_revision_id=refresh_payload.parent_generation_revision_id,
                scheduled_recipe_ids=refresh_payload.scheduled_recipe_ids,
                client_wall_time=command.client_wall_time,
                logical_operation_id=refresh_payload.logical_operation_id,
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
        if command.command_kind == "shopping_list.create_ad_hoc_item":
            ad_hoc_payload = CreateAdHocShoppingItemPayload.model_validate(command.payload)
            return CreateAdHocShoppingItemCommand(
                mutation_id=command.mutation_id,
                organization_id=organization_id,
                shopping_list_id=ad_hoc_payload.shopping_list_id,
                ad_hoc_shopping_item_id=ad_hoc_payload.ad_hoc_shopping_item_id,
                name=ad_hoc_payload.name,
                target_amount=ad_hoc_payload.target_amount,
                unit_id=ad_hoc_payload.unit_id,
                store_section_id=ad_hoc_payload.store_section_id,
                note=ad_hoc_payload.note,
                client_wall_time=command.client_wall_time,
                logical_operation_id=ad_hoc_payload.logical_operation_id,
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
        if command.command_kind == "recipe.publish_version":
            recipe_payload = PublishRecipeVersionPayload.model_validate(command.payload)
            return PublishRecipeVersionCommand(
                mutation_id=command.mutation_id,
                recipe_id=recipe_payload.recipe_id,
                recipe_version_id=recipe_payload.recipe_version_id,
                based_on_version_id=recipe_payload.based_on_version_id,
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
                estimated_diners_per_scaling_unit=recipe_payload.estimated_diners_per_scaling_unit,
                round_suggestions_up=recipe_payload.round_suggestions_up,
                logical_operation_id=recipe_payload.logical_operation_id,
            )
        if command.command_kind == "ingredient.create":
            ingredient_payload = CreateIngredientPayload.model_validate(command.payload)
            price = ingredient_payload.initial_price
            return CreateIngredientCommand(
                mutation_id=command.mutation_id,
                ingredient_id=ingredient_payload.ingredient_id,
                ingredient_version_id=ingredient_payload.ingredient_version_id,
                organization_id=organization_id,
                name=ingredient_payload.name,
                canonical_unit_id=ingredient_payload.canonical_unit_id,
                mass_per_canonical_quantity=ingredient_payload.mass_per_canonical_quantity,
                client_wall_time=command.client_wall_time,
                dietary_tag_ids=ingredient_payload.dietary_tag_ids,
                default_store_section_id=ingredient_payload.default_store_section_id,
                initial_price=(
                    InitialPrice(
                        price.id,
                        price.amount,
                        price.quantity,
                        price.unit_id,
                        price.currency,
                    )
                    if price
                    else None
                ),
                logical_operation_id=ingredient_payload.logical_operation_id,
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
        if command.command_kind == "scheduled_recipe.attendance":
            payload = ScheduledRecipeAttendancePayload.model_validate(command.payload)
            return SetScheduledRecipeAttendanceCommand(
                mutation_id=command.mutation_id,
                scheduled_recipe_id=payload.scheduled_recipe_id,
                organization_id=organization_id,
                event_id=payload.event_id,
                operation=payload.operation,
                diner_count=payload.diner_count,
                client_wall_time=command.client_wall_time,
                logical_operation_id=payload.logical_operation_id,
            )
        if command.command_kind == "scheduled_recipe.context":
            payload = ScheduledRecipeContextPayload.model_validate(command.payload)
            return SetScheduledRecipeContextCommand(
                mutation_id=command.mutation_id,
                scheduled_recipe_id=payload.scheduled_recipe_id,
                organization_id=organization_id,
                event_id=payload.event_id,
                consumption_percentage=payload.consumption_percentage,
                operation=payload.operation,
                selected_scale_amount=payload.selected_scale_amount,
                client_wall_time=command.client_wall_time,
                logical_operation_id=payload.logical_operation_id,
            )
        if command.command_kind == "scheduled_recipe.ingredient_override":
            override_payload = scheduled_ingredient_override_payload_adapter.validate_python(
                command.payload
            )
            return SetScheduledIngredientOverrideCommand(
                mutation_id=command.mutation_id,
                override_id=override_payload.override_id,
                organization_id=organization_id,
                event_id=override_payload.event_id,
                scheduled_recipe_id=override_payload.scheduled_recipe_id,
                operation=override_payload.operation,
                override_kind=override_payload.override_kind,
                target_line_key=getattr(override_payload, "target_line_key", None),
                ingredient_id=getattr(override_payload, "ingredient_id", None),
                ingredient_version_id=getattr(override_payload, "ingredient_version_id", None),
                quantity=override_payload.quantity,
                include_in_portion_weight=getattr(
                    override_payload, "include_in_portion_weight", None
                ),
                note=override_payload.note,
                position_key=getattr(override_payload, "position_key", None),
                client_wall_time=command.client_wall_time,
                logical_operation_id=override_payload.logical_operation_id,
            )
        if command.command_kind in ("receipt.create", "receipt.update"):
            receipt_payload = ReceiptMetadataPayload.model_validate(command.payload)
            command_class = (
                CreateReceiptCommand
                if command.command_kind == "receipt.create"
                else UpdateReceiptCommand
            )
            return command_class(
                mutation_id=command.mutation_id,
                receipt_id=receipt_payload.receipt_id,
                organization_id=organization_id,
                event_id=receipt_payload.event_id,
                title=receipt_payload.title,
                total_amount=receipt_payload.total_amount,
                client_wall_time=command.client_wall_time,
                receipt_date=receipt_payload.receipt_date,
                note=receipt_payload.note,
                logical_operation_id=receipt_payload.logical_operation_id,
            )
        if command.command_kind == "receipt.lifecycle":
            lifecycle_payload = ReceiptLifecyclePayload.model_validate(command.payload)
            return SetReceiptLifecycleCommand(
                mutation_id=command.mutation_id,
                receipt_id=lifecycle_payload.receipt_id,
                organization_id=organization_id,
                event_id=lifecycle_payload.event_id,
                operation=lifecycle_payload.operation,
                client_wall_time=command.client_wall_time,
                logical_operation_id=lifecycle_payload.logical_operation_id,
            )
        if command.command_kind == "catalog_configuration.mutate":
            catalog_payload = CatalogConfigurationPayload.model_validate(command.payload)
            return CatalogConfigurationCommand(
                mutation_id=command.mutation_id,
                organization_id=organization_id,
                entity_id=catalog_payload.entity_id,
                entity_kind=catalog_payload.entity_kind,
                operation=catalog_payload.operation,
                client_wall_time=command.client_wall_time,
                name=catalog_payload.name,
                color=catalog_payload.color,
                position_key=catalog_payload.position_key,
                allows_ingredient_quantity=catalog_payload.allows_ingredient_quantity,
                allows_recipe_scaling=catalog_payload.allows_recipe_scaling,
                logical_operation_id=catalog_payload.logical_operation_id,
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

    @router.websocket("/hints")
    async def change_hints(websocket: WebSocket) -> None:
        """Poll authorized feed heads and send only availability hints.

        PostgreSQL is already the shared change source.  Polling its tiny per-org
        head avoids inventing a process-local broker which would lose cross-worker
        notifications; pull remains recovery for every missed hint.
        """

        services = _services(websocket)
        secret = websocket.cookies.get(settings.browser_session_cookie_name)
        if secret is None:
            await websocket.close(code=4401)
            return
        authenticated = await services.browser_sessions.authenticate(secret)
        if authenticated is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            message = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            subscription = HintSubscriptionRequest.model_validate(message)
        except WebSocketDisconnect:
            return
        except (TimeoutError, ValidationError, ValueError):
            await websocket.close(code=4400)
            return
        organization_ids = tuple(sorted(subscription.organization_ids, key=str))
        try:
            known = await services.synchronization.change_hints(
                actor_user_id=authenticated.user_id, organization_ids=organization_ids
            )
        except SyncQueryDenied:
            # A forbidden and an unknown organization intentionally look the same
            # on this transport, and no subscription details are reflected.
            await websocket.close(code=4403)
            return
        for organization_id, cursor in known.items():
            await websocket.send_json(
                {
                    "type": "change_available",
                    "organization_id": str(organization_id),
                    "cursor": cursor,
                    "reason": "subscription",
                }
            )
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=HINT_POLL_SECONDS)
                await websocket.close(code=4400)
                return
            except TimeoutError:
                pass
            except WebSocketDisconnect:
                return
            current_session = await services.browser_sessions.authenticate(secret)
            if current_session is None or current_session.user_id != authenticated.user_id:
                await websocket.close(code=4401)
                return
            try:
                current = await services.synchronization.change_hints(
                    actor_user_id=current_session.user_id,
                    organization_ids=organization_ids,
                )
            except SyncHintsDenied as error:
                # The client only subscribed to these already-authorized IDs;
                # signal a recheck without disclosing which access check failed.
                await websocket.send_json(
                    {"type": "access_changed", "organization_id": str(error.organization_id)}
                )
                await websocket.close(code=4403)
                return
            for organization_id, cursor in current.items():
                if cursor != known[organization_id]:
                    known[organization_id] = cursor
                    await websocket.send_json(
                        {
                            "type": "change_available",
                            "organization_id": str(organization_id),
                            "cursor": cursor,
                            "reason": "domain_change",
                        }
                    )

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
