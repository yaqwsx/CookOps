"""Event-local scheduled recipe ingredient override commands."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    FieldClock,
    Ingredient,
    IngredientVersion,
    Mutation,
    OrganizationChange,
    RecipeVersionIngredientLine,
    ScheduledIngredientOverride,
    ScheduledRecipe,
)

COMMAND_KIND = "scheduled_recipe.ingredient_override"
COMMAND_SCHEMA_VERSION = 1
MAX_SERIALIZED_NOTE_BYTES = 131_072


@dataclass(frozen=True, slots=True)
class SetScheduledIngredientOverrideCommand:
    """Set or clear exactly one local replacement or added ingredient line."""

    mutation_id: UUID
    override_id: UUID
    organization_id: UUID
    event_id: UUID
    scheduled_recipe_id: UUID
    operation: Literal["set", "clear"]
    override_kind: Literal["replace", "add"]
    client_wall_time: datetime
    target_line_key: UUID | None = None
    ingredient_id: UUID | None = None
    ingredient_version_id: UUID | None = None
    quantity: Decimal | None = None
    include_in_portion_weight: bool | None = None
    note: str | None = None
    position_key: str | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ScheduledIngredientOverrideResult:
    mutation_id: UUID
    override_id: UUID | None
    organization_id: UUID
    event_id: UUID
    scheduled_recipe_id: UUID
    override_kind: Literal["replace", "add"]
    target_line_key: UUID | None
    ingredient_id: UUID | None
    ingredient_version_id: UUID | None
    quantity: Decimal | None
    include_in_portion_weight: bool | None
    note: str | None
    position_key: str | None
    retired_at: datetime | None
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


@dataclass(frozen=True, slots=True)
class _PreparedCommand:
    mutation_id: UUID
    override_id: UUID
    organization_id: UUID
    event_id: UUID
    scheduled_recipe_id: UUID
    operation: Literal["set", "clear"]
    override_kind: Literal["replace", "add"]
    client_wall_time: datetime
    target_line_key: UUID | None
    ingredient_id: UUID | None
    ingredient_version_id: UUID | None
    quantity: Decimal | None
    include_in_portion_weight: bool | None
    note: str | None
    position_key: str | None
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


def _canonical_decimal_string(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _canonical_note(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _invalid_hash_value(value: object) -> dict[str, str]:
    return {"invalid_type": type(value).__qualname__, "repr": repr(value)}


def _raw_uuid(value: object) -> str | dict[str, str] | None:
    if value is None:
        return None
    return str(value) if isinstance(value, UUID) else _invalid_hash_value(value)


def _raw_decimal(value: object) -> str | dict[str, str] | None:
    if value is None:
        return None
    return (
        _canonical_decimal_string(value)
        if isinstance(value, Decimal) and value.is_finite()
        else _invalid_hash_value(value)
    )


def _raw_time(value: object) -> str | dict[str, str]:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _invalid_hash_value(value)


def _request_hash(command: SetScheduledIngredientOverrideCommand) -> bytes:
    request = {
        "client_wall_time": _raw_time(command.client_wall_time),
        "command_kind": COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "event_id": _raw_uuid(command.event_id),
        "include_in_portion_weight": command.include_in_portion_weight
        if isinstance(command.include_in_portion_weight, bool)
        or command.include_in_portion_weight is None
        else _invalid_hash_value(command.include_in_portion_weight),
        "ingredient_id": _raw_uuid(command.ingredient_id),
        "ingredient_version_id": _raw_uuid(command.ingredient_version_id),
        "logical_operation_id": _raw_uuid(command.logical_operation_id),
        "note": _canonical_note(command.note) or None
        if isinstance(command.note, str)
        else (None if command.note is None else _invalid_hash_value(command.note)),
        "operation": command.operation
        if isinstance(command.operation, str)
        else _invalid_hash_value(command.operation),
        "organization_id": _raw_uuid(command.organization_id),
        "override_id": _raw_uuid(command.override_id),
        "override_kind": command.override_kind
        if isinstance(command.override_kind, str)
        else _invalid_hash_value(command.override_kind),
        "position_key": unicodedata.normalize("NFC", command.position_key).strip()
        if isinstance(command.position_key, str)
        else (None if command.position_key is None else _invalid_hash_value(command.position_key)),
        "quantity": _raw_decimal(command.quantity),
        "scheduled_recipe_id": _raw_uuid(command.scheduled_recipe_id),
        "target_line_key": _raw_uuid(command.target_line_key),
    }
    return hashlib.sha256(
        json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()


def _prepare(command: SetScheduledIngredientOverrideCommand) -> _PreparedCommand:
    violations: list[FieldViolation] = []
    for name, value in (
        ("mutation_id", command.mutation_id),
        ("override_id", command.override_id),
        ("organization_id", command.organization_id),
        ("event_id", command.event_id),
        ("scheduled_recipe_id", command.scheduled_recipe_id),
    ):
        if not isinstance(value, UUID):
            violations.append(FieldViolation(name, "must_be_uuid"))
    if command.operation not in ("set", "clear"):
        violations.append(FieldViolation("operation", "must_be_set_or_clear"))
    if command.override_kind not in ("replace", "add"):
        violations.append(FieldViolation("override_kind", "must_be_replace_or_add"))
    operation = command.operation if command.operation in ("set", "clear") else "set"
    kind = command.override_kind if command.override_kind in ("replace", "add") else "replace"
    has_time = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not has_time:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    target_line_key = command.target_line_key if isinstance(command.target_line_key, UUID) else None
    if command.target_line_key is not None and target_line_key is None:
        violations.append(FieldViolation("target_line_key", "must_be_uuid_or_null"))
    ingredient_id = command.ingredient_id if isinstance(command.ingredient_id, UUID) else None
    ingredient_version_id = (
        command.ingredient_version_id if isinstance(command.ingredient_version_id, UUID) else None
    )
    quantity = (
        command.quantity
        if isinstance(command.quantity, Decimal) and command.quantity.is_finite()
        else None
    )
    note = _canonical_note(command.note) if isinstance(command.note, str) else None
    if command.note is not None and not isinstance(command.note, str):
        violations.append(FieldViolation("note", "must_be_string_or_null"))
    if note == "":
        note = None
    if note is not None and (
        "\x00" in note
        or len(json.dumps(note, ensure_ascii=False).encode()) > MAX_SERIALIZED_NOTE_BYTES
    ):
        violations.append(FieldViolation("note", "must_be_valid_and_fit_change_record"))
    position = (
        unicodedata.normalize("NFC", command.position_key).strip()
        if isinstance(command.position_key, str)
        else None
    )
    if command.position_key is not None and (
        position is None
        or not position
        or len(position) > 255
        or not position.isascii()
        or not position.isalnum()
    ):
        violations.append(
            FieldViolation("position_key", "must_be_ascii_alphanumeric_at_most_255_or_null")
        )
    if operation == "set":
        if quantity is None or quantity < 0:
            violations.append(FieldViolation("quantity", "must_be_nonnegative_finite_decimal"))
        if kind == "replace":
            if target_line_key is None:
                violations.append(FieldViolation("target_line_key", "required_for_replacement"))
            if command.ingredient_id is not None or command.ingredient_version_id is not None:
                violations.append(FieldViolation("ingredient", "derived_from_pinned_recipe_line"))
            if command.include_in_portion_weight is not None:
                violations.append(
                    FieldViolation("include_in_portion_weight", "inherited_for_replacement")
                )
            if command.position_key is not None:
                violations.append(FieldViolation("position_key", "not_applicable_to_replacement"))
        else:
            if target_line_key is not None:
                violations.append(
                    FieldViolation("target_line_key", "not_applicable_to_added_ingredient")
                )
            if ingredient_id is None or ingredient_version_id is None:
                violations.append(
                    FieldViolation("ingredient", "active_catalog_ingredient_version_required")
                )
            if not isinstance(command.include_in_portion_weight, bool):
                violations.append(
                    FieldViolation(
                        "include_in_portion_weight", "must_be_boolean_for_added_ingredient"
                    )
                )
            if position is None:
                violations.append(FieldViolation("position_key", "required_for_added_ingredient"))
    else:
        if kind == "replace" and target_line_key is None:
            violations.append(FieldViolation("target_line_key", "required_for_replacement"))
    return _PreparedCommand(
        mutation_id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        override_id=command.override_id if isinstance(command.override_id, UUID) else UUID(int=0),
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        event_id=command.event_id if isinstance(command.event_id, UUID) else UUID(int=0),
        scheduled_recipe_id=command.scheduled_recipe_id
        if isinstance(command.scheduled_recipe_id, UUID)
        else UUID(int=0),
        operation=operation,
        override_kind=kind,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if has_time
        else datetime(1970, 1, 1, tzinfo=UTC),
        target_line_key=target_line_key,
        ingredient_id=ingredient_id,
        ingredient_version_id=ingredient_version_id,
        quantity=quantity,
        include_in_portion_weight=command.include_in_portion_weight
        if isinstance(command.include_in_portion_weight, bool)
        else None,
        note=note,
        position_key=position,
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        violations=tuple(violations),
    )


def _field_name(command: _PreparedCommand) -> str:
    return f"{command.override_kind}." + str(
        command.target_line_key if command.override_kind == "replace" else command.override_id
    )


def _clock_wins(clock: FieldClock | None, command: _PreparedCommand) -> bool:
    return clock is None or (command.client_wall_time, command.mutation_id) > (
        clock.winning_client_wall_time,
        clock.winning_mutation_id,
    )


def _record(override: ScheduledIngredientOverride) -> dict[str, object]:
    return {
        "id": str(override.id),
        "organization_id": str(override.organization_id),
        "event_id": str(override.event_id),
        "scheduled_recipe_id": str(override.scheduled_recipe_id),
        "override_kind": override.override_kind,
        "target_line_key": str(override.target_line_key) if override.target_line_key else None,
        "ingredient_id": str(override.ingredient_id),
        "ingredient_version_id": str(override.ingredient_version_id),
        "quantity": _canonical_decimal_string(override.quantity),
        "include_in_portion_weight": override.include_in_portion_weight,
        "note": override.note,
        "position_key": override.position_key,
        "retired_at": override.retired_at.isoformat() if override.retired_at else None,
        "retired_by_user_id": str(override.retired_by_user_id)
        if override.retired_by_user_id
        else None,
        "created_by_user_id": str(override.created_by_user_id),
    }


def _result(
    command: _PreparedCommand,
    override: ScheduledIngredientOverride,
    first: int,
    last: int,
    replayed: bool,
    outcome: Literal["accepted", "partially_superseded"],
) -> ScheduledIngredientOverrideResult:
    return ScheduledIngredientOverrideResult(
        command.mutation_id,
        override.id,
        command.organization_id,
        command.event_id,
        command.scheduled_recipe_id,
        cast(Literal["replace", "add"], override.override_kind),
        override.target_line_key,
        override.ingredient_id,
        override.ingredient_version_id,
        override.quantity,
        override.include_in_portion_weight,
        override.note,
        override.position_key,
        override.retired_at,
        first,
        last,
        replayed,
        outcome,
    )


def _result_payload(result: ScheduledIngredientOverrideResult) -> dict[str, object]:
    return {
        "scheduled_ingredient_override": {
            "id": str(result.override_id) if result.override_id else None,
            "organization_id": str(result.organization_id),
            "event_id": str(result.event_id),
            "scheduled_recipe_id": str(result.scheduled_recipe_id),
            "override_kind": result.override_kind,
            "target_line_key": str(result.target_line_key) if result.target_line_key else None,
            "ingredient_id": str(result.ingredient_id) if result.ingredient_id else None,
            "ingredient_version_id": str(result.ingredient_version_id)
            if result.ingredient_version_id
            else None,
            "quantity": _canonical_decimal_string(result.quantity)
            if result.quantity is not None
            else None,
            "include_in_portion_weight": result.include_in_portion_weight,
            "note": result.note,
            "position_key": result.position_key,
            "retired_at": result.retired_at.isoformat() if result.retired_at else None,
        },
        "outcome": result.outcome,
    }


def _error_payload(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [{"path": v.path, "code": v.code} for v in error.field_violations],
        }
    }


def _validation(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
    )


def _mutation(
    command: _PreparedCommand,
    context: ExecutionContext,
    actor_role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "partially_superseded", "rejected"],
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
            {"entity_kind": "scheduled_ingredient_override", "entity_id": str(command.override_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def set_scheduled_ingredient_override(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetScheduledIngredientOverrideCommand,
) -> ScheduledIngredientOverrideResult:
    """Apply one LWW local override change to an active scheduled recipe."""

    prepared, request_hash = _prepare(command), _request_hash(command)
    deferred: ApplicationServiceError | None = None
    result: ScheduledIngredientOverrideResult | None = None
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
            if retained.outcome == "rejected":
                error = retained.outcome_payload.get("error") if retained.outcome_payload else None
                if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                    raise RuntimeError("Retained override rejection has an invalid payload")
                deferred = ApplicationServiceError(
                    cast(
                        Literal["validation_failed", "archived_event", "client_time_too_far_ahead"],
                        error["code"],
                    ),
                    retry_same_identity=False,
                )
            elif retained.outcome in ("accepted", "partially_superseded"):
                payload = (
                    retained.outcome_payload.get("scheduled_ingredient_override")
                    if retained.outcome_payload
                    else None
                )
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("id"), str)
                    or retained.first_change_sequence is None
                    or retained.last_change_sequence is None
                ):
                    raise RuntimeError("Retained override result has an invalid payload")
                existing = await session.get(ScheduledIngredientOverride, UUID(payload["id"]))
                if existing is None:
                    raise RuntimeError("Retained override no longer exists")
                return _result(
                    prepared,
                    existing,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    cast(Literal["accepted", "partially_superseded"], retained.outcome),
                )
            else:
                raise RuntimeError("Retained override has an unsupported outcome")
        elif prepared.violations:
            deferred = _validation(prepared.violations)
        elif prepared.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            deferred = ApplicationServiceError(
                "client_time_too_far_ahead", retry_same_identity=False
            )
        if deferred is not None:
            if retained is None:
                session.add(
                    _mutation(
                        prepared, context, role, request_hash, "rejected", _error_payload(deferred)
                    )
                )
        elif retained is None:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        "scheduled_recipe_override", prepared.scheduled_recipe_id
                    )
                },
            )
            scheduled = await session.scalar(
                select(ScheduledRecipe)
                .join(Event, Event.id == ScheduledRecipe.event_id)
                .where(
                    ScheduledRecipe.id == prepared.scheduled_recipe_id,
                    ScheduledRecipe.organization_id == prepared.organization_id,
                    ScheduledRecipe.event_id == prepared.event_id,
                    ScheduledRecipe.retired_at.is_(None),
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ScheduledRecipe, Event))
            )
            if scheduled is None:
                event = await session.scalar(
                    select(Event.lifecycle).where(
                        Event.id == prepared.event_id,
                        Event.organization_id == prepared.organization_id,
                    )
                )
                deferred = (
                    ApplicationServiceError("archived_event", retry_same_identity=False)
                    if event == "archived"
                    else _validation(
                        (
                            FieldViolation(
                                "scheduled_recipe_id", "must_be_active_and_belong_to_active_event"
                            ),
                        )
                    )
                )
            else:
                line: RecipeVersionIngredientLine | None = None
                if prepared.override_kind == "replace":
                    line = await session.scalar(
                        select(RecipeVersionIngredientLine)
                        .where(
                            RecipeVersionIngredientLine.recipe_version_id
                            == scheduled.recipe_version_id,
                            RecipeVersionIngredientLine.line_key == prepared.target_line_key,
                        )
                        .with_for_update(of=RecipeVersionIngredientLine)
                    )
                    if line is None:
                        deferred = _validation(
                            (
                                FieldViolation(
                                    "target_line_key", "must_exist_in_pinned_recipe_version"
                                ),
                            )
                        )
                elif prepared.operation == "set":
                    ingredient = await session.scalar(
                        select(Ingredient)
                        .where(
                            Ingredient.id == prepared.ingredient_id,
                            Ingredient.organization_id == prepared.organization_id,
                            Ingredient.retired_at.is_(None),
                            Ingredient.current_version_id == prepared.ingredient_version_id,
                        )
                        .with_for_update(of=Ingredient)
                    )
                    version = await session.scalar(
                        select(IngredientVersion.id)
                        .where(
                            IngredientVersion.id == prepared.ingredient_version_id,
                            IngredientVersion.ingredient_id == prepared.ingredient_id,
                            IngredientVersion.organization_id == prepared.organization_id,
                        )
                        .with_for_update(of=IngredientVersion)
                    )
                    if ingredient is None or version is None:
                        deferred = _validation(
                            (
                                FieldViolation(
                                    "ingredient",
                                    "must_be_active_current_catalog_ingredient_version",
                                ),
                            )
                        )
                    elif (
                        await session.scalar(
                            select(RecipeVersionIngredientLine.id)
                            .join(
                                IngredientVersion,
                                IngredientVersion.id
                                == RecipeVersionIngredientLine.ingredient_version_id,
                            )
                            .where(
                                RecipeVersionIngredientLine.recipe_version_id
                                == scheduled.recipe_version_id,
                                IngredientVersion.ingredient_id == prepared.ingredient_id,
                            )
                        )
                        is not None
                    ):
                        deferred = _validation(
                            (
                                FieldViolation(
                                    "ingredient_id",
                                    "must_not_already_exist_in_pinned_recipe_version",
                                ),
                            )
                        )
                if deferred is None:
                    field_name = _field_name(prepared)
                    clock = await session.scalar(
                        select(FieldClock)
                        .where(
                            FieldClock.organization_id == prepared.organization_id,
                            FieldClock.entity_kind == "scheduled_ingredient_override",
                            FieldClock.entity_id == scheduled.id,
                            FieldClock.field_name == field_name,
                        )
                        .with_for_update(of=FieldClock)
                    )
                    predicate = [
                        ScheduledIngredientOverride.scheduled_recipe_id == scheduled.id,
                        ScheduledIngredientOverride.override_kind == prepared.override_kind,
                    ]
                    predicate.append(
                        ScheduledIngredientOverride.target_line_key == prepared.target_line_key
                        if prepared.override_kind == "replace"
                        else ScheduledIngredientOverride.id == prepared.override_id
                    )
                    existing = await session.scalar(
                        select(ScheduledIngredientOverride)
                        .where(*predicate)
                        .order_by(
                            ScheduledIngredientOverride.retired_at.is_(None).desc(),
                            ScheduledIngredientOverride.created_at.desc(),
                        )
                        .with_for_update(of=ScheduledIngredientOverride)
                    )
                    is_active = existing is not None and existing.retired_at is None
                    if prepared.operation == "clear" and not is_active:
                        deferred = _validation(
                            (FieldViolation("override", "must_be_active_to_clear"),)
                        )
                    elif _clock_wins(clock, prepared):
                        now = datetime.now(UTC)
                        if prepared.operation == "clear":
                            assert existing is not None
                            existing.retired_at, existing.retired_by_user_id = (
                                now,
                                context.actor_user_id,
                            )
                            existing.last_modified_at, existing.last_modified_by_user_id = (
                                now,
                                context.actor_user_id,
                            )
                            changed = existing
                        elif existing is None:
                            assert prepared.quantity is not None
                            changed = ScheduledIngredientOverride(
                                id=prepared.override_id,
                                organization_id=prepared.organization_id,
                                event_id=prepared.event_id,
                                scheduled_recipe_id=scheduled.id,
                                override_kind=prepared.override_kind,
                                target_line_key=prepared.target_line_key,
                                ingredient_id=line
                                and (
                                    await session.scalar(
                                        select(IngredientVersion.ingredient_id).where(
                                            IngredientVersion.id == line.ingredient_version_id
                                        )
                                    )
                                )
                                or prepared.ingredient_id,
                                ingredient_version_id=line.ingredient_version_id
                                if line
                                else cast(UUID, prepared.ingredient_version_id),
                                quantity=prepared.quantity,
                                include_in_portion_weight=None
                                if line
                                else prepared.include_in_portion_weight,
                                note=prepared.note,
                                position_key=None if line else prepared.position_key,
                                created_by_user_id=context.actor_user_id,
                                last_modified_by_user_id=context.actor_user_id,
                            )
                            session.add(changed)
                        else:
                            assert prepared.quantity is not None
                            existing.quantity, existing.note = prepared.quantity, prepared.note
                            existing.retired_at, existing.retired_by_user_id = None, None
                            if prepared.override_kind == "add":
                                existing.include_in_portion_weight, existing.position_key = (
                                    prepared.include_in_portion_weight,
                                    prepared.position_key,
                                )
                            existing.last_modified_at, existing.last_modified_by_user_id = (
                                now,
                                context.actor_user_id,
                            )
                            changed = existing
                        if clock is None:
                            session.add(
                                FieldClock(
                                    organization_id=prepared.organization_id,
                                    entity_kind="scheduled_ingredient_override",
                                    entity_id=scheduled.id,
                                    field_name=field_name,
                                    winning_client_wall_time=prepared.client_wall_time,
                                    winning_mutation_id=prepared.mutation_id,
                                )
                            )
                        else:
                            clock.winning_client_wall_time, clock.winning_mutation_id = (
                                prepared.client_wall_time,
                                prepared.mutation_id,
                            )
                        outcome: Literal["accepted", "partially_superseded"] = "accepted"
                    else:
                        assert existing is not None
                        changed, outcome = existing, "partially_superseded"
                    if deferred is None:
                        first, last = await _reserve_change_range(
                            session, prepared.organization_id, prepared.mutation_id, 1
                        )
                        result = _result(prepared, changed, first, last, False, outcome)
                        session.add(
                            OrganizationChange(
                                organization_id=prepared.organization_id,
                                sequence=first,
                                mutation_id=prepared.mutation_id,
                                entity_id=changed.id,
                                entity_kind="scheduled_ingredient_override",
                                operation="upsert",
                                payload={"record_schema_version": 1, "record": _record(changed)},
                            )
                        )
                        session.add(
                            _mutation(
                                prepared,
                                context,
                                role,
                                request_hash,
                                outcome,
                                _result_payload(result),
                                first,
                                last,
                            )
                        )
            if deferred is not None:
                session.add(
                    _mutation(
                        prepared, context, role, request_hash, "rejected", _error_payload(deferred)
                    )
                )
    if deferred is not None:
        raise deferred
    if result is None:
        raise RuntimeError("Scheduled ingredient override produced no outcome")
    return result
