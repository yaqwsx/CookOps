"""Update editable event metadata through independent field clocks."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import (
    MAX_EVENT_DAY_COUNT,
    _authorize_and_lock_organization,
    _canonical_decimal_string,
    _event_day_change_record,
    _reserve_change_range,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import Event, EventDay, FieldClock, Mutation, OrganizationChange

COMMAND_KIND = "event.metadata"
COMMAND_SCHEMA_VERSION = 1
_FIELDS = ("name", "location", "budget_amount", "general_note", "start_date", "end_date")
MAX_DECIMAL_LITERAL_LENGTH = 100


def is_bounded_decimal_string(value: object) -> bool:
    """Accept plain decimal text whose expanded form remains bounded."""
    if not isinstance(value, str) or len(value) > MAX_DECIMAL_LITERAL_LENGTH:
        return False
    if not value or value[0] == "0" and len(value) > 1 and not value.startswith("0."):
        return False
    if not value.isascii() or value.count(".") > 1:
        return False
    integer, _, fraction = value.partition(".")
    return integer.isdecimal() and ("." not in value or bool(fraction and fraction.isdecimal()))


def _bounded_decimal(value: object) -> bool:
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    _, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        return False
    expanded_length = (
        len(digits) + exponent if exponent >= 0 else max(0, len(digits) + exponent) + -exponent
    )
    return expanded_length <= MAX_DECIMAL_LITERAL_LENGTH


@dataclass(frozen=True, slots=True)
class UpdateEventMetadataCommand:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    name: str
    location: str | None
    budget_amount: Decimal
    general_note: str | None
    client_wall_time: datetime
    start_date: date | None = None
    end_date: date | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventMetadataResult:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: str = "accepted"


def _text(value: object, *, note: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value)
    if note:
        return value.replace("\r\n", "\n").replace("\r", "\n")
    return value.strip()


def _hash_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _canonical_decimal_string(value) if _bounded_decimal(value) else str(value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"invalid": type(value).__name__}


def _request_hash(
    command: UpdateEventMetadataCommand,
    name: str | None,
    location: str | None,
    note: str | None,
) -> bytes:
    """Hash the canonical intent so retries do not depend on whitespace/newlines."""
    return hashlib.sha256(
        json.dumps(
            {
                key: (
                    name
                    if key == "name"
                    else location
                    if key == "location"
                    else note
                    if key == "general_note"
                    else _hash_value(getattr(command, key))
                )
                for key in command.__dataclass_fields__
            }
            | {"command_kind": COMMAND_KIND, "command_schema_version": COMMAND_SCHEMA_VERSION},
            ensure_ascii=False,
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


def _record(event: Event, clocks: list[FieldClock]) -> dict[str, object]:
    return {
        "id": str(event.id),
        "organization_id": str(event.organization_id),
        "name": event.name,
        "start_date": event.start_date.isoformat(),
        "end_date": event.end_date.isoformat(),
        "location": event.location,
        "general_note": event.general_note,
        "base_expected_attendance": event.base_expected_attendance,
        "budget_amount": _canonical_decimal_string(event.budget_amount),
        "currency": event.currency,
        "created_at": event.created_at.isoformat(),
        "lifecycle": event.lifecycle,
        "current_archive_snapshot_id": str(event.current_archive_snapshot_id)
        if event.current_archive_snapshot_id
        else None,
        "archived_at": event.archived_at.isoformat() if event.archived_at else None,
        "archived_by_user_id": str(event.archived_by_user_id)
        if event.archived_by_user_id
        else None,
        "created_by_user_id": str(event.created_by_user_id),
        "field_clocks": {
            clock.field_name: {
                "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                "winning_mutation_id": str(clock.winning_mutation_id),
            }
            for clock in clocks
        },
    }


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    error = (mutation.outcome_payload or {}).get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise RuntimeError("Event metadata retained invalid rejection")
    violations = error.get("field_violations", [])
    return ApplicationServiceError(
        error["code"],
        field_violations=tuple(
            FieldViolation(item["path"], item["code"])
            for item in violations
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("code"), str)
        ),
        retry_same_identity=False,
    )


async def update_event_metadata(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: UpdateEventMetadataCommand,
) -> EventMetadataResult:
    name = _text(command.name)
    location = _text(command.location) or None
    note = _text(command.general_note, note=True) or None
    violations = [
        FieldViolation(field, "must_be_uuid")
        for field in ("mutation_id", "event_id", "organization_id")
        if not isinstance(getattr(command, field), UUID)
    ]
    if name is None or not name or len(name) > 200 or "\0" in name:
        violations.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
    if location is not None and (len(location) > 300 or "\0" in location):
        violations.append(FieldViolation("location", "must_be_at_most_300_characters"))
    if note is not None and (len(note) > 4000 or "\0" in note):
        violations.append(FieldViolation("general_note", "must_be_at_most_4000_characters"))
    supplied_dates = command.start_date is not None or command.end_date is not None
    valid_start_date = isinstance(command.start_date, date) and not isinstance(
        command.start_date, datetime
    )
    valid_end_date = isinstance(command.end_date, date) and not isinstance(
        command.end_date, datetime
    )
    start_date = command.start_date if valid_start_date else None
    end_date = command.end_date if valid_end_date else None
    if supplied_dates and not valid_start_date:
        violations.append(FieldViolation("start_date", "must_be_calendar_date"))
    if supplied_dates and not valid_end_date:
        violations.append(FieldViolation("end_date", "must_be_calendar_date"))
    if supplied_dates and valid_start_date and valid_end_date:
        assert start_date is not None and end_date is not None
        if end_date < start_date:
            violations.append(FieldViolation("end_date", "must_not_precede_start_date"))
        elif (end_date - start_date).days >= MAX_EVENT_DAY_COUNT:
            violations.append(
                FieldViolation("end_date", f"must_not_exceed_{MAX_EVENT_DAY_COUNT}_days")
            )
    for field, value in (("name", name), ("location", location), ("general_note", note)):
        if value is not None:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                violations.append(FieldViolation(field, "must_be_utf8"))
    if not _bounded_decimal(command.budget_amount) or command.budget_amount < 0:
        violations.append(FieldViolation("budget_amount", "must_be_nonnegative_finite_decimal"))
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
    request_hash = _request_hash(command, name, location, note)
    mutation_id = command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0)
    organization_id = (
        command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
    )
    when = (
        command.client_wall_time.astimezone(UTC)
        if not violations
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    deferred: ApplicationServiceError | None = None
    result: EventMetadataResult | None = None

    async with session_factory() as session, session.begin():
        actor_role, _ = await _authorize_and_lock_organization(session, context, organization_id)
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
                deferred = _retained_error(retained)
            elif (
                retained.first_change_sequence is not None
                and retained.last_change_sequence is not None
            ):
                return EventMetadataResult(
                    command.mutation_id,
                    command.event_id,
                    organization_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    retained.outcome,
                )
            else:
                raise RuntimeError("Event metadata retained invalid outcome")
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
                    Event.id == command.event_id, Event.organization_id == command.organization_id
                )
                .with_for_update(of=Event)
            )
            if event is None or event.lifecycle != "active":
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(FieldViolation("event_id", "must_belong_to_active_event"),),
                    retry_same_identity=False,
                )
            else:
                clocks = list(
                    (
                        await session.scalars(
                            select(FieldClock)
                            .where(
                                FieldClock.organization_id == organization_id,
                                FieldClock.entity_kind == "event",
                                FieldClock.entity_id == event.id,
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                by_field = {clock.field_name: clock for clock in clocks}
                values = {
                    "name": name,
                    "location": location,
                    "budget_amount": command.budget_amount,
                    "general_note": note,
                    "start_date": command.start_date,
                    "end_date": command.end_date,
                }
                fields: tuple[str, ...] = _FIELDS if supplied_dates else _FIELDS[:-2]
                date_wins = {
                    field: (
                        by_field.get(field) is None
                        or (when, mutation_id)
                        > (
                            by_field[field].winning_client_wall_time,
                            by_field[field].winning_mutation_id,
                        )
                    )
                    for field in ("start_date", "end_date")
                }
                candidate_dates = (
                    start_date
                    if valid_start_date and start_date is not None and date_wins["start_date"]
                    else event.start_date,
                    end_date
                    if valid_end_date and end_date is not None and date_wins["end_date"]
                    else event.end_date,
                )
                if candidate_dates[1] < candidate_dates[0]:
                    deferred = ApplicationServiceError(
                        "validation_failed",
                        field_violations=(
                            FieldViolation("end_date", "must_not_precede_start_date"),
                        ),
                        retry_same_identity=False,
                    )
                    fields = ()
                elif (candidate_dates[1] - candidate_dates[0]).days >= MAX_EVENT_DAY_COUNT:
                    deferred = ApplicationServiceError(
                        "validation_failed",
                        field_violations=(
                            FieldViolation(
                                "end_date", f"must_not_exceed_{MAX_EVENT_DAY_COUNT}_days"
                            ),
                        ),
                        retry_same_identity=False,
                    )
                    fields = ()
                if deferred is None:
                    wins: set[str] = set()
                    for field in fields:
                        clock = by_field.get(field)
                        if clock is None or (when, mutation_id) > (
                            clock.winning_client_wall_time,
                            clock.winning_mutation_id,
                        ):
                            setattr(event, field, values[field])
                            wins.add(field)
                            if clock is None:
                                clock = FieldClock(
                                    organization_id=organization_id,
                                    entity_kind="event",
                                    entity_id=event.id,
                                    field_name=field,
                                    winning_client_wall_time=when,
                                    winning_mutation_id=mutation_id,
                                )
                                session.add(clock)
                                clocks.append(clock)
                            else:
                                clock.winning_client_wall_time, clock.winning_mutation_id = (
                                    when,
                                    mutation_id,
                                )
                    clocks.sort(key=lambda clock: clock.field_name)
                    outcome = "accepted" if wins == set(fields) else "partially_superseded"
                    existing_days = {
                        day.calendar_date: day
                        for day in (
                            await session.scalars(
                                select(EventDay)
                                .where(EventDay.event_id == event.id, EventDay.retired_at.is_(None))
                                .with_for_update()
                            )
                        ).all()
                    }
                    days: list[EventDay] = []
                    current = event.start_date
                    while current <= event.end_date:
                        if current not in existing_days:
                            day = EventDay(
                                event_id=event.id,
                                calendar_date=current,
                                is_visible=True,
                                provenance="range_generated",
                                created_by_user_id=context.actor_user_id,
                            )
                            session.add(day)
                            days.append(day)
                        current += timedelta(days=1)
                    await session.flush()
                    changes: list[tuple[UUID, str, dict[str, object]]] = [
                        (
                            event.id,
                            "event",
                            {"record_schema_version": 1, "record": _record(event, clocks)},
                        )
                    ]
                    changes.extend(
                        (day_id, kind, {"record_schema_version": 1, "record": record})
                        for kind, day_id, record in (
                            _event_day_change_record(event, day) for day in days
                        )
                    )
                    first, last = await _reserve_change_range(
                        session, organization_id, mutation_id, len(changes)
                    )
                    session.add_all(
                        OrganizationChange(
                            organization_id=organization_id,
                            sequence=first + index,
                            mutation_id=mutation_id,
                            entity_id=entity_id,
                            entity_kind=kind,
                            operation="upsert",
                            payload=payload,
                        )
                        for index, (entity_id, kind, payload) in enumerate(changes)
                    )
                    session.add(
                        Mutation(
                            id=mutation_id,
                            logical_operation_id=command.logical_operation_id
                            if isinstance(command.logical_operation_id, UUID)
                            else None,
                            organization_id=organization_id,
                            is_system_administration_scope=False,
                            actor_user_id=context.actor_user_id,
                            actor_role=actor_role,
                            client_installation_id=context.client_installation_id,
                            oauth_client_id=context.oauth_client_id,
                            oauth_grant_id=context.oauth_grant_id,
                            client_wall_time=when,
                            command_schema_version=COMMAND_SCHEMA_VERSION,
                            command_kind=COMMAND_KIND,
                            target_identities=[
                                {"entity_kind": "event", "entity_id": str(event.id)}
                            ],
                            request_hash=request_hash,
                            outcome=outcome,
                            outcome_payload={"outcome": outcome},
                            first_change_sequence=first,
                            last_change_sequence=last,
                        )
                    )
                    result = EventMetadataResult(
                        mutation_id, event.id, organization_id, first, last, False, outcome
                    )
        if deferred is not None and retained is None:
            session.add(
                Mutation(
                    id=mutation_id,
                    logical_operation_id=command.logical_operation_id
                    if isinstance(command.logical_operation_id, UUID)
                    else None,
                    organization_id=organization_id,
                    is_system_administration_scope=False,
                    actor_user_id=context.actor_user_id,
                    actor_role=actor_role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=context.oauth_client_id,
                    oauth_grant_id=context.oauth_grant_id,
                    client_wall_time=when,
                    command_schema_version=COMMAND_SCHEMA_VERSION,
                    command_kind=COMMAND_KIND,
                    target_identities=[
                        {
                            "entity_kind": "event",
                            "entity_id": str(command.event_id)
                            if isinstance(command.event_id, UUID)
                            else str(mutation_id),
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
        raise RuntimeError("Event metadata produced no outcome")
    return result
