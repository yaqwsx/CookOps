"""Guarded event archive and reactivation commands."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.inspection import inspect

from cookops.application.events import (
    _authorize_and_lock_organization,
    _event_change_record,
    _reserve_change_range,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import (
    AdHocShoppingItem,
    DietaryTag,
    Event,
    EventArchiveSnapshot,
    EventDay,
    EventIngredientPrice,
    EventIngredientPriceSnapshot,
    EventMealRole,
    FieldClock,
    Ingredient,
    IngredientVersion,
    IngredientVersionDietaryTag,
    Mutation,
    OrganizationChange,
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
    UnitDefinition,
    User,
)

COMMAND_KIND = "event.lifecycle"
COMMAND_SCHEMA_VERSION = 1
ARCHIVE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SetEventLifecycleCommand:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    operation: Literal["archive", "reactivate"]
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventLifecycleResult:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    lifecycle: Literal["active", "archived"]
    archive_snapshot_id: UUID | None
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


def _value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bytes):
        return value.hex()
    return value


def _row(value: Any) -> dict[str, object]:
    return {
        column.key: _value(getattr(value, column.key)) for column in inspect(value).mapper.columns
    }


async def _rows(session: AsyncSession, model: type[Any], criterion: Any) -> list[dict[str, object]]:
    values = (
        (
            await session.execute(
                select(model).where(criterion).order_by(*inspect(model).primary_key)
            )
        )
        .scalars()
        .all()
    )
    return [_row(value) for value in values]


async def _selected(session: AsyncSession, statement: Any) -> list[Any]:
    return list((await session.execute(statement)).scalars().all())


async def _archive_payload(
    session: AsyncSession, event: Event, archive_actor_id: UUID
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Copy every current event-owned row plus pinned immutable recipe inputs."""

    schedules = await _selected(
        session, select(ScheduledRecipe).where(ScheduledRecipe.event_id == event.id)
    )
    days = await _selected(session, select(EventDay).where(EventDay.event_id == event.id))
    roles = await _selected(
        session, select(EventMealRole).where(EventMealRole.event_id == event.id)
    )
    schedule_ids = tuple(item.id for item in schedules)
    recipe_version_ids = tuple({item.recipe_version_id for item in schedules})
    prices = await _selected(
        session, select(EventIngredientPrice).where(EventIngredientPrice.event_id == event.id)
    )
    price_ids = tuple(item.id for item in prices)
    price_snapshots = (
        await _selected(
            session,
            select(EventIngredientPriceSnapshot).where(
                EventIngredientPriceSnapshot.event_ingredient_price_id.in_(price_ids)
            ),
        )
        if price_ids
        else []
    )
    lists = await _selected(session, select(ShoppingList).where(ShoppingList.event_id == event.id))
    list_ids = tuple(item.id for item in lists)
    revisions = (
        await _selected(
            session,
            select(ShoppingGenerationRevision).where(
                ShoppingGenerationRevision.shopping_list_id.in_(list_ids)
            ),
        )
        if list_ids
        else []
    )
    revision_ids = tuple(item.id for item in revisions)
    shopping_rows = (
        await _selected(
            session,
            select(ShoppingIngredientRow).where(
                ShoppingIngredientRow.shopping_list_id.in_(list_ids)
            ),
        )
        if list_ids
        else []
    )
    contributions = (
        await _selected(
            session,
            select(ShoppingContribution).where(ShoppingContribution.shopping_list_id.in_(list_ids)),
        )
        if list_ids
        else []
    )
    contribution_snapshots = (
        await _selected(
            session,
            select(ShoppingContributionSnapshot).where(
                ShoppingContributionSnapshot.generation_revision_id.in_(revision_ids)
            ),
        )
        if revision_ids
        else []
    )
    ad_hoc_items = (
        await _selected(
            session,
            select(AdHocShoppingItem).where(AdHocShoppingItem.shopping_list_id.in_(list_ids)),
        )
        if list_ids
        else []
    )
    receipts = await _selected(session, select(Receipt).where(Receipt.event_id == event.id))
    receipt_ids = tuple(item.id for item in receipts)
    attachments = (
        await _selected(
            session, select(ReceiptAttachment).where(ReceiptAttachment.receipt_id.in_(receipt_ids))
        )
        if receipt_ids
        else []
    )
    if any(item.storage_state == "pending" and item.retired_at is None for item in attachments):
        raise ApplicationServiceError(
            "validation_failed",
            field_violations=(FieldViolation("attachments", "must_be_finalized_before_archive"),),
            retry_same_identity=False,
        )
    overrides = (
        await _selected(
            session,
            select(ScheduledIngredientOverride).where(
                ScheduledIngredientOverride.scheduled_recipe_id.in_(schedule_ids)
            ),
        )
        if schedule_ids
        else []
    )
    lines = (
        await _selected(
            session,
            select(RecipeVersionIngredientLine).where(
                RecipeVersionIngredientLine.recipe_version_id.in_(recipe_version_ids)
            ),
        )
        if recipe_version_ids
        else []
    )
    ingredient_version_ids = tuple({item.ingredient_version_id for item in (*lines, *overrides)})
    ingredient_ids = tuple({item.ingredient_id for item in (*lines, *overrides)})
    recipe_versions = (
        await _selected(
            session, select(RecipeVersion).where(RecipeVersion.id.in_(recipe_version_ids))
        )
        if recipe_version_ids
        else []
    )
    ingredient_versions = (
        await _selected(
            session,
            select(IngredientVersion).where(IngredientVersion.id.in_(ingredient_version_ids)),
        )
        if ingredient_version_ids
        else []
    )
    user_ids = {event.created_by_user_id, archive_actor_id}
    for group in (
        schedules,
        days,
        roles,
        overrides,
        receipts,
        attachments,
        prices,
        price_snapshots,
        lists,
        revisions,
        shopping_rows,
        contributions,
        contribution_snapshots,
        ad_hoc_items,
        recipe_versions,
        ingredient_versions,
    ):
        for item in group:
            for column in inspect(item).mapper.columns:
                if not column.key.endswith("_by_user_id"):
                    continue
                value = getattr(item, column.key)
                if value is not None:
                    user_ids.add(value)
    payload: dict[str, object] = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "event": _row(event),
        "event_days": [_row(value) for value in days],
        "event_meal_roles": [_row(value) for value in roles],
        "scheduled_recipes": [_row(value) for value in schedules],
        "scheduled_ingredient_overrides": [_row(value) for value in overrides],
        "event_ingredient_prices": [_row(value) for value in prices],
        "event_ingredient_price_snapshots": [_row(value) for value in price_snapshots],
        "shopping_lists": [_row(value) for value in lists],
        "shopping_generation_revisions": [_row(value) for value in revisions],
        "shopping_revision_sources": await _rows(
            session,
            ShoppingRevisionSource,
            ShoppingRevisionSource.generation_revision_id.in_(revision_ids),
        )
        if revision_ids
        else [],
        "shopping_ingredient_rows": [_row(value) for value in shopping_rows],
        "shopping_contributions": [_row(value) for value in contributions],
        "shopping_contribution_snapshots": [_row(value) for value in contribution_snapshots],
        "ad_hoc_shopping_items": [_row(value) for value in ad_hoc_items],
        "receipts": [_row(value) for value in receipts],
        "receipt_attachments": [_row(value) for value in attachments],
        "recipe_versions": [_row(value) for value in recipe_versions],
        "recipes": await _rows(
            session, Recipe, Recipe.id.in_(tuple({item.recipe_id for item in schedules}))
        )
        if schedules
        else [],
        "recipe_version_lines": [_row(value) for value in lines],
        "recipe_version_tags": await _rows(
            session, RecipeVersionTag, RecipeVersionTag.recipe_version_id.in_(recipe_version_ids)
        )
        if recipe_version_ids
        else [],
        "recipe_tags": await _rows(
            session,
            RecipeTag,
            RecipeTag.organization_id == event.organization_id,
        ),
        "ingredients": await _rows(session, Ingredient, Ingredient.id.in_(ingredient_ids))
        if ingredient_ids
        else [],
        "ingredient_versions": [_row(value) for value in ingredient_versions],
        "ingredient_version_dietary_tags": await _rows(
            session,
            IngredientVersionDietaryTag,
            IngredientVersionDietaryTag.ingredient_version_id.in_(ingredient_version_ids),
        )
        if ingredient_version_ids
        else [],
        "units": await _rows(
            session,
            UnitDefinition,
            or_(
                UnitDefinition.organization_id == event.organization_id,
                UnitDefinition.organization_id.is_(None),
            ),
        ),
        "dietary_tags": await _rows(
            session,
            DietaryTag,
            DietaryTag.organization_id == event.organization_id,
        ),
        "store_sections": await _rows(
            session,
            StoreSection,
            StoreSection.organization_id == event.organization_id,
        ),
        # Dietary exceptions and derived warnings are not modeled by this MVP schema;
        # preserve their explicit empty historical projection rather than infer them later.
        "dietary_exceptions": [],
        "resolved_dietary_warnings": [],
        "field_clocks": await _rows(
            session,
            FieldClock,
            (FieldClock.organization_id == event.organization_id)
            & (FieldClock.entity_id.in_((event.id, *schedule_ids))),
        ),
        "attribution_users": await _rows(session, User, User.id.in_(tuple(user_ids))),
    }
    manifest = [
        {
            "attachment_id": str(item.id),
            "content_hash": item.content_hash.hex(),
            "byte_size": item.byte_size,
        }
        for item in attachments
        if item.storage_state == "ready"
        and item.retired_at is None
        and item.content_hash is not None
        and item.byte_size is not None
    ]
    return payload, manifest


