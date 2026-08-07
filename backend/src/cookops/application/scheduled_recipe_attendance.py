"""LWW scheduled-recipe attendance commands."""

import hashlib
import json
from dataclasses import dataclass
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
    FieldClock,
    Mutation,
    OrganizationChange,
    ScheduledRecipe,
)

COMMAND_KIND = "scheduled_recipe.attendance"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SetScheduledRecipeAttendanceCommand:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    operation: Literal["set_manual", "follow_event"]
    client_wall_time: datetime
    diner_count: int | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ScheduledRecipeAttendanceResult:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    diner_count: int
    attendance_mode: Literal["manual", "follows_event"]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


def _hash(command: SetScheduledRecipeAttendanceCommand) -> bytes:
    def value(item: object) -> object:
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime) and item.tzinfo and item.utcoffset() is not None:
            return item.astimezone(UTC).isoformat()
        return (
            item
            if item is None or isinstance(item, (str, int))
            else {"invalid": type(item).__name__}
        )

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


def _reject(*violations: FieldViolation) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
    )


async def set_scheduled_recipe_attendance(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetScheduledRecipeAttendanceCommand,
) -> ScheduledRecipeAttendanceResult:
    """Set manual diners or explicitly resume following active event attendance."""
    request_hash = _hash(command)
    violations: list[FieldViolation] = []
    if command.operation not in ("set_manual", "follow_event"):
        violations.append(FieldViolation("operation", "must_be_set_manual_or_follow_event"))
    if command.operation == "set_manual" and (
        not isinstance(command.diner_count, int)
        or isinstance(command.diner_count, bool)
        or command.diner_count < 0
    ):
        violations.append(FieldViolation("diner_count", "must_be_nonnegative_integer"))
    if command.operation == "follow_event" and command.diner_count is not None:
        violations.append(FieldViolation("diner_count", "must_be_null_when_following_event"))
    if (
        not isinstance(command.client_wall_time, datetime)
        or command.client_wall_time.tzinfo is None
        or command.client_wall_time.utcoffset() is None
    ):
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
        if not violations
        or isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    deferred: ApplicationServiceError | None = None
    result: ScheduledRecipeAttendanceResult | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_and_lock_organization(
            session,
            context,
            command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0),
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": _advisory_lock_key(
                    "mutation",
                    command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
                )
            },
        )
        retained = (
            await session.get(Mutation, command.mutation_id)
            if isinstance(command.mutation_id, UUID)
            else None
        )
        if retained:
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
            else:
                record = (retained.outcome_payload or {}).get("scheduled_recipe")
                if (
                    not isinstance(record, dict)
                    or retained.first_change_sequence is None
                    or retained.last_change_sequence is None
                ):
                    raise RuntimeError("invalid retained attendance outcome")
                return ScheduledRecipeAttendanceResult(
                    command.mutation_id,
                    command.scheduled_recipe_id,
                    command.organization_id,
                    command.event_id,
                    record["diner_count"],
                    record["attendance_mode"],
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
                event = await session.get(Event, command.event_id, with_for_update=True)
                clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == command.organization_id,
                        FieldClock.entity_kind == "scheduled_recipe",
                        FieldClock.entity_id == scheduled.id,
                        FieldClock.field_name == "attendance",
                    )
                    .with_for_update(of=FieldClock)
                )
                wins = clock is None or (when, command.mutation_id) > (
                    clock.winning_client_wall_time,
                    clock.winning_mutation_id,
                )
                if wins:
                    scheduled.diner_count = (
                        event.base_expected_attendance
                        if command.operation == "follow_event"
                        else command.diner_count
                    )
                    scheduled.attendance_mode = (
                        "follows_event" if command.operation == "follow_event" else "manual"
                    )
                    if clock is None:
                        session.add(
                            FieldClock(
                                organization_id=command.organization_id,
                                entity_kind="scheduled_recipe",
                                entity_id=scheduled.id,
                                field_name="attendance",
                                winning_client_wall_time=when,
                                winning_mutation_id=command.mutation_id,
                            )
                        )
                    else:
                        clock.winning_client_wall_time, clock.winning_mutation_id = (
                            when,
                            command.mutation_id,
                        )
                outcome: Literal["accepted", "partially_superseded"] = (
                    "accepted" if wins else "partially_superseded"
                )
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                result = ScheduledRecipeAttendanceResult(
                    command.mutation_id,
                    scheduled.id,
                    command.organization_id,
                    command.event_id,
                    scheduled.diner_count,
                    scheduled.attendance_mode,
                    first,
                    last,
                    False,
                    outcome,
                )
                record = _scheduled_recipe_change_record(scheduled)[2]
                record["field_clocks"]["attendance"] = {
                    "winning_client_wall_time": (
                        when if wins else clock.winning_client_wall_time
                    ).isoformat(),
                    "winning_mutation_id": str(
                        command.mutation_id if wins else clock.winning_mutation_id
                    ),
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
                    Mutation(
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
                            {"entity_kind": "scheduled_recipe", "entity_id": str(scheduled.id)}
                        ],
                        request_hash=request_hash,
                        outcome=outcome,
                        outcome_payload={
                            "scheduled_recipe": {
                                "diner_count": scheduled.diner_count,
                                "attendance_mode": scheduled.attendance_mode,
                            },
                            "outcome": outcome,
                        },
                        first_change_sequence=first,
                        last_change_sequence=last,
                    )
                )
        if deferred and not retained:
            session.add(
                Mutation(
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
                    target_identities=[],
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error(deferred),
                )
            )
    if deferred:
        raise deferred
    if result is None:
        raise RuntimeError("Scheduled attendance produced no outcome")
    return result
