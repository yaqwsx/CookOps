"""Move and order scheduled recipes through one LWW placement field."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import _reserve_change_range, _scheduled_recipe_change_record
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.application.recipes import _authorize_and_lock_organization
from cookops.persistence.models import (
    Event,
    EventDay,
    EventMealRole,
    FieldClock,
    Mutation,
    OrganizationChange,
    ScheduledRecipe,
)

COMMAND_KIND = "scheduled_recipe.move"
COMMAND_SCHEMA_VERSION = 1
_ORDER_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _between(left: str, right: str) -> str | None:
    """Return a C-collation alphanumeric key strictly between neighbours."""
    prefix = ""
    for index in range(255):
        low = _ORDER_ALPHABET.index(left[index]) if index < len(left) else -1
        high = (
            _ORDER_ALPHABET.index(right[index])
            if index < len(right)
            else len(_ORDER_ALPHABET)
        )
        if high - low > 1:
            candidate = prefix + _ORDER_ALPHABET[low + 1]
            return candidate if left < candidate and (not right or candidate < right) else None
        prefix += _ORDER_ALPHABET[low] if low >= 0 else _ORDER_ALPHABET[0]
    return None


@dataclass(frozen=True, slots=True)
class MoveScheduledRecipeCommand:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    event_day_id: UUID
    event_meal_role_id: UUID
    position_key: str | None
    client_wall_time: datetime
    placement: Literal["before", "after", "start", "end"] | None = None
    target_scheduled_recipe_id: UUID | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class MoveScheduledRecipeResult:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    event_day_id: UUID
    event_meal_role_id: UUID
    position_key: str
    first_change_sequence: int | None
    last_change_sequence: int | None
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


@dataclass(frozen=True, slots=True)
class _PreparedCommand:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    event_day_id: UUID
    event_meal_role_id: UUID
    position_key: str | None
    placement: Literal["before", "after", "start", "end"] | None
    target_scheduled_recipe_id: UUID | None
    client_wall_time: datetime
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


def _invalid(value: object) -> dict[str, str]:
    return {"invalid_type": type(value).__qualname__, "repr": repr(value)}


def _raw_uuid(value: object) -> str | dict[str, str]:
    return str(value) if isinstance(value, UUID) else _invalid(value)


def _raw_time(value: object) -> str | dict[str, str]:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _invalid(value)


def _raw_position(value: object) -> str | dict[str, str]:
    return (
        unicodedata.normalize("NFC", value).strip() if isinstance(value, str) else _invalid(value)
    )


def _request_hash(command: MoveScheduledRecipeCommand) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {
                "client_wall_time": _raw_time(command.client_wall_time),
                "command_kind": COMMAND_KIND,
                "command_schema_version": COMMAND_SCHEMA_VERSION,
                "event_day_id": _raw_uuid(command.event_day_id),
                "event_id": _raw_uuid(command.event_id),
                "event_meal_role_id": _raw_uuid(command.event_meal_role_id),
                "logical_operation_id": _raw_uuid(command.logical_operation_id)
                if command.logical_operation_id is not None
                else None,
                "organization_id": _raw_uuid(command.organization_id),
                "position_key": _raw_position(command.position_key),
                "placement": command.placement,
                "target_scheduled_recipe_id": _raw_uuid(command.target_scheduled_recipe_id)
                if command.target_scheduled_recipe_id is not None
                else None,
                "scheduled_recipe_id": _raw_uuid(command.scheduled_recipe_id),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).digest()


def _prepare(command: MoveScheduledRecipeCommand) -> _PreparedCommand:
    violations: list[FieldViolation] = []
    position_key = (
        unicodedata.normalize("NFC", command.position_key).strip()
        if isinstance(command.position_key, str)
        else ""
    )
    if command.placement is None and (
        not isinstance(command.position_key, str)
        or not position_key
        or len(position_key) > 255
        or not position_key.isascii()
        or not position_key.isalnum()
    ):
        violations.append(FieldViolation("position_key", "must_be_ascii_alphanumeric_at_most_255"))
    if command.placement is not None and command.placement not in (
        "before",
        "after",
        "start",
        "end",
    ):
        violations.append(FieldViolation("placement", "must_be_before_after_start_or_end"))
    if command.placement is not None and command.position_key is not None:
        violations.append(FieldViolation("position_key", "not_allowed_for_relative_placement"))
    if command.placement is None and command.target_scheduled_recipe_id is not None:
        violations.append(
            FieldViolation("target_scheduled_recipe_id", "not_valid_for_raw_placement")
        )
    if command.placement in ("before", "after") and not isinstance(
        command.target_scheduled_recipe_id, UUID
    ):
        violations.append(
            FieldViolation("target_scheduled_recipe_id", "required_for_relative_placement")
        )
    if command.placement in ("start", "end") and command.target_scheduled_recipe_id is not None:
        violations.append(
            FieldViolation("target_scheduled_recipe_id", "not_valid_for_boundary_placement")
        )
    if command.target_scheduled_recipe_id is not None and not isinstance(
        command.target_scheduled_recipe_id, UUID
    ):
        violations.append(FieldViolation("target_scheduled_recipe_id", "must_be_uuid"))
    if (
        command.placement in ("before", "after")
        and isinstance(command.target_scheduled_recipe_id, UUID)
        and command.target_scheduled_recipe_id == command.scheduled_recipe_id
    ):
        violations.append(
            FieldViolation("target_scheduled_recipe_id", "must_not_match_scheduled_recipe")
        )
    has_timezone = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not has_timezone:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    for name, value in (
        ("mutation_id", command.mutation_id),
        ("scheduled_recipe_id", command.scheduled_recipe_id),
        ("organization_id", command.organization_id),
        ("event_id", command.event_id),
        ("event_day_id", command.event_day_id),
        ("event_meal_role_id", command.event_meal_role_id),
    ):
        if not isinstance(value, UUID):
            violations.append(FieldViolation(name, "must_be_uuid"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    return _PreparedCommand(
        mutation_id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        scheduled_recipe_id=command.scheduled_recipe_id
        if isinstance(command.scheduled_recipe_id, UUID)
        else UUID(int=0),
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        event_id=command.event_id if isinstance(command.event_id, UUID) else UUID(int=0),
        event_day_id=command.event_day_id
        if isinstance(command.event_day_id, UUID)
        else UUID(int=0),
        event_meal_role_id=command.event_meal_role_id
        if isinstance(command.event_meal_role_id, UUID)
        else UUID(int=0),
        position_key=position_key or None,
        placement=command.placement,
        target_scheduled_recipe_id=command.target_scheduled_recipe_id
        if isinstance(command.target_scheduled_recipe_id, UUID)
        else None,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if has_timezone
        else datetime(1970, 1, 1, tzinfo=UTC),
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        violations=tuple(violations),
    )


def _error(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": violation.path, "code": violation.code}
                for violation in error.field_violations
            ],
        }
    }


def _validation(*violations: FieldViolation) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
    )


def _record(scheduled: ScheduledRecipe, clock: FieldClock | None) -> dict[str, object]:
    _, _, record = _scheduled_recipe_change_record(scheduled, clock)
    return record


def _result(
    prepared: _PreparedCommand,
    scheduled: ScheduledRecipe,
    first: int | None,
    last: int | None,
    replayed: bool,
    outcome: Literal["accepted", "partially_superseded"],
) -> MoveScheduledRecipeResult:
    return MoveScheduledRecipeResult(
        prepared.mutation_id,
        scheduled.id,
        prepared.organization_id,
        scheduled.event_id,
        scheduled.event_day_id,
        scheduled.event_meal_role_id,
        scheduled.position_key,
        first,
        last,
        replayed,
        outcome,
    )


def _payload(result: MoveScheduledRecipeResult) -> dict[str, object]:
    return {
        "scheduled_recipe": {
            "id": str(result.scheduled_recipe_id),
            "organization_id": str(result.organization_id),
            "event_id": str(result.event_id),
            "event_day_id": str(result.event_day_id),
            "event_meal_role_id": str(result.event_meal_role_id),
            "position_key": result.position_key,
        },
        "outcome": result.outcome,
    }


def _retained_result(mutation: Mutation) -> MoveScheduledRecipeResult:
    payload = mutation.outcome_payload or {}
    record = payload.get("scheduled_recipe")
    outcome = payload.get("outcome")
    first, last = mutation.first_change_sequence, mutation.last_change_sequence
    if not isinstance(record, dict) or outcome not in ("accepted", "partially_superseded"):
        raise RuntimeError("Retained scheduled recipe move has an invalid outcome payload")
    if (first is None) != (last is None):
        raise RuntimeError("Retained scheduled recipe move has an invalid outcome payload")
    try:
        return MoveScheduledRecipeResult(
            mutation.id,
            UUID(str(record["id"])),
            UUID(str(record["organization_id"])),
            UUID(str(record["event_id"])),
            UUID(str(record["event_day_id"])),
            UUID(str(record["event_meal_role_id"])),
            str(record["position_key"]),
            first,
            last,
            True,
            outcome,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "Retained scheduled recipe move has an invalid outcome payload"
        ) from error


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload or {}
    error = payload.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise RuntimeError("Retained scheduled recipe move has an invalid error payload")
    violations = error.get("field_violations")
    if not isinstance(violations, list):
        raise RuntimeError("Retained scheduled recipe move has an invalid error payload")
    try:
        return ApplicationServiceError(
            error["code"],
            field_violations=tuple(
                FieldViolation(item["path"], item["code"])
                for item in violations
                if isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("code"), str)
            ),
            retry_same_identity=False,
        )
    except (KeyError, TypeError) as value_error:
        raise RuntimeError(
            "Retained scheduled recipe move has an invalid error payload"
        ) from value_error


def _mutation(
    prepared: _PreparedCommand,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "partially_superseded", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=prepared.mutation_id,
        logical_operation_id=prepared.logical_operation_id,
        organization_id=prepared.organization_id,
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=prepared.client_wall_time,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=COMMAND_KIND,
        target_identities=[
            {"entity_kind": "event", "entity_id": str(prepared.event_id)},
            {"entity_kind": "scheduled_recipe", "entity_id": str(prepared.scheduled_recipe_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def move_scheduled_recipe(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: MoveScheduledRecipeCommand,
) -> MoveScheduledRecipeResult:
    """Atomically move or reorder one active scheduled recipe by its LWW placement."""

    prepared, request_hash = _prepare(command), _request_hash(command)
    deferred: ApplicationServiceError | None = None
    result: MoveScheduledRecipeResult | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_and_lock_organization(session, context, prepared.organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", prepared.mutation_id)},
        )
        retained = await session.get(Mutation, prepared.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome in ("accepted", "partially_superseded"):
                return _retained_result(retained)
            if retained.outcome == "rejected":
                deferred = _retained_error(retained)
            else:
                raise RuntimeError("Retained scheduled recipe move has an unsupported outcome")
        elif prepared.violations:
            deferred = _validation(*prepared.violations)
        elif prepared.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            deferred = ApplicationServiceError(
                "client_time_too_far_ahead", retry_same_identity=False
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("scheduled_recipe", prepared.scheduled_recipe_id)},
            )
            scheduled = await session.scalar(
                select(ScheduledRecipe)
                .join(Event, Event.id == ScheduledRecipe.event_id)
                .where(
                    ScheduledRecipe.id == prepared.scheduled_recipe_id,
                    ScheduledRecipe.organization_id == prepared.organization_id,
                    ScheduledRecipe.event_id == prepared.event_id,
                    ScheduledRecipe.retired_at.is_(None),
                )
                .with_for_update(of=(ScheduledRecipe, Event))
            )
            if scheduled is None:
                deferred = _validation(
                    FieldViolation("scheduled_recipe_id", "must_be_active_and_belong_to_event")
                )
            else:
                event = await session.scalar(
                    select(Event.lifecycle)
                    .where(
                        Event.id == prepared.event_id,
                        Event.organization_id == prepared.organization_id,
                    )
                    .with_for_update(of=Event)
                )
                if event != "active":
                    deferred = ApplicationServiceError("archived_event", retry_same_identity=False)
                else:
                    day = await session.scalar(
                        select(EventDay.id)
                        .where(
                            EventDay.id == prepared.event_day_id,
                            EventDay.event_id == prepared.event_id,
                            EventDay.retired_at.is_(None),
                        )
                        .with_for_update(of=EventDay)
                    )
                    meal_role = await session.scalar(
                        select(EventMealRole.id)
                        .where(
                            EventMealRole.id == prepared.event_meal_role_id,
                            EventMealRole.event_id == prepared.event_id,
                            EventMealRole.retired_at.is_(None),
                        )
                        .with_for_update(of=EventMealRole)
                    )
                    if day is None or meal_role is None:
                        deferred = _validation(
                            FieldViolation(
                                "placement", "must_reference_active_day_and_meal_role_in_event"
                            )
                        )
                    else:
                        rekeyed: list[ScheduledRecipe] = []
                        clock = await session.scalar(
                            select(FieldClock)
                            .where(
                                FieldClock.organization_id == prepared.organization_id,
                                FieldClock.entity_kind == "scheduled_recipe",
                                FieldClock.entity_id == scheduled.id,
                                FieldClock.field_name == "placement",
                            )
                            .with_for_update(of=FieldClock)
                        )
                        wins = clock is None or (
                            prepared.client_wall_time,
                            prepared.mutation_id,
                        ) > (clock.winning_client_wall_time, clock.winning_mutation_id)
                        stale_relative = prepared.placement is not None and not wins
                        if stale_relative and prepared.placement in ("before", "after"):
                            target = await session.scalar(
                                select(ScheduledRecipe).where(
                                    ScheduledRecipe.id == prepared.target_scheduled_recipe_id,
                                    ScheduledRecipe.organization_id == prepared.organization_id,
                                    ScheduledRecipe.event_id == prepared.event_id,
                                    ScheduledRecipe.event_day_id == prepared.event_day_id,
                                    ScheduledRecipe.event_meal_role_id
                                    == prepared.event_meal_role_id,
                                    ScheduledRecipe.retired_at.is_(None),
                                )
                            )
                            if target is None:
                                deferred = _validation(
                                    FieldViolation(
                                        "target_scheduled_recipe_id",
                                        "must_be_active_in_target_scope",
                                    )
                                )
                        if prepared.placement is not None and not stale_relative:
                            await session.execute(
                                text("SELECT pg_advisory_xact_lock(:key)"),
                                {
                                    "key": _advisory_lock_key(
                                        "scheduled_recipe_order", scheduled.event_id
                                    )
                                },
                            )
                            rows = (
                                await session.scalars(
                                    select(ScheduledRecipe)
                                    .where(
                                        ScheduledRecipe.organization_id == prepared.organization_id,
                                        ScheduledRecipe.event_id == prepared.event_id,
                                        ScheduledRecipe.event_day_id == prepared.event_day_id,
                                        ScheduledRecipe.event_meal_role_id
                                        == prepared.event_meal_role_id,
                                        ScheduledRecipe.retired_at.is_(None),
                                    )
                                    .order_by(ScheduledRecipe.position_key, ScheduledRecipe.id)
                                    .with_for_update()
                                )
                            ).all()
                            target = next(
                                (
                                    row
                                    for row in rows
                                    if row.id == prepared.target_scheduled_recipe_id
                                ),
                                None,
                            )
                            if prepared.placement in ("before", "after") and target is None:
                                deferred = _validation(
                                    FieldViolation(
                                        "target_scheduled_recipe_id",
                                        "must_be_active_in_target_scope",
                                    )
                                )
                            else:
                                if not wins:
                                    prepared = replace(
                                        prepared,
                                        placement=None,
                                        position_key=scheduled.position_key,
                                    )
                                ordered = [row for row in rows if row.id != scheduled.id]
                                if prepared.placement == "start":
                                    index = 0
                                elif prepared.placement == "end":
                                    index = len(ordered)
                                else:
                                    assert target is not None
                                    index = ordered.index(target) + (prepared.placement == "after")
                                left = ordered[index - 1].position_key if index else ""
                                right = ordered[index].position_key if index < len(ordered) else ""
                                candidate = _between(left, right)
                                if candidate is None:
                                    width = max(1, len(str(len(ordered))))
                                    for row_index, row in enumerate(
                                        ordered[:index] + [scheduled] + ordered[index:]
                                    ):
                                        new_key = str(row_index).zfill(width)
                                        if row.position_key != new_key:
                                            row.position_key = new_key
                                            rekeyed.append(row)
                                    candidate = scheduled.position_key
                                else:
                                    rekeyed = [scheduled]
                                prepared = replace(prepared, position_key=candidate)
                        if deferred is None and wins:
                            assert prepared.position_key is not None
                            scheduled.event_day_id = prepared.event_day_id
                            scheduled.event_meal_role_id = prepared.event_meal_role_id
                            scheduled.position_key = prepared.position_key
                            if clock is None:
                                clock = FieldClock(
                                    organization_id=prepared.organization_id,
                                    entity_kind="scheduled_recipe",
                                    entity_id=scheduled.id,
                                    field_name="placement",
                                    winning_client_wall_time=prepared.client_wall_time,
                                    winning_mutation_id=prepared.mutation_id,
                                )
                                session.add(clock)
                            else:
                                clock.winning_client_wall_time = prepared.client_wall_time
                                clock.winning_mutation_id = prepared.mutation_id
                            outcome: Literal["accepted", "partially_superseded"] = "accepted"
                        else:
                            outcome = "partially_superseded"
                        changed = (
                            []
                            if stale_relative
                            else
                            rekeyed
                            if prepared.placement is not None and deferred is None and rekeyed
                            else [scheduled]
                        )
                        first = last = None
                        if deferred is None and not stale_relative:
                            first, last = await _reserve_change_range(
                                session,
                                prepared.organization_id,
                                prepared.mutation_id,
                                len(changed),
                            )
                        if deferred is None:
                            result = _result(prepared, scheduled, first, last, False, outcome)
                        for sequence, changed_row in (
                            enumerate(changed, first or 0)
                            if deferred is None and not stale_relative
                            else ()
                        ):
                            row_clock = (
                                clock
                                if changed_row.id == scheduled.id
                                else await session.scalar(
                                    select(FieldClock).where(
                                        FieldClock.organization_id == prepared.organization_id,
                                        FieldClock.entity_kind == "scheduled_recipe",
                                        FieldClock.entity_id == changed_row.id,
                                        FieldClock.field_name == "placement",
                                    )
                                )
                            )
                            session.add(
                                OrganizationChange(
                                    organization_id=prepared.organization_id,
                                    sequence=sequence,
                                    mutation_id=prepared.mutation_id,
                                    entity_id=changed_row.id,
                                    entity_kind="scheduled_recipe",
                                    operation="upsert",
                                    payload={
                                        "record_schema_version": 1,
                                        "record": _record(changed_row, row_clock),
                                    },
                                )
                            )
                        if deferred is None:
                            assert result is not None
                            session.add(
                                _mutation(
                                prepared,
                                context,
                                role,
                                request_hash,
                                outcome,
                                _payload(result),
                                None if stale_relative else first,
                                None if stale_relative else last,
                                )
                            )
        if deferred is not None and retained is None:
            session.add(
                _mutation(prepared, context, role, request_hash, "rejected", _error(deferred))
            )
    if deferred is not None:
        raise deferred
    if result is None:
        raise RuntimeError("Scheduled recipe move produced no outcome")
    return result