def _request_hash(command: SetEventLifecycleCommand) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {
                "event_id": str(command.event_id),
                "organization_id": str(command.organization_id),
                "operation": command.operation,
                "logical_operation_id": str(command.logical_operation_id)
                if command.logical_operation_id
                else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _result_from_mutation(mutation: Mutation) -> EventLifecycleResult:
    payload = mutation.outcome_payload or {}
    try:
        return EventLifecycleResult(
            mutation.id,
            UUID(str(payload["event_id"])),
            UUID(str(payload["organization_id"])),
            cast(Literal["active", "archived"], payload["lifecycle"]),
            UUID(str(payload["archive_snapshot_id"]))
            if payload.get("archive_snapshot_id")
            else None,
            cast(int, mutation.first_change_sequence),
            cast(int, mutation.last_change_sequence),
            True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Event lifecycle retained an invalid outcome") from error


def _error_payload(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": violation.path, "code": violation.code}
                for violation in error.field_violations
            ],
        }
    }


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    error = (mutation.outcome_payload or {}).get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise RuntimeError("Event lifecycle retained an invalid rejection")
    violations = error.get("field_violations", [])
    if not isinstance(violations, list):
        raise RuntimeError("Event lifecycle retained invalid field violations")
    return ApplicationServiceError(
        cast(Literal["client_time_too_far_ahead", "validation_failed"], error["code"]),
        field_violations=tuple(
            FieldViolation(cast(str, item["path"]), cast(str, item["code"]))
            for item in violations
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("code"), str)
        ),
        retry_same_identity=False,
    )


