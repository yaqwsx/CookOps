"""Duplicate an archived event's immutable, locked planning projection."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.event_lifecycle import _row
from cookops.application.event_prices import _snapshot_record
from cookops.application.events import (
    _authorize_member_and_lock_organization,
    _event_change_record,
    _reserve_change_range,
    _scheduled_recipe_change_record,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.application.scheduled_recipe_overrides import _record as _override_record
from cookops.persistence.models import (
    Event,
    EventArchiveSnapshot,
    EventDay,
    EventIngredientPrice,
    EventIngredientPriceSnapshot,
    EventMealRole,
    FieldClock,
    Mutation,
    OrganizationChange,
    ScheduledIngredientOverride,
    ScheduledRecipe,
)

COMMAND_KIND = "event.duplicate"
COMMAND_SCHEMA_VERSION = 1
MAX_DUPLICATED_RECORDS = 2_000


class _DeterministicRejection(ApplicationServiceError):
    pass


@dataclass(frozen=True, slots=True)
class DuplicateEventCommand:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    source_event_id: UUID
    source_archive_snapshot_id: UUID
    name: str
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class DuplicateEventResult:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    source_event_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


def _hash(command: DuplicateEventCommand) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {
                "event_id": str(command.event_id),
                "organization_id": str(command.organization_id),
                "source_event_id": str(command.source_event_id),
                "source_archive_snapshot_id": str(command.source_archive_snapshot_id),
                "name": command.name,
                "client_wall_time": command.client_wall_time.isoformat(),
                "logical_operation_id": str(command.logical_operation_id)
                if command.logical_operation_id
                else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _error(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": value.path, "code": value.code} for value in error.field_violations
            ],
        }
    }


def _replayed(mutation: Mutation) -> DuplicateEventResult:
    value = mutation.outcome_payload or {}
    try:
        return DuplicateEventResult(
            mutation.id,
            UUID(str(value["event_id"])),
            UUID(str(value["organization_id"])),
            UUID(str(value["source_event_id"])),
            cast(int, mutation.first_change_sequence),
            cast(int, mutation.last_change_sequence),
            True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Event duplication retained an invalid outcome") from error


def _price_pointer(price: EventIngredientPrice) -> dict[str, object]:
    return {
        "id": str(price.id),
        "organization_id": str(price.organization_id),
        "event_id": str(price.event_id),
        "ingredient_id": str(price.ingredient_id),
        "current_snapshot_id": str(price.current_snapshot_id)
        if price.current_snapshot_id is not None
        else None,
        "created_at": price.created_at.isoformat(),
        "created_by_user_id": str(price.created_by_user_id),
    }


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    value = (mutation.outcome_payload or {}).get("error")
    if not isinstance(value, dict) or value.get("code") not in (
        "validation_failed",
        "client_time_too_far_ahead",
    ):
        raise RuntimeError("Event duplication retained an invalid rejection")
    violations = value.get("field_violations", [])
    if not isinstance(violations, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("path"), str)
        or not isinstance(item.get("code"), str)
        for item in violations
    ):
        raise RuntimeError("Event duplication retained invalid field violations")
    return ApplicationServiceError(
        cast(Literal["validation_failed", "client_time_too_far_ahead"], value["code"]),
        field_violations=tuple(
            FieldViolation(cast(str, item["path"]), cast(str, item["code"])) for item in violations
        ),
        retry_same_identity=False,
    )


def _archive_matches_plan(
    payload: object,
    source: Event,
    days: list[EventDay],
    roles: list[EventMealRole],
    schedules: list[ScheduledRecipe],
    overrides: list[ScheduledIngredientOverride],
    prices: list[EventIngredientPrice],
    price_snapshots: list[EventIngredientPriceSnapshot],
) -> bool:
    """Refuse a copy unless its live planning rows equal the signed archive."""
    source_record = _row(source)
    # The archive payload is captured immediately before archive metadata is set.
    source_record.update(
        lifecycle="active",
        current_archive_snapshot_id=None,
        archived_at=None,
        archived_by_user_id=None,
    )
    if not isinstance(payload, dict) or payload.get("event") != source_record:
        return False

    def active(records: object) -> set[str] | None:
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            return None
        return {
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in records
            if item.get("retired_at") is None
        }

    for key, values in (
        ("event_days", days),
        ("event_meal_roles", roles),
        ("scheduled_recipes", schedules),
        ("scheduled_ingredient_overrides", overrides),
        ("event_ingredient_prices", prices),
        ("event_ingredient_price_snapshots", price_snapshots),
    ):
        archived = active(payload.get(key))
        if archived is None or archived != {
            json.dumps(_row(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for value in values
        }:
            return False
    return True


def _validate(command: DuplicateEventCommand) -> tuple[str, tuple[FieldViolation, ...]]:
    name = (
        unicodedata.normalize("NFC", command.name).strip() if isinstance(command.name, str) else ""
    )
    errors: list[FieldViolation] = []
    if not name or len(name) > 200:
        errors.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
    if not all(
        isinstance(value, UUID)
        for value in (
            command.mutation_id,
            command.event_id,
            command.organization_id,
            command.source_event_id,
            command.source_archive_snapshot_id,
        )
    ):
        errors.append(FieldViolation("command", "invalid"))
    if command.event_id == command.source_event_id:
        errors.append(FieldViolation("event_id", "must_differ_from_source_event_id"))
    if command.client_wall_time.tzinfo is None or command.client_wall_time.utcoffset() is None:
        errors.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        errors.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    return name, tuple(errors)


def _mutation(
    command: DuplicateEventCommand,
    context: ExecutionContext,
    role: str,
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
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
        target_identities=[
            {"entity_kind": "event", "entity_id": str(command.event_id)},
            {"entity_kind": "event", "entity_id": str(command.source_event_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


def _retention_is_safe(command: DuplicateEventCommand) -> bool:
    return (
        all(
            isinstance(value, UUID)
            for value in (
                command.mutation_id,
                command.event_id,
                command.organization_id,
                command.source_event_id,
                command.source_archive_snapshot_id,
            )
        )
        and command.client_wall_time.tzinfo is not None
    )


async def _retain_rejection(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: DuplicateEventCommand,
    request_hash: bytes,
    error: ApplicationServiceError,
) -> None:
    """Commit replayable deterministic rejections in their own transaction."""
    async with session_factory() as session, session.begin():
        role = await _authorize_member_and_lock_organization(
            session, context, command.organization_id
        )
        if role not in ("member", "organization_admin", "system_admin"):
            raise ApplicationServiceError("forbidden", retry_same_identity=False)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", command.mutation_id)},
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
        session.add(_mutation(command, context, role, request_hash, "rejected", _error(error)))


async def duplicate_event(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: DuplicateEventCommand,
) -> DuplicateEventResult:
    """Copy only planning data; shopping, receipts, and media remain historical."""
    request_hash = _hash(command)
    name, violations = _validate(command)
    if violations:
        error = ApplicationServiceError(
            "validation_failed", field_violations=violations, retry_same_identity=False
        )
        if _retention_is_safe(command):
            await _retain_rejection(session_factory, context, command, request_hash, error)
        raise error
    if command.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
        error = ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        await _retain_rejection(session_factory, context, command, request_hash, error)
        raise error
    session = session_factory()
    await session.begin()
    try:
        try:
            role = await _authorize_member_and_lock_organization(
                session, context, command.organization_id
            )
            if role not in ("member", "organization_admin", "system_admin"):
                raise ApplicationServiceError("forbidden", retry_same_identity=False)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("mutation", command.mutation_id)},
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
                    return _replayed(retained)
                raise _retained_error(retained)
            source = await session.scalar(
                select(Event)
                .where(
                    Event.id == command.source_event_id,
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "archived",
                    Event.current_archive_snapshot_id == command.source_archive_snapshot_id,
                )
                .with_for_update(of=Event)
            )
            snapshot = await session.get(EventArchiveSnapshot, command.source_archive_snapshot_id)
            if source is None or snapshot is None or snapshot.event_id != command.source_event_id:
                raise _DeterministicRejection(
                    "validation_failed",
                    field_violations=(FieldViolation("source_archive_snapshot_id", "not_found"),),
                    retry_same_identity=False,
                )
            encoded = json.dumps(
                snapshot.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            if hashlib.sha256(encoded).digest() != snapshot.content_hash:
                raise _DeterministicRejection(
                    "validation_failed",
                    field_violations=(
                        FieldViolation("source_archive_snapshot_id", "integrity_failed"),
                    ),
                    retry_same_identity=False,
                )
            exists = await session.get(Event, command.event_id)
            if exists is not None:
                raise _DeterministicRejection(
                    "validation_failed",
                    field_violations=(FieldViolation("event_id", "already_exists"),),
                    retry_same_identity=False,
                )
            days = list(
                (
                    await session.scalars(
                        select(EventDay)
                        .where(EventDay.event_id == source.id, EventDay.retired_at.is_(None))
                        .order_by(EventDay.id)
                    )
                ).all()
            )
            roles = list(
                (
                    await session.scalars(
                        select(EventMealRole)
                        .where(
                            EventMealRole.event_id == source.id, EventMealRole.retired_at.is_(None)
                        )
                        .order_by(EventMealRole.id)
                    )
                ).all()
            )
            schedules = list(
                (
                    await session.scalars(
                        select(ScheduledRecipe)
                        .where(
                            ScheduledRecipe.event_id == source.id,
                            ScheduledRecipe.retired_at.is_(None),
                        )
                        .order_by(ScheduledRecipe.id)
                    )
                ).all()
            )
            overrides = list(
                (
                    await session.scalars(
                        select(ScheduledIngredientOverride)
                        .where(
                            ScheduledIngredientOverride.event_id == source.id,
                            ScheduledIngredientOverride.retired_at.is_(None),
                        )
                        .order_by(ScheduledIngredientOverride.id)
                    )
                ).all()
            )
            prices = list(
                (
                    await session.scalars(
                        select(EventIngredientPrice)
                        .where(EventIngredientPrice.event_id == source.id)
                        .order_by(EventIngredientPrice.id)
                    )
                ).all()
            )
            price_ids = tuple(value.id for value in prices)
            price_snapshots = (
                list(
                    (
                        await session.scalars(
                            select(EventIngredientPriceSnapshot)
                            .where(
                                EventIngredientPriceSnapshot.event_ingredient_price_id.in_(
                                    price_ids
                                )
                            )
                            .order_by(EventIngredientPriceSnapshot.id)
                        )
                    ).all()
                )
                if price_ids
                else []
            )
            if (
                1
                + len(days)
                + len(roles)
                + len(schedules)
                + len(overrides)
                + len(prices)
                + len(price_snapshots)
                > MAX_DUPLICATED_RECORDS
            ):
                raise _DeterministicRejection(
                    "validation_failed",
                    field_violations=(FieldViolation("source_event_id", "duplicate_too_large"),),
                    retry_same_identity=False,
                )
            if not _archive_matches_plan(
                snapshot.payload, source, days, roles, schedules, overrides, prices, price_snapshots
            ):
                raise _DeterministicRejection(
                    "validation_failed",
                    field_violations=(
                        FieldViolation("source_archive_snapshot_id", "does_not_match_live_plan"),
                    ),
                    retry_same_identity=False,
                )
            copied = Event(
                id=command.event_id,
                organization_id=source.organization_id,
                name=name,
                start_date=source.start_date,
                end_date=source.end_date,
                location=source.location,
                general_note=source.general_note,
                base_expected_attendance=source.base_expected_attendance,
                budget_amount=source.budget_amount,
                currency=source.currency,
                created_by_user_id=context.actor_user_id,
                lifecycle="active",
            )
        except _DeterministicRejection as error:
            await session.rollback()
            await _retain_rejection(session_factory, context, command, request_hash, error)
            raise
        day_ids, role_ids, schedule_ids, price_ids_new, snapshot_ids = (
            {value.id: uuid4() for value in values}
            for values in (days, roles, schedules, prices, price_snapshots)
        )
        copied_days = [
            EventDay(
                id=day_ids[value.id],
                event_id=copied.id,
                calendar_date=value.calendar_date,
                note=value.note,
                is_visible=value.is_visible,
                provenance=value.provenance,
                created_by_user_id=context.actor_user_id,
            )
            for value in days
        ]
        copied_roles = [
            EventMealRole(
                id=role_ids[value.id],
                event_id=copied.id,
                source_preset_id=value.source_preset_id,
                built_in_translation_key=value.built_in_translation_key,
                custom_name=value.custom_name,
                normalized_custom_name=value.normalized_custom_name,
                position_key=value.position_key,
                created_by_user_id=context.actor_user_id,
            )
            for value in roles
        ]
        copied_schedules = [
            ScheduledRecipe(
                id=schedule_ids[value.id],
                organization_id=copied.organization_id,
                event_id=copied.id,
                event_day_id=day_ids[value.event_day_id],
                event_meal_role_id=role_ids[value.event_meal_role_id],
                recipe_id=value.recipe_id,
                recipe_version_id=value.recipe_version_id,
                diner_count=value.diner_count,
                attendance_mode=value.attendance_mode,
                consumption_percentage=value.consumption_percentage,
                selected_scale_amount=value.selected_scale_amount,
                scale_mode=value.scale_mode,
                note=value.note,
                position_key=value.position_key,
                created_by_user_id=context.actor_user_id,
            )
            for value in schedules
        ]
        copied_overrides = [
            ScheduledIngredientOverride(
                id=uuid4(),
                organization_id=copied.organization_id,
                event_id=copied.id,
                scheduled_recipe_id=schedule_ids[value.scheduled_recipe_id],
                override_kind=value.override_kind,
                target_line_key=value.target_line_key,
                ingredient_id=value.ingredient_id,
                ingredient_version_id=value.ingredient_version_id,
                quantity=value.quantity,
                include_in_portion_weight=value.include_in_portion_weight,
                note=value.note,
                position_key=value.position_key,
                created_by_user_id=context.actor_user_id,
                last_modified_by_user_id=context.actor_user_id,
            )
            for value in overrides
        ]
        copied_prices = [
            EventIngredientPrice(
                id=price_ids_new[value.id],
                organization_id=copied.organization_id,
                event_id=copied.id,
                ingredient_id=value.ingredient_id,
                current_snapshot_id=(
                    snapshot_ids.get(value.current_snapshot_id)
                    if value.current_snapshot_id is not None
                    else None
                ),
                created_by_user_id=context.actor_user_id,
            )
            for value in prices
        ]
        copied_snapshots = [
            EventIngredientPriceSnapshot(
                id=snapshot_ids[value.id],
                organization_id=copied.organization_id,
                event_id=copied.id,
                ingredient_id=value.ingredient_id,
                event_ingredient_price_id=price_ids_new[value.event_ingredient_price_id],
                previous_snapshot_id=(
                    snapshot_ids.get(value.previous_snapshot_id)
                    if value.previous_snapshot_id is not None
                    else None
                ),
                source_ingredient_price_estimate_id=value.source_ingredient_price_estimate_id,
                state=value.state,
                price_amount=value.price_amount,
                priced_quantity=value.priced_quantity,
                priced_unit_id=value.priced_unit_id,
                currency=value.currency,
                captured_by_user_id=context.actor_user_id,
                effective_client_action_time=value.effective_client_action_time,
                server_received_at=value.server_received_at,
                originating_mutation_id=command.mutation_id,
            )
            for value in price_snapshots
        ]
        session.add_all(
            (
                copied,
                *copied_days,
                *copied_roles,
                *copied_schedules,
                *copied_overrides,
                *copied_prices,
                *copied_snapshots,
            )
        )
        await session.flush()
        clocks = [
            FieldClock(
                organization_id=copied.organization_id,
                entity_kind="event",
                entity_id=copied.id,
                field_name="base_expected_attendance",
                winning_client_wall_time=command.client_wall_time,
                winning_mutation_id=command.mutation_id,
            )
        ]
        clocks.extend(
            FieldClock(
                organization_id=copied.organization_id,
                entity_kind="scheduled_recipe",
                entity_id=value.id,
                field_name="placement",
                winning_client_wall_time=command.client_wall_time,
                winning_mutation_id=command.mutation_id,
            )
            for value in copied_schedules
        )
        clocks.extend(
            FieldClock(
                organization_id=copied.organization_id,
                entity_kind="scheduled_ingredient_override",
                entity_id=value.id,
                field_name=(
                    f"{value.override_kind}."
                    f"{value.target_line_key if value.override_kind == 'replace' else value.id}"
                ),
                winning_client_wall_time=command.client_wall_time,
                winning_mutation_id=command.mutation_id,
            )
            for value in copied_overrides
        )
        records: list[tuple[str, UUID, dict[str, object]]] = [
            _event_change_record(copied, clocks[0])
        ]
        records.extend(
            (
                "event_day",
                value.id,
                {
                    "id": str(value.id),
                    "event_id": str(copied.id),
                    "calendar_date": value.calendar_date.isoformat(),
                    "note": value.note,
                    "is_visible": value.is_visible,
                    "provenance": value.provenance,
                    "created_at": value.created_at.isoformat(),
                    "created_by_user_id": str(value.created_by_user_id),
                    "retired_at": None,
                    "retired_by_user_id": None,
                    "field_clocks": {"note": None, "is_visible": None},
                },
            )
            for value in copied_days
        )
        records.extend(
            (
                "event_meal_role",
                value.id,
                {
                    "id": str(value.id),
                    "event_id": str(copied.id),
                    "source_preset_id": str(value.source_preset_id)
                    if value.source_preset_id
                    else None,
                    "built_in_translation_key": value.built_in_translation_key,
                    "custom_name": value.custom_name,
                    "normalized_custom_name": value.normalized_custom_name,
                    "position_key": value.position_key,
                    "created_at": value.created_at.isoformat(),
                    "created_by_user_id": str(value.created_by_user_id),
                    "retired_at": None,
                    "retired_by_user_id": None,
                    "field_clocks": {"position_key": None},
                },
            )
            for value in copied_roles
        )
        records.extend(
            _scheduled_recipe_change_record(value, clocks[index + 1])
            for index, value in enumerate(copied_schedules)
        )
        offset = 1 + len(copied_schedules)
        for index, value in enumerate(copied_overrides):
            record = _override_record(value)
            record["field_clocks"] = {
                clocks[offset + index].field_name: {
                    "winning_client_wall_time": command.client_wall_time.isoformat(),
                    "winning_mutation_id": str(command.mutation_id),
                }
            }
            records.append(("scheduled_ingredient_override", value.id, record))
        records.extend(
            ("event_ingredient_price", value.id, _price_pointer(value)) for value in copied_prices
        )
        records.extend(
            ("event_ingredient_price_snapshot", value.id, _snapshot_record(value))
            for value in copied_snapshots
        )
        first, last = await _reserve_change_range(
            session, copied.organization_id, command.mutation_id, len(records)
        )
        session.add_all(
            (
                *clocks,
                _mutation(
                    command,
                    context,
                    role,
                    request_hash,
                    "accepted",
                    {
                        "event_id": str(copied.id),
                        "organization_id": str(copied.organization_id),
                        "source_event_id": str(source.id),
                    },
                    first,
                    last,
                ),
                *(
                    OrganizationChange(
                        organization_id=copied.organization_id,
                        sequence=first + index,
                        mutation_id=command.mutation_id,
                        entity_id=entity_id,
                        entity_kind=kind,
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": record},
                    )
                    for index, (kind, entity_id, record) in enumerate(records)
                ),
            )
        )
        await session.commit()
        return DuplicateEventResult(
            command.mutation_id, copied.id, copied.organization_id, source.id, first, last, False
        )
    finally:
        await session.close()
