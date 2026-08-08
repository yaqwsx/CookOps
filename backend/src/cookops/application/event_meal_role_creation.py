"""Create one custom meal role owned by an active event."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

COMMAND_KIND = "event_meal_role.create"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CreateEventMealRoleCommand:
    mutation_id: UUID
    event_meal_role_id: UUID
    organization_id: UUID
    event_id: UUID
    custom_name: str
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventMealRoleCreationResult:
    mutation_id: UUID
    event_meal_role_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: str = "accepted"


def _record(role: EventMealRole, *clocks: FieldClock) -> dict[str, object]:
    return {
        "id": str(role.id),
        "event_id": str(role.event_id),
        "source_preset_id": None,
        "built_in_translation_key": None,
        "custom_name": role.custom_name,
        "normalized_custom_name": role.normalized_custom_name,
        "position_key": role.position_key,
        "created_at": role.created_at.isoformat(),
        "created_by_user_id": str(role.created_by_user_id),
        "retired_at": role.retired_at.isoformat() if role.retired_at else None,
        "retired_by_user_id": str(role.retired_by_user_id) if role.retired_by_user_id else None,
        "field_clocks": {
            clock.field_name: {
                "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                "winning_mutation_id": str(clock.winning_mutation_id),
            }
            for clock in clocks
        },
    }


def _error(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": item.path, "code": item.code} for item in error.field_violations
            ],
        }
    }


async def create_event_meal_role(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateEventMealRoleCommand,
) -> EventMealRoleCreationResult:
    """Create a custom role once; custom roles sort after copied presets."""
    name = (
        unicodedata.normalize("NFC", command.custom_name).strip()
        if isinstance(command.custom_name, str)
        else ""
    )
    normalized_name = name.lower()
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "mutation_id": str(command.mutation_id)
                if isinstance(command.mutation_id, UUID)
                else {"invalid": type(command.mutation_id).__name__},
                "event_meal_role_id": str(command.event_meal_role_id)
                if isinstance(command.event_meal_role_id, UUID)
                else {"invalid": type(command.event_meal_role_id).__name__},
                "organization_id": str(command.organization_id)
                if isinstance(command.organization_id, UUID)
                else {"invalid": type(command.organization_id).__name__},
                "event_id": str(command.event_id)
                if isinstance(command.event_id, UUID)
                else {"invalid": type(command.event_id).__name__},
                "custom_name": name,
                "client_wall_time": command.client_wall_time.astimezone(UTC).isoformat()
                if isinstance(command.client_wall_time, datetime)
                and command.client_wall_time.tzinfo
                and command.client_wall_time.utcoffset() is not None
                else {"invalid": type(command.client_wall_time).__name__},
                "logical_operation_id": str(command.logical_operation_id)
                if isinstance(command.logical_operation_id, UUID)
                else None,
                "command_kind": COMMAND_KIND,
                "command_schema_version": COMMAND_SCHEMA_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()
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
    deferred: ApplicationServiceError | None = None
    result: EventMealRoleCreationResult | None = None
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
                return EventMealRoleCreationResult(
                    command.mutation_id,
                    command.event_meal_role_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                )
            else:
                raise RuntimeError("invalid retained event meal role creation outcome")
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
            existing = await session.get(EventMealRole, command.event_meal_role_id)
            duplicate = await session.scalar(
                select(EventMealRole.id).where(
                    EventMealRole.event_id == command.event_id,
                    EventMealRole.normalized_custom_name == normalized_name,
                )
            )
            if event is None or existing is not None or duplicate is not None:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation(
                            "event_meal_role_id" if existing else "custom_name",
                            "already_exists"
                            if existing or duplicate
                            else "must_belong_to_active_event",
                        ),
                    ),
                    retry_same_identity=False,
                )
            else:
                meal_role = EventMealRole(
                    id=command.event_meal_role_id,
                    event_id=command.event_id,
                    source_preset_id=None,
                    built_in_translation_key=None,
                    custom_name=name,
                    normalized_custom_name=normalized_name,
                    position_key=f"z{command.event_meal_role_id.hex}",
                    created_by_user_id=context.actor_user_id,
                )
                clock = FieldClock(
                    organization_id=command.organization_id,
                    entity_kind="event_meal_role",
                    entity_id=meal_role.id,
                    field_name="position_key",
                    winning_client_wall_time=when,
                    winning_mutation_id=command.mutation_id,
                )
                session.add_all((meal_role, clock))
                await session.flush()
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                session.add(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first,
                        mutation_id=command.mutation_id,
                        entity_id=meal_role.id,
                        entity_kind="event_meal_role",
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": _record(meal_role, clock)},
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
                            {"entity_kind": "event_meal_role", "entity_id": str(meal_role.id)}
                        ],
                        request_hash=request_hash,
                        outcome="accepted",
                        outcome_payload={
                            "event_meal_role": {"custom_name": name},
                            "outcome": "accepted",
                        },
                        first_change_sequence=first,
                        last_change_sequence=last,
                    )
                )
                result = EventMealRoleCreationResult(
                    command.mutation_id, meal_role.id, first, last, False
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
        raise RuntimeError("Event meal role creation produced no outcome")
    return result
