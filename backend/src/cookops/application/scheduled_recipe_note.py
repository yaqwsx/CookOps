"""Set one event scheduled's note with field-level last-write-wins."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

COMMAND_KIND = "scheduled_recipe.note"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SetScheduledRecipeNoteCommand:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    organization_id: UUID
    event_id: UUID
    note: str | None
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ScheduledRecipeNoteResult:
    mutation_id: UUID
    scheduled_recipe_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: str = "accepted"
    note: str | None = None


def _hash(command: SetScheduledRecipeNoteCommand, note: str | None) -> bytes:
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


async def set_scheduled_recipe_note(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetScheduledRecipeNoteCommand,
) -> ScheduledRecipeNoteResult:
    """Set an active event scheduled's normalized Markdown note once per mutation."""
    violations = [
        FieldViolation(name, "must_be_uuid")
        for name in ("mutation_id", "scheduled_recipe_id", "organization_id", "event_id")
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
    result: ScheduledRecipeNoteResult | None = None
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
                    ApplicationServiceError(
                        error["code"],
                        field_violations=tuple(
                            FieldViolation(item["path"], item["code"])
                            for item in error.get("field_violations", [])
                            if isinstance(item, dict)
                            and isinstance(item.get("path"), str)
                            and isinstance(item.get("code"), str)
                        ),
                        retry_same_identity=False,
                    )
                    if isinstance(error, dict) and isinstance(error.get("code"), str)
                    else ApplicationServiceError("validation_failed", retry_same_identity=False)
                )
            elif (
                retained.first_change_sequence is not None
                and retained.last_change_sequence is not None
            ):
                retained_payload = retained.outcome_payload
                retained_scheduled_recipe = (
                    retained_payload.get("scheduled_recipe")
                    if isinstance(retained_payload, dict)
                    else None
                )
                if not isinstance(retained_scheduled_recipe, dict):
                    raise RuntimeError("invalid retained event scheduled note outcome")
                retained_note = retained_scheduled_recipe.get("note")
                if retained_note is not None and not isinstance(retained_note, str):
                    raise RuntimeError("invalid retained event scheduled note outcome")
                return ScheduledRecipeNoteResult(
                    command.mutation_id,
                    command.scheduled_recipe_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    retained.outcome,
                    retained_note,
                )
            else:
                raise RuntimeError("invalid retained event scheduled note outcome")
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
                    ScheduledRecipe.event_id == command.event_id,
                    ScheduledRecipe.retired_at.is_(None),
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ScheduledRecipe, Event))
            )
            if scheduled is None:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation("scheduled_recipe_id", "must_belong_to_active_event"),
                    ),
                    retry_same_identity=False,
                )
            else:
                clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == command.organization_id,
                        FieldClock.entity_kind == "scheduled_recipe",
                        FieldClock.entity_id == scheduled.id,
                        FieldClock.field_name == "note",
                    )
                    .with_for_update(of=FieldClock)
                )
                wins = clock is None or (when, command.mutation_id) > (
                    clock.winning_client_wall_time,
                    clock.winning_mutation_id,
                )
                if wins:
                    scheduled.note = note
                    if clock is None:
                        clock = FieldClock(
                            organization_id=command.organization_id,
                            entity_kind="scheduled_recipe",
                            entity_id=scheduled.id,
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
                                FieldClock.entity_kind == "scheduled_recipe",
                                FieldClock.entity_id == scheduled.id,
                            )
                        )
                    ).scalars()
                )
                record = _scheduled_recipe_change_record(scheduled)[2]
                record["field_clocks"] = {
                    item.field_name: {
                        "winning_client_wall_time": item.winning_client_wall_time.isoformat(),
                        "winning_mutation_id": str(item.winning_mutation_id),
                    }
                    for item in clocks
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
                        target_identities=[
                            {"entity_kind": "scheduled_recipe", "entity_id": str(scheduled.id)}
                        ],
                        request_hash=request_hash,
                        outcome=outcome,
                        outcome_payload={
                            "scheduled_recipe": {"note": scheduled.note},
                            "outcome": outcome,
                        },
                        first_change_sequence=first,
                        last_change_sequence=last,
                    )
                )
                result = ScheduledRecipeNoteResult(
                    command.mutation_id, scheduled.id, first, last, False, outcome, scheduled.note
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
                            "entity_kind": "scheduled_recipe",
                            "entity_id": str(
                                command.scheduled_recipe_id
                                if isinstance(command.scheduled_recipe_id, UUID)
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
        raise RuntimeError("Event scheduled note produced no outcome")
    return result
