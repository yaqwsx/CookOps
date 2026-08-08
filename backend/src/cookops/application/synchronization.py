"""Read the retained organization change feed for browser synchronization.

This intentionally implements only the server-to-client half of the protocol.
Commands continue to use their existing application services; a generic push
dispatcher is not useful until those commands have one common typed envelope.
"""

# The explicit projection below reuses one loop variable across independently
# typed query blocks; SQLAlchemy's precise model attributes remain runtime-safe.
# mypy: disable-error-code=assignment
# mypy: disable-error-code=attr-defined
# mypy: disable-error-code=arg-type

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application import scheduled_recipe_overrides
from cookops.application.browser_sessions import decode_browser_session_hmac_key
from cookops.application.catalog_configuration import (
    CatalogConfigurationCommand,
    CatalogConfigurationResult,
    mutate_catalog_configuration,
)
from cookops.application.event_duplication import (
    DuplicateEventCommand,
    DuplicateEventResult,
    duplicate_event,
)
from cookops.application.event_lifecycle import (
    EventLifecycleResult,
    SetEventLifecycleCommand,
    set_event_lifecycle,
)
from cookops.application.event_prices import (
    UpdateEventPriceEstimatesCommand,
    UpdateEventPriceEstimatesResult,
    _price_pointer_record,
    _snapshot_record,
    update_event_price_estimates,
)
from cookops.application.events import (
    CreateEventCommand,
    CreateEventResult,
    UpdateEventBaseAttendanceCommand,
    UpdateEventBaseAttendanceResult,
    _event_change_record,
    _scheduled_recipe_change_record,
    create_event,
    update_event_base_attendance,
)
from cookops.application.ingredients import (
    CreateIngredientCommand,
    CreateIngredientResult,
    create_ingredient,
)
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.application.receipt_media import _record as _attachment_record
from cookops.application.receipts import (
    CreateReceiptCommand,
    ReceiptResult,
    SetReceiptLifecycleCommand,
    UpdateReceiptCommand,
    create_receipt,
    restore_receipt,
    retire_receipt,
    update_receipt,
)
from cookops.application.receipts import (
    _record as _receipt_record,
)
from cookops.application.recipes import (
    CreateRecipeCommand,
    CreateRecipeResult,
    PublishRecipeVersionCommand,
    create_recipe,
    publish_recipe_version,
    recipe_version_tag_change_id,
)
from cookops.application.scheduled_recipe_attendance import (
    ScheduledRecipeAttendanceResult,
    SetScheduledRecipeAttendanceCommand,
    set_scheduled_recipe_attendance,
)
from cookops.application.scheduled_recipe_context import (
    ScheduledRecipeContextResult,
    SetScheduledRecipeContextCommand,
    set_scheduled_recipe_context,
)
from cookops.application.scheduled_recipe_lifecycle import (
    ScheduledRecipeLifecycleResult,
    SetScheduledRecipeLifecycleCommand,
    set_scheduled_recipe_lifecycle,
)
from cookops.application.scheduled_recipe_moves import (
    MoveScheduledRecipeCommand,
    MoveScheduledRecipeResult,
    move_scheduled_recipe,
)
from cookops.application.scheduled_recipe_overrides import (
    ScheduledIngredientOverrideResult,
    SetScheduledIngredientOverrideCommand,
    set_scheduled_ingredient_override,
)
from cookops.application.scheduled_recipes import (
    ScheduleRecipeCommand,
    ScheduleRecipeResult,
    schedule_recipe,
)
from cookops.application.shopping_lists import (
    CreateAdHocShoppingItemCommand,
    CreateAdHocShoppingItemResult,
    CreateShoppingListCommand,
    CreateShoppingListResult,
    RefreshShoppingListCommand,
    RefreshShoppingListResult,
    SetAdHocShoppingItemFulfilmentCommand,
    SetAdHocShoppingItemFulfilmentResult,
    SetAdHocShoppingItemLifecycleCommand,
    SetAdHocShoppingItemLifecycleResult,
    SetShoppingAvailableSupplyCommand,
    SetShoppingContributionFulfilmentCommand,
    SetShoppingManualPurchaseTargetCommand,
    SetShoppingRowFulfilmentCommand,
    ShoppingOperationResult,
    UpdateAdHocShoppingItemCommand,
    UpdateAdHocShoppingItemResult,
    _contribution_record,
    _contribution_snapshot_record,
    _generation_revision_record,
    _revision_source_record,
    _row_record,
    _shopping_list_record,
    create_ad_hoc_shopping_item,
    create_shopping_list,
    refresh_shopping_list,
    set_ad_hoc_shopping_item_fulfilment,
    set_ad_hoc_shopping_item_lifecycle,
    set_shopping_available_supply,
    set_shopping_contribution_fulfilment,
    set_shopping_manual_purchase_target,
    set_shopping_row_fulfilment,
    update_ad_hoc_shopping_item,
)
from cookops.persistence.models import (
    AdHocShoppingItem,
    ClientInstallation,
    DietaryTag,
    Event,
    EventDay,
    EventIngredientPrice,
    EventIngredientPriceSnapshot,
    EventMealRole,
    FieldClock,
    Ingredient,
    IngredientPriceEstimate,
    IngredientVersion,
    IngredientVersionDietaryTag,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationChangeHead,
    OrganizationChangeTransaction,
    OrganizationMealRolePreset,
    OrganizationMembership,
    Receipt,
    ReceiptAttachment,
    Recipe,
    RecipeTag,
    RecipeVersion,
    RecipeVersionIngredientLine,
    RecipeVersionTag,
    ScheduledIngredientOverride,
    ScheduledRecipe,
    ShoppingContribution,
    ShoppingContributionSnapshot,
    ShoppingGenerationRevision,
    ShoppingIngredientRow,
    ShoppingList,
    ShoppingRevisionSource,
    StoreSection,
    SystemRoleAssignment,
    UnitDefinition,
    User,
)

SYNC_SCHEMA_VERSION: Literal[1] = 1
MAX_TRANSACTION_GROUPS_PER_PULL = 100
MAX_COMMANDS_PER_PUSH = 100

Clock = Callable[[], datetime]


class SyncQueryDenied(PermissionError):
    """The caller is not currently allowed to read this organization."""


class SyncHintsDenied(SyncQueryDenied):
    """One already-subscribed organization no longer has current access."""

    def __init__(self, organization_id: UUID) -> None:
        self.organization_id = organization_id
        super().__init__("organization access denied")


class InvalidSyncCursor(ValueError):
    """The supplied opaque cursor is malformed, forged, or not a safe boundary."""


class SyncPushDenied(PermissionError):
    """The caller or browser installation is not currently allowed to push."""


@dataclass(frozen=True, slots=True)
class SyncCursor:
    organization_id: UUID
    after_sequence: int


@dataclass(frozen=True, slots=True)
class SyncRecord:
    organization_id: UUID
    sequence: int
    entity_id: UUID
    entity_kind: str
    operation: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class SyncTransactionGroup:
    mutation_id: UUID
    first_sequence: int
    last_sequence: int
    records: tuple[SyncRecord, ...]


@dataclass(frozen=True, slots=True)
class PullRequest:
    organization_id: UUID
    cursor: str | None
    transaction_group_limit: int = MAX_TRANSACTION_GROUPS_PER_PULL

    def __post_init__(self) -> None:
        if not 1 <= self.transaction_group_limit <= MAX_TRANSACTION_GROUPS_PER_PULL:
            raise ValueError(
                f"transaction_group_limit must be between 1 and {MAX_TRANSACTION_GROUPS_PER_PULL}"
            )


@dataclass(frozen=True, slots=True)
class PullResult:
    status: Literal["ok", "bootstrap_required"]
    sync_schema_version: Literal[1]
    server_time: datetime
    next_cursor: str | None
    transaction_groups: tuple[SyncTransactionGroup, ...]
    oldest_available_at: datetime | None


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    sync_schema_version: Literal[1]
    server_time: datetime
    cursor: str
    records: tuple[SyncRecord, ...]


@dataclass(frozen=True, slots=True)
class UnsupportedSyncCommand:
    mutation_id: UUID
    command_kind: str
    request_hash: bytes
    client_wall_time: datetime
    rejection_code: str = "unsupported_command_kind"


