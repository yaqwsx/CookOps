"""Synchronizable organization catalog configuration commands."""

# The row type is selected from four SQLAlchemy models by the typed command kind.
# mypy: disable-error-code=attr-defined

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import (
    _authorize_member_and_lock_organization,
    _reserve_change_range,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import (
    DietaryTag,
    FieldClock,
    Mutation,
    OrganizationChange,
    RecipeTag,
    StoreSection,
    UnitDefinition,
)

COMMAND_SCHEMA_VERSION = 1
COMMAND_KIND = "catalog_configuration.mutate"
CatalogKind = Literal["store_section", "recipe_tag", "dietary_tag", "unit_definition"]
CatalogOperation = Literal["create", "update", "retire", "restore"]

_models = {
    "store_section": StoreSection,
    "recipe_tag": RecipeTag,
    "dietary_tag": DietaryTag,
    "unit_definition": UnitDefinition,
}
_fields = {
    "store_section": ("name", "position_key"),
    "recipe_tag": ("name", "color"),
    "dietary_tag": ("name", "color"),
    "unit_definition": ("custom_name",),
}
_color = re.compile(r"^#[0-9A-Fa-f]{6}$")
_position = re.compile(r"^[0-9A-Za-z]+$")


@dataclass(frozen=True, slots=True)
class CatalogConfigurationCommand:
    mutation_id: UUID
    organization_id: UUID
    entity_id: UUID
    entity_kind: CatalogKind
    operation: CatalogOperation
    client_wall_time: datetime
    name: str | None = None
    color: str | None = None
    position_key: str | None = None
    allows_ingredient_quantity: bool | None = None
    allows_recipe_scaling: bool | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CatalogConfigurationResult:
    mutation_id: UUID
    organization_id: UUID
    entity_id: UUID
    entity_kind: CatalogKind
    record: dict[str, object]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


def _text(value: object, path: str, errors: list[FieldViolation]) -> str:
    if not isinstance(value, str):
        errors.append(FieldViolation(path, "must_be_string"))
        return ""
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > 200:
        errors.append(FieldViolation(path, "must_be_nonblank_and_at_most_200_characters"))
    return value


def _prepared(
    command: CatalogConfigurationCommand,
) -> tuple[dict[str, object], tuple[FieldViolation, ...]]:
    errors: list[FieldViolation] = []
    values: dict[str, object] = {}
    if command.client_wall_time.tzinfo is None or command.client_wall_time.utcoffset() is None:
        errors.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if command.entity_kind not in _models:
        errors.append(FieldViolation("entity_kind", "must_be_supported"))
    if command.operation not in ("create", "update", "retire", "restore"):
        errors.append(FieldViolation("operation", "must_be_supported"))
    if command.operation in ("create", "update"):
        if command.entity_kind == "store_section":
            values["name"] = _text(command.name, "name", errors)
            if not isinstance(command.position_key, str) or not _position.fullmatch(
                command.position_key
            ):
                errors.append(FieldViolation("position_key", "must_be_sortable_position_key"))
            else:
                values["position_key"] = command.position_key
        elif command.entity_kind in ("recipe_tag", "dietary_tag"):
            values["name"] = _text(command.name, "name", errors)
            if command.color is not None and (
                not isinstance(command.color, str) or not _color.fullmatch(command.color)
            ):
                errors.append(FieldViolation("color", "must_be_hex_color_or_null"))
            else:
                values["color"] = command.color
            if command.entity_kind == "recipe_tag" and command.color is None:
                errors.append(FieldViolation("color", "must_be_hex_color"))
        else:
            values["custom_name"] = _text(command.name, "name", errors)
            if command.operation == "create":
                if type(command.allows_ingredient_quantity) is not bool:
                    errors.append(FieldViolation("allows_ingredient_quantity", "must_be_boolean"))
                if type(command.allows_recipe_scaling) is not bool:
                    errors.append(FieldViolation("allows_recipe_scaling", "must_be_boolean"))
                if not (command.allows_ingredient_quantity or command.allows_recipe_scaling):
                    errors.append(FieldViolation("unit", "must_allow_a_context"))
                values["allows_ingredient_quantity"] = command.allows_ingredient_quantity
                values["allows_recipe_scaling"] = command.allows_recipe_scaling
    return values, tuple(errors)


def _hash(command: CatalogConfigurationCommand, values: dict[str, object]) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {
                "kind": COMMAND_KIND,
                "id": str(command.mutation_id),
                "organization_id": str(command.organization_id),
                "entity_id": str(command.entity_id),
                "entity_kind": command.entity_kind,
                "operation": command.operation,
                "client_wall_time": command.client_wall_time.astimezone(UTC).isoformat(),
                "values": values,
                "logical_operation_id": str(command.logical_operation_id)
                if command.logical_operation_id
                else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _clock_wins(clock: FieldClock | None, command: CatalogConfigurationCommand) -> bool:
    return clock is None or (command.client_wall_time.astimezone(UTC), command.mutation_id) > (
        clock.winning_client_wall_time,
        clock.winning_mutation_id,
    )


async def _clock_metadata(
    session: AsyncSession, command: CatalogConfigurationCommand
) -> dict[str, object]:
    names = (*_fields[command.entity_kind], "lifecycle")
    clocks = {
        row.field_name: row
        for row in (
            await session.scalars(
                select(FieldClock).where(
                    FieldClock.organization_id == command.organization_id,
                    FieldClock.entity_kind == command.entity_kind,
                    FieldClock.entity_id == command.entity_id,
                    FieldClock.field_name.in_(names),
                )
            )
        ).all()
    }
    return {
        name: (
            {
                "winning_client_wall_time": row.winning_client_wall_time.isoformat(),
                "winning_mutation_id": str(row.winning_mutation_id),
            }
            if (row := clocks.get(name))
            else None
        )
        for name in names
    }


async def _record(
    session: AsyncSession, command: CatalogConfigurationCommand, row: Any
) -> dict[str, object]:
    base: dict[str, object] = {
        "id": str(row.id),
        "organization_id": str(row.organization_id),
        "created_at": row.created_at.isoformat(),
        "created_by_user_id": str(row.created_by_user_id),
        "retired_at": row.retired_at.isoformat() if row.retired_at else None,
        "retired_by_user_id": str(row.retired_by_user_id) if row.retired_by_user_id else None,
    }
    if command.entity_kind == "store_section":
        base.update(
            name=row.name, normalized_name=row.normalized_name, position_key=row.position_key
        )
    elif command.entity_kind == "recipe_tag":
        base.update(name=row.name, normalized_name=row.normalized_name, color=row.color)
    elif command.entity_kind == "dietary_tag":
        base.update(
            seed_key=row.seed_key,
            name=row.name,
            normalized_name=row.normalized_name,
            color=row.color,
        )
    else:
        base.update(
            code=row.code,
            custom_name=row.custom_name,
            normalized_custom_name=row.normalized_custom_name,
            dimension=row.dimension,
            base_unit_factor=None,
            rounds_up_to_whole_unit=row.rounds_up_to_whole_unit,
            allows_ingredient_quantity=row.allows_ingredient_quantity,
            allows_recipe_scaling=row.allows_recipe_scaling,
        )
    base["field_clocks"] = await _clock_metadata(session, command)
    return base


def _error(errors: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=errors, retry_same_identity=False
    )


def _error_payload(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": item.path, "code": item.code} for item in error.field_violations
            ],
        }
    }


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload
    try:
        error = payload["error"] if payload is not None else None
        if not isinstance(error, dict) or error.get("code") not in (
            "validation_failed",
            "client_time_too_far_ahead",
        ):
            raise TypeError
        if error["code"] == "client_time_too_far_ahead":
            return ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        violations = error.get("field_violations")
        if not isinstance(violations, list):
            raise TypeError
        parsed = tuple(
            FieldViolation(item["path"], item["code"])
            for item in violations
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("code"), str)
        )
        if len(parsed) != len(violations):
            raise TypeError
        return _error(parsed)
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Rejected catalog mutation has invalid outcome payload") from exc


