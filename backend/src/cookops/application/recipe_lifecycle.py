"""Retire or restore one recipe catalog root without touching immutable versions."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from cookops.persistence.models import FieldClock, Mutation, OrganizationChange, Recipe

COMMAND_KIND = "recipe.lifecycle"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SetRecipeLifecycleCommand:
    mutation_id: UUID
    recipe_id: UUID
    organization_id: UUID
    operation: Literal["retire", "restore"]
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RecipeLifecycleResult:
    mutation_id: UUID
    recipe_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


def _hash(command: SetRecipeLifecycleCommand) -> bytes:
    def value(item: object) -> object:
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime) and item.tzinfo and item.utcoffset() is not None:
            return item.astimezone(UTC).isoformat()
        return item if item is None or isinstance(item, str) else {"invalid": type(item).__name__}

    return hashlib.sha256(
        json.dumps(
            {key: value(getattr(command, key)) for key in command.__dataclass_fields__}
            | {"command_kind": COMMAND_KIND, "command_schema_version": COMMAND_SCHEMA_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _error(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": item.path, "code": item.code} for item in error.field_violations
            ],
        }
    }


def _record(recipe: Recipe, clocks: list[FieldClock]) -> dict[str, object]:
    return {
        "id": str(recipe.id),
        "organization_id": str(recipe.organization_id),
        "current_version_id": str(recipe.current_version_id) if recipe.current_version_id else None,
        "created_at": recipe.created_at.isoformat(),
        "created_by_user_id": str(recipe.created_by_user_id),
        "retired_at": recipe.retired_at.isoformat() if recipe.retired_at else None,
        "retired_by_user_id": str(recipe.retired_by_user_id) if recipe.retired_by_user_id else None,
        "lifecycle": "retired" if recipe.retired_at else "active",
        "field_clocks": {
            clock.field_name: {
                "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                "winning_mutation_id": str(clock.winning_mutation_id),
            }
            for clock in clocks
        },
    }


def _mutation(
    command: SetRecipeLifecycleCommand,
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
        client_wall_time=(
            command.client_wall_time.astimezone(UTC)
            if isinstance(command.client_wall_time, datetime)
            and command.client_wall_time.tzinfo is not None
            and command.client_wall_time.utcoffset() is not None
            else datetime(1970, 1, 1, tzinfo=UTC)
        ),
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=COMMAND_KIND,
        target_identities=[{"entity_kind": "recipe", "entity_id": str(command.recipe_id)}],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def set_recipe_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetRecipeLifecycleCommand,
) -> RecipeLifecycleResult:
    request_hash = _hash(command)
    violations = [
        FieldViolation(name, "must_be_uuid")
        for name in ("mutation_id", "recipe_id", "organization_id")
        if not isinstance(getattr(command, name), UUID)
    ]
    if command.operation not in ("retire", "restore"):
        violations.append(FieldViolation("operation", "must_be_retire_or_restore"))
    if (
        not isinstance(command.client_wall_time, datetime)
        or command.client_wall_time.tzinfo is None
        or command.client_wall_time.utcoffset() is None
    ):
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    when = (
        command.client_wall_time.astimezone(UTC)
        if not violations
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    organization_id = (
        command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
    )
    mutation_id = command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0)
    deferred: ApplicationServiceError | None = None
    result: RecipeLifecycleResult | None = None
    async with session_factory() as session, session.begin():
        actor_role = await _authorize_and_lock_organization(session, context, organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", mutation_id)},
        )
        retained = await session.get(Mutation, mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "rejected":
                error = (retained.outcome_payload or {}).get("error")
                deferred = ApplicationServiceError(
                    error["code"]
                    if isinstance(error, dict) and isinstance(error.get("code"), str)
                    else "validation_failed",
                    retry_same_identity=False,
                )
            elif (
                retained.first_change_sequence is not None
                and retained.last_change_sequence is not None
            ):
                if retained.outcome not in ("accepted", "partially_superseded"):
                    raise RuntimeError("invalid retained recipe lifecycle outcome")
                return RecipeLifecycleResult(
                    command.mutation_id,
                    command.recipe_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    cast(Literal["accepted", "partially_superseded"], retained.outcome),
                )
            else:
                raise RuntimeError("invalid retained recipe lifecycle outcome")
        elif violations:
            deferred = ApplicationServiceError(
                "validation_failed", field_violations=tuple(violations), retry_same_identity=False
            )
        elif when > datetime.now(UTC) + timedelta(hours=24):
            deferred = ApplicationServiceError(
                "client_time_too_far_ahead", retry_same_identity=False
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("recipe", command.recipe_id)},
            )
            recipe = await session.scalar(
                select(Recipe)
                .where(
                    Recipe.id == command.recipe_id,
                    Recipe.organization_id == command.organization_id,
                )
                .with_for_update(of=Recipe)
            )
            if recipe is None:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(FieldViolation("recipe_id", "must_belong_to_organization"),),
                    retry_same_identity=False,
                )
            else:
                clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == command.organization_id,
                        FieldClock.entity_kind == "recipe",
                        FieldClock.entity_id == recipe.id,
                        FieldClock.field_name == "lifecycle",
                    )
                    .with_for_update(of=FieldClock)
                )
                wins = clock is None or (when, command.mutation_id) > (
                    clock.winning_client_wall_time,
                    clock.winning_mutation_id,
                )
                if wins:
                    recipe.retired_at, recipe.retired_by_user_id = (
                        (datetime.now(UTC), context.actor_user_id)
                        if command.operation == "retire"
                        else (None, None)
                    )
                    if clock is None:
                        clock = FieldClock(
                            organization_id=command.organization_id,
                            entity_kind="recipe",
                            entity_id=recipe.id,
                            field_name="lifecycle",
                            winning_client_wall_time=when,
                            winning_mutation_id=command.mutation_id,
                        )
                        session.add(clock)
                    else:
                        clock.winning_client_wall_time, clock.winning_mutation_id = (
                            when,
                            command.mutation_id,
                        )
                outcome: Literal["accepted", "partially_superseded"] = (
                    "accepted" if wins else "partially_superseded"
                )
                clocks = (
                    await session.scalars(
                        select(FieldClock).where(
                            FieldClock.organization_id == command.organization_id,
                            FieldClock.entity_kind == "recipe",
                            FieldClock.entity_id == recipe.id,
                        )
                    )
                ).all()
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                record = _record(recipe, list(clocks))
                result = RecipeLifecycleResult(
                    command.mutation_id, recipe.id, first, last, False, outcome
                )
                session.add(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first,
                        mutation_id=command.mutation_id,
                        entity_id=recipe.id,
                        entity_kind="recipe",
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": record},
                    )
                )
                session.add(
                    _mutation(
                        command,
                        context,
                        actor_role,
                        request_hash,
                        outcome,
                        {"recipe": record, "outcome": outcome},
                        first,
                        last,
                    )
                )
        if deferred is not None and retained is None:
            session.add(
                _mutation(command, context, actor_role, request_hash, "rejected", _error(deferred))
            )
    if deferred is not None:
        raise deferred
    if result is None:
        raise RuntimeError("Recipe lifecycle produced no outcome")
    return result