SyncCommand = (
    CreateEventCommand
    | UpdateEventBaseAttendanceCommand
    | SetEventLifecycleCommand
    | DuplicateEventCommand
    | UpdateEventPriceEstimatesCommand
    | CreateShoppingListCommand
    | RefreshShoppingListCommand
    | CreateRecipeCommand
    | PublishRecipeVersionCommand
    | CreateIngredientCommand
    | ScheduleRecipeCommand
    | MoveScheduledRecipeCommand
    | SetScheduledRecipeAttendanceCommand
    | SetScheduledRecipeContextCommand
    | SetScheduledRecipeLifecycleCommand
    | SetScheduledIngredientOverrideCommand
    | SetShoppingAvailableSupplyCommand
    | SetShoppingManualPurchaseTargetCommand
    | SetShoppingContributionFulfilmentCommand
    | SetShoppingRowFulfilmentCommand
    | CreateAdHocShoppingItemCommand
    | SetAdHocShoppingItemFulfilmentCommand
    | SetAdHocShoppingItemLifecycleCommand
    | UpdateAdHocShoppingItemCommand
    | CreateReceiptCommand
    | UpdateReceiptCommand
    | SetReceiptLifecycleCommand
    | CatalogConfigurationCommand
    | UnsupportedSyncCommand
)


def _command_kind(
    command: (
        CreateEventCommand
        | UpdateEventBaseAttendanceCommand
        | SetEventLifecycleCommand
        | DuplicateEventCommand
        | UpdateEventPriceEstimatesCommand
        | CreateShoppingListCommand
        | RefreshShoppingListCommand
        | CreateRecipeCommand
        | PublishRecipeVersionCommand
        | CreateIngredientCommand
        | ScheduleRecipeCommand
        | MoveScheduledRecipeCommand
        | SetScheduledRecipeAttendanceCommand
        | SetScheduledRecipeContextCommand
        | SetScheduledRecipeLifecycleCommand
        | SetScheduledIngredientOverrideCommand
        | SetShoppingAvailableSupplyCommand
        | SetShoppingManualPurchaseTargetCommand
        | SetShoppingContributionFulfilmentCommand
        | SetShoppingRowFulfilmentCommand
        | CreateAdHocShoppingItemCommand
        | SetAdHocShoppingItemFulfilmentCommand
        | SetAdHocShoppingItemLifecycleCommand
        | UpdateAdHocShoppingItemCommand
        | CreateReceiptCommand
        | UpdateReceiptCommand
        | SetReceiptLifecycleCommand
        | CatalogConfigurationCommand
    ),
) -> str:
    if isinstance(command, CreateEventCommand):
        return "event.create"
    if isinstance(command, UpdateEventBaseAttendanceCommand):
        return "event.update_base_attendance"
    if isinstance(command, SetEventLifecycleCommand):
        return "event.lifecycle"
    if isinstance(command, DuplicateEventCommand):
        return "event.duplicate"
    if isinstance(command, UpdateEventPriceEstimatesCommand):
        return "event.update_price_estimates"
    if isinstance(command, RefreshShoppingListCommand):
        return "shopping_list.refresh"
    if isinstance(command, CreateRecipeCommand):
        return "recipe.create"
    if isinstance(command, PublishRecipeVersionCommand):
        return "recipe.publish_version"
    if isinstance(command, CreateIngredientCommand):
        return "ingredient.create"
    if isinstance(command, ScheduleRecipeCommand):
        return "scheduled_recipe.schedule"
    if isinstance(command, MoveScheduledRecipeCommand):
        return "scheduled_recipe.move"
    if isinstance(command, SetScheduledRecipeAttendanceCommand):
        return "scheduled_recipe.attendance"
    if isinstance(command, SetScheduledRecipeContextCommand):
        return "scheduled_recipe.context"
    if isinstance(command, SetScheduledRecipeLifecycleCommand):
        return "scheduled_recipe.lifecycle"
    if isinstance(command, SetScheduledIngredientOverrideCommand):
        return "scheduled_recipe.ingredient_override"
    if isinstance(command, SetShoppingAvailableSupplyCommand):
        return "shopping_list.set_available_supply"
    if isinstance(command, SetShoppingManualPurchaseTargetCommand):
        return "shopping_list.set_manual_purchase_target"
    if isinstance(command, SetShoppingContributionFulfilmentCommand):
        return "shopping_list.set_contribution_fulfilment"
    if isinstance(command, SetShoppingRowFulfilmentCommand):
        return "shopping_list.set_row_fulfilment"
    if isinstance(command, CreateAdHocShoppingItemCommand):
        return "shopping_list.create_ad_hoc_item"
    if isinstance(command, SetAdHocShoppingItemFulfilmentCommand):
        return "shopping_list.set_ad_hoc_item_fulfilment"
    if isinstance(command, SetAdHocShoppingItemLifecycleCommand):
        return "shopping_list.ad_hoc_item_lifecycle"
    if isinstance(command, UpdateAdHocShoppingItemCommand):
        return "shopping_list.update_ad_hoc_item"
    if isinstance(command, CreateReceiptCommand):
        return "receipt.create"
    if isinstance(command, UpdateReceiptCommand):
        return "receipt.update"
    if isinstance(command, SetReceiptLifecycleCommand):
        return "receipt.lifecycle"
    if isinstance(command, CatalogConfigurationCommand):
        return "catalog_configuration.mutate"
    return "shopping_list.create"


@dataclass(frozen=True, slots=True)
class PushRequest:
    organization_id: UUID
    client_installation_id: UUID
    request_sent_at: datetime
    commands: tuple[SyncCommand, ...]

    def __post_init__(self) -> None:
        if len(self.commands) > MAX_COMMANDS_PER_PUSH:
            raise ValueError(f"commands must contain at most {MAX_COMMANDS_PER_PUSH} items")


@dataclass(frozen=True, slots=True)
class PushCommandOutcome:
    mutation_id: UUID
    command_kind: str
    status: Literal["accepted", "partially_superseded", "rejected"]
    replayed: bool
    first_change_sequence: int | None
    last_change_sequence: int | None
    error_code: str | None
    field_violations: tuple[tuple[str, str], ...]
    retry_same_identity: bool