async def _retain_rejection(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetEventLifecycleCommand,
    request_hash: bytes,
    error: ApplicationServiceError,
) -> None:
    async with session_factory() as session, session.begin():
        role, _ = await _authorize_and_lock_organization(session, context, command.organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            return
        session.add(
            Mutation(
                id=command.mutation_id,
                logical_operation_id=command.logical_operation_id,
                organization_id=command.organization_id,
                is_system_administration_scope=False,
                actor_user_id=context.actor_user_id,
                actor_role=role,
                client_installation_id=context.client_installation_id,
                oauth_client_id=context.oauth_client_id,
                oauth_grant_id=context.oauth_grant_id,
                client_wall_time=command.client_wall_time,
                command_schema_version=COMMAND_SCHEMA_VERSION,
                command_kind=COMMAND_KIND,
                target_identities=[{"entity_kind": "event", "entity_id": str(command.event_id)}],
                request_hash=request_hash,
                outcome="rejected",
                outcome_payload=_error_payload(error),
                first_change_sequence=None,
                last_change_sequence=None,
            )
        )


async def set_event_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetEventLifecycleCommand,
) -> EventLifecycleResult:
    request_hash = _request_hash(command)
    try:
        return await _set_event_lifecycle(session_factory, context, command, request_hash)
    except ApplicationServiceError as error:
        if error.code in ("validation_failed", "client_time_too_far_ahead"):
            await _retain_rejection(session_factory, context, command, request_hash, error)
        raise


