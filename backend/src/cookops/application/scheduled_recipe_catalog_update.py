"""Explicitly update a scheduled recipe to a published catalog version."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
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
from cookops.application.scheduled_recipe_overrides import _record as _override_record
from cookops.persistence.models import (
    Event,
    FieldClock,
    Mutation,
    OrganizationChange,
    RecipeVersion,
    RecipeVersionIngredientLine,
    ScheduledIngredientOverride,
    ScheduledRecipe,
    UnitDefinition,
)

COMMAND_KIND = "scheduled_recipe.catalog_update"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class UpdateScheduledRecipeCatalogCommand:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    expected_recipe_version_id: UUID
    target_recipe_version_id: UUID
    preserve_overrides: bool
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class UpdateScheduledRecipeCatalogResult:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


def _hash(command: UpdateScheduledRecipeCatalogCommand) -> bytes:
    def value(item: object) -> object:
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime) and item.tzinfo and item.utcoffset() is not None:
            return item.astimezone(UTC).isoformat()
        return (
            item
            if item is None or isinstance(item, (str, bool))
            else {"invalid": type(item).__name__}
        )

    payload = {key: value(getattr(command, key)) for key in command.__dataclass_fields__}
    payload.update(command_kind=COMMAND_KIND, command_schema_version=COMMAND_SCHEMA_VERSION)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _error(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [{"path": v.path, "code": v.code} for v in error.field_violations],
        }
    }


def _mutation(
    command: UpdateScheduledRecipeCatalogCommand,
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
            {"entity_kind": "event", "entity_id": str(command.event_id)},
            {"entity_kind": "scheduled_recipe", "entity_id": str(command.scheduled_recipe_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def update_scheduled_recipe_catalog(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: UpdateScheduledRecipeCatalogCommand,
) -> UpdateScheduledRecipeCatalogResult:
    request_hash = _hash(command)
    violations = [
        FieldViolation(name, "must_be_uuid")
        for name in (
            "mutation_id",
            "scheduled_recipe_id",
            "organization_id",
            "event_id",
            "expected_recipe_version_id",
            "target_recipe_version_id",
        )
        if not isinstance(getattr(command, name), UUID)
    ]
    if not isinstance(command.preserve_overrides, bool):
        violations.append(FieldViolation("preserve_overrides", "must_be_boolean"))
    if (
        not isinstance(command.client_wall_time, datetime)
        or command.client_wall_time.tzinfo is None
        or command.client_wall_time.utcoffset() is None
    ):
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    when = (
        command.client_wall_time.astimezone(UTC)
        if not violations
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    deferred = None
    async with session_factory() as session, session.begin():
        organization_id = (
            command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
        )
        role = await _authorize_and_lock_organization(session, context, organization_id)
        mutation_id = command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", mutation_id)},
        )
        retained = await session.get(Mutation, mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "rejected":
                payload = retained.outcome_payload or {}
                error = payload.get("error")
                deferred = (
                    ApplicationServiceError(error["code"], retry_same_identity=False)
                    if isinstance(error, dict) and isinstance(error.get("code"), str)
                    else ApplicationServiceError("validation_failed", retry_same_identity=False)
                )
            elif (
                retained.first_change_sequence is not None
                and retained.last_change_sequence is not None
            ):
                return UpdateScheduledRecipeCatalogResult(
                    command.mutation_id,
                    command.scheduled_recipe_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                )
            else:
                raise RuntimeError("invalid retained catalog update outcome")
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
                {"key": _advisory_lock_key("scheduled_recipe", command.scheduled_recipe_id)},
            )
            scheduled = await session.scalar(
                select(ScheduledRecipe)
                .join(Event)
                .where(
                    ScheduledRecipe.id == command.scheduled_recipe_id,
                    ScheduledRecipe.organization_id == organization_id,
                    ScheduledRecipe.event_id == command.event_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ScheduledRecipe, Event))
            )
            target = (
                await session.scalar(
                    select(RecipeVersion).where(
                        RecipeVersion.id == command.target_recipe_version_id,
                        RecipeVersion.organization_id == organization_id,
                        RecipeVersion.recipe_id == scheduled.recipe_id,
                    )
                )
                if scheduled
                else None
            )
            if scheduled is None:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation("scheduled_recipe_id", "must_belong_to_active_event"),
                    ),
                    retry_same_identity=False,
                )
            elif (
                scheduled.recipe_version_id != command.expected_recipe_version_id
                or target is None
                or target.published_at is None
            ):
                deferred = ApplicationServiceError("stale_precondition", retry_same_identity=False)
            else:
                overrides = (
                    await session.scalars(
                        select(ScheduledIngredientOverride)
                        .where(
                            ScheduledIngredientOverride.scheduled_recipe_id == scheduled.id,
                            ScheduledIngredientOverride.retired_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).all()
                new_lines = {
                    line.line_key: line
                    for line in (
                        await session.scalars(
                            select(RecipeVersionIngredientLine).where(
                                RecipeVersionIngredientLine.recipe_version_id == target.id
                            )
                        )
                    ).all()
                }
                if command.preserve_overrides:
                    old_lines = {
                        line.line_key: line
                        for line in (
                            await session.scalars(
                                select(RecipeVersionIngredientLine).where(
                                    RecipeVersionIngredientLine.recipe_version_id
                                    == scheduled.recipe_version_id
                                )
                            )
                        ).all()
                    }
                    incompatible = any(
                        o.override_kind == "replace"
                        and (
                            o.target_line_key not in new_lines
                            or new_lines[o.target_line_key].ingredient_version_id
                            != o.ingredient_version_id
                        )
                        for o in overrides
                    )
                    if incompatible:
                        for override in overrides:
                            if override.override_kind == "replace" and (
                                override.target_line_key not in new_lines
                                or new_lines[override.target_line_key].ingredient_version_id
                                != override.ingredient_version_id
                            ):
                                old_line = old_lines.get(override.target_line_key)
                                override.override_kind = "add"
                                override.target_line_key = None
                                override.include_in_portion_weight = getattr(
                                    old_line, "include_in_portion_weight", True
                                )
                if deferred is None:
                    old_version = await session.get(RecipeVersion, scheduled.recipe_version_id)
                    scheduled.recipe_version_id = target.id
                    if (
                        old_version is not None
                        and old_version.scaling_unit_id != target.scaling_unit_id
                    ):
                        unit_code = await session.scalar(
                            select(UnitDefinition.code).where(
                                UnitDefinition.id == target.scaling_unit_id
                            )
                        )
                        capacity = (
                            Decimal(1)
                            if unit_code == "person"
                            else target.estimated_diners_per_scaling_unit
                        )
                        if capacity is not None and capacity > 0:
                            suggestion = (
                                Decimal(scheduled.diner_count)
                                * scheduled.consumption_percentage
                                / Decimal(100)
                                / capacity
                            )
                            if target.round_suggestions_up:
                                suggestion = suggestion.to_integral_value(rounding=ROUND_CEILING)
                        else:
                            suggestion = target.base_scaling_amount
                        scheduled.selected_scale_amount = suggestion
                        scheduled.scale_mode = "suggested"
                        for field_name in ("selected_scale_amount", "scale_mode"):
                            scale_clock = await session.scalar(select(FieldClock).where(
                                FieldClock.organization_id == organization_id,
                                FieldClock.entity_kind == "scheduled_recipe",
                                FieldClock.entity_id == scheduled.id,
                                FieldClock.field_name == field_name,
                            ).with_for_update())
                            if scale_clock is None:
                                session.add(FieldClock(
                                    organization_id=organization_id,
                                    entity_kind="scheduled_recipe",
                                    entity_id=scheduled.id,
                                    field_name=field_name,
                                    winning_client_wall_time=when,
                                    winning_mutation_id=command.mutation_id,
                                ))
                            else:
                                scale_clock.winning_client_wall_time = when
                                scale_clock.winning_mutation_id = command.mutation_id
                    version_clock = await session.scalar(
                        select(FieldClock)
                        .where(
                            FieldClock.organization_id == organization_id,
                            FieldClock.entity_kind == "scheduled_recipe",
                            FieldClock.entity_id == scheduled.id,
                            FieldClock.field_name == "recipe_version_id",
                        )
                        .with_for_update()
                    )
                    if version_clock is None:
                        session.add(
                            FieldClock(
                                organization_id=organization_id,
                                entity_kind="scheduled_recipe",
                                entity_id=scheduled.id,
                                field_name="recipe_version_id",
                                winning_client_wall_time=when,
                                winning_mutation_id=command.mutation_id,
                            )
                        )
                    else:
                        version_clock.winning_client_wall_time = when
                        version_clock.winning_mutation_id = command.mutation_id
                    if not command.preserve_overrides:
                        now = datetime.now(UTC)
                        for override in overrides:
                            override.retired_at, override.retired_by_user_id = (
                                now,
                                context.actor_user_id,
                            )
                    first, last = await _reserve_change_range(
                        session, organization_id, command.mutation_id, 1 + len(overrides)
                    )
                    record = _scheduled_recipe_change_record(scheduled)[2]
                    clocks = {
                        clock.field_name: {
                            "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                            "winning_mutation_id": str(clock.winning_mutation_id),
                        }
                        for clock in (await session.scalars(select(FieldClock).where(
                            FieldClock.organization_id == organization_id,
                            FieldClock.entity_kind == "scheduled_recipe",
                            FieldClock.entity_id == scheduled.id,
                        ))).all()
                    }
                    existing_clocks = record.get("field_clocks")
                    if isinstance(existing_clocks, dict):
                        existing_clocks.update(clocks)
                        clocks = existing_clocks
                    record["field_clocks"] = clocks
                    if isinstance(record["field_clocks"], dict):
                        record["field_clocks"]["recipe_version_id"] = {
                            "winning_client_wall_time": when.isoformat(),
                            "winning_mutation_id": str(command.mutation_id),
                        }
                    session.add(
                        OrganizationChange(
                            organization_id=organization_id,
                            sequence=first,
                            mutation_id=command.mutation_id,
                            entity_id=scheduled.id,
                            entity_kind="scheduled_recipe",
                            operation="upsert",
                            payload={"record_schema_version": 1, "record": record},
                        )
                    )
                    for index, override in enumerate(overrides, first + 1):
                        override_clocks = (
                            await session.scalars(
                                select(FieldClock)
                                .where(
                                    FieldClock.organization_id == organization_id,
                                    FieldClock.entity_kind == "scheduled_ingredient_override",
                                    FieldClock.entity_id == override.id,
                                )
                                .with_for_update()
                            )
                        ).all()
                        clocks = {
                            clock.field_name: {
                                "winning_client_wall_time": (
                                    clock.winning_client_wall_time.isoformat()
                                ),
                                "winning_mutation_id": str(clock.winning_mutation_id),
                            }
                            for clock in override_clocks
                        }
                        override_clock = next(
                            (
                                clock
                                for clock in override_clocks
                                if clock.field_name == "catalog_update"
                            ),
                            None,
                        )
                        if override_clock is None:
                            override_clock = FieldClock(
                                organization_id=organization_id,
                                entity_kind="scheduled_ingredient_override",
                                entity_id=override.id,
                                field_name="catalog_update",
                                winning_client_wall_time=when,
                                winning_mutation_id=command.mutation_id,
                            )
                            session.add(override_clock)
                        else:
                            override_clock.winning_client_wall_time = when
                            override_clock.winning_mutation_id = command.mutation_id
                        clocks["catalog_update"] = {
                            "winning_client_wall_time": when.isoformat(),
                            "winning_mutation_id": str(command.mutation_id),
                        }
                        override_record = _override_record(override)
                        override_record["field_clocks"] = clocks
                        session.add(
                            OrganizationChange(
                                organization_id=organization_id,
                                sequence=index,
                                mutation_id=command.mutation_id,
                                entity_id=override.id,
                                entity_kind="scheduled_ingredient_override",
                                operation="upsert",
                                payload={"record_schema_version": 1, "record": override_record},
                            )
                        )
                    session.add(
                        _mutation(
                            command,
                            context,
                            role,
                            request_hash,
                            "accepted",
                            {"outcome": "accepted"},
                            first,
                            last,
                        )
                    )
                    return UpdateScheduledRecipeCatalogResult(
                        command.mutation_id, scheduled.id, first, last, False
                    )
        if deferred is not None and retained is None:
            session.add(
                _mutation(command, context, role, request_hash, "rejected", _error(deferred))
            )
    if deferred is not None:
        raise deferred
    raise RuntimeError("catalog update produced no outcome")
