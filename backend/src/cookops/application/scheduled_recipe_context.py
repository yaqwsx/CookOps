"""Atomic LWW scaling-context changes for scheduled recipes."""
# ruff: noqa: E501

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
from cookops.persistence.models import (
    Event,
    FieldClock,
    Mutation,
    OrganizationChange,
    RecipeVersion,
    ScheduledRecipe,
    UnitDefinition,
)

COMMAND_KIND = "scheduled_recipe.context"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SetScheduledRecipeContextCommand:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    consumption_percentage: Decimal
    operation: Literal["set_manual", "use_suggestion"]
    client_wall_time: datetime
    selected_scale_amount: Decimal | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ScheduledRecipeContextResult:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    consumption_percentage: Decimal
    selected_scale_amount: Decimal
    scale_mode: Literal["manual", "suggested"]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


def _decimal(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _hash(command: SetScheduledRecipeContextCommand) -> bytes:
    def value(item: object) -> object:
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime) and item.tzinfo and item.utcoffset() is not None:
            return item.astimezone(UTC).isoformat()
        if isinstance(item, Decimal) and item.is_finite():
            return _decimal(item)
        return item if item is None or isinstance(item, str) else {"invalid": type(item).__name__}

    request = {name: value(getattr(command, name)) for name in command.__dataclass_fields__}
    request |= {"command_kind": COMMAND_KIND, "command_schema_version": COMMAND_SCHEMA_VERSION}
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
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


def _reject(*violations: FieldViolation) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
    )


def _result(
    command: SetScheduledRecipeContextCommand,
    scheduled: ScheduledRecipe,
    first: int,
    last: int,
    replayed: bool,
    outcome: Literal["accepted", "partially_superseded"],
) -> ScheduledRecipeContextResult:
    return ScheduledRecipeContextResult(
        command.mutation_id,
        scheduled.id,
        command.organization_id,
        command.event_id,
        scheduled.consumption_percentage,
        scheduled.selected_scale_amount,
        scheduled.scale_mode,
        first,
        last,
        replayed,
        outcome,
    )


def _mutation(
    command: SetScheduledRecipeContextCommand,
    context: ExecutionContext,
    role: str,
    when: datetime,
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
        client_wall_time=when,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=COMMAND_KIND,
        target_identities=[
            {"entity_kind": "scheduled_recipe", "entity_id": str(command.scheduled_recipe_id)}
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def _suggestion(
    session: AsyncSession, scheduled: ScheduledRecipe, consumption: Decimal
) -> Decimal:
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
                RecipeVersion.id == scheduled.recipe_version_id,
                RecipeVersion.organization_id == scheduled.organization_id,
            )
        )
    ).one_or_none()
    if version is None:
        raise RuntimeError("scheduled recipe has no recipe version")
    base, capacity, round_up, unit = version
    capacity = Decimal(1) if unit == "person" else capacity
    if capacity is None or capacity <= 0:
        return base
    value = Decimal(scheduled.diner_count) * consumption / Decimal(100) / capacity
    return value.to_integral_value(rounding=ROUND_CEILING) if round_up else value