async def _set_event_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetEventLifecycleCommand,
    request_hash: bytes,
) -> EventLifecycleResult:
    if (
        command.operation not in ("archive", "reactivate")
        or not all(
            isinstance(value, UUID)
            for value in (command.mutation_id, command.event_id, command.organization_id)
        )
        or command.client_wall_time.tzinfo is None
    ):
        raise ApplicationServiceError(
            "validation_failed",
            field_violations=(FieldViolation("command", "invalid"),),
            retry_same_identity=False,
        )
    async with session_factory() as session, session.begin():
        role, _ = await _authorize_and_lock_organization(session, context, command.organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "accepted":
                return _result_from_mutation(retained)
            raise _retained_error(retained)
        if command.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            raise ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        event = await session.scalar(
            select(Event)
            .where(Event.id == command.event_id, Event.organization_id == command.organization_id)
            .with_for_update(of=Event)
        )
        if event is None:
            raise ApplicationServiceError(
                "validation_failed",
                field_violations=(FieldViolation("event_id", "not_found"),),
                retry_same_identity=False,
            )
        expected = "active" if command.operation == "archive" else "archived"
        if event.lifecycle != expected:
            raise ApplicationServiceError(
                "validation_failed",
                field_violations=(FieldViolation("operation", "invalid_for_lifecycle"),),
                retry_same_identity=False,
            )
        snapshot_id: UUID | None = None
        if command.operation == "archive":
            payload, manifest = await _archive_payload(session, event, context.actor_user_id)
            encoded = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            snapshot_id = uuid4()
            previous_snapshot_id = await session.scalar(
                select(EventArchiveSnapshot.id)
                .where(EventArchiveSnapshot.event_id == event.id)
                .order_by(EventArchiveSnapshot.created_at.desc(), EventArchiveSnapshot.id.desc())
                .limit(1)
            )
            session.add(
                EventArchiveSnapshot(
                    id=snapshot_id,
                    event_id=event.id,
                    previous_snapshot_id=previous_snapshot_id,
                    archive_schema_version=ARCHIVE_SCHEMA_VERSION,
                    payload=payload,
                    content_hash=hashlib.sha256(encoded).digest(),
                    attachment_manifest=manifest,
                    created_by_user_id=context.actor_user_id,
                )
            )
            (
                event.lifecycle,
                event.current_archive_snapshot_id,
                event.archived_at,
                event.archived_by_user_id,
            ) = "archived", snapshot_id, datetime.now(UTC), context.actor_user_id
        else:
            (
                event.lifecycle,
                event.current_archive_snapshot_id,
                event.archived_at,
                event.archived_by_user_id,
            ) = "active", None, None, None
        clock = await session.scalar(
            select(FieldClock)
            .where(
                FieldClock.organization_id == event.organization_id,
                FieldClock.entity_kind == "event",
                FieldClock.entity_id == event.id,
                FieldClock.field_name == "lifecycle",
            )
            .with_for_update(of=FieldClock)
        )
        if clock is None:
            session.add(
                FieldClock(
                    organization_id=event.organization_id,
                    entity_kind="event",
                    entity_id=event.id,
                    field_name="lifecycle",
                    winning_client_wall_time=command.client_wall_time,
                    winning_mutation_id=command.mutation_id,
                )
            )
        else:
            clock.winning_client_wall_time = command.client_wall_time
            clock.winning_mutation_id = command.mutation_id
        attendance_clock = await session.scalar(
            select(FieldClock).where(
                FieldClock.organization_id == event.organization_id,
                FieldClock.entity_kind == "event",
                FieldClock.entity_id == event.id,
                FieldClock.field_name == "base_expected_attendance",
            )
        )
        record = _event_change_record(event, attendance_clock)[2]
        field_clocks = cast(dict[str, object], record["field_clocks"])
        field_clocks["lifecycle"] = {
            "winning_client_wall_time": command.client_wall_time.isoformat(),
            "winning_mutation_id": str(command.mutation_id),
        }
        first, last = await _reserve_change_range(
            session, event.organization_id, command.mutation_id, 1
        )
        result = EventLifecycleResult(
            command.mutation_id,
            event.id,
            event.organization_id,
            cast(Literal["active", "archived"], event.lifecycle),
            snapshot_id if snapshot_id else event.current_archive_snapshot_id,
            first,
            last,
            False,
        )
        outcome = {
            "event_id": str(event.id),
            "organization_id": str(event.organization_id),
            "lifecycle": event.lifecycle,
            "archive_snapshot_id": str(result.archive_snapshot_id)
            if result.archive_snapshot_id
            else None,
        }
        session.add_all(
            (
                OrganizationChange(
                    organization_id=event.organization_id,
                    sequence=first,
                    mutation_id=command.mutation_id,
                    entity_id=event.id,
                    entity_kind="event",
                    operation="upsert",
                    payload={"record_schema_version": 1, "record": record},
                ),
                Mutation(
                    id=command.mutation_id,
                    logical_operation_id=command.logical_operation_id,
                    organization_id=event.organization_id,
                    is_system_administration_scope=False,
                    actor_user_id=context.actor_user_id,
                    actor_role=role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=context.oauth_client_id,
                    oauth_grant_id=context.oauth_grant_id,
                    client_wall_time=command.client_wall_time,
                    command_schema_version=COMMAND_SCHEMA_VERSION,
                    command_kind=COMMAND_KIND,
                    target_identities=[{"entity_kind": "event", "entity_id": str(event.id)}],
                    request_hash=request_hash,
                    outcome="accepted",
                    outcome_payload=outcome,
                    first_change_sequence=first,
                    last_change_sequence=last,
                ),
            )
        )
        return result
