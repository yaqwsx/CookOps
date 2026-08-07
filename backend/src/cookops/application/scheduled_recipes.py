"""Scheduled-recipe application services."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.event_prices import (
    _emit_event_price_snapshots,
    _prepare_initial_event_price_captures,
)
from cookops.application.events import _reserve_change_range
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
    IngredientVersion,
    Mutation,
    OrganizationChange,
    Recipe,
    RecipeVersion,
    RecipeVersionIngredientLine,
    ScheduledRecipe,
    UnitDefinition,
)

COMMAND_KIND = "scheduled_recipe.schedule"
COMMAND_SCHEMA_VERSION = 1
DEFAULT_CONSUMPTION_PERCENTAGE = Decimal("100")
MAX_SERIALIZED_NOTE_BYTES = 131_072


@dataclass(frozen=True, slots=True)
class ScheduleRecipeCommand:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    event_day_id: UUID
    event_meal_role_id: UUID
    recipe_id: UUID
    recipe_version_id: UUID
    client_wall_time: datetime
    consumption_percentage: Decimal = DEFAULT_CONSUMPTION_PERCENTAGE
    position_key: str = "a"
    note: str | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ScheduleRecipeResult:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    event_day_id: UUID
    event_meal_role_id: UUID
    recipe_id: UUID
    recipe_version_id: UUID
    diner_count: int
    attendance_mode: Literal["follows_event"]
    consumption_percentage: Decimal
    selected_scale_amount: Decimal
    scale_mode: Literal["suggested"]
    position_key: str
    note: str | None
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


@dataclass(frozen=True, slots=True)
class _PreparedCommand:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    event_day_id: UUID
    event_meal_role_id: UUID
    recipe_id: UUID
    recipe_version_id: UUID
    client_wall_time: datetime
    consumption_percentage: Decimal
    position_key: str
    note: str | None
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


@dataclass(frozen=True, slots=True)
class _ScheduleReferences:
    diner_count: int
    base_scaling_amount: Decimal
    estimated_diners_per_scaling_unit: Decimal | None
    round_suggestions_up: bool
    scaling_unit_code: str


def _canonical_decimal_string(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _canonical_note(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _finite_decimal(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) and value.is_finite() else None


def _prepare_command(command: ScheduleRecipeCommand) -> _PreparedCommand:
    violations: list[FieldViolation] = []
    consumption_percentage = _finite_decimal(command.consumption_percentage)
    if consumption_percentage is None or consumption_percentage < 0:
        violations.append(
            FieldViolation("consumption_percentage", "must_be_nonnegative_finite_decimal")
        )
        consumption_percentage = Decimal(0)

    position_key = (
        unicodedata.normalize("NFC", command.position_key).strip()
        if isinstance(command.position_key, str)
        else ""
    )
    if (
        not isinstance(command.position_key, str)
        or not position_key
        or len(position_key) > 255
        or not position_key.isascii()
        or not position_key.isalnum()
    ):
        violations.append(FieldViolation("position_key", "must_be_ascii_alphanumeric_at_most_255"))

    note = _canonical_note(command.note) if isinstance(command.note, str) else None
    if command.note is not None and not isinstance(command.note, str):
        violations.append(FieldViolation("note", "must_be_string_or_null"))
    if note == "":
        note = None
    if note is not None and "\x00" in note:
        violations.append(FieldViolation("note", "must_not_contain_nul"))
    if (
        note is not None
        and len(json.dumps(note, ensure_ascii=False).encode()) > MAX_SERIALIZED_NOTE_BYTES
    ):
        violations.append(FieldViolation("note", "must_fit_change_record"))

    wall_time_has_timezone = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not wall_time_has_timezone:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    for path, value in (
        ("mutation_id", command.mutation_id),
        ("scheduled_recipe_id", command.scheduled_recipe_id),
        ("organization_id", command.organization_id),
        ("event_id", command.event_id),
        ("event_day_id", command.event_day_id),
        ("event_meal_role_id", command.event_meal_role_id),
        ("recipe_id", command.recipe_id),
        ("recipe_version_id", command.recipe_version_id),
    ):
        if not isinstance(value, UUID):
            violations.append(FieldViolation(path, "must_be_uuid"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))

    return _PreparedCommand(
        mutation_id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        scheduled_recipe_id=(
            command.scheduled_recipe_id
            if isinstance(command.scheduled_recipe_id, UUID)
            else UUID(int=0)
        ),
        organization_id=(
            command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
        ),
        event_id=command.event_id if isinstance(command.event_id, UUID) else UUID(int=0),
        event_day_id=command.event_day_id
        if isinstance(command.event_day_id, UUID)
        else UUID(int=0),
        event_meal_role_id=(
            command.event_meal_role_id
            if isinstance(command.event_meal_role_id, UUID)
            else UUID(int=0)
        ),
        recipe_id=command.recipe_id if isinstance(command.recipe_id, UUID) else UUID(int=0),
        recipe_version_id=(
            command.recipe_version_id
            if isinstance(command.recipe_version_id, UUID)
            else UUID(int=0)
        ),
        client_wall_time=(
            command.client_wall_time.astimezone(UTC)
            if wall_time_has_timezone
            else datetime(1970, 1, 1, tzinfo=UTC)
        ),
        consumption_percentage=consumption_percentage,
        position_key=position_key,
        note=note,
        logical_operation_id=(
            command.logical_operation_id if isinstance(command.logical_operation_id, UUID) else None
        ),
        violations=tuple(violations),
    )


def _invalid_hash_value(value: object) -> dict[str, str]:
    """Represent invalid data without colliding with a valid canonical value."""

    return {"invalid_type": type(value).__qualname__, "repr": repr(value)}


def _raw_uuid_hash_value(value: object) -> str | dict[str, str]:
    return str(value) if isinstance(value, UUID) else _invalid_hash_value(value)


def _raw_wall_time_hash_value(value: object) -> str | dict[str, str]:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _invalid_hash_value(value)


def _raw_decimal_hash_value(value: object) -> str | dict[str, str]:
    if isinstance(value, Decimal) and value.is_finite():
        return _canonical_decimal_string(value)
    return _invalid_hash_value(value)


def _raw_position_key_hash_value(value: object) -> str | dict[str, str]:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value).strip()
    return _invalid_hash_value(value)


def _raw_note_hash_value(value: object) -> str | None | dict[str, str]:
    if value is None:
        return None
    if isinstance(value, str):
        return _canonical_note(value) or None
    return _invalid_hash_value(value)


def _request_hash(command: ScheduleRecipeCommand) -> bytes:
    request = {
        "client_wall_time": _raw_wall_time_hash_value(command.client_wall_time),
        "command_kind": COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "consumption_percentage": _raw_decimal_hash_value(command.consumption_percentage),
        "event_day_id": _raw_uuid_hash_value(command.event_day_id),
        "event_id": _raw_uuid_hash_value(command.event_id),
        "event_meal_role_id": _raw_uuid_hash_value(command.event_meal_role_id),
        "logical_operation_id": (
            _raw_uuid_hash_value(command.logical_operation_id)
            if command.logical_operation_id is not None
            else None
        ),
        "note": _raw_note_hash_value(command.note),
        "organization_id": _raw_uuid_hash_value(command.organization_id),
        "position_key": _raw_position_key_hash_value(command.position_key),
        "recipe_id": _raw_uuid_hash_value(command.recipe_id),
        "recipe_version_id": _raw_uuid_hash_value(command.recipe_version_id),
        "scheduled_recipe_id": _raw_uuid_hash_value(command.scheduled_recipe_id),
    }
    return hashlib.sha256(
        json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()


def _suggested_scale(command: _PreparedCommand, references: _ScheduleReferences) -> Decimal:
    capacity = (
        Decimal(1)
        if references.scaling_unit_code == "person"
        else references.estimated_diners_per_scaling_unit
    )
    if capacity is None or capacity <= 0:
        return references.base_scaling_amount
    suggested = Decimal(references.diner_count) * command.consumption_percentage / Decimal(100)
    suggested /= capacity
    if references.round_suggestions_up:
        return suggested.to_integral_value(rounding=ROUND_CEILING)
    return suggested


async def _load_references(
    session: AsyncSession, command: _PreparedCommand
) -> _ScheduleReferences | None:
    event = await session.execute(
        select(Event.base_expected_attendance)
        .where(
            Event.id == command.event_id,
            Event.organization_id == command.organization_id,
            Event.lifecycle == "active",
        )
        .with_for_update(of=Event)
    )
    diner_count = event.scalar_one_or_none()
    if diner_count is None:
        return None
    day = await session.scalar(
        select(EventDay.id)
        .where(
            EventDay.id == command.event_day_id,
            EventDay.event_id == command.event_id,
            EventDay.retired_at.is_(None),
        )
        .with_for_update(of=EventDay)
    )
    if day is None:
        return None
    role = await session.scalar(
        select(EventMealRole.id)
        .where(
            EventMealRole.id == command.event_meal_role_id,
            EventMealRole.event_id == command.event_id,
            EventMealRole.retired_at.is_(None),
        )
        .with_for_update(of=EventMealRole)
    )
    if role is None:
        return None
    current_recipe_version_id = await session.scalar(
        select(Recipe.current_version_id)
        .where(
            Recipe.id == command.recipe_id,
            Recipe.organization_id == command.organization_id,
            Recipe.retired_at.is_(None),
        )
        .with_for_update(of=Recipe)
    )
    if current_recipe_version_id is None or current_recipe_version_id != command.recipe_version_id:
        return None
    version = (
        await session.execute(
            select(
                RecipeVersion.base_scaling_amount,
                RecipeVersion.estimated_diners_per_scaling_unit,
                RecipeVersion.round_suggestions_up,
                UnitDefinition.code,
            )
            .join(UnitDefinition, UnitDefinition.id == RecipeVersion.scaling_unit_id)
            .where(
                RecipeVersion.id == command.recipe_version_id,
                RecipeVersion.recipe_id == command.recipe_id,
                RecipeVersion.organization_id == command.organization_id,
            )
            .with_for_update(of=(RecipeVersion, UnitDefinition))
        )
    ).one_or_none()
    if version is None:
        return None
    return _ScheduleReferences(
        diner_count=diner_count,
        base_scaling_amount=version.base_scaling_amount,
        estimated_diners_per_scaling_unit=version.estimated_diners_per_scaling_unit,
        round_suggestions_up=version.round_suggestions_up,
        scaling_unit_code=version.code,
    )


async def _nonzero_recipe_ingredient_ids(
    session: AsyncSession,
    *,
    recipe_version_id: UUID,
    selected_scale_amount: Decimal,
    base_scaling_amount: Decimal,
) -> set[UUID]:
    """Return resolved ingredients whose scheduled quantity is nonzero."""

    rows = await session.execute(
        select(
            IngredientVersion.ingredient_id,
            RecipeVersionIngredientLine.base_quantity,
            RecipeVersionIngredientLine.scaling_behavior,
        )
        .join(
            IngredientVersion,
            IngredientVersion.id == RecipeVersionIngredientLine.ingredient_version_id,
        )
        .where(RecipeVersionIngredientLine.recipe_version_id == recipe_version_id)
    )
    return {
        ingredient_id
        for ingredient_id, base_quantity, scaling_behavior in rows
        if (
            base_quantity
            if scaling_behavior == "fixed"
            else base_quantity * selected_scale_amount / base_scaling_amount
        )
        > 0
    }


def _result_payload(result: ScheduleRecipeResult) -> dict[str, object]:
    return {
        "scheduled_recipe": {
            "id": str(result.scheduled_recipe_id),
            "organization_id": str(result.organization_id),
            "event_id": str(result.event_id),
            "event_day_id": str(result.event_day_id),
            "event_meal_role_id": str(result.event_meal_role_id),
            "recipe_id": str(result.recipe_id),
            "recipe_version_id": str(result.recipe_version_id),
            "diner_count": result.diner_count,
            "attendance_mode": result.attendance_mode,
            "consumption_percentage": _canonical_decimal_string(result.consumption_percentage),
            "selected_scale_amount": _canonical_decimal_string(result.selected_scale_amount),
            "scale_mode": result.scale_mode,
            "position_key": result.position_key,
            "note": result.note,
            "retired_at": None,
        }
    }


def _retained_result(mutation: Mutation) -> ScheduleRecipeResult:
    payload = mutation.outcome_payload
    record = payload.get("scheduled_recipe") if payload is not None else None
    if not isinstance(record, dict):
        raise RuntimeError("Accepted scheduled recipe mutation has an invalid outcome payload")
    try:
        diner_count = record["diner_count"]
        if not isinstance(diner_count, int) or isinstance(diner_count, bool):
            raise TypeError
        attendance_mode = record["attendance_mode"]
        scale_mode = record["scale_mode"]
        if attendance_mode != "follows_event" or scale_mode != "suggested":
            raise TypeError
        note = record["note"]
        if note is not None and not isinstance(note, str):
            raise TypeError
        first, last = mutation.first_change_sequence, mutation.last_change_sequence
        if first is None or last is None:
            raise TypeError
        return ScheduleRecipeResult(
            mutation_id=mutation.id,
            scheduled_recipe_id=UUID(str(record["id"])),
            organization_id=UUID(str(record["organization_id"])),
            event_id=UUID(str(record["event_id"])),
            event_day_id=UUID(str(record["event_day_id"])),
            event_meal_role_id=UUID(str(record["event_meal_role_id"])),
            recipe_id=UUID(str(record["recipe_id"])),
            recipe_version_id=UUID(str(record["recipe_version_id"])),
            diner_count=diner_count,
            attendance_mode="follows_event",
            consumption_percentage=Decimal(str(record["consumption_percentage"])),
            selected_scale_amount=Decimal(str(record["selected_scale_amount"])),
            scale_mode="suggested",
            position_key=str(record["position_key"]),
            note=note,
            first_change_sequence=first,
            last_change_sequence=last,
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "Accepted scheduled recipe mutation has an invalid outcome payload"
        ) from error


def _validation_error(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
    )


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
    payload = mutation.outcome_payload
    error = payload.get("error") if payload is not None else None
    if not isinstance(error, dict) or error.get("code") != "validation_failed":
        raise RuntimeError("Rejected scheduled recipe mutation has an invalid outcome payload")
    violations = error.get("field_violations")
    if not isinstance(violations, list):
        raise RuntimeError("Rejected scheduled recipe mutation has an invalid outcome payload")
    try:
        parsed = tuple(
            FieldViolation(item["path"], item["code"])
            for item in violations
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("code"), str)
        )
        if len(parsed) != len(violations):
            raise TypeError
        return _validation_error(parsed)
    except (KeyError, TypeError) as error_value:
        raise RuntimeError(
            "Rejected scheduled recipe mutation has an invalid outcome payload"
        ) from error_value


def _mutation(
    *,
    command: _PreparedCommand,
    context: ExecutionContext,
    actor_role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
    outcome_payload: dict[str, object],
    first_change_sequence: int | None = None,
    last_change_sequence: int | None = None,
) -> Mutation:
    return Mutation(
        id=command.mutation_id,
        logical_operation_id=command.logical_operation_id,
        organization_id=command.organization_id,
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=actor_role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=COMMAND_KIND,
        target_identities=[
            {"entity_kind": "event", "entity_id": str(command.event_id)},
            {"entity_kind": "scheduled_recipe", "entity_id": str(command.scheduled_recipe_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=outcome_payload,
        first_change_sequence=first_change_sequence,
        last_change_sequence=last_change_sequence,
    )


async def schedule_recipe(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: ScheduleRecipeCommand,
) -> ScheduleRecipeResult:
    """Pin a published catalog recipe version into one active event day and role."""

    prepared = _prepare_command(command)
    request_hash = _request_hash(command)
    deferred_error: ApplicationServiceError | None = None
    result: ScheduleRecipeResult | None = None
    async with session_factory() as session, session.begin():
        actor_role = await _authorize_and_lock_organization(
            session, context, prepared.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key("mutation", prepared.mutation_id)},
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
            if retained.outcome == "accepted":
                return _retained_result(retained)
            if retained.outcome == "rejected":
                deferred_error = _retained_error(retained)
            else:
                raise RuntimeError("Scheduled recipe mutation retained an unsupported outcome")
        elif prepared.violations:
            deferred_error = _validation_error(prepared.violations)
            session.add(
                _mutation(
                    command=prepared,
                    context=context,
                    actor_role=actor_role,
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error_payload(deferred_error),
                )
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _advisory_lock_key("scheduled_recipe", prepared.scheduled_recipe_id)},
            )
            exists = await session.scalar(
                select(ScheduledRecipe.id).where(ScheduledRecipe.id == prepared.scheduled_recipe_id)
            )
            if exists is not None:
                deferred_error = _validation_error(
                    (
                        FieldViolation(
                            "catalog_or_event_references", "must_be_active_and_consistent"
                        ),
                    )
                )
            else:
                references = await _load_references(session, prepared)
                if references is None:
                    deferred_error = _validation_error(
                        (
                            FieldViolation(
                                "catalog_or_event_references", "must_be_active_and_consistent"
                            ),
                        )
                    )
            if deferred_error is not None:
                session.add(
                    _mutation(
                        command=prepared,
                        context=context,
                        actor_role=actor_role,
                        request_hash=request_hash,
                        outcome="rejected",
                        outcome_payload=_error_payload(deferred_error),
                    )
                )
            else:
                assert references is not None
                selected_scale_amount = _suggested_scale(prepared, references)
                event = await session.scalar(
                    select(Event)
                    .where(
                        Event.id == prepared.event_id,
                        Event.organization_id == prepared.organization_id,
                        Event.lifecycle == "active",
                    )
                    .with_for_update(of=Event)
                )
                if event is None:
                    raise RuntimeError("Validated active event disappeared while scheduling")
                initial_price_captures = await _prepare_initial_event_price_captures(
                    session,
                    organization_id=prepared.organization_id,
                    event=event,
                    ingredient_ids=await _nonzero_recipe_ingredient_ids(
                        session,
                        recipe_version_id=prepared.recipe_version_id,
                        selected_scale_amount=selected_scale_amount,
                        base_scaling_amount=references.base_scaling_amount,
                    ),
                    actor_user_id=context.actor_user_id,
                )
                first_change_sequence, last_change_sequence = await _reserve_change_range(
                    session,
                    prepared.organization_id,
                    prepared.mutation_id,
                    1 + len(initial_price_captures) * 2,
                )
                result = ScheduleRecipeResult(
                    mutation_id=prepared.mutation_id,
                    scheduled_recipe_id=prepared.scheduled_recipe_id,
                    organization_id=prepared.organization_id,
                    event_id=prepared.event_id,
                    event_day_id=prepared.event_day_id,
                    event_meal_role_id=prepared.event_meal_role_id,
                    recipe_id=prepared.recipe_id,
                    recipe_version_id=prepared.recipe_version_id,
                    diner_count=references.diner_count,
                    attendance_mode="follows_event",
                    consumption_percentage=prepared.consumption_percentage,
                    selected_scale_amount=selected_scale_amount,
                    scale_mode="suggested",
                    position_key=prepared.position_key,
                    note=prepared.note,
                    first_change_sequence=first_change_sequence,
                    last_change_sequence=last_change_sequence,
                    replayed=False,
                )
                record = _result_payload(result)["scheduled_recipe"]
                assert isinstance(record, dict)
                placement_clock = FieldClock(
                    organization_id=result.organization_id,
                    entity_kind="scheduled_recipe",
                    entity_id=result.scheduled_recipe_id,
                    field_name="placement",
                    winning_client_wall_time=prepared.client_wall_time,
                    winning_mutation_id=prepared.mutation_id,
                )
                record["field_clocks"] = {
                    "placement": {
                        "winning_client_wall_time": (
                            placement_clock.winning_client_wall_time.isoformat()
                        ),
                        "winning_mutation_id": str(placement_clock.winning_mutation_id),
                    }
                }
                session.add(
                    ScheduledRecipe(
                        id=result.scheduled_recipe_id,
                        organization_id=result.organization_id,
                        event_id=result.event_id,
                        event_day_id=result.event_day_id,
                        event_meal_role_id=result.event_meal_role_id,
                        recipe_id=result.recipe_id,
                        recipe_version_id=result.recipe_version_id,
                        diner_count=result.diner_count,
                        attendance_mode=result.attendance_mode,
                        consumption_percentage=result.consumption_percentage,
                        selected_scale_amount=result.selected_scale_amount,
                        scale_mode=result.scale_mode,
                        position_key=result.position_key,
                        note=result.note,
                        created_by_user_id=context.actor_user_id,
                    )
                )
                session.add(placement_clock)
                session.add(
                    OrganizationChange(
                        organization_id=result.organization_id,
                        sequence=first_change_sequence,
                        mutation_id=result.mutation_id,
                        entity_id=result.scheduled_recipe_id,
                        entity_kind="scheduled_recipe",
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": record},
                    )
                )
                session.add(
                    _mutation(
                        command=prepared,
                        context=context,
                        actor_role=actor_role,
                        request_hash=request_hash,
                        outcome="accepted",
                        outcome_payload=_result_payload(result),
                        first_change_sequence=first_change_sequence,
                        last_change_sequence=last_change_sequence,
                    )
                )
                await session.flush()
                mutation = await session.get(Mutation, prepared.mutation_id)
                if mutation is None:
                    raise RuntimeError("Scheduled recipe mutation was not persisted")
                await _emit_event_price_snapshots(
                    session,
                    captures=initial_price_captures,
                    organization_id=prepared.organization_id,
                    event=event,
                    actor_user_id=context.actor_user_id,
                    client_wall_time=prepared.client_wall_time,
                    originating_mutation=mutation,
                    first_change_sequence=first_change_sequence + 1,
                )
    if deferred_error is not None:
        raise deferred_error
    if result is None:
        raise RuntimeError("Scheduling recipe produced no outcome")
    return result
