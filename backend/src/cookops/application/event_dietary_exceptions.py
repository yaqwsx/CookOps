"""Create and read active named dietary exceptions for an active event."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import (
    _authorize_member_and_lock_organization,
    _reserve_change_range,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import (
    DietaryTag,
    Event,
    EventDietaryException,
    EventDietaryExceptionTag,
    FieldClock,
    Mutation,
    OrganizationChange,
)

COMMAND_KIND = "event_dietary_exception.create"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CreateEventDietaryExceptionCommand:
    mutation_id: UUID
    exception_id: UUID
    organization_id: UUID
    event_id: UUID
    name: str
    tag_ids: tuple[UUID, ...]
    client_wall_time: datetime
    note: str | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventDietaryExceptionCreationResult:
    mutation_id: UUID
    exception_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: str = "accepted"


def _canonical(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _hash(command: CreateEventDietaryExceptionCommand) -> bytes:
    def raw(value: object) -> object:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime) and value.tzinfo and value.utcoffset() is not None:
            return value.astimezone(UTC).isoformat()
        if isinstance(value, tuple):
            return [raw(item) for item in value]
        return value

    return hashlib.sha256(
        json.dumps(
            {key: raw(getattr(command, key)) for key in command.__dataclass_fields__}
            | {"command_kind": COMMAND_KIND, "command_schema_version": COMMAND_SCHEMA_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _error(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [{"path": x.path, "code": x.code} for x in error.field_violations],
        }
    }


def _record(item: EventDietaryException, clocks: list[FieldClock]) -> dict[str, object]:
    return {
        "id": str(item.id),
        "event_id": str(item.event_id),
        "name": item.name,
        "note": item.note,
        "created_at": item.created_at.isoformat(),
        "created_by_user_id": str(item.created_by_user_id),
        "retired_at": None,
        "retired_by_user_id": None,
        "field_clocks": {
            c.field_name: {
                "winning_client_wall_time": c.winning_client_wall_time.isoformat(),
                "winning_mutation_id": str(c.winning_mutation_id),
            }
            for c in clocks
        },
    }


def _tag_record(item: EventDietaryExceptionTag, clock: FieldClock) -> dict[str, object]:
    return {
        "id": str(item.id),
        "exception_id": str(item.exception_id),
        "dietary_tag_id": str(item.dietary_tag_id),
        "created_at": item.created_at.isoformat(),
        "created_by_user_id": str(item.created_by_user_id),
        "retired_at": None,
        "retired_by_user_id": None,
        "field_clocks": {
            "lifecycle": {
                "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                "winning_mutation_id": str(clock.winning_mutation_id),
            }
        },
    }


async def create_event_dietary_exception(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateEventDietaryExceptionCommand,
) -> EventDietaryExceptionCreationResult:
    name = _canonical(command.name) if isinstance(command.name, str) else ""
    note = _canonical(command.note) if isinstance(command.note, str) else command.note
    tag_ids = tuple(dict.fromkeys(command.tag_ids)) if isinstance(command.tag_ids, tuple) else ()
    violations = []
    for key in ("mutation_id", "exception_id", "organization_id", "event_id"):
        if not isinstance(getattr(command, key), UUID):
            violations.append(FieldViolation(key, "must_be_uuid"))
    if not name or len(name) > 200 or not name.encode("utf-8", "strict"):
        violations.append(FieldViolation("name", "must_be_nonempty_utf8_name"))
    if note is not None and (not isinstance(note, str) or len(note.encode("utf-8")) > 131072):
        violations.append(FieldViolation("note", "must_be_utf8_note"))
    if len(tag_ids) != len(command.tag_ids) or any(not isinstance(x, UUID) for x in tag_ids):
        violations.append(FieldViolation("tag_ids", "must_be_unique_uuids"))
    if (
        not isinstance(command.client_wall_time, datetime)
        or command.client_wall_time.tzinfo is None
        or command.client_wall_time.utcoffset() is None
    ):
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    when = (
        command.client_wall_time.astimezone(UTC)
        if isinstance(command.client_wall_time, datetime) and command.client_wall_time.tzinfo
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    request_hash = _hash(command)
    deferred = None
    result = None
    async with session_factory() as session, session.begin():
        organization_id = (
            command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
        )
        role = await _authorize_member_and_lock_organization(session, context, organization_id)
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
                raise ApplicationServiceError("validation_failed", retry_same_identity=False)
            return EventDietaryExceptionCreationResult(
                mutation_id,
                command.exception_id,
                retained.first_change_sequence or 0,
                retained.last_change_sequence or 0,
                True,
            )
        if violations:
            deferred = ApplicationServiceError(
                "validation_failed", field_violations=tuple(violations), retry_same_identity=False
            )
        elif when > datetime.now(UTC) + timedelta(hours=24):
            deferred = ApplicationServiceError(
                "client_time_too_far_ahead", retry_same_identity=False
            )
        else:
            event = await session.scalar(
                select(Event)
                .where(
                    Event.id == command.event_id,
                    Event.organization_id == organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update()
            )
            tags = (
                await session.scalars(
                    select(DietaryTag)
                    .where(
                        DietaryTag.organization_id == organization_id,
                        DietaryTag.id.in_(tag_ids),
                        DietaryTag.retired_at.is_(None),
                    )
                    .with_for_update()
                )
            ).all()
            existing = await session.get(EventDietaryException, command.exception_id)
            if event is None or existing is not None or len(tags) != len(tag_ids):
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation(
                            "event_id" if event is None else "tag_ids",
                            "must_belong_to_active_event_or_tag",
                        ),
                    ),
                    retry_same_identity=False,
                )
            else:
                item = EventDietaryException(
                    id=command.exception_id,
                    organization_id=organization_id,
                    event_id=command.event_id,
                    name=name,
                    note=note,
                    created_by_user_id=context.actor_user_id,
                )
                session.add(item)
                await session.flush()
                clocks = [
                    FieldClock(
                        organization_id=organization_id,
                        entity_kind="event_dietary_exception",
                        entity_id=item.id,
                        field_name=f,
                        winning_client_wall_time=when,
                        winning_mutation_id=mutation_id,
                    )
                    for f in ("name", "note", "lifecycle")
                ]
                tag_items = []
                for tag_id in tag_ids:
                    tag_item = EventDietaryExceptionTag(
                        organization_id=organization_id,
                        exception_id=item.id,
                        dietary_tag_id=tag_id,
                        created_by_user_id=context.actor_user_id,
                    )
                    session.add(tag_item)
                    await session.flush()
                    tag_items.append(tag_item)
                    clocks.append(
                        FieldClock(
                            organization_id=organization_id,
                            entity_kind="event_dietary_exception_tag",
                            entity_id=tag_item.id,
                            field_name="lifecycle",
                            winning_client_wall_time=when,
                            winning_mutation_id=mutation_id,
                        )
                    )
                session.add_all(clocks)
                await session.flush()
                first, last = await _reserve_change_range(
                    session, organization_id, mutation_id, 1 + len(tag_items)
                )
                session.add(
                    OrganizationChange(
                        organization_id=organization_id,
                        sequence=first,
                        mutation_id=mutation_id,
                        entity_id=item.id,
                        entity_kind="event_dietary_exception",
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": _record(item, clocks[:3])},
                    )
                )
                for offset, tag_item in enumerate(tag_items, 1):
                    session.add(
                        OrganizationChange(
                            organization_id=organization_id,
                            sequence=first + offset,
                            mutation_id=mutation_id,
                            entity_id=tag_item.id,
                            entity_kind="event_dietary_exception_tag",
                            operation="upsert",
                            payload={
                                "record_schema_version": 1,
                                "record": _tag_record(tag_item, clocks[3 + offset - 1]),
                            },
                        )
                    )
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
                        command_schema_version=1,
                        command_kind=COMMAND_KIND,
                        target_identities=[
                    {"entity_kind": "event_dietary_exception", "entity_id": str(item.id)}
                    ] + [
                        {"entity_kind": "event_dietary_exception_tag", "entity_id": str(tag.id)}
                        for tag in tag_items
                    ],
                        request_hash=request_hash,
                        outcome="accepted",
                        outcome_payload={"outcome": "accepted"},
                        first_change_sequence=first,
                        last_change_sequence=last,
                    )
                )
                result = EventDietaryExceptionCreationResult(
                    mutation_id, item.id, first, last, False
                )
        if deferred is not None:
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
                    command_schema_version=1,
                    command_kind=COMMAND_KIND,
                    target_identities=[],
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error(deferred),
                )
            )
    if deferred:
        raise deferred
    assert result is not None
    return result
