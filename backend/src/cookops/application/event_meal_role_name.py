"""Set one custom event meal role name through its LWW field."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.event_meal_role_creation import _error, _record
from cookops.application.events import _reserve_change_range
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.application.recipes import _authorize_and_lock_organization
from cookops.persistence.models import (
    Event,
    EventMealRole,
    FieldClock,
    Mutation,
    OrganizationChange,
)

COMMAND_KIND = "event_meal_role.name"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SetEventMealRoleNameCommand:
    mutation_id: UUID
    event_meal_role_id: UUID
    organization_id: UUID
    event_id: UUID
    custom_name: str
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventMealRoleNameResult:
    mutation_id: UUID
    event_meal_role_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: str = "accepted"


def _value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value).strip()
    if value is None:
        return None
    return {"invalid": type(value).__name__}


def _hash(command: SetEventMealRoleNameCommand) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {key: _value(getattr(command, key)) for key in command.__dataclass_fields__}
            | {"command_kind": COMMAND_KIND, "command_schema_version": COMMAND_SCHEMA_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


async def set_event_meal_role_name(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetEventMealRoleNameCommand,
) -> EventMealRoleNameResult:
    """Rename one active custom role without changing its placement."""
    name = (
        unicodedata.normalize("NFC", command.custom_name).strip()
        if isinstance(command.custom_name, str)
        else ""
    )
    normalized_name = name.lower()
    request_hash = _hash(command)
    violations = [
        FieldViolation(field, "must_be_uuid")
        for field in ("mutation_id", "event_meal_role_id", "organization_id", "event_id")
        if not isinstance(getattr(command, field), UUID)
    ]
    if not name or len(name) > 200 or "\0" in name:
        violations.append(FieldViolation("custom_name", "must_be_nonempty_at_most_200_characters"))
    else:
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            violations.append(FieldViolation("custom_name", "must_be_utf8"))
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
    result: EventMealRoleNameResult | None = None
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
                    else ApplicationServiceError("validation_failed", retry_same_identity=False)
                )
            elif (
                retained.first_change_sequence is not None
                and retained.last_change_sequence is not None
            ):
                return EventMealRoleNameResult(
                    command.mutation_id,
                    command.event_meal_role_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    retained.outcome,
                )
            else:
                raise RuntimeError("invalid retained event meal role name outcome")
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
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("event_meal_role", command.event_meal_role_id)},
            )
            meal_role = await session.scalar(
                select(EventMealRole)
                .join(Event)
                .where(
                    EventMealRole.id == command.event_meal_role_id,
                    EventMealRole.event_id == command.event_id,
                    EventMealRole.retired_at.is_(None),
                    EventMealRole.built_in_translation_key.is_(None),
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(EventMealRole, Event))
            )
            duplicate = (
                await session.scalar(
                    select(EventMealRole.id).where(
                        EventMealRole.event_id == command.event_id,
                        EventMealRole.normalized_custom_name == normalized_name,
                        EventMealRole.id != command.event_meal_role_id,
                    )
                )
                if meal_role is not None
                else None
            )
            if meal_role is None or duplicate is not None:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation(
                            "custom_name" if duplicate else "event_meal_role_id",
                            "already_exists" if duplicate else "must_belong_to_active_custom_role",
                        ),
                    ),
                    retry_same_identity=False,
                )
            else:
                clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == command.organization_id,
                        FieldClock.entity_kind == "event_meal_role",
                        FieldClock.entity_id == meal_role.id,
                        FieldClock.field_name == "custom_name",
                    )
                    .with_for_update(of=FieldClock)
                )
                wins = clock is None or (when, command.mutation_id) > (
                    clock.winning_client_wall_time,
                    clock.winning_mutation_id,
                )
                if wins:
                    meal_role.custom_name, meal_role.normalized_custom_name = name, normalized_name
                    if clock is None:
                        clock = FieldClock(
                            organization_id=command.organization_id,
                            entity_kind="event_meal_role",
                            entity_id=meal_role.id,
                            field_name="custom_name",
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
                outcome = "accepted" if wins else "partially_superseded"
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                clocks = (
                    await session.scalars(
                        select(FieldClock).where(
                            FieldClock.organization_id == command.organization_id,
                            FieldClock.entity_kind == "event_meal_role",
                            FieldClock.entity_id == meal_role.id,
                        )
                    )
                ).all()
                session.add(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first,
                        mutation_id=command.mutation_id,
                        entity_id=meal_role.id,
                        entity_kind="event_meal_role",
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": _record(meal_role, *clocks)},
                    )
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
                        actor_role=role,
                        client_installation_id=context.client_installation_id,
                        oauth_client_id=context.oauth_client_id,
                        oauth_grant_id=context.oauth_grant_id,
                        client_wall_time=when,
                        command_schema_version=COMMAND_SCHEMA_VERSION,
                        command_kind=COMMAND_KIND,
                        target_identities=[
                            {"entity_kind": "event_meal_role", "entity_id": str(meal_role.id)}
                        ],
                        request_hash=request_hash,
                        outcome=outcome,
                        outcome_payload={"outcome": outcome},
                        first_change_sequence=first,
                        last_change_sequence=last,
                    )
                )
                result = EventMealRoleNameResult(
                    command.mutation_id, meal_role.id, first, last, False, outcome
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
                    actor_role=role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=context.oauth_client_id,
                    oauth_grant_id=context.oauth_grant_id,
                    client_wall_time=when,
                    command_schema_version=COMMAND_SCHEMA_VERSION,
                    command_kind=COMMAND_KIND,
                    target_identities=[
                        {
                            "entity_kind": "event_meal_role",
                            "entity_id": str(
                                command.event_meal_role_id
                                if isinstance(command.event_meal_role_id, UUID)
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
        raise RuntimeError("Event meal role name produced no outcome")
    return result