@dataclass(frozen=True, slots=True)
class PushResult:
    sync_schema_version: Literal[1]
    server_time: datetime
    clock_skew_seconds: int | None
    change_cursor: str
    outcomes: tuple[PushCommandOutcome, ...]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_now(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock result must include a timezone")
    return now.astimezone(UTC)


class SyncCursorCodec:
    """Bind an opaque cursor to its organization and protocol schema.

    Cursors are not credentials, but authenticating them prevents a client from
    accidentally or deliberately advancing through the middle of a transaction
    group and losing its own canonical data.
    """

    def __init__(self, *, encoded_hmac_key: str) -> None:
        self._key = decode_browser_session_hmac_key(encoded_hmac_key)

    def encode(self, cursor: SyncCursor) -> str:
        if cursor.after_sequence < 0:
            raise ValueError("after_sequence must be nonnegative")
        payload = json.dumps(
            {
                "after_sequence": cursor.after_sequence,
                "organization_id": str(cursor.organization_id),
                "sync_schema_version": SYNC_SCHEMA_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(
            self._key,
            b"cookops.sync.cursor.v1:" + encoded_payload,
            hashlib.sha256,
        ).digest()
        return (
            "v1."
            + encoded_payload.decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )

    def decode(self, token: str) -> SyncCursor:
        if not isinstance(token, str) or len(token) > 512:
            raise InvalidSyncCursor("invalid cursor")
        version, separator, remainder = token.partition(".")
        encoded_payload, separator_two, encoded_signature = remainder.partition(".")
        if (
            version != "v1"
            or not separator
            or not separator_two
            or not encoded_payload
            or not encoded_signature
        ):
            raise InvalidSyncCursor("invalid cursor")
        try:
            payload_bytes = self._decode_base64url(encoded_payload)
            signature = self._decode_base64url(encoded_signature)
            decoded = json.loads(payload_bytes)
            organization_id = UUID(decoded["organization_id"])
            after_sequence = decoded["after_sequence"]
            schema_version = decoded["sync_schema_version"]
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            UnicodeEncodeError,
            binascii.Error,
            json.JSONDecodeError,
        ) as error:
            raise InvalidSyncCursor("invalid cursor") from error
        expected_signature = hmac.new(
            self._key,
            b"cookops.sync.cursor.v1:" + encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if (
            not hmac.compare_digest(signature, expected_signature)
            or type(after_sequence) is not int
            or after_sequence < 0
            or schema_version != SYNC_SCHEMA_VERSION
        ):
            raise InvalidSyncCursor("invalid cursor")
        return SyncCursor(organization_id=organization_id, after_sequence=after_sequence)

    @staticmethod
    def _decode_base64url(value: str) -> bytes:
        if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise InvalidSyncCursor("invalid cursor")
        try:
            decoded = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        except (binascii.Error, ValueError) as error:
            raise InvalidSyncCursor("invalid cursor") from error
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(value, canonical):
            raise InvalidSyncCursor("invalid cursor")
        return decoded


class SynchronizationCommandService:
    """Apply the small, typed browser-command subset in transport order."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        encoded_cursor_hmac_key: str,
        clock: Clock = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._cursor_codec = SyncCursorCodec(encoded_hmac_key=encoded_cursor_hmac_key)
        self._clock = clock

    async def push(self, *, actor_user_id: UUID, request: PushRequest) -> PushResult:
        now = _normalize_now(self._clock())
        request_sent_at = _normalize_now(request.request_sent_at)
        actor_role = await self._authorize_push(
            actor_user_id=actor_user_id,
            organization_id=request.organization_id,
            client_installation_id=request.client_installation_id,
        )
        context = ExecutionContext(
            actor_user_id=actor_user_id,
            client_installation_id=request.client_installation_id,
        )
        outcomes: list[PushCommandOutcome] = []
        for command in request.commands:
            outcomes.append(
                await self._dispatch(
                    context=context,
                    actor_role=actor_role,
                    organization_id=request.organization_id,
                    command=command,
                )
            )
        async with self._session_factory() as session, session.begin():
            head = await session.scalar(
                select(OrganizationChangeHead.next_sequence).where(
                    OrganizationChangeHead.organization_id == request.organization_id
                )
            )
        return PushResult(
            sync_schema_version=SYNC_SCHEMA_VERSION,
            server_time=now,
            clock_skew_seconds=(
                int((request_sent_at - now).total_seconds())
                if abs(request_sent_at - now) > timedelta(minutes=5)
                else None
            ),
            change_cursor=self._cursor_codec.encode(
                SyncCursor(
                    organization_id=request.organization_id,
                    after_sequence=(head - 1 if head is not None else 0),
                )
            ),
            outcomes=tuple(outcomes),
        )

    async def _authorize_push(
        self, *, actor_user_id: UUID, organization_id: UUID, client_installation_id: UUID
    ) -> Literal["member", "organization_admin", "system_admin"]:
        async with self._session_factory() as session, session.begin():
            await SynchronizationQueryService._authorize_read(
                session, actor_user_id, organization_id
            )
            installation = await session.scalar(
                select(ClientInstallation.id)
                .where(
                    ClientInstallation.id == client_installation_id,
                    ClientInstallation.user_id == actor_user_id,
                    ClientInstallation.installation_kind == "browser",
                    ClientInstallation.disabled_at.is_(None),
                )
                .with_for_update(of=ClientInstallation)
            )
            if installation is None:
                await session.execute(
                    insert(ClientInstallation)
                    .values(
                        id=client_installation_id,
                        user_id=actor_user_id,
                        installation_kind="browser",
                    )
                    .on_conflict_do_nothing(index_elements=("id",))
                )
                installation = await session.scalar(
                    select(ClientInstallation.id)
                    .where(
                        ClientInstallation.id == client_installation_id,
                        ClientInstallation.user_id == actor_user_id,
                        ClientInstallation.installation_kind == "browser",
                        ClientInstallation.disabled_at.is_(None),
                    )
                    .with_for_update(of=ClientInstallation)
                )
                if installation is None:
                    raise SyncPushDenied("browser installation access denied")
            system_admin = await session.scalar(
                select(SystemRoleAssignment.id).where(
                    SystemRoleAssignment.user_id == actor_user_id,
                    SystemRoleAssignment.role == "system_admin",
                    SystemRoleAssignment.revoked_at.is_(None),
                )
            )
            if system_admin is not None:
                return "system_admin"
            role = await session.scalar(
                select(OrganizationMembership.role).where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id == actor_user_id,
                    OrganizationMembership.state == "active",
                    OrganizationMembership.role.in_(("member", "organization_admin")),
                )
            )
            if role in ("member", "organization_admin"):
                return cast(Literal["member", "organization_admin"], role)
            raise SyncPushDenied("organization access denied")

    async def _dispatch(
        self,
        *,
        context: ExecutionContext,
        actor_role: Literal["member", "organization_admin", "system_admin"],
        organization_id: UUID,
        command: SyncCommand,
    ) -> PushCommandOutcome:
        if isinstance(command, UnsupportedSyncCommand):
            return await self._retain_adapter_rejection(
                context=context,
                actor_role=actor_role,
                organization_id=organization_id,
                command=command,
            )
        if command.organization_id != organization_id:
            return PushCommandOutcome(
                mutation_id=command.mutation_id,
                command_kind=_command_kind(command),
                status="rejected",
                replayed=False,
                first_change_sequence=None,
                last_change_sequence=None,
                error_code="organization_mismatch",
                field_violations=(),
                retry_same_identity=False,
            )
        try:
            result: (
                CreateEventResult
                | UpdateEventBaseAttendanceResult
                | EventLifecycleResult
                | DuplicateEventResult
                | UpdateEventPriceEstimatesResult
                | CreateShoppingListResult
                | RefreshShoppingListResult
                | CreateAdHocShoppingItemResult
                | SetAdHocShoppingItemFulfilmentResult
                | SetAdHocShoppingItemLifecycleResult
                | UpdateAdHocShoppingItemResult
                | CreateRecipeResult
                | CreateIngredientResult
                | ScheduleRecipeResult
                | MoveScheduledRecipeResult
                | ScheduledRecipeAttendanceResult
                | ScheduledRecipeContextResult
                | ScheduledRecipeLifecycleResult
                | ScheduledIngredientOverrideResult
                | ShoppingOperationResult
                | ReceiptResult
                | CatalogConfigurationResult
            )
            if isinstance(command, CreateEventCommand):
                result = await create_event(self._session_factory, context, command)
            elif isinstance(command, UpdateEventBaseAttendanceCommand):
                result = await update_event_base_attendance(self._session_factory, context, command)
            elif isinstance(command, SetEventLifecycleCommand):
                result = await set_event_lifecycle(self._session_factory, context, command)
            elif isinstance(command, DuplicateEventCommand):
                result = await duplicate_event(self._session_factory, context, command)
            elif isinstance(command, UpdateEventPriceEstimatesCommand):
                result = await update_event_price_estimates(self._session_factory, context, command)
            elif isinstance(command, RefreshShoppingListCommand):
                result = await refresh_shopping_list(self._session_factory, context, command)
            elif isinstance(command, CreateRecipeCommand):
                result = await create_recipe(self._session_factory, context, command)
            elif isinstance(command, PublishRecipeVersionCommand):
                result = await publish_recipe_version(self._session_factory, context, command)
            elif isinstance(command, CreateIngredientCommand):
                result = await create_ingredient(self._session_factory, context, command)
            elif isinstance(command, ScheduleRecipeCommand):
                result = await schedule_recipe(self._session_factory, context, command)
            elif isinstance(command, MoveScheduledRecipeCommand):
                result = await move_scheduled_recipe(self._session_factory, context, command)
            elif isinstance(command, SetScheduledRecipeAttendanceCommand):
                result = await set_scheduled_recipe_attendance(
                    self._session_factory, context, command
                )
            elif isinstance(command, SetScheduledRecipeContextCommand):
                result = await set_scheduled_recipe_context(
                    self._session_factory, context, command
                )
            elif isinstance(command, SetScheduledRecipeLifecycleCommand):
                result = await set_scheduled_recipe_lifecycle(
                    self._session_factory, context, command
                )
            elif isinstance(command, SetScheduledIngredientOverrideCommand):
                result = await set_scheduled_ingredient_override(
                    self._session_factory, context, command
                )
            elif isinstance(command, SetShoppingAvailableSupplyCommand):
                result = await set_shopping_available_supply(
                    self._session_factory, context, command
                )
            elif isinstance(command, SetShoppingManualPurchaseTargetCommand):
                result = await set_shopping_manual_purchase_target(
                    self._session_factory, context, command
                )
            elif isinstance(command, SetShoppingContributionFulfilmentCommand):
                result = await set_shopping_contribution_fulfilment(
                    self._session_factory, context, command
                )
            elif isinstance(command, SetShoppingRowFulfilmentCommand):
                result = await set_shopping_row_fulfilment(self._session_factory, context, command)
            elif isinstance(command, CreateAdHocShoppingItemCommand):
                result = await create_ad_hoc_shopping_item(self._session_factory, context, command)
            elif isinstance(command, SetAdHocShoppingItemFulfilmentCommand):
                result = await set_ad_hoc_shopping_item_fulfilment(
                    self._session_factory, context, command
                )
            elif isinstance(command, SetAdHocShoppingItemLifecycleCommand):
                result = await set_ad_hoc_shopping_item_lifecycle(
                    self._session_factory, context, command
                )
            elif isinstance(command, UpdateAdHocShoppingItemCommand):
                result = await update_ad_hoc_shopping_item(self._session_factory, context, command)
            elif isinstance(command, CreateReceiptCommand):
                result = await create_receipt(self._session_factory, context, command)
            elif isinstance(command, UpdateReceiptCommand):
                result = await update_receipt(self._session_factory, context, command)
            elif isinstance(command, SetReceiptLifecycleCommand):
                result = (
                    await retire_receipt(self._session_factory, context, command)
                    if command.operation == "retire"
                    else await restore_receipt(self._session_factory, context, command)
                )
            elif isinstance(command, CatalogConfigurationCommand):
                result = await mutate_catalog_configuration(
                    self._session_factory, context, command
                )
            else:
                result = await create_shopping_list(self._session_factory, context, command)
        except ApplicationServiceError as error:
            return PushCommandOutcome(
                mutation_id=command.mutation_id,
                command_kind=_command_kind(command),
                status="rejected",
                replayed=False,
                first_change_sequence=None,
                last_change_sequence=None,
                error_code=error.code,
                field_violations=tuple(
                    (violation.path, violation.code) for violation in error.field_violations
                ),
                retry_same_identity=error.retry_same_identity,
            )
        return self._result_outcome(result, command_kind=_command_kind(command))

    async def _retain_adapter_rejection(
        self,
        *,
        context: ExecutionContext,
        actor_role: Literal["member", "organization_admin", "system_admin"],
        organization_id: UUID,
        command: UnsupportedSyncCommand,
    ) -> PushCommandOutcome:
        async with self._session_factory() as session, session.begin():
            retained = await session.get(Mutation, command.mutation_id, with_for_update=True)
            if retained is not None:
                if (
                    retained.organization_id != organization_id
                    or retained.actor_user_id != context.actor_user_id
                    or retained.command_kind != command.command_kind
                    or retained.command_schema_version != SYNC_SCHEMA_VERSION
                    or retained.request_hash != command.request_hash
                    or retained.outcome != "rejected"
                ):
                    return PushCommandOutcome(
                        mutation_id=command.mutation_id,
                        command_kind=command.command_kind,
                        status="rejected",
                        replayed=False,
                        first_change_sequence=None,
                        last_change_sequence=None,
                        error_code="idempotency_mismatch",
                        field_violations=(),
                        retry_same_identity=False,
                    )
                return PushCommandOutcome(
                    mutation_id=command.mutation_id,
                    command_kind=command.command_kind,
                    status="rejected",
                    replayed=True,
                    first_change_sequence=None,
                    last_change_sequence=None,
                    error_code=command.rejection_code,
                    field_violations=(),
                    retry_same_identity=False,
                )
            session.add(
                Mutation(
                    id=command.mutation_id,
                    organization_id=organization_id,
                    is_system_administration_scope=False,
                    actor_user_id=context.actor_user_id,
                    actor_role=actor_role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=None,
                    oauth_grant_id=None,
                    client_wall_time=command.client_wall_time,
                    command_schema_version=SYNC_SCHEMA_VERSION,
                    command_kind=command.command_kind,
                    target_identities=[
                        {"entity_kind": "sync_command", "entity_id": str(command.mutation_id)}
                    ],
                    request_hash=command.request_hash,
                    outcome="rejected",
                    outcome_payload={
                        "error": {
                            "code": command.rejection_code,
                            "field_violations": [],
                            "retry_same_identity": False,
                        }
                    },
                    first_change_sequence=None,
                    last_change_sequence=None,
                )
            )
        return PushCommandOutcome(
            mutation_id=command.mutation_id,
            command_kind=command.command_kind,
            status="rejected",
            replayed=False,
            first_change_sequence=None,
            last_change_sequence=None,
            error_code=command.rejection_code,
            field_violations=(),
            retry_same_identity=False,
        )

    @staticmethod
    def _result_outcome(
        result: (
            CreateEventResult
            | UpdateEventBaseAttendanceResult
            | EventLifecycleResult
            | DuplicateEventResult
            | UpdateEventPriceEstimatesResult
            | CreateShoppingListResult
            | RefreshShoppingListResult
            | CreateAdHocShoppingItemResult
            | CreateRecipeResult
            | CreateIngredientResult
            | ScheduleRecipeResult
            | MoveScheduledRecipeResult
            | ScheduledRecipeAttendanceResult
            | ScheduledIngredientOverrideResult
            | ShoppingOperationResult
            | ReceiptResult
            | CatalogConfigurationResult
        ),
        *,
        command_kind: str | None = None,
    ) -> PushCommandOutcome:
        return PushCommandOutcome(
            mutation_id=result.mutation_id,
            command_kind=command_kind
            or (
                "event.create"
                if isinstance(result, CreateEventResult)
                else "event.update_base_attendance"
                if isinstance(result, UpdateEventBaseAttendanceResult)
                else "event.lifecycle"
                if isinstance(result, EventLifecycleResult)
                else "event.duplicate"
                if isinstance(result, DuplicateEventResult)
                else "event.update_price_estimates"
                if isinstance(result, UpdateEventPriceEstimatesResult)
                else "shopping_list.refresh"
                if isinstance(result, RefreshShoppingListResult)
                else "recipe.create"
                if isinstance(result, CreateRecipeResult)
                else "ingredient.create"
                if isinstance(result, CreateIngredientResult)
                else "scheduled_recipe.schedule"
                if isinstance(result, ScheduleRecipeResult)
                else "scheduled_recipe.move"
                if isinstance(result, MoveScheduledRecipeResult)
                else "scheduled_recipe.attendance"
                if isinstance(result, ScheduledRecipeAttendanceResult)
                else "scheduled_recipe.context"
                if isinstance(result, ScheduledRecipeContextResult)
                else "receipt.create"
                if isinstance(result, ReceiptResult)
                else "scheduled_recipe.ingredient_override"
                if isinstance(result, ScheduledIngredientOverrideResult)
                else "catalog_configuration.mutate"
                if isinstance(result, CatalogConfigurationResult)
                else "shopping_list.create"
            ),
            status=result.outcome,
            replayed=result.replayed,
            first_change_sequence=result.first_change_sequence,
            last_change_sequence=result.last_change_sequence,
            error_code=None,
            field_violations=(),
            retry_same_identity=True,
        )


class SynchronizationQueryService:
    """Authorize and page canonical organization changes without splitting commands."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        encoded_cursor_hmac_key: str,
        clock: Clock = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._cursor_codec = SyncCursorCodec(encoded_hmac_key=encoded_cursor_hmac_key)
        self._clock = clock

    async def pull(self, *, actor_user_id: UUID, request: PullRequest) -> PullResult:
        now = _normalize_now(self._clock())
        cursor = self._decode_cursor(request)
        async with self._session_factory() as session, session.begin():
            await self._authorize_read(session, actor_user_id, request.organization_id)
            if cursor is None:
                return await self._bootstrap_required(session, request.organization_id, now)

            head, physical_head = (
                await session.execute(
                    select(
                        select(OrganizationChangeHead.next_sequence)
                        .where(OrganizationChangeHead.organization_id == request.organization_id)
                        .scalar_subquery(),
                        select(func.max(OrganizationChange.sequence))
                        .where(OrganizationChange.organization_id == request.organization_id)
                        .scalar_subquery(),
                    )
                )
            ).one()
            if head is None:
                if physical_head is not None:
                    return await self._bootstrap_required(session, request.organization_id, now)
                current_sequence = 0
            else:
                current_sequence = head - 1
                if physical_head != current_sequence:
                    return await self._bootstrap_required(session, request.organization_id, now)
            if cursor.after_sequence > current_sequence:
                return await self._bootstrap_required(session, request.organization_id, now)
            if not await self._is_transaction_boundary(
                session, request.organization_id, cursor.after_sequence
            ):
                raise InvalidSyncCursor("invalid cursor")

            oldest_available_at, oldest_available_sequence = await self._oldest_available(
                session, request.organization_id
            )
            # A cursor at the head is always safe. In particular, an organization
            # with no retained records must not force an unnecessary bootstrap.
            if cursor.after_sequence == current_sequence:
                return PullResult(
                    status="ok",
                    sync_schema_version=SYNC_SCHEMA_VERSION,
                    server_time=now,
                    next_cursor=request.cursor,
                    transaction_groups=(),
                    oldest_available_at=oldest_available_at,
                )
            # The feed currently has no physical cleanup job. Do not treat
            # ``published_at`` as a retention boundary: PostgreSQL's
            # CURRENT_TIMESTAMP is the beginning of the publishing transaction,
            # not its commit time. Until a cleanup job has a trustworthy
            # commit-time marker, every stored change remains available. A real
            # physical gap still requires a bootstrap.
            if (
                oldest_available_sequence is not None
                and cursor.after_sequence < oldest_available_sequence - 1
            ):
                return PullResult(
                    status="bootstrap_required",
                    sync_schema_version=SYNC_SCHEMA_VERSION,
                    server_time=now,
                    next_cursor=None,
                    transaction_groups=(),
                    oldest_available_at=oldest_available_at,
                )

            transactions = (
                (
                    await session.execute(
                        select(OrganizationChangeTransaction)
                        .where(
                            OrganizationChangeTransaction.organization_id
                            == request.organization_id,
                            OrganizationChangeTransaction.last_change_sequence
                            > cursor.after_sequence,
                        )
                        .order_by(OrganizationChangeTransaction.first_change_sequence)
                        .limit(request.transaction_group_limit)
                    )
                )
                .scalars()
                .all()
            )
            # Feed rows are append-only in normal operation, but a partial
            # restore or manual repair must never let a replica advance past a
            # physical hole. Validate only this bounded page: a later gap is
            # checked before its page can advance a cursor.
            if not await self._page_is_contiguous(
                session,
                organization_id=request.organization_id,
                after_sequence=cursor.after_sequence,
                transactions=transactions,
            ):
                return await self._bootstrap_required(session, request.organization_id, now)
            groups = await self._load_groups(session, request.organization_id, transactions)
            next_sequence = groups[-1].last_sequence if groups else cursor.after_sequence
            return PullResult(
                status="ok",
                sync_schema_version=SYNC_SCHEMA_VERSION,
                server_time=now,
                next_cursor=self._cursor_codec.encode(
                    SyncCursor(
                        organization_id=request.organization_id,
                        after_sequence=next_sequence,
                    )
                ),
                transaction_groups=groups,
                oldest_available_at=oldest_available_at,
            )

    async def bootstrap(self, *, actor_user_id: UUID, organization_id: UUID) -> BootstrapResult:
        """Read one complete, repeatable-read canonical organization projection.

        This deliberately reads projection tables rather than the retained change
        feed: a history gap is precisely when a bootstrap is needed.
        """

        now = _normalize_now(self._clock())
        async with self._session_factory() as session, session.begin():
            # PostgreSQL requires this to be the transaction's first statement.
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            role = await self._authorize_read(session, actor_user_id, organization_id)
            head = await session.scalar(
                select(OrganizationChangeHead.next_sequence).where(
                    OrganizationChangeHead.organization_id == organization_id
                )
            )
            records = await _bootstrap_records(
                session, organization_id=organization_id, actor_user_id=actor_user_id, role=role
            )
            return BootstrapResult(
                sync_schema_version=SYNC_SCHEMA_VERSION,
                server_time=now,
                cursor=self._cursor_codec.encode(
                    SyncCursor(organization_id=organization_id, after_sequence=(head or 1) - 1)
                ),
                records=records,
            )

    async def change_hints(
        self, *, actor_user_id: UUID, organization_ids: Sequence[UUID]
    ) -> dict[UUID, str]:
        """Return the current opaque pull boundary after checking live access.

        Hints deliberately expose no records.  The normal pull endpoint remains
        the only source of authoritative synchronization data.
        """

        async with self._session_factory() as session, session.begin():
            for organization_id in organization_ids:
                try:
                    await self._authorize_read(session, actor_user_id, organization_id)
                except SyncQueryDenied as error:
                    raise SyncHintsDenied(organization_id) from error
            heads: dict[UUID, int] = dict(
                (
                    await session.execute(
                        select(
                            OrganizationChangeHead.organization_id,
                            OrganizationChangeHead.next_sequence,
                        ).where(OrganizationChangeHead.organization_id.in_(organization_ids))
                    )
                ).all()
            )
            return {
                organization_id: self._cursor_codec.encode(
                    SyncCursor(
                        organization_id=organization_id,
                        after_sequence=(heads.get(organization_id) or 1) - 1,
                    )
                )
                for organization_id in organization_ids
            }

    def _decode_cursor(self, request: PullRequest) -> SyncCursor | None:
        if request.cursor is None:
            return None
        cursor = self._cursor_codec.decode(request.cursor)
        if cursor.organization_id != request.organization_id:
            raise InvalidSyncCursor("invalid cursor")
        return cursor

    @staticmethod
    async def _authorize_read(
        session: AsyncSession, actor_user_id: UUID, organization_id: UUID
    ) -> Literal["member", "organization_admin", "system_admin"]:
        actor = await session.scalar(
            select(User.id)
            .where(User.id == actor_user_id, User.disabled_at.is_(None))
            .with_for_update(of=User)
        )
        organization = await session.scalar(
            select(Organization.id)
            .where(Organization.id == organization_id, Organization.retired_at.is_(None))
            .with_for_update(of=Organization)
        )
        if actor is None or organization is None:
            raise SyncQueryDenied("organization access denied")
        system_admin = await session.scalar(
            select(SystemRoleAssignment.id)
            .where(
                SystemRoleAssignment.user_id == actor_user_id,
                SystemRoleAssignment.role == "system_admin",
                SystemRoleAssignment.revoked_at.is_(None),
            )
            .with_for_update(of=SystemRoleAssignment)
        )
        if system_admin is not None:
            return "system_admin"
        membership = await session.scalar(
            select(OrganizationMembership.role)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == actor_user_id,
                OrganizationMembership.state == "active",
                OrganizationMembership.role.in_(("member", "organization_admin")),
            )
            .with_for_update(of=OrganizationMembership)
        )
        if membership not in ("member", "organization_admin"):
            raise SyncQueryDenied("organization access denied")
        return cast(Literal["member", "organization_admin"], membership)

    async def _bootstrap_required(
        self, session: AsyncSession, organization_id: UUID, now: datetime
    ) -> PullResult:
        oldest_available_at, _ = await self._oldest_available(session, organization_id)
        return PullResult(
            status="bootstrap_required",
            sync_schema_version=SYNC_SCHEMA_VERSION,
            server_time=now,
            next_cursor=None,
            transaction_groups=(),
            oldest_available_at=oldest_available_at,
        )

    @staticmethod
    async def _is_transaction_boundary(
        session: AsyncSession, organization_id: UUID, after_sequence: int
    ) -> bool:
        if after_sequence == 0:
            return True
        return (
            await session.scalar(
                select(OrganizationChangeTransaction.mutation_id).where(
                    OrganizationChangeTransaction.organization_id == organization_id,
                    OrganizationChangeTransaction.last_change_sequence == after_sequence,
                )
            )
            is not None
        )

    @staticmethod
    async def _page_is_contiguous(
        session: AsyncSession,
        *,
        organization_id: UUID,
        after_sequence: int,
        transactions: Sequence[OrganizationChangeTransaction],
    ) -> bool:
        """Verify complete, ordered command groups before a page advances a cursor."""
        if not transactions:
            return False
        expected_sequence = after_sequence + 1
        for transaction in transactions:
            first_sequence = transaction.first_change_sequence
            last_sequence = transaction.last_change_sequence
            if first_sequence != expected_sequence or last_sequence < first_sequence:
                return False
            expected_sequence = last_sequence + 1

        first_sequence = transactions[0].first_change_sequence
        last_sequence = transactions[-1].last_change_sequence
        changes = (
            (
                await session.execute(
                    select(OrganizationChange.sequence, OrganizationChange.mutation_id)
                    .where(
                        OrganizationChange.organization_id == organization_id,
                        OrganizationChange.sequence >= first_sequence,
                        OrganizationChange.sequence <= last_sequence,
                    )
                    .order_by(OrganizationChange.sequence)
                )
            )
            .tuples()
            .all()
        )
        transaction_index = 0
        expected_sequence = after_sequence + 1
        for sequence, mutation_id in changes:
            if sequence != expected_sequence or transaction_index >= len(transactions):
                return False
            transaction = transactions[transaction_index]
            if mutation_id != transaction.mutation_id:
                return False
            if sequence == transaction.last_change_sequence:
                transaction_index += 1
            expected_sequence += 1
        return expected_sequence == last_sequence + 1 and transaction_index == len(transactions)

    @staticmethod
    async def _oldest_available(
        session: AsyncSession, organization_id: UUID
    ) -> tuple[datetime | None, int | None]:
        oldest_available = (
            await session.execute(
                select(
                    func.min(OrganizationChange.published_at),
                    func.min(OrganizationChange.sequence),
                ).where(OrganizationChange.organization_id == organization_id)
            )
        ).one()
        return oldest_available[0], oldest_available[1]

    @staticmethod
    async def _load_groups(
        session: AsyncSession,
        organization_id: UUID,
        transactions: Sequence[OrganizationChangeTransaction],
    ) -> tuple[SyncTransactionGroup, ...]:
        if not transactions:
            return ()
        first_sequence = transactions[0].first_change_sequence
        last_sequence = transactions[-1].last_change_sequence
        changes = (
            (
                await session.execute(
                    select(OrganizationChange)
                    .where(
                        OrganizationChange.organization_id == organization_id,
                        OrganizationChange.sequence >= first_sequence,
                        OrganizationChange.sequence <= last_sequence,
                    )
                    .order_by(OrganizationChange.sequence)
                )
            )
            .scalars()
            .all()
        )
        by_mutation: dict[UUID, list[SyncRecord]] = {}
        for change in changes:
            by_mutation.setdefault(change.mutation_id, []).append(
                SyncRecord(
                    organization_id=organization_id,
                    sequence=change.sequence,
                    entity_id=change.entity_id,
                    entity_kind=change.entity_kind,
                    operation=change.operation,
                    payload=change.payload,
                )
            )
        return tuple(
            SyncTransactionGroup(
                mutation_id=transaction.mutation_id,
                first_sequence=transaction.first_change_sequence,
                last_sequence=transaction.last_change_sequence,
                records=tuple(by_mutation.get(transaction.mutation_id, ())),
            )
            for transaction in transactions
        )


def _bootstrap_record(
    kind: str, entity_id: UUID, record: dict[str, object], organization_id: UUID
) -> SyncRecord:
    return SyncRecord(
        organization_id=organization_id,
        sequence=0,
        entity_id=entity_id,
        entity_kind=kind,
        operation="upsert",
        payload={"record_schema_version": 1, "record": record},
    )


def _uuid(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decimal(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None


async def _bootstrap_records(
    session: AsyncSession,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    role: Literal["member", "organization_admin", "system_admin"],
) -> tuple[SyncRecord, ...]:
    """Project the protocol's explicitly supported records from current tables.

    Keeping these record shapes next to their source tables is intentional: it
    prevents a new persisted field from being silently exported by reflection.
    """

    records: list[SyncRecord] = []

    def append(kind: str, entity_id: UUID, record: dict[str, object]) -> None:
        records.append(_bootstrap_record(kind, entity_id, record, organization_id))

    configuration_clocks = {
        (clock.entity_kind, clock.entity_id, clock.field_name): clock
        for clock in (
            await session.scalars(
                select(FieldClock).where(
                    FieldClock.organization_id == organization_id,
                    FieldClock.entity_kind.in_(
                        ("store_section", "recipe_tag", "dietary_tag", "unit_definition")
                    ),
                )
            )
        ).all()
    }

    def configuration_clock_record(kind: str, entity_id: UUID) -> dict[str, object]:
        return {
            name: (
                {
                    "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                    "winning_mutation_id": str(clock.winning_mutation_id),
                }
                if (clock := configuration_clocks.get((kind, entity_id, name)))
                else None
            )
            for name in {
                "store_section": ("name", "position_key", "lifecycle"),
                "recipe_tag": ("name", "color", "lifecycle"),
                "dietary_tag": ("name", "color", "lifecycle"),
                "unit_definition": ("custom_name", "lifecycle"),
            }[kind]
        }

    organization = await session.get(Organization, organization_id)
    if organization is None:  # authorization already established this invariant.
        raise RuntimeError("Authorized organization disappeared")
    append(
        "organization",
        organization.id,
        {
            "id": str(organization.id),
            "name": organization.name,
            "description": organization.description,
            "default_currency": organization.default_currency,
            "created_at": organization.created_at.isoformat(),
            "created_by_user_id": str(organization.created_by_user_id),
            "retired_at": _time(organization.retired_at),
            "retired_by_user_id": _uuid(organization.retired_by_user_id),
        },
    )
    append(
        "organization_capabilities",
        organization.id,
        {
            "organization_id": str(organization.id),
            "actor_user_id": str(actor_user_id),
            "role": role,
            "can_manage_organization": role in ("organization_admin", "system_admin"),
        },
    )

    for item in (
        await session.execute(
            select(StoreSection)
            .where(StoreSection.organization_id == organization_id)
            .order_by(StoreSection.id)
        )
    ).scalars():
        append(
            "store_section",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "name": item.name,
                "normalized_name": item.normalized_name,
                "position_key": item.position_key,
                "created_at": item.created_at.isoformat(),
                "created_by_user_id": str(item.created_by_user_id),
                "retired_at": _time(item.retired_at),
                "retired_by_user_id": _uuid(item.retired_by_user_id),
                "field_clocks": configuration_clock_record("store_section", item.id),
            },
        )
    for item in (
        await session.execute(
            select(OrganizationMealRolePreset)
            .where(OrganizationMealRolePreset.organization_id == organization_id)
            .order_by(OrganizationMealRolePreset.id)
        )
    ).scalars():
        append(
            "organization_meal_role_preset",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "built_in_translation_key": item.built_in_translation_key,
                "custom_name": item.custom_name,
                "normalized_custom_name": item.normalized_custom_name,
                "position_key": item.position_key,
                "created_at": item.created_at.isoformat(),
                "created_by_user_id": str(item.created_by_user_id),
                "retired_at": _time(item.retired_at),
                "retired_by_user_id": _uuid(item.retired_by_user_id),
                "field_clocks": configuration_clock_record("recipe_tag", item.id),
            },
        )
    for item in (
        await session.execute(
            select(RecipeTag)
            .where(RecipeTag.organization_id == organization_id)
            .order_by(RecipeTag.id)
        )
    ).scalars():
        append(
            "recipe_tag",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "name": item.name,
                "normalized_name": item.normalized_name,
                "color": item.color,
                "created_at": item.created_at.isoformat(),
                "created_by_user_id": str(item.created_by_user_id),
                "retired_at": _time(item.retired_at),
                "retired_by_user_id": _uuid(item.retired_by_user_id),
                "field_clocks": configuration_clock_record("dietary_tag", item.id),
            },
        )
    for item in (
        await session.execute(
            select(DietaryTag)
            .where(DietaryTag.organization_id == organization_id)
            .order_by(DietaryTag.id)
        )
    ).scalars():
        append(
            "dietary_tag",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "seed_key": item.seed_key,
                "name": item.name,
                "normalized_name": item.normalized_name,
                "color": item.color,
                "created_at": item.created_at.isoformat(),
                "created_by_user_id": str(item.created_by_user_id),
                "retired_at": _time(item.retired_at),
                "retired_by_user_id": _uuid(item.retired_by_user_id),
                "field_clocks": configuration_clock_record("unit_definition", item.id),
            },
        )
    for item in (
        await session.execute(
            select(UnitDefinition)
            .where(
                (UnitDefinition.organization_id == organization_id)
                | UnitDefinition.organization_id.is_(None)
            )
            .order_by(UnitDefinition.id)
        )
    ).scalars():
        append(
            "unit_definition",
            item.id,
            {
                "id": str(item.id),
                "organization_id": _uuid(item.organization_id),
                "code": item.code,
                "custom_name": item.custom_name,
                "normalized_custom_name": item.normalized_custom_name,
                "dimension": item.dimension,
                "base_unit_factor": _decimal(item.base_unit_factor),
                "rounds_up_to_whole_unit": item.rounds_up_to_whole_unit,
                "allows_ingredient_quantity": item.allows_ingredient_quantity,
                "allows_recipe_scaling": item.allows_recipe_scaling,
                "created_at": item.created_at.isoformat(),
                "created_by_user_id": _uuid(item.created_by_user_id),
                "retired_at": _time(item.retired_at),
                "retired_by_user_id": _uuid(item.retired_by_user_id),
            },
        )

    ingredient_tags: dict[UUID, list[str]] = {}
    for version_id, tag_id in (
        await session.execute(
            select(
                IngredientVersionDietaryTag.ingredient_version_id,
                IngredientVersionDietaryTag.dietary_tag_id,
            ).where(IngredientVersionDietaryTag.organization_id == organization_id)
        )
    ).all():
        ingredient_tags.setdefault(version_id, []).append(str(tag_id))
    for item in (
        await session.execute(
            select(Ingredient)
            .where(Ingredient.organization_id == organization_id)
            .order_by(Ingredient.id)
        )
    ).scalars():
        append(
            "ingredient",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "current_version_id": _uuid(item.current_version_id),
                "current_price_estimate_id": _uuid(item.current_price_estimate_id),
                "created_at": item.created_at.isoformat(),
                "created_by_user_id": str(item.created_by_user_id),
                "retired_at": _time(item.retired_at),
                "retired_by_user_id": _uuid(item.retired_by_user_id),
            },
        )
    for item in (
        await session.execute(
            select(IngredientVersion)
            .where(IngredientVersion.organization_id == organization_id)
            .order_by(IngredientVersion.id)
        )
    ).scalars():
        append(
            "ingredient_version",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "ingredient_id": str(item.ingredient_id),
                "based_on_version_id": _uuid(item.based_on_version_id),
                "name": item.name,
                "normalized_name": item.normalized_name,
                "canonical_unit_id": str(item.canonical_unit_id),
                "mass_per_canonical_quantity": _decimal(item.mass_per_canonical_quantity),
                "default_store_section_id": _uuid(item.default_store_section_id),
                "dietary_tag_ids": sorted(ingredient_tags.get(item.id, [])),
                "published_at": item.published_at.isoformat(),
                "published_by_user_id": str(item.published_by_user_id),
                "immutable": True,
            },
        )
    for item in (
        await session.execute(
            select(IngredientPriceEstimate)
            .where(IngredientPriceEstimate.organization_id == organization_id)
            .order_by(IngredientPriceEstimate.id)
        )
    ).scalars():
        append(
            "ingredient_price_estimate",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "ingredient_id": str(item.ingredient_id),
                "based_on_estimate_id": _uuid(item.based_on_estimate_id),
                "state": item.state,
                "price_amount": _decimal(item.price_amount),
                "priced_quantity": _decimal(item.priced_quantity),
                "priced_unit_id": _uuid(item.priced_unit_id),
                "currency": item.currency,
                "published_at": item.published_at.isoformat(),
                "published_by_user_id": str(item.published_by_user_id),
                "immutable": True,
            },
        )

    version_tags: dict[UUID, list[str]] = {}
    for version_id, tag_id in (
        await session.execute(
            select(RecipeVersionTag.recipe_version_id, RecipeVersionTag.recipe_tag_id).where(
                RecipeVersionTag.organization_id == organization_id
            )
        )
    ).all():
        version_tags.setdefault(version_id, []).append(str(tag_id))
    for item in (
        await session.execute(
            select(Recipe).where(Recipe.organization_id == organization_id).order_by(Recipe.id)
        )
    ).scalars():
        append(
            "recipe",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "current_version_id": _uuid(item.current_version_id),
                "created_at": item.created_at.isoformat(),
                "created_by_user_id": str(item.created_by_user_id),
                "retired_at": _time(item.retired_at),
                "retired_by_user_id": _uuid(item.retired_by_user_id),
            },
        )
    for item in (
        await session.execute(
            select(RecipeVersion)
            .where(RecipeVersion.organization_id == organization_id)
            .order_by(RecipeVersion.id)
        )
    ).scalars():
        append(
            "recipe_version",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "recipe_id": str(item.recipe_id),
                "based_on_version_id": _uuid(item.based_on_version_id),
                "name": item.name,
                "description": item.description,
                "scaling_model": item.scaling_model,
                "scaling_unit_id": str(item.scaling_unit_id),
                "base_scaling_amount": _decimal(item.base_scaling_amount),
                "estimated_diners_per_scaling_unit": _decimal(
                    item.estimated_diners_per_scaling_unit
                ),
                "round_suggestions_up": item.round_suggestions_up,
                "published_at": item.published_at.isoformat(),
                "published_by_user_id": str(item.published_by_user_id),
                "immutable": True,
            },
        )
    for version_id, tag_ids in version_tags.items():
        for tag_id in tag_ids:
            association_id = recipe_version_tag_change_id(version_id, UUID(tag_id))
            append(
                "recipe_version_tag",
                association_id,
                {
                    "id": str(association_id),
                    "recipe_version_id": str(version_id),
                    "recipe_tag_id": tag_id,
                    "organization_id": str(organization_id),
                },
            )
    for item in (
        await session.execute(
            select(RecipeVersionIngredientLine)
            .where(RecipeVersionIngredientLine.organization_id == organization_id)
            .order_by(RecipeVersionIngredientLine.id)
        )
    ).scalars():
        append(
            "recipe_ingredient_line",
            item.id,
            {
                "id": str(item.id),
                "organization_id": str(item.organization_id),
                "recipe_id": str(item.recipe_id),
                "recipe_version_id": str(item.recipe_version_id),
                "line_key": str(item.line_key),
                "ingredient_version_id": str(item.ingredient_version_id),
                "base_quantity": _decimal(item.base_quantity),
                "preferred_display_unit_id": _uuid(item.preferred_display_unit_id),
                "note": item.note,
                "position_key": item.position_key,
                "scaling_behavior": item.scaling_behavior,
                "include_in_portion_weight": item.include_in_portion_weight,
                "immutable": True,
            },
        )

    events = (
        (
            await session.execute(
                select(Event).where(Event.organization_id == organization_id).order_by(Event.id)
            )
        )
        .scalars()
        .all()
    )
    active_ids = tuple(event.id for event in events if event.lifecycle == "active")
    clocks = {
        (clock.entity_kind, clock.entity_id, clock.field_name): clock
        for clock in (
            await session.execute(
                select(FieldClock).where(FieldClock.organization_id == organization_id)
            )
        ).scalars()
    }
    for item in events:
        append(
            *_event_change_record(item, clocks.get(("event", item.id, "base_expected_attendance")))
        )
    if not active_ids:
        return tuple(records)

    for item in (
        await session.execute(
            select(EventDay).where(EventDay.event_id.in_(active_ids)).order_by(EventDay.id)
        )
    ).scalars():
        append(
            "event_day",
            item.id,
            {
                "id": str(item.id),
                "event_id": str(item.event_id),
                "calendar_date": item.calendar_date.isoformat(),
                "note": item.note,
                "is_visible": item.is_visible,
                "provenance": item.provenance,
                "created_at": item.created_at.isoformat(),
                "created_by_user_id": str(item.created_by_user_id),
                "retired_at": _time(item.retired_at),
                "retired_by_user_id": _uuid(item.retired_by_user_id),
            },
        )
    for item in (
        await session.execute(
            select(EventMealRole)
            .where(EventMealRole.event_id.in_(active_ids))
            .order_by(EventMealRole.id)
        )
    ).scalars():
        append(
            "event_meal_role",
            item.id,
            {
                "id": str(item.id),
                "event_id": str(item.event_id),
                "source_preset_id": _uuid(item.source_preset_id),
                "built_in_translation_key": item.built_in_translation_key,
                "custom_name": item.custom_name,
                "normalized_custom_name": item.normalized_custom_name,
                "position_key": item.position_key,
                "created_at": item.created_at.isoformat(),
                "created_by_user_id": str(item.created_by_user_id),
                "retired_at": _time(item.retired_at),
                "retired_by_user_id": _uuid(item.retired_by_user_id),
            },
        )
    for item in (
        await session.execute(
            select(ScheduledRecipe)
            .where(
                ScheduledRecipe.organization_id == organization_id,
                ScheduledRecipe.event_id.in_(active_ids),
            )
            .order_by(ScheduledRecipe.id)
        )
    ).scalars():
        kind, entity_id, record = _scheduled_recipe_change_record(
            item, clocks.get(("scheduled_recipe", item.id, "placement"))
        )
        field_clocks = record["field_clocks"]
        assert isinstance(field_clocks, dict)
        field_clocks.update(_clock_fields(clocks, kind, entity_id))
        append(kind, entity_id, record)
    for item in (
        await session.execute(
            select(ScheduledIngredientOverride)
            .where(
                ScheduledIngredientOverride.organization_id == organization_id,
                ScheduledIngredientOverride.event_id.in_(active_ids),
            )
            .order_by(ScheduledIngredientOverride.id)
        )
    ).scalars():
        record = scheduled_recipe_overrides._record(item)
        record["field_clocks"] = _clock_fields(clocks, "scheduled_ingredient_override", item.id)
        append("scheduled_ingredient_override", item.id, record)
    for item in (
        await session.execute(
            select(EventIngredientPrice)
            .where(
                EventIngredientPrice.organization_id == organization_id,
                EventIngredientPrice.event_id.in_(active_ids),
            )
            .order_by(EventIngredientPrice.id)
        )
    ).scalars():
        if item.current_snapshot_id is not None:
            append("event_ingredient_price", item.id, _price_pointer_record(item))
    for item in (
        await session.execute(
            select(EventIngredientPriceSnapshot)
            .where(
                EventIngredientPriceSnapshot.organization_id == organization_id,
                EventIngredientPriceSnapshot.event_id.in_(active_ids),
            )
            .order_by(EventIngredientPriceSnapshot.id)
        )
    ).scalars():
        append("event_ingredient_price_snapshot", item.id, _snapshot_record(item))
    for item in (
        await session.execute(
            select(ShoppingList)
            .where(
                ShoppingList.organization_id == organization_id,
                ShoppingList.event_id.in_(active_ids),
            )
            .order_by(ShoppingList.id)
        )
    ).scalars():
        append("shopping_list", item.id, await _shopping_list_record(session, item, clocks))
    for item in (
        await session.execute(
            select(ShoppingGenerationRevision)
            .where(
                ShoppingGenerationRevision.organization_id == organization_id,
                ShoppingGenerationRevision.event_id.in_(active_ids),
            )
            .order_by(ShoppingGenerationRevision.id)
        )
    ).scalars():
        append(*_generation_revision_record(item))
    for item in (
        await session.execute(
            select(ShoppingRevisionSource)
            .where(
                ShoppingRevisionSource.organization_id == organization_id,
                ShoppingRevisionSource.event_id.in_(active_ids),
            )
            .order_by(
                ShoppingRevisionSource.generation_revision_id,
                ShoppingRevisionSource.scheduled_recipe_id,
            )
        )
    ).scalars():
        append(*_revision_source_record(item))
    for item in (
        await session.execute(
            select(ShoppingIngredientRow)
            .where(
                ShoppingIngredientRow.organization_id == organization_id,
                ShoppingIngredientRow.event_id.in_(active_ids),
            )
            .order_by(ShoppingIngredientRow.id)
        )
    ).scalars():
        append(*await _row_record(session, item, clocks))
    for item in (
        await session.execute(
            select(ShoppingContribution)
            .where(
                ShoppingContribution.organization_id == organization_id,
                ShoppingContribution.event_id.in_(active_ids),
            )
            .order_by(ShoppingContribution.id)
        )
    ).scalars():
        append(*await _contribution_record(session, item, clocks))
    for item in (
        await session.execute(
            select(ShoppingContributionSnapshot)
            .where(
                ShoppingContributionSnapshot.organization_id == organization_id,
                ShoppingContributionSnapshot.event_id.in_(active_ids),
            )
            .order_by(ShoppingContributionSnapshot.id)
        )
    ).scalars():
        append(*_contribution_snapshot_record(item))
    for item in (
        await session.execute(
            select(AdHocShoppingItem)
            .where(
                AdHocShoppingItem.organization_id == organization_id,
                AdHocShoppingItem.event_id.in_(active_ids),
            )
            .order_by(AdHocShoppingItem.id)
        )
    ).scalars():
        record = {
            "id": str(item.id),
            "organization_id": str(item.organization_id),
            "event_id": str(item.event_id),
            "shopping_list_id": str(item.shopping_list_id),
            "name": item.name,
            "target_amount": _decimal(item.target_amount),
            "unit_id": str(item.unit_id),
            "store_section_id": str(item.store_section_id),
            "note": item.note,
            "fulfilment_credit": _decimal(item.fulfilment_credit),
            "fulfilment_updated_at": _time(item.fulfilment_updated_at),
            "fulfilment_updated_by_user_id": _uuid(item.fulfilment_updated_by_user_id),
            "fulfilment_updated_by_installation_id": _uuid(
                item.fulfilment_updated_by_installation_id
            ),
            "created_at": item.created_at.isoformat(),
            "created_by_user_id": str(item.created_by_user_id),
            "retired_at": _time(item.retired_at),
            "retired_by_user_id": _uuid(item.retired_by_user_id),
        }
        record["field_clocks"] = _clock_fields(clocks, "ad_hoc_shopping_item", item.id)
        append("ad_hoc_shopping_item", item.id, record)
    for item in (
        await session.execute(
            select(Receipt)
            .where(Receipt.organization_id == organization_id, Receipt.event_id.in_(active_ids))
            .order_by(Receipt.id)
        )
    ).scalars():
        record = _receipt_record(item)
        record["field_clocks"] = _clock_fields(clocks, "receipt", item.id)
        append("receipt", item.id, record)
    for item in (
        await session.execute(
            select(ReceiptAttachment)
            .join(Receipt, Receipt.id == ReceiptAttachment.receipt_id)
            .where(
                ReceiptAttachment.organization_id == organization_id,
                Receipt.event_id.in_(active_ids),
            )
            .order_by(ReceiptAttachment.id)
        )
    ).scalars():
        append("receipt_attachment", item.id, _attachment_record(item))
    return tuple(records)


def _clock_fields(
    clocks: dict[tuple[str, UUID, str], FieldClock], entity_kind: str, entity_id: UUID
) -> dict[str, dict[str, str]]:
    return {
        field_name: {
            "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
            "winning_mutation_id": str(clock.winning_mutation_id),
        }
        for (kind, clock_entity_id, field_name), clock in clocks.items()
        if kind == entity_kind and clock_entity_id == entity_id
    }
