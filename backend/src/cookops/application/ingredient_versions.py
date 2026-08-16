"""Publish immutable ingredient catalog versions."""

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import _reserve_change_range
from cookops.application.ingredients import (
    CreateIngredientCommand,
    CreateIngredientResult,
    _authorize,
    _decimal_text,
    _error_payload,
    _prepare_command,
    _references_are_valid,
    _retained_error,
    _validation_error,
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
    Ingredient,
    IngredientVersion,
    IngredientVersionDietaryTag,
    Mutation,
    OrganizationChange,
    UnitDefinition,
)

COMMAND_KIND = "ingredient.publish_version"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PublishIngredientVersionCommand:
    mutation_id: UUID
    ingredient_id: UUID
    based_on_version_id: UUID
    ingredient_version_id: UUID
    organization_id: UUID
    name: str
    canonical_unit_id: UUID
    mass_per_canonical_quantity: Decimal
    client_wall_time: datetime
    dietary_tag_ids: tuple[UUID, ...] = ()
    default_store_section_id: UUID | None = None
    logical_operation_id: UUID | None = None


def _hash(command: PublishIngredientVersionCommand) -> bytes:
    value = {key: getattr(command, key) for key in command.__dataclass_fields__}
    value.update({"command_kind": COMMAND_KIND, "command_schema_version": COMMAND_SCHEMA_VERSION})

    def encode(item: object) -> object:
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime):
            return item.astimezone(UTC).isoformat()
        if isinstance(item, Decimal):
            return _decimal_text(item)
        if isinstance(item, tuple):
            return [encode(x) for x in item]
        return item

    return hashlib.sha256(
        json.dumps(
            {k: encode(v) for k, v in value.items()}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).digest()


async def publish_ingredient_version(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: PublishIngredientVersionCommand,
) -> CreateIngredientResult:
    base = (
        command.based_on_version_id
        if isinstance(command.based_on_version_id, UUID)
        else UUID(int=0)
    )
    prepared = _prepare_command(
        CreateIngredientCommand(
            mutation_id=command.mutation_id,
            ingredient_id=command.ingredient_id,
            ingredient_version_id=command.ingredient_version_id,
            organization_id=command.organization_id,
            name=command.name,
            canonical_unit_id=command.canonical_unit_id,
            mass_per_canonical_quantity=command.mass_per_canonical_quantity,
            client_wall_time=command.client_wall_time,
            dietary_tag_ids=command.dietary_tag_ids,
            default_store_section_id=command.default_store_section_id,
            logical_operation_id=command.logical_operation_id,
        )
    )
    violations = list(prepared.violations)
    if not isinstance(command.based_on_version_id, UUID):
        violations.append(FieldViolation("based_on_version_id", "must_be_uuid"))
    if base == prepared.ingredient_version_id:
        violations.append(
            FieldViolation("based_on_version_id", "must_differ_from_ingredient_version_id")
        )
    request_hash = _hash(command)
    result: CreateIngredientResult | None = None
    deferred: ApplicationServiceError | None = None
    async with session_factory() as session, session.begin():
        role, _currency = await _authorize(session, context, prepared.organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", prepared.mutation_id)},
        )
        retained = await session.get(Mutation, prepared.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "accepted":
                return CreateIngredientResult(
                    prepared.mutation_id,
                    prepared.ingredient_id,
                    prepared.ingredient_version_id,
                    prepared.organization_id,
                    prepared.name,
                    prepared.normalized_name,
                    prepared.canonical_unit_id,
                    prepared.mass_per_canonical_quantity,
                    prepared.dietary_tag_ids,
                    prepared.default_store_section_id,
                    None,
                    retained.first_change_sequence or 0,
                    retained.last_change_sequence or 0,
                    True,
                )
            payload = retained.outcome_payload or {}
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get("code") != "validation_failed":
                raw_code = error.get("code")
                allowed_codes = {
                    "archived_event",
                    "client_time_too_far_ahead",
                    "forbidden",
                    "idempotency_mismatch",
                    "stale_precondition",
                    "validation_failed",
                }
                code = (
                    cast(
                        Literal[
                            "archived_event",
                            "client_time_too_far_ahead",
                            "forbidden",
                            "idempotency_mismatch",
                            "stale_precondition",
                            "validation_failed",
                        ],
                        raw_code,
                    )
                    if isinstance(raw_code, str) and raw_code in allowed_codes
                    else "validation_failed"
                )
                deferred = ApplicationServiceError(
                    code, retry_same_identity=False
                )
            else:
                deferred = _retained_error(retained)
        elif violations:
            deferred = _validation_error(tuple(violations))
        else:
            for kind, identity in (
                ("ingredient", prepared.ingredient_id),
                ("ingredient-version", prepared.ingredient_version_id),
                ("ingredient-version", base),
            ):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": _advisory_lock_key(kind, identity)},
                )
            ingredient = await session.scalar(
                select(Ingredient)
                .where(
                    Ingredient.id == prepared.ingredient_id,
                    Ingredient.organization_id == prepared.organization_id,
                )
                .with_for_update(of=Ingredient)
            )
            current = await session.scalar(
                select(IngredientVersion)
                .where(
                    IngredientVersion.id == base,
                    IngredientVersion.ingredient_id == prepared.ingredient_id,
                    IngredientVersion.organization_id == prepared.organization_id,
                )
                .with_for_update(of=IngredientVersion)
            )
            exists = await session.scalar(
                select(IngredientVersion.id).where(
                    IngredientVersion.id == prepared.ingredient_version_id
                )
            )
            old_tags: set[UUID] = set()
            if current is not None:
                old_tags = set(
                    (
                        await session.execute(
                            select(IngredientVersionDietaryTag.dietary_tag_id).where(
                                IngredientVersionDietaryTag.ingredient_version_id == current.id
                            )
                        )
                    ).scalars()
                )
            new_tags = tuple(tag for tag in prepared.dietary_tag_ids if tag not in old_tags)
            reference_command = replace(prepared, dietary_tag_ids=new_tags)
            errors = list(await _references_are_valid(session, reference_command, _currency))
            if new_tags:
                new_tag_set = set(new_tags)
                active = set(
                    (
                        await session.execute(
                            select(DietaryTag.id).where(
                                DietaryTag.organization_id == prepared.organization_id,
                                DietaryTag.id.in_(new_tag_set),
                                DietaryTag.retired_at.is_(None),
                            )
                        )
                    ).scalars()
                )
                if active != new_tag_set:
                    errors.append(FieldViolation("dietary_tag_ids", "new_tags_must_be_active"))
            if ingredient is None or current is None or ingredient.retired_at is not None:
                deferred = ApplicationServiceError("stale_precondition", retry_same_identity=False)
            elif exists is not None:
                errors.append(FieldViolation("ingredient_version_id", "already_exists"))
            else:
                old_unit = await session.get(
                    UnitDefinition, current.canonical_unit_id, with_for_update=True
                )
                new_unit = await session.get(
                    UnitDefinition, prepared.canonical_unit_id, with_for_update=True
                )
                if old_unit is None or new_unit is None or old_unit.dimension != new_unit.dimension:
                    errors.append(
                        FieldViolation(
                            "canonical_unit_id", "must_match_current_unit_dimension"
                        )
                    )
            if errors and deferred is None:
                deferred = _validation_error(tuple(errors))
            if deferred is None:
                assert ingredient is not None
                pointer_clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == prepared.organization_id,
                        FieldClock.entity_kind == "ingredient",
                        FieldClock.entity_id == prepared.ingredient_id,
                        FieldClock.field_name == "current_version_id",
                    )
                    .with_for_update(of=FieldClock)
                )
                pointer_wins = pointer_clock is None or (
                    prepared.client_wall_time,
                    prepared.mutation_id,
                ) > (
                    pointer_clock.winning_client_wall_time,
                    pointer_clock.winning_mutation_id,
                )
                pointer_version_id = (
                    prepared.ingredient_version_id
                    if pointer_wins
                    else ingredient.current_version_id
                )
                if not pointer_wins:
                    assert pointer_clock is not None
                winning_clock_time = prepared.client_wall_time
                winning_clock_mutation = prepared.mutation_id
                if not pointer_wins:
                    assert pointer_clock is not None
                    winning_clock_time = pointer_clock.winning_client_wall_time
                    winning_clock_mutation = pointer_clock.winning_mutation_id
                records = [
                    (
                        "ingredient",
                        prepared.ingredient_id,
                        {
                            "id": str(prepared.ingredient_id),
                            "organization_id": str(prepared.organization_id),
                            "current_version_id": str(pointer_version_id),
                            "current_price_estimate_id": (
                                str(ingredient.current_price_estimate_id)
                                if ingredient.current_price_estimate_id
                                else None
                            ),
                            "created_at": ingredient.created_at.isoformat(),
                            "retired_at": None,
                            "retired_by_user_id": None,
                            "lifecycle": "active",
                            "created_by_user_id": str(ingredient.created_by_user_id),
                            "field_clocks": {
                                "current_version_id": {
                                    "winning_client_wall_time": winning_clock_time.isoformat(),
                                    "winning_mutation_id": str(winning_clock_mutation),
                                }
                            },
                        },
                    ),
                    (
                        "ingredient_version",
                        prepared.ingredient_version_id,
                        {
                            "id": str(prepared.ingredient_version_id),
                            "ingredient_id": str(prepared.ingredient_id),
                            "organization_id": str(prepared.organization_id),
                            "based_on_version_id": str(base),
                            "name": prepared.name,
                            "normalized_name": prepared.normalized_name,
                            "canonical_unit_id": str(prepared.canonical_unit_id),
                            "mass_per_canonical_quantity": _decimal_text(
                                prepared.mass_per_canonical_quantity
                            ),
                            "default_store_section_id": str(prepared.default_store_section_id)
                            if prepared.default_store_section_id
                            else None,
                            "dietary_tag_ids": [str(x) for x in prepared.dietary_tag_ids],
                            "published_by_user_id": str(context.actor_user_id),
                            "immutable": True,
                        },
                    ),
                ]
                first, last = await _reserve_change_range(
                    session, prepared.organization_id, prepared.mutation_id, len(records)
                )
                result = CreateIngredientResult(
                    prepared.mutation_id,
                    prepared.ingredient_id,
                    prepared.ingredient_version_id,
                    prepared.organization_id,
                    prepared.name,
                    prepared.normalized_name,
                    prepared.canonical_unit_id,
                    prepared.mass_per_canonical_quantity,
                    prepared.dietary_tag_ids,
                    prepared.default_store_section_id,
                    None,
                    first,
                    last,
                    False,
                )
                if pointer_wins:
                    ingredient.current_version_id = prepared.ingredient_version_id
                session.add_all(
                    IngredientVersionDietaryTag(
                        ingredient_version_id=prepared.ingredient_version_id,
                        dietary_tag_id=tag,
                        organization_id=prepared.organization_id,
                    )
                    for tag in prepared.dietary_tag_ids
                )
                await session.flush()
                session.add(
                    IngredientVersion(
                        id=prepared.ingredient_version_id,
                        organization_id=prepared.organization_id,
                        ingredient_id=prepared.ingredient_id,
                        based_on_version_id=base,
                        name=prepared.name,
                        normalized_name=prepared.normalized_name,
                        canonical_unit_id=prepared.canonical_unit_id,
                        mass_per_canonical_quantity=prepared.mass_per_canonical_quantity,
                        default_store_section_id=prepared.default_store_section_id,
                        published_by_user_id=context.actor_user_id,
                    )
                )
                await session.flush()
                mutation = Mutation(
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
                        {"entity_kind": "ingredient", "entity_id": str(prepared.ingredient_id)}
                    ],
                    request_hash=request_hash,
                    outcome="accepted",
                    outcome_payload={
                        "ingredient": {
                            "id": str(prepared.ingredient_id),
                            "organization_id": str(prepared.organization_id),
                            "version_id": str(prepared.ingredient_version_id),
                            "name": prepared.name,
                            "normalized_name": prepared.normalized_name,
                            "canonical_unit_id": str(prepared.canonical_unit_id),
                            "mass_per_canonical_quantity": _decimal_text(
                                prepared.mass_per_canonical_quantity
                            ),
                            "dietary_tag_ids": [str(x) for x in prepared.dietary_tag_ids],
                            "default_store_section_id": str(prepared.default_store_section_id)
                            if prepared.default_store_section_id
                            else None,
                        }
                    },
                    first_change_sequence=first,
                    last_change_sequence=last,
                )
                session.add(mutation)
                await session.flush()
                for kind, entity_id, record in records:
                    session.add(
                        OrganizationChange(
                            organization_id=prepared.organization_id,
                            sequence=first + records.index((kind, entity_id, record)),
                            mutation_id=prepared.mutation_id,
                            entity_id=entity_id,
                            entity_kind=kind,
                            operation="upsert",
                            payload={"record_schema_version": 1, "record": record},
                        )
                    )
                fields = [
                    ("name", prepared.ingredient_version_id),
                    ("canonical_unit_id", prepared.ingredient_version_id),
                    ("mass_per_canonical_quantity", prepared.ingredient_version_id),
                    ("dietary_tag_ids", prepared.ingredient_version_id),
                ]
                if pointer_wins:
                    fields.insert(0, ("current_version_id", prepared.ingredient_id))
                for field, entity_id in fields:
                    session.add(
                        FieldClock(
                            organization_id=prepared.organization_id,
                            entity_kind="ingredient"
                            if entity_id == prepared.ingredient_id
                            else "ingredient_version",
                            entity_id=entity_id,
                            field_name=field,
                            winning_client_wall_time=prepared.client_wall_time,
                            winning_mutation_id=prepared.mutation_id,
                        )
                    )
        if deferred is not None and retained is None:
            session.add(
                Mutation(
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
                        {"entity_kind": "ingredient", "entity_id": str(prepared.ingredient_id)}
                    ],
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error_payload(deferred),
                )
            )
    if deferred is not None:
        raise deferred
    if result is None:
        raise RuntimeError("Ingredient publication produced no outcome")
    return result