async def set_scheduled_recipe_context(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetScheduledRecipeContextCommand,
) -> ScheduledRecipeContextResult:
    """Store manual scale or recompute suggestion, with one context field clock."""
    request_hash = _hash(command)
    violations: list[FieldViolation] = []
    if command.operation not in ("set_manual", "use_suggestion"):
        violations.append(FieldViolation("operation", "must_be_set_manual_or_use_suggestion"))
    consumption = command.consumption_percentage
    if not isinstance(consumption, Decimal) or not consumption.is_finite() or consumption < 0:
        violations.append(
            FieldViolation("consumption_percentage", "must_be_nonnegative_finite_decimal")
        )
    amount = command.selected_scale_amount
    if command.operation == "set_manual" and (
        not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0
    ):
        violations.append(
            FieldViolation("selected_scale_amount", "must_be_nonnegative_finite_decimal")
        )
    if command.operation == "use_suggestion" and amount is not None:
        violations.append(
            FieldViolation("selected_scale_amount", "must_be_null_when_using_suggestion")
        )
    has_timezone = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not has_timezone:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    for name in ("mutation_id", "scheduled_recipe_id", "organization_id", "event_id"):
        if not isinstance(getattr(command, name), UUID):
            violations.append(FieldViolation(name, "must_be_uuid"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    when = (
        command.client_wall_time.astimezone(UTC)
        if has_timezone
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    organization_id = (
        command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
    )
    mutation_id = command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0)
    deferred: ApplicationServiceError | None = None
    result: ScheduledRecipeContextResult | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_and_lock_organization(session, context, organization_id)
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
                error = (retained.outcome_payload or {}).get("error")
                deferred = (
                    ApplicationServiceError(error["code"], retry_same_identity=False)
                    if isinstance(error, dict) and isinstance(error.get("code"), str)
                    else _reject()
                )
            else:
                payload = (retained.outcome_payload or {}).get("scheduled_recipe")
                if (
                    not isinstance(payload, dict)
                    or retained.first_change_sequence is None
                    or retained.last_change_sequence is None
                ):
                    raise RuntimeError("invalid retained context outcome")
                return ScheduledRecipeContextResult(
                    command.mutation_id,
                    command.scheduled_recipe_id,
                    command.organization_id,
                    command.event_id,
                    Decimal(str(payload["consumption_percentage"])),
                    Decimal(str(payload["selected_scale_amount"])),
                    payload["scale_mode"],
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    retained.outcome,
                )
        elif violations:
            deferred = _reject(*violations)
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
                    ScheduledRecipe.organization_id == command.organization_id,
                    ScheduledRecipe.event_id == command.event_id,
                    ScheduledRecipe.retired_at.is_(None),
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ScheduledRecipe, Event))
            )
            if scheduled is None:
                deferred = _reject(
                    FieldViolation(
                        "scheduled_recipe_id", "must_be_active_and_belong_to_active_event"
                    )
                )
            else:
                clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == command.organization_id,
                        FieldClock.entity_kind == "scheduled_recipe",
                        FieldClock.entity_id == scheduled.id,
                        FieldClock.field_name == "context",
                    )
                    .with_for_update(of=FieldClock)
                )
                wins = clock is None or (when, command.mutation_id) > (
                    clock.winning_client_wall_time,
                    clock.winning_mutation_id,
                )
                if wins:
                    selected = (
                        amount
                        if command.operation == "set_manual"
                        else await _suggestion(session, scheduled, command.consumption_percentage)
                    )
                    assert selected is not None
                    scheduled.consumption_percentage = command.consumption_percentage
                    scheduled.selected_scale_amount = selected
                    scheduled.scale_mode = (
                        "manual" if command.operation == "set_manual" else "suggested"
                    )
                    if clock is None:
                        clock = FieldClock(
                            organization_id=command.organization_id,
                            entity_kind="scheduled_recipe",
                            entity_id=scheduled.id,
                            field_name="context",
                            winning_client_wall_time=when,
                            winning_mutation_id=command.mutation_id,
                        )
                        session.add(clock)
                    else:
                        clock.winning_client_wall_time, clock.winning_mutation_id = (
                            when,
                            command.mutation_id,
                        )
                outcome = "accepted" if wins else "partially_superseded"
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                result = _result(command, scheduled, first, last, False, outcome)
                record = _scheduled_recipe_change_record(scheduled)[2]
                record["field_clocks"]["context"] = {
                    "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                    "winning_mutation_id": str(clock.winning_mutation_id),
                }
                session.add(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first,
                        mutation_id=command.mutation_id,
                        entity_id=scheduled.id,
                        entity_kind="scheduled_recipe",
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": record},
                    )
                )
                session.add(
                    _mutation(
                        command,
                        context,
                        role,
                        when,
                        request_hash,
                        outcome,
                        {
                            "scheduled_recipe": {
                                "consumption_percentage": _decimal(
                                    scheduled.consumption_percentage
                                ),
                                "selected_scale_amount": _decimal(scheduled.selected_scale_amount),
                                "scale_mode": scheduled.scale_mode,
                            }
                        },
                        first,
                        last,
                    )
                )
        if deferred is not None and retained is None:
            session.add(
                _mutation(command, context, role, when, request_hash, "rejected", _error(deferred))
            )
    if deferred is not None:
        raise deferred
    if result is None:
        raise RuntimeError("Scheduled context produced no outcome")
    return result