async def mutate_catalog_configuration(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CatalogConfigurationCommand,
) -> CatalogConfigurationResult:
    values, validation = _prepared(command)
    request_hash = _hash(command, values)
    deferred: ApplicationServiceError | None = None
    result: CatalogConfigurationResult | None = None
    applied = False
    async with session_factory() as session, session.begin():
        role = await _authorize_member_and_lock_organization(
            session, context, command.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id, with_for_update=True)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "rejected":
                deferred = _retained_error(retained)
            elif (
                retained.outcome in ("accepted", "partially_superseded")
                and retained.first_change_sequence
                and retained.last_change_sequence
            ):
                row = await session.get(_models[command.entity_kind], command.entity_id)
                if row is None:
                    raise RuntimeError("retained catalog record disappeared")
                result = CatalogConfigurationResult(
                    command.mutation_id,
                    command.organization_id,
                    command.entity_id,
                    command.entity_kind,
                    await _record(session, command, row),
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    cast(Literal["accepted", "partially_superseded"], retained.outcome),
                )
            else:
                raise RuntimeError("unsupported retained catalog mutation")
        elif validation:
            deferred = _error(validation)
            session.add(
                _mutation(
                    command, context, role, request_hash, "rejected", _error_payload(deferred)
                )
            )
        elif command.client_wall_time.astimezone(UTC) > datetime.now(UTC) + timedelta(hours=24):
            deferred = ApplicationServiceError(
                "client_time_too_far_ahead", retry_same_identity=False
            )
            session.add(
                _mutation(
                    command, context, role, request_hash, "rejected", _error_payload(deferred)
                )
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key(f"catalog:{command.entity_kind}", command.entity_id)},
            )
            model = _models[command.entity_kind]
            row = await session.get(model, command.entity_id, with_for_update=True)
            if command.operation == "create":
                if row is not None and row.organization_id != command.organization_id:
                    raise ApplicationServiceError("forbidden", retry_same_identity=True)
                if row is not None:
                    deferred = _error((FieldViolation("entity_id", "already_exists"),))
                else:
                    name_field = (
                        "custom_name" if command.entity_kind == "unit_definition" else "name"
                    )
                    normalized_field = (
                        "normalized_custom_name"
                        if command.entity_kind == "unit_definition"
                        else "normalized_name"
                    )
                    normalized = cast(str, values[name_field]).lower()
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {
                            "key": _advisory_lock_key(
                                f"catalog-name:{command.entity_kind}:{normalized}",
                                command.organization_id,
                            )
                        },
                    )
                    duplicate = await session.scalar(
                        select(model.id).where(
                            model.organization_id == command.organization_id,
                            getattr(model, normalized_field) == normalized,
                        )
                    )
                    if duplicate is not None:
                        deferred = _error((FieldViolation("name", "already_exists"),))
                    if duplicate is None and command.entity_kind == "store_section":
                        row = StoreSection(
                            id=command.entity_id,
                            organization_id=command.organization_id,
                            name=values["name"],
                            normalized_name=cast(str, values["name"]).lower(),
                            position_key=values["position_key"],
                            created_by_user_id=context.actor_user_id,
                        )
                    elif duplicate is None and command.entity_kind == "recipe_tag":
                        row = RecipeTag(
                            id=command.entity_id,
                            organization_id=command.organization_id,
                            name=values["name"],
                            normalized_name=cast(str, values["name"]).lower(),
                            color=values["color"],
                            created_by_user_id=context.actor_user_id,
                        )
                    elif duplicate is None and command.entity_kind == "dietary_tag":
                        row = DietaryTag(
                            id=command.entity_id,
                            organization_id=command.organization_id,
                            seed_key=None,
                            name=values["name"],
                            normalized_name=cast(str, values["name"]).lower(),
                            color=values["color"],
                            created_by_user_id=context.actor_user_id,
                        )
                    elif duplicate is None:
                        row = UnitDefinition(
                            id=command.entity_id,
                            organization_id=command.organization_id,
                            code=f"custom.{command.entity_id.hex}",
                            custom_name=values["custom_name"],
                            normalized_custom_name=cast(str, values["custom_name"]).lower(),
                            dimension="custom",
                            base_unit_factor=None,
                            rounds_up_to_whole_unit=False,
                            allows_ingredient_quantity=values["allows_ingredient_quantity"],
                            allows_recipe_scaling=values["allows_recipe_scaling"],
                            created_by_user_id=context.actor_user_id,
                        )
                    if row is not None:
                        session.add(row)
                        await session.flush()
                        for field in (*_fields[command.entity_kind], "lifecycle"):
                            session.add(
                                FieldClock(
                                    organization_id=command.organization_id,
                                    entity_kind=command.entity_kind,
                                    entity_id=command.entity_id,
                                    field_name=field,
                                    winning_client_wall_time=command.client_wall_time.astimezone(
                                        UTC
                                    ),
                                    winning_mutation_id=command.mutation_id,
                                )
                            )
                        applied = True
            elif row is None or row.organization_id != command.organization_id:
                raise ApplicationServiceError("forbidden", retry_same_identity=True)
            elif command.operation == "update":
                if row.retired_at is not None:
                    deferred = _error((FieldViolation("entity_id", "retired_reference"),))
                else:
                    name_field = (
                        "custom_name" if command.entity_kind == "unit_definition" else "name"
                    )
                    normalized_field = (
                        "normalized_custom_name"
                        if command.entity_kind == "unit_definition"
                        else "normalized_name"
                    )
                    normalized = cast(str, values[name_field]).lower()
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {
                            "key": _advisory_lock_key(
                                f"catalog-name:{command.entity_kind}:{normalized}",
                                command.organization_id,
                            )
                        },
                    )
                    duplicate = await session.scalar(
                        select(model.id).where(
                            model.organization_id == command.organization_id,
                            getattr(model, normalized_field) == normalized,
                            model.id != command.entity_id,
                        )
                    )
                    if duplicate is not None:
                        deferred = _error((FieldViolation("name", "already_exists"),))
                    clocks = {
                        item.field_name: item
                        for item in (
                            await session.scalars(
                                select(FieldClock)
                                .where(
                                    FieldClock.organization_id == command.organization_id,
                                    FieldClock.entity_kind == command.entity_kind,
                                    FieldClock.entity_id == command.entity_id,
                                )
                                .with_for_update()
                            )
                        ).all()
                    }
                    for field in _fields[command.entity_kind]:
                        value = values[field]
                        if deferred is None and _clock_wins(clocks.get(field), command):
                            applied = True
                            setattr(row, field, value)
                            if field in ("name", "custom_name"):
                                setattr(
                                    row,
                                    "normalized_name"
                                    if field == "name"
                                    else "normalized_custom_name",
                                    cast(str, value).lower(),
                                )
                            session.add(
                                FieldClock(
                                    organization_id=command.organization_id,
                                    entity_kind=command.entity_kind,
                                    entity_id=command.entity_id,
                                    field_name=field,
                                    winning_client_wall_time=command.client_wall_time.astimezone(
                                        UTC
                                    ),
                                    winning_mutation_id=command.mutation_id,
                                )
                            ) if field not in clocks else setattr(
                                clocks[field],
                                "winning_client_wall_time",
                                command.client_wall_time.astimezone(UTC),
                            )
                            if field in clocks:
                                clocks[field].winning_mutation_id = command.mutation_id
            else:
                clock = await session.get(
                    FieldClock,
                    (command.organization_id, command.entity_kind, command.entity_id, "lifecycle"),
                    with_for_update=True,
                )
                if _clock_wins(clock, command):
                    applied = True
                    if command.operation == "retire":
                        row.retired_at, row.retired_by_user_id = (
                            command.client_wall_time.astimezone(UTC),
                            context.actor_user_id,
                        )
                    else:
                        row.retired_at, row.retired_by_user_id = None, None
                    if clock is None:
                        session.add(
                            FieldClock(
                                organization_id=command.organization_id,
                                entity_kind=command.entity_kind,
                                entity_id=command.entity_id,
                                field_name="lifecycle",
                                winning_client_wall_time=command.client_wall_time.astimezone(UTC),
                                winning_mutation_id=command.mutation_id,
                            )
                        )
                    else:
                        clock.winning_client_wall_time, clock.winning_mutation_id = (
                            command.client_wall_time.astimezone(UTC),
                            command.mutation_id,
                        )
            if deferred is not None:
                session.add(
                    _mutation(
                        command, context, role, request_hash, "rejected", _error_payload(deferred)
                    )
                )
            else:
                await session.flush()
                record = await _record(session, command, row)
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                outcome: Literal["accepted", "partially_superseded"] = (
                    "accepted" if applied else "partially_superseded"
                )
                result = CatalogConfigurationResult(
                    command.mutation_id,
                    command.organization_id,
                    command.entity_id,
                    command.entity_kind,
                    record,
                    first,
                    last,
                    False,
                    outcome,
                )
                session.add(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first,
                        mutation_id=command.mutation_id,
                        entity_id=command.entity_id,
                        entity_kind=command.entity_kind,
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": record},
                    )
                )
                session.add(
                    _mutation(
                        command,
                        context,
                        role,
                        request_hash,
                        outcome,
                        {"record": record},
                        first,
                        last,
                    )
                )
    if deferred:
        raise deferred
    if result is None:
        raise RuntimeError("catalog mutation produced no outcome")
    return result


def _mutation(
    command: CatalogConfigurationCommand,
    context: ExecutionContext,
    role: str,
    request_hash: bytes,
    outcome: str,
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
        client_wall_time=command.client_wall_time.astimezone(UTC),
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=COMMAND_KIND,
        target_identities=[
            {"entity_kind": command.entity_kind, "entity_id": str(command.entity_id)}
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )
