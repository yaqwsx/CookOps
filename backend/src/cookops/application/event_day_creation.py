"""Create one manually added event day."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.event_day_visibility import _record
from cookops.application.events import _reserve_change_range
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.application.recipes import _authorize_and_lock_organization
from cookops.persistence.models import Event, EventDay, FieldClock, Mutation, OrganizationChange

COMMAND_KIND = "event_day.create"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CreateEventDayCommand:
    mutation_id: UUID
    event_day_id: UUID
    organization_id: UUID
    event_id: UUID
    calendar_date: date
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventDayCreationResult:
    mutation_id: UUID
    event_day_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: str = "accepted"


def _hash(command: CreateEventDayCommand) -> bytes:
    def value(item: object) -> object:
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime) and item.tzinfo and item.utcoffset() is not None:
            return item.astimezone(UTC).isoformat()
        if isinstance(item, date) and not isinstance(item, datetime):
            return item.isoformat()
        return item if item is None else {"invalid": type(item).__name__}

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


async def create_event_day(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateEventDayCommand,
) -> EventDayCreationResult:
    """Create an active, visible manually-added day once for its mutation identity."""
    request_hash = _hash(command)
    violations = [
        FieldViolation(name, "must_be_uuid")
        for name in ("mutation_id", "event_day_id", "organization_id", "event_id")
        if not isinstance(getattr(command, name), UUID)
    ]
    if not isinstance(command.calendar_date, date) or isinstance(command.calendar_date, datetime):
        violations.append(FieldViolation("calendar_date", "must_be_calendar_date"))
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
    deferred: ApplicationServiceError | None = None
    result: EventDayCreationResult | None = None
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
                return EventDayCreationResult(
                    command.mutation_id,
                    command.event_day_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                )
            else:
                raise RuntimeError("invalid retained event day creation outcome")
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
                {"key": _advisory_lock_key("event", command.event_id)},
            )
            event = await session.scalar(
                select(Event)
                .where(
                    Event.id == command.event_id,
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update()
            )
            duplicate = await session.scalar(
                select(EventDay.id).where(
                    EventDay.event_id == command.event_id,
                    EventDay.calendar_date == command.calendar_date,
                    EventDay.retired_at.is_(None),
                )
            )
            existing = await session.get(EventDay, command.event_day_id)
            if event is None or existing is not None or duplicate is not None:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation(
                            "event_day_id" if existing else "calendar_date",
                            "already_exists"
                            if existing or duplicate
                            else "must_belong_to_active_event",
                        ),
                    ),
                    retry_same_identity=False,
                )
            else:
                day = EventDay(
                    id=command.event_day_id,
                    event_id=command.event_id,
                    calendar_date=command.calendar_date,
                    note=None,
                    is_visible=True,
                    provenance="manually_added",
                    created_by_user_id=context.actor_user_id,
                )
                clock = FieldClock(
                    organization_id=command.organization_id,
                    entity_kind="event_day",
                    entity_id=day.id,
                    field_name="is_visible",
                    winning_client_wall_time=when,
                    winning_mutation_id=command.mutation_id,
                )
                session.add_all((day, clock))
                await session.flush()
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                session.add(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first,
                        mutation_id=command.mutation_id,
                        entity_id=day.id,
                        entity_kind="event_day",
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": _record(day, clock)},
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
                        target_identities=[{"entity_kind": "event_day", "entity_id": str(day.id)}],
                        request_hash=request_hash,
                        outcome="accepted",
                        outcome_payload={
                            "event_day": {"calendar_date": day.calendar_date.isoformat()},
                            "outcome": "accepted",
                        },
                        first_change_sequence=first,
                        last_change_sequence=last,
                    )
                )
                result = EventDayCreationResult(command.mutation_id, day.id, first, last, False)
        if deferred is not None and not retained:
            session.add(
                Mutation(
                    id=mutation_id,
                    logical_operation_id=command.logical_operation_id,
                    organization_id=organization_id,
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
        raise RuntimeError("Event day creation produced no outcome")
    return result
