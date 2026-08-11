"""Retire or restore an event day with one LWW lifecycle field."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.event_day_visibility import _error, _record
from cookops.application.events import _reserve_change_range
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.application.recipes import _authorize_and_lock_organization
from cookops.persistence.models import Event, EventDay, FieldClock, Mutation, OrganizationChange

COMMAND_KIND = "event_day.lifecycle"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SetEventDayLifecycleCommand:
    mutation_id: UUID
    event_day_id: UUID
    organization_id: UUID
    event_id: UUID
    operation: Literal["retire", "restore"]
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventDayLifecycleResult:
    mutation_id: UUID
    event_day_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


def _hash(command: SetEventDayLifecycleCommand) -> bytes:
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


def _mutation(
    command: SetEventDayLifecycleCommand,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "partially_superseded", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    client_time = (
        command.client_wall_time.astimezone(UTC)
        if isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo
        and command.client_wall_time.utcoffset() is not None
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
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
        client_wall_time=client_time,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=COMMAND_KIND,
        target_identities=[
            {"entity_kind": "event", "entity_id": str(command.event_id)},
            {"entity_kind": "event_day", "entity_id": str(command.event_day_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def set_event_day_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetEventDayLifecycleCommand,
) -> EventDayLifecycleResult:
    request_hash = _hash(command)
    violations = [
        FieldViolation(name, "must_be_uuid")
        for name in ("mutation_id", "event_day_id", "organization_id", "event_id")
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
    mutation_id = command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0)
    organization_id = (
        command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
    )
    deferred: ApplicationServiceError | None = None
    result: EventDayLifecycleResult | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_and_lock_organization(session, context, organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", mutation_id)},
        )
        retained = await session.get(Mutation, mutation_id)
        if retained:
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
                    else ApplicationServiceError("validation_failed", retry_same_identity=False)
                )
            elif (
                retained.first_change_sequence is not None
                and retained.last_change_sequence is not None
            ):
                return EventDayLifecycleResult(
                    command.mutation_id,
                    command.event_day_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    cast(Literal["accepted", "partially_superseded"], retained.outcome),
                )
            else:
                raise RuntimeError("invalid retained event day lifecycle outcome")
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
                {"key": _advisory_lock_key("event_day", command.event_day_id)},
            )
            day = await session.scalar(
                select(EventDay)
                .join(Event)
                .where(
                    EventDay.id == command.event_day_id,
                    EventDay.event_id == command.event_id,
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(EventDay, Event))
            )
            if day is None:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation("event_day_id", "must_belong_to_active_event"),
                    ),
                    retry_same_identity=False,
                )
            else:
                clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == command.organization_id,
                        FieldClock.entity_kind == "event_day",
                        FieldClock.entity_id == day.id,
                        FieldClock.field_name == "lifecycle",
                    )
                    .with_for_update(of=FieldClock)
                )
                wins = clock is None or (when, command.mutation_id) > (
                    clock.winning_client_wall_time,
                    clock.winning_mutation_id,
                )
                if wins:
                    day.retired_at, day.retired_by_user_id = (
                        (datetime.now(UTC), context.actor_user_id)
                        if command.operation == "retire"
                        else (None, None)
                    )
                    if clock is None:
                        clock = FieldClock(
                            organization_id=command.organization_id,
                            entity_kind="event_day",
                            entity_id=day.id,
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
                assert clock is not None
                outcome: Literal["accepted", "partially_superseded"] = (
                    "accepted" if wins else "partially_superseded"
                )
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                clocks = list(
                    (
                        await session.execute(
                            select(FieldClock).where(
                                FieldClock.organization_id == command.organization_id,
                                FieldClock.entity_kind == "event_day",
                                FieldClock.entity_id == day.id,
                            )
                        )
                    ).scalars()
                )
                record = _record(day, *clocks)
                session.add(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first,
                        mutation_id=command.mutation_id,
                        entity_id=day.id,
                        entity_kind="event_day",
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
                        {"event_day": record, "outcome": outcome},
                        first,
                        last,
                    )
                )
                result = EventDayLifecycleResult(
                    command.mutation_id, day.id, first, last, False, outcome
                )
        if deferred is not None and retained is None:
            session.add(
                _mutation(command, context, role, request_hash, "rejected", _error(deferred))
            )
    if deferred:
        raise deferred
    if result is None:
        raise RuntimeError("Event day lifecycle produced no outcome")
    return result
