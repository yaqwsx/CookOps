"""Set one event day's note with field-level last-write-wins."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

COMMAND_KIND = "event_day.note"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SetEventDayNoteCommand:
    mutation_id: UUID
    event_day_id: UUID
    organization_id: UUID
    event_id: UUID
    note: str | None
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventDayNoteResult:
    mutation_id: UUID
    event_day_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: str = "accepted"


def _hash(command: SetEventDayNoteCommand, note: str | None) -> bytes:
    def value(item: object) -> object:
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime) and item.tzinfo and item.utcoffset() is not None:
            return item.astimezone(UTC).isoformat()
        return item if item is None or isinstance(item, str) else {"invalid": type(item).__name__}

    return hashlib.sha256(
        json.dumps(
            {
                key: value(note if key == "note" else getattr(command, key))
                for key in command.__dataclass_fields__
            }
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


async def set_event_day_note(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetEventDayNoteCommand,
) -> EventDayNoteResult:
    """Set an active event day's normalized Markdown note once per mutation."""
    violations = [
        FieldViolation(name, "must_be_uuid")
        for name in ("mutation_id", "event_day_id", "organization_id", "event_id")
        if not isinstance(getattr(command, name), UUID)
    ]
    note = (
        unicodedata.normalize("NFC", command.note).replace("\r\n", "\n").replace("\r", "\n")
        if isinstance(command.note, str)
        else None
    )
    if command.note is not None and not isinstance(command.note, str):
        violations.append(FieldViolation("note", "must_be_string_or_null"))
    if note is not None and len(note) > 4000:
        violations.append(FieldViolation("note", "must_be_at_most_4000_characters"))
    if note is not None and "\0" in note:
        violations.append(FieldViolation("note", "must_not_contain_nul"))
    if note is not None:
        try:
            note.encode("utf-8")
        except UnicodeEncodeError:
            violations.append(FieldViolation("note", "must_be_utf8"))
    request_hash = _hash(command, note)
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
    result: EventDayNoteResult | None = None
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
                return EventDayNoteResult(
                    command.mutation_id,
                    command.event_day_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    retained.outcome,
                )
            else:
                raise RuntimeError("invalid retained event day note outcome")
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
                    EventDay.retired_at.is_(None),
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
                        FieldClock.field_name == "note",
                    )
                    .with_for_update(of=FieldClock)
                )
                wins = clock is None or (when, command.mutation_id) > (
                    clock.winning_client_wall_time,
                    clock.winning_mutation_id,
                )
                if wins:
                    day.note = note
                    if clock is None:
                        clock = FieldClock(
                            organization_id=command.organization_id,
                            entity_kind="event_day",
                            entity_id=day.id,
                            field_name="note",
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
                outcome = "accepted" if wins else "partially_superseded"
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
                        outcome=outcome,
                        outcome_payload={"event_day": {"note": day.note}, "outcome": outcome},
                        first_change_sequence=first,
                        last_change_sequence=last,
                    )
                )
                result = EventDayNoteResult(
                    command.mutation_id, day.id, first, last, False, outcome
                )
        if deferred is not None and retained is None:
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
                    target_identities=[
                        {
                            "entity_kind": "event_day",
                            "entity_id": str(
                                command.event_day_id
                                if isinstance(command.event_day_id, UUID)
                                else mutation_id
                            ),
                        }
                    ],
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error(deferred),
                )
            )
    if deferred is not None:
        raise deferred
    if result is None:
        raise RuntimeError("Event day note produced no outcome")
    return result
