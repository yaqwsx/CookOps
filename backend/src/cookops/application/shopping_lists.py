"""Materialize the first immutable revision of a shopping list."""

import hashlib
import json
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

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
    EventDay,
    EventMealRole,
    IngredientVersion,
    Mutation,
    OrganizationChange,
    RecipeVersion,
    RecipeVersionIngredientLine,
    ScheduledIngredientOverride,
    ScheduledRecipe,
    ShoppingContribution,
    ShoppingContributionSnapshot,
    ShoppingGenerationRevision,
    ShoppingIngredientRow,
    ShoppingList,
    ShoppingRevisionSource,
    StoreSection,
)

COMMAND_KIND = "shopping_list.create"
COMMAND_SCHEMA_VERSION = 1
MAX_SERIALIZED_NAME_BYTES = 800


@dataclass(frozen=True, slots=True)
class CreateShoppingListCommand:
    mutation_id: UUID
    shopping_list_id: UUID
    generation_revision_id: UUID
    organization_id: UUID
    event_id: UUID
    name: str
    scheduled_recipe_ids: tuple[UUID, ...]
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CreateShoppingListResult:
    mutation_id: UUID
    shopping_list_id: UUID
    generation_revision_id: UUID
    organization_id: UUID
    event_id: UUID
    name: str
    scheduled_recipe_ids: tuple[UUID, ...]
    ingredient_row_ids: tuple[UUID, ...]
    contribution_ids: tuple[UUID, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


@dataclass(frozen=True, slots=True)
class _Prepared:
    mutation_id: UUID
    shopping_list_id: UUID
    generation_revision_id: UUID
    organization_id: UUID
    event_id: UUID
    name: str
    scheduled_recipe_ids: tuple[UUID, ...]
    client_wall_time: datetime
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedLine:
    ingredient_id: UUID
    ingredient_version_id: UUID
    ingredient_name: str
    calculation_unit_id: UUID
    default_store_section_id: UUID | None
    default_store_section_name: str | None
    quantity: Decimal
    note: str | None


@dataclass(frozen=True, slots=True)
class _Source:
    scheduled_recipe_id: UUID
    recipe_name: str
    recipe_description: str | None
    calendar_date: str
    meal_role: str
    selected_scale_amount: Decimal
    recipe_base_scaling_amount: Decimal


def _canonical_decimal(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _canonical_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _invalid(value: object) -> dict[str, str]:
    return {"invalid_type": type(value).__qualname__, "repr": repr(value)}


def _raw_uuid(value: object) -> str | dict[str, str]:
    return str(value) if isinstance(value, UUID) else _invalid(value)


def _raw_time(value: object) -> str | dict[str, str]:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _invalid(value)


def _raw_source_ids(value: object) -> object:
    if not isinstance(value, tuple):
        return _invalid(value)
    encoded = [_raw_uuid(item) for item in value]
    if all(isinstance(item, str) for item in encoded):
        return sorted(str(item) for item in encoded)
    return encoded


def _hash(command: CreateShoppingListCommand) -> bytes:
    value = {
        "command_kind": COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "mutation_id": _raw_uuid(command.mutation_id),
        "shopping_list_id": _raw_uuid(command.shopping_list_id),
        "generation_revision_id": _raw_uuid(command.generation_revision_id),
        "organization_id": _raw_uuid(command.organization_id),
        "event_id": _raw_uuid(command.event_id),
        "name": _canonical_name(command.name)
        if isinstance(command.name, str)
        else _invalid(command.name),
        "scheduled_recipe_ids": _raw_source_ids(command.scheduled_recipe_ids),
        "client_wall_time": _raw_time(command.client_wall_time),
        "logical_operation_id": (
            _raw_uuid(command.logical_operation_id)
            if command.logical_operation_id is not None
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _prepare(command: CreateShoppingListCommand) -> _Prepared:
    violations: list[FieldViolation] = []
    values = (
        ("mutation_id", command.mutation_id),
        ("shopping_list_id", command.shopping_list_id),
        ("generation_revision_id", command.generation_revision_id),
        ("organization_id", command.organization_id),
        ("event_id", command.event_id),
    )
    for path, value in values:
        if not isinstance(value, UUID):
            violations.append(FieldViolation(path, "must_be_uuid"))
    name = _canonical_name(command.name) if isinstance(command.name, str) else ""
    if (
        not isinstance(command.name, str)
        or not name
        or len(name) > 200
        or len(json.dumps(name, ensure_ascii=False).encode()) > MAX_SERIALIZED_NAME_BYTES
    ):
        violations.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
    if not isinstance(command.scheduled_recipe_ids, tuple):
        violations.append(FieldViolation("scheduled_recipe_ids", "must_be_uuid_tuple"))
        source_ids: tuple[UUID, ...] = ()
    else:
        source_ids = tuple(item for item in command.scheduled_recipe_ids if isinstance(item, UUID))
        if len(source_ids) != len(command.scheduled_recipe_ids):
            violations.append(FieldViolation("scheduled_recipe_ids", "must_contain_only_uuids"))
        if len(set(source_ids)) != len(source_ids):
            violations.append(FieldViolation("scheduled_recipe_ids", "must_not_contain_duplicates"))
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
    return _Prepared(
        mutation_id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        shopping_list_id=command.shopping_list_id
        if isinstance(command.shopping_list_id, UUID)
        else UUID(int=0),
        generation_revision_id=command.generation_revision_id
        if isinstance(command.generation_revision_id, UUID)
        else UUID(int=0),
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        event_id=command.event_id if isinstance(command.event_id, UUID) else UUID(int=0),
        name=name,
        scheduled_recipe_ids=source_ids,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if has_time
        else datetime(1970, 1, 1, tzinfo=UTC),
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        violations=tuple(violations),
    )


def _error(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
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


def _mutation(
    prepared: _Prepared,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
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
            {"entity_kind": "shopping_list", "entity_id": str(prepared.shopping_list_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


def _payload(result: CreateShoppingListResult) -> dict[str, object]:
    return {
        "shopping_list": {
            "id": str(result.shopping_list_id),
            "generation_revision_id": str(result.generation_revision_id),
            "organization_id": str(result.organization_id),
            "event_id": str(result.event_id),
            "name": result.name,
            "scheduled_recipe_ids": [str(item) for item in result.scheduled_recipe_ids],
            "ingredient_row_ids": [str(item) for item in result.ingredient_row_ids],
            "contribution_ids": [str(item) for item in result.contribution_ids],
        }
    }


def _retained(mutation: Mutation) -> CreateShoppingListResult:
    try:
        payload = mutation.outcome_payload
        if payload is None:
            raise TypeError
        record = payload["shopping_list"]
        if (
            not isinstance(record, dict)
            or mutation.first_change_sequence is None
            or mutation.last_change_sequence is None
        ):
            raise TypeError
        return CreateShoppingListResult(
            mutation_id=mutation.id,
            shopping_list_id=UUID(str(record["id"])),
            generation_revision_id=UUID(str(record["generation_revision_id"])),
            organization_id=UUID(str(record["organization_id"])),
            event_id=UUID(str(record["event_id"])),
            name=str(record["name"]),
            scheduled_recipe_ids=tuple(UUID(str(item)) for item in record["scheduled_recipe_ids"]),
            ingredient_row_ids=tuple(UUID(str(item)) for item in record["ingredient_row_ids"]),
            contribution_ids=tuple(UUID(str(item)) for item in record["contribution_ids"]),
            first_change_sequence=mutation.first_change_sequence,
            last_change_sequence=mutation.last_change_sequence,
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Accepted shopping-list mutation has invalid outcome payload") from exc


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload
    try:
        error = payload["error"] if payload is not None else None
        violations = error["field_violations"] if isinstance(error, dict) else None
        if (
            not isinstance(error, dict)
            or error.get("code") != "validation_failed"
            or not isinstance(violations, list)
        ):
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
        raise RuntimeError("Rejected shopping-list mutation has invalid outcome payload") from exc


async def _resolve_source(
    session: AsyncSession, prepared: _Prepared, source_id: UUID
) -> tuple[_Source, list[_ResolvedLine]] | None:
    source = (
        await session.execute(
            select(ScheduledRecipe, RecipeVersion, EventDay, EventMealRole)
            .join(RecipeVersion, RecipeVersion.id == ScheduledRecipe.recipe_version_id)
            .join(EventDay, EventDay.id == ScheduledRecipe.event_day_id)
            .join(EventMealRole, EventMealRole.id == ScheduledRecipe.event_meal_role_id)
            .where(
                ScheduledRecipe.id == source_id,
                ScheduledRecipe.event_id == prepared.event_id,
                ScheduledRecipe.organization_id == prepared.organization_id,
                ScheduledRecipe.retired_at.is_(None),
            )
            .with_for_update(of=ScheduledRecipe)
        )
    ).one_or_none()
    if source is None:
        return None
    scheduled, recipe, day, role = source
    label = role.custom_name or role.built_in_translation_key or ""
    details = _Source(
        source_id,
        recipe.name,
        recipe.description,
        day.calendar_date.isoformat(),
        label,
        scheduled.selected_scale_amount,
        recipe.base_scaling_amount,
    )
    base_lines = list(
        (
            await session.scalars(
                select(RecipeVersionIngredientLine)
                .where(RecipeVersionIngredientLine.recipe_version_id == scheduled.recipe_version_id)
                .order_by(RecipeVersionIngredientLine.position_key)
                .with_for_update(of=RecipeVersionIngredientLine)
            )
        ).all()
    )
    overrides = list(
        (
            await session.scalars(
                select(ScheduledIngredientOverride)
                .where(
                    ScheduledIngredientOverride.scheduled_recipe_id == source_id,
                    ScheduledIngredientOverride.retired_at.is_(None),
                )
                .with_for_update(of=ScheduledIngredientOverride)
            )
        ).all()
    )
    replacements = {
        override.target_line_key: override
        for override in overrides
        if override.override_kind == "replace"
    }
    resolved: list[tuple[UUID, Decimal, str | None]] = []
    for line in base_lines:
        override = replacements.get(line.line_key)
        raw_quantity = (
            override.quantity
            if override is not None
            else line.base_quantity * scheduled.selected_scale_amount / recipe.base_scaling_amount
            if line.scaling_behavior == "proportional"
            else line.base_quantity
        )
        resolved.append(
            (
                line.ingredient_version_id,
                raw_quantity,
                override.note if override is not None else line.note,
            )
        )
    for override in overrides:
        if override.override_kind == "add":
            resolved.append(
                (
                    override.ingredient_version_id,
                    override.quantity,
                    override.note,
                )
            )
    version_ids = [version_id for version_id, *_ in resolved]
    versions = {
        item.id: item
        for item in (
            await session.scalars(
                select(IngredientVersion).where(IngredientVersion.id.in_(version_ids))
            )
        ).all()
    }
    sections = {
        item.id: item.name
        for item in (
            await session.scalars(
                select(StoreSection).where(
                    StoreSection.id.in_(
                        [
                            value.default_store_section_id
                            for value in versions.values()
                            if value.default_store_section_id is not None
                        ]
                    )
                )
            )
        ).all()
    }
    lines: list[_ResolvedLine] = []
    for version_id, quantity, note in resolved:
        version = versions.get(version_id)
        if version is None or quantity < 0:
            return None
        if quantity == 0:
            continue
        lines.append(
            _ResolvedLine(
                version.ingredient_id,
                version.id,
                version.name,
                version.canonical_unit_id,
                version.default_store_section_id,
                (
                    sections.get(version.default_store_section_id)
                    if version.default_store_section_id is not None
                    else None
                ),
                quantity,
                note,
            )
        )
    return details, lines


async def create_shopping_list(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateShoppingListCommand,
) -> CreateShoppingListResult:
    """Create a shopping aggregate and materialize its initial immutable revision."""
    prepared, request_hash = _prepare(command), _hash(command)
    error: ApplicationServiceError | None = None
    result: CreateShoppingListResult | None = None
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
            if retained.outcome == "accepted":
                return _retained(retained)
            if retained.outcome == "rejected":
                error = _retained_error(retained)
            else:
                raise RuntimeError("Unsupported retained shopping mutation")
        elif prepared.violations:
            error = _error(prepared.violations)
            session.add(
                _mutation(prepared, context, role, request_hash, "rejected", _error_payload(error))
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("shopping_list", prepared.shopping_list_id)},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        "shopping_generation_revision", prepared.generation_revision_id
                    )
                },
            )
            collision = await session.scalar(
                select(ShoppingList.id).where(ShoppingList.id == prepared.shopping_list_id)
            )
            revision_collision = await session.scalar(
                select(ShoppingGenerationRevision.id).where(
                    ShoppingGenerationRevision.id == prepared.generation_revision_id
                )
            )
            event = await session.scalar(
                select(Event.id)
                .where(
                    Event.id == prepared.event_id,
                    Event.organization_id == prepared.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=Event)
            )
            materialized: list[tuple[_Source, list[_ResolvedLine]]] = []
            if collision is not None or revision_collision is not None or event is None:
                error = (
                    ApplicationServiceError("archived_event", retry_same_identity=False)
                    if event is None
                    else _error((FieldViolation("identity", "must_not_already_exist"),))
                )
            else:
                for source_id in prepared.scheduled_recipe_ids:
                    loaded = await _resolve_source(session, prepared, source_id)
                    if loaded is None:
                        error = _error(
                            (
                                FieldViolation(
                                    "scheduled_recipe_ids",
                                    "must_reference_active_recipes_from_event",
                                ),
                            )
                        )
                        break
                    materialized.append(loaded)
            if error is not None:
                session.add(
                    _mutation(
                        prepared, context, role, request_hash, "rejected", _error_payload(error)
                    )
                )
            else:
                rows: dict[UUID, ShoppingIngredientRow] = {}
                contributions: list[ShoppingContribution] = []
                snapshots: list[ShoppingContributionSnapshot] = []
                shopping_list = ShoppingList(
                    id=prepared.shopping_list_id,
                    organization_id=prepared.organization_id,
                    event_id=prepared.event_id,
                    name=prepared.name,
                    created_by_user_id=context.actor_user_id,
                )
                session.add(shopping_list)
                await session.flush()
                session.add(
                    ShoppingGenerationRevision(
                        id=prepared.generation_revision_id,
                        organization_id=prepared.organization_id,
                        event_id=prepared.event_id,
                        shopping_list_id=prepared.shopping_list_id,
                        generated_by_user_id=context.actor_user_id,
                    )
                )
                await session.flush()
                for source, lines in materialized:
                    session.add(
                        ShoppingRevisionSource(
                            generation_revision_id=prepared.generation_revision_id,
                            shopping_list_id=prepared.shopping_list_id,
                            organization_id=prepared.organization_id,
                            event_id=prepared.event_id,
                            scheduled_recipe_id=source.scheduled_recipe_id,
                        )
                    )
                    grouped: dict[UUID, list[_ResolvedLine]] = defaultdict(list)
                    for line in lines:
                        grouped[line.ingredient_id].append(line)
                    for ingredient_id, items in grouped.items():
                        representative = items[-1]
                        row = rows.get(ingredient_id)
                        if row is None:
                            row = ShoppingIngredientRow(
                                id=uuid4(),
                                organization_id=prepared.organization_id,
                                event_id=prepared.event_id,
                                shopping_list_id=prepared.shopping_list_id,
                                ingredient_id=ingredient_id,
                                ingredient_name=representative.ingredient_name,
                                calculation_unit_id=representative.calculation_unit_id,
                                default_store_section_id=representative.default_store_section_id,
                                default_store_section_name=representative.default_store_section_name,
                                created_by_user_id=context.actor_user_id,
                            )
                            rows[ingredient_id] = row
                            session.add(row)
                            await session.flush()
                        quantity = sum((item.quantity for item in items), Decimal(0))
                        contribution = ShoppingContribution(
                            id=uuid4(),
                            organization_id=prepared.organization_id,
                            event_id=prepared.event_id,
                            shopping_list_id=prepared.shopping_list_id,
                            shopping_ingredient_row_id=row.id,
                            ingredient_id=ingredient_id,
                            scheduled_recipe_id=source.scheduled_recipe_id,
                        )
                        contributions.append(contribution)
                        session.add(contribution)
                        notes = [item.note for item in items if item.note]
                        snapshots.append(
                            ShoppingContributionSnapshot(
                                id=uuid4(),
                                organization_id=prepared.organization_id,
                                event_id=prepared.event_id,
                                shopping_list_id=prepared.shopping_list_id,
                                generation_revision_id=prepared.generation_revision_id,
                                shopping_contribution_id=contribution.id,
                                ingredient_id=ingredient_id,
                                active_in_revision=True,
                                generated_quantity=quantity,
                                ingredient_version_id=representative.ingredient_version_id,
                                ingredient_name=representative.ingredient_name,
                                source_details={
                                    "recipe_name": source.recipe_name,
                                    "recipe_description": source.recipe_description,
                                    "day": source.calendar_date,
                                    "meal_role": source.meal_role,
                                    "selected_scale_amount": _canonical_decimal(
                                        source.selected_scale_amount
                                    ),
                                    "recipe_base_scaling_amount": _canonical_decimal(
                                        source.recipe_base_scaling_amount
                                    ),
                                    "line_notes": notes,
                                    "line_count": len(items),
                                },
                            )
                        )
                for snapshot in snapshots:
                    session.add(snapshot)
                shopping_list.current_generation_revision_id = prepared.generation_revision_id
                first, last = await _reserve_change_range(
                    session, prepared.organization_id, prepared.mutation_id, 1
                )
                result = CreateShoppingListResult(
                    prepared.mutation_id,
                    prepared.shopping_list_id,
                    prepared.generation_revision_id,
                    prepared.organization_id,
                    prepared.event_id,
                    prepared.name,
                    prepared.scheduled_recipe_ids,
                    tuple(row.id for row in rows.values()),
                    tuple(item.id for item in contributions),
                    first,
                    last,
                    False,
                )
                payload = _payload(result)
                session.add(
                    OrganizationChange(
                        organization_id=prepared.organization_id,
                        sequence=first,
                        mutation_id=prepared.mutation_id,
                        entity_id=prepared.shopping_list_id,
                        entity_kind="shopping_list",
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": payload["shopping_list"]},
                    )
                )
                session.add(
                    _mutation(
                        prepared, context, role, request_hash, "accepted", payload, first, last
                    )
                )
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("Shopping-list creation produced no outcome")
    return result
