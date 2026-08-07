"""Event-planning application services."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import (
    ClientInstallation,
    Event,
    EventDay,
    EventMealRole,
    FieldClock,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMealRolePreset,
    OrganizationMembership,
    ScheduledRecipe,
    SystemRoleAssignment,
    User,
)

COMMAND_KIND = "event.create"
COMMAND_SCHEMA_VERSION = 1
MAX_EVENT_DAY_COUNT = 366
UPDATE_BASE_ATTENDANCE_COMMAND_KIND = "event.update_base_attendance"


@dataclass(frozen=True, slots=True)
class CreateEventCommand:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    name: str
    start_date: date
    end_date: date
    base_expected_attendance: int
    budget_amount: Decimal
    client_wall_time: datetime
    location: str | None = None
    general_note: str | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventDayResult:
    id: UUID
    calendar_date: date
    provenance: Literal["range_generated"]


@dataclass(frozen=True, slots=True)
class EventMealRoleResult:
    id: UUID
    source_preset_id: UUID | None
    position_key: str
    built_in_translation_key: str | None
    custom_name: str | None


@dataclass(frozen=True, slots=True)
class CreateEventResult:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    name: str
    start_date: date
    end_date: date
    base_expected_attendance: int
    budget_amount: Decimal
    currency: str
    location: str | None
    general_note: str | None
    days: tuple[EventDayResult, ...]
    meal_roles: tuple[EventMealRoleResult, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


@dataclass(frozen=True, slots=True)
class UpdateEventBaseAttendanceCommand:
    """Set an event's base attendance and refresh its following recipe instances."""

    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    base_expected_attendance: int
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class UpdateEventBaseAttendanceResult:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    base_expected_attendance: int
    updated_scheduled_recipe_ids: tuple[UUID, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


@dataclass(frozen=True, slots=True)
class _PreparedCommand:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    name: str
    start_date: date
    end_date: date
    base_expected_attendance: int
    budget_amount: Decimal
    client_wall_time: datetime
    location: str | None
    general_note: str | None
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


@dataclass(frozen=True, slots=True)
class _PreparedAttendanceCommand:
    mutation_id: UUID
    event_id: UUID
    organization_id: UUID
    base_expected_attendance: int
    client_wall_time: datetime
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


def _canonical_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _canonical_note(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _prepare_command(command: CreateEventCommand) -> _PreparedCommand:
    violations: list[FieldViolation] = []
    name = _canonical_text(command.name) if isinstance(command.name, str) else ""
    if not isinstance(command.name, str) or not name or len(name) > 200:
        violations.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
    location = _canonical_text(command.location) if isinstance(command.location, str) else None
    if command.location is not None and not isinstance(command.location, str):
        violations.append(FieldViolation("location", "must_be_string_or_null"))
    if location is not None and len(location) > 300:
        violations.append(FieldViolation("location", "must_be_at_most_300_characters"))
    general_note = (
        _canonical_note(command.general_note) if isinstance(command.general_note, str) else None
    )

    if command.general_note is not None and not isinstance(command.general_note, str):
        violations.append(FieldViolation("general_note", "must_be_string_or_null"))
    valid_start_date = isinstance(command.start_date, date) and not isinstance(
        command.start_date, datetime
    )
    valid_end_date = isinstance(command.end_date, date) and not isinstance(
        command.end_date, datetime
    )
    start_date = command.start_date if valid_start_date else date.min
    end_date = command.end_date if valid_end_date else date.min
    if not valid_start_date:
        violations.append(FieldViolation("start_date", "must_be_calendar_date"))
    if not valid_end_date:
        violations.append(FieldViolation("end_date", "must_be_calendar_date"))
    if valid_start_date and valid_end_date and end_date < start_date:
        violations.append(FieldViolation("end_date", "must_not_precede_start_date"))
    if valid_start_date and valid_end_date and (end_date - start_date).days >= MAX_EVENT_DAY_COUNT:
        violations.append(FieldViolation("end_date", f"must_not_exceed_{MAX_EVENT_DAY_COUNT}_days"))
    attendance = (
        command.base_expected_attendance
        if isinstance(command.base_expected_attendance, int)
        and not isinstance(command.base_expected_attendance, bool)
        else 0
    )
    if (
        not isinstance(command.base_expected_attendance, int)
        or isinstance(command.base_expected_attendance, bool)
        or command.base_expected_attendance < 0
    ):
        violations.append(FieldViolation("base_expected_attendance", "must_be_nonnegative_integer"))
    budget_amount = (
        command.budget_amount if isinstance(command.budget_amount, Decimal) else Decimal(0)
    )
    if (
        not isinstance(command.budget_amount, Decimal)
        or not command.budget_amount.is_finite()
        or command.budget_amount < 0
    ):
        violations.append(FieldViolation("budget_amount", "must_be_nonnegative_finite_decimal"))
    wall_time_has_timezone = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not wall_time_has_timezone:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if not isinstance(command.mutation_id, UUID):
        violations.append(FieldViolation("mutation_id", "must_be_uuid"))
    if not isinstance(command.event_id, UUID):
        violations.append(FieldViolation("event_id", "must_be_uuid"))
    if not isinstance(command.organization_id, UUID):
        violations.append(FieldViolation("organization_id", "must_be_uuid"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    return _PreparedCommand(
        mutation_id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        event_id=command.event_id if isinstance(command.event_id, UUID) else UUID(int=0),
        organization_id=(
            command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
        ),
        name=name,
        start_date=start_date,
        end_date=end_date,
        base_expected_attendance=attendance,
        budget_amount=budget_amount,
        client_wall_time=(
            command.client_wall_time.astimezone(UTC)
            if wall_time_has_timezone
            else datetime(1970, 1, 1, tzinfo=UTC)
        ),
        location=location or None,
        general_note=general_note,
        logical_operation_id=(
            command.logical_operation_id if isinstance(command.logical_operation_id, UUID) else None
        ),
        violations=tuple(violations),
    )


def _prepare_attendance_command(
    command: UpdateEventBaseAttendanceCommand,
) -> _PreparedAttendanceCommand:
    violations: list[FieldViolation] = []
    attendance = (
        command.base_expected_attendance
        if isinstance(command.base_expected_attendance, int)
        and not isinstance(command.base_expected_attendance, bool)
        else 0
    )
    if (
        not isinstance(command.base_expected_attendance, int)
        or isinstance(command.base_expected_attendance, bool)
        or command.base_expected_attendance < 0
    ):
        violations.append(FieldViolation("base_expected_attendance", "must_be_nonnegative_integer"))
    wall_time_has_timezone = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not wall_time_has_timezone:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if not isinstance(command.mutation_id, UUID):
        violations.append(FieldViolation("mutation_id", "must_be_uuid"))
    if not isinstance(command.event_id, UUID):
        violations.append(FieldViolation("event_id", "must_be_uuid"))
    if not isinstance(command.organization_id, UUID):
        violations.append(FieldViolation("organization_id", "must_be_uuid"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    return _PreparedAttendanceCommand(
        mutation_id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        event_id=command.event_id if isinstance(command.event_id, UUID) else UUID(int=0),
        organization_id=(
            command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
        ),
        base_expected_attendance=attendance,
        client_wall_time=(
            command.client_wall_time.astimezone(UTC)
            if wall_time_has_timezone
            else datetime(1970, 1, 1, tzinfo=UTC)
        ),
        logical_operation_id=(
            command.logical_operation_id if isinstance(command.logical_operation_id, UUID) else None
        ),
        violations=tuple(violations),
    )


def _request_hash(command: _PreparedCommand) -> bytes:
    semantic_request = {
        "base_expected_attendance": command.base_expected_attendance,
        "budget_amount": _canonical_decimal_string(command.budget_amount),
        "client_wall_time": command.client_wall_time.isoformat().replace("+00:00", "Z"),
        "command_kind": COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "end_date": command.end_date.isoformat(),
        "event_id": str(command.event_id),
        "general_note": command.general_note,
        "location": command.location,
        "logical_operation_id": (
            str(command.logical_operation_id) if command.logical_operation_id else None
        ),
        "name": command.name,
        "organization_id": str(command.organization_id),
        "start_date": command.start_date.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(
            semantic_request, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).digest()


def _invalid_hash_value(value: object) -> dict[str, str]:
    return {"invalid_type": type(value).__qualname__, "repr": repr(value)}


def _raw_uuid_hash_value(value: object) -> str | dict[str, str]:
    return str(value) if isinstance(value, UUID) else _invalid_hash_value(value)


def _raw_wall_time_hash_value(value: object) -> str | dict[str, str]:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _invalid_hash_value(value)


def _raw_attendance_hash_value(value: object) -> int | dict[str, str]:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool)
        else _invalid_hash_value(value)
    )


def _attendance_request_hash(command: UpdateEventBaseAttendanceCommand) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {
                "base_expected_attendance": _raw_attendance_hash_value(
                    command.base_expected_attendance
                ),
                "client_wall_time": _raw_wall_time_hash_value(command.client_wall_time),
                "command_kind": UPDATE_BASE_ATTENDANCE_COMMAND_KIND,
                "command_schema_version": COMMAND_SCHEMA_VERSION,
                "event_id": _raw_uuid_hash_value(command.event_id),
                "logical_operation_id": (
                    _raw_uuid_hash_value(command.logical_operation_id)
                    if command.logical_operation_id is not None
                    else None
                ),
                "organization_id": _raw_uuid_hash_value(command.organization_id),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).digest()


def _canonical_decimal_string(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


async def _authorize_and_lock_organization(
    session: AsyncSession, context: ExecutionContext, organization_id: UUID
) -> tuple[Literal["organization_admin", "system_admin"], str]:
    """Return current actor role and organization currency, or fail without enumeration."""

    expected_installation_kind = "agent" if context.oauth_client_id is not None else "browser"
    actor = await session.scalar(
        select(User.id)
        .join(
            ClientInstallation,
            (ClientInstallation.user_id == User.id)
            & (ClientInstallation.id == context.client_installation_id),
        )
        .where(
            User.id == context.actor_user_id,
            User.disabled_at.is_(None),
            ClientInstallation.disabled_at.is_(None),
            ClientInstallation.installation_kind == expected_installation_kind,
        )
        .with_for_update(of=(User, ClientInstallation))
    )
    if actor is None:
        raise ApplicationServiceError("forbidden", retry_same_identity=True)

    is_system_admin = await session.scalar(
        select(SystemRoleAssignment.id)
        .where(
            SystemRoleAssignment.user_id == context.actor_user_id,
            SystemRoleAssignment.role == "system_admin",
            SystemRoleAssignment.revoked_at.is_(None),
        )
        .with_for_update(of=SystemRoleAssignment)
    )
    organization = (
        await session.execute(
            select(Organization.default_currency)
            .where(Organization.id == organization_id, Organization.retired_at.is_(None))
            .with_for_update(of=Organization)
        )
    ).scalar_one_or_none()
    if organization is None:
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    if is_system_admin is not None:
        return "system_admin", organization

    organization_admin = await session.scalar(
        select(OrganizationMembership.id)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == context.actor_user_id,
            OrganizationMembership.role == "organization_admin",
            OrganizationMembership.state == "active",
        )
        .with_for_update(of=OrganizationMembership)
    )
    if organization_admin is None:
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    return "organization_admin", organization


async def _authorize_member_and_lock_organization(
    session: AsyncSession, context: ExecutionContext, organization_id: UUID
) -> Literal["member", "organization_admin", "system_admin"]:
    """Authorize ordinary event planning against current installation and membership state."""

    expected_installation_kind = "agent" if context.oauth_client_id is not None else "browser"
    actor = await session.scalar(
        select(User.id)
        .join(
            ClientInstallation,
            (ClientInstallation.user_id == User.id)
            & (ClientInstallation.id == context.client_installation_id),
        )
        .where(
            User.id == context.actor_user_id,
            User.disabled_at.is_(None),
            ClientInstallation.disabled_at.is_(None),
            ClientInstallation.installation_kind == expected_installation_kind,
        )
        .with_for_update(of=(User, ClientInstallation))
    )
    organization = await session.scalar(
        select(Organization.id)
        .where(Organization.id == organization_id, Organization.retired_at.is_(None))
        .with_for_update(of=Organization)
    )
    if actor is None or organization is None:
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    is_system_admin = await session.scalar(
        select(SystemRoleAssignment.id)
        .where(
            SystemRoleAssignment.user_id == context.actor_user_id,
            SystemRoleAssignment.role == "system_admin",
            SystemRoleAssignment.revoked_at.is_(None),
        )
        .with_for_update(of=SystemRoleAssignment)
    )
    if is_system_admin is not None:
        return "system_admin"
    membership = await session.scalar(
        select(OrganizationMembership.role)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == context.actor_user_id,
            OrganizationMembership.state == "active",
            OrganizationMembership.role.in_(("member", "organization_admin")),
        )
        .with_for_update(of=OrganizationMembership)
    )
    if membership not in ("member", "organization_admin"):
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    return cast(Literal["member", "organization_admin"], membership)


def _result_payload(result: CreateEventResult) -> dict[str, object]:
    return {
        "event": {
            "id": str(result.event_id),
            "organization_id": str(result.organization_id),
            "name": result.name,
            "start_date": result.start_date.isoformat(),
            "end_date": result.end_date.isoformat(),
            "base_expected_attendance": result.base_expected_attendance,
            "budget_amount": str(result.budget_amount),
            "currency": result.currency,
            "location": result.location,
            "general_note": result.general_note,
        },
        "days": [
            {"id": str(day.id), "calendar_date": day.calendar_date.isoformat()}
            for day in result.days
        ],
        "meal_roles": [
            {
                "id": str(role.id),
                "source_preset_id": (
                    str(role.source_preset_id) if role.source_preset_id is not None else None
                ),
                "position_key": role.position_key,
                "built_in_translation_key": role.built_in_translation_key,
                "custom_name": role.custom_name,
            }
            for role in result.meal_roles
        ],
    }


def _required_str(values: dict[object, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise TypeError
    return value


def _optional_uuid(values: dict[object, object], key: str) -> UUID | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError
    return UUID(value)


def _retained_result(mutation: Mutation) -> CreateEventResult:
    payload = mutation.outcome_payload
    event = payload.get("event") if payload is not None else None
    days = payload.get("days") if payload is not None else None
    roles = payload.get("meal_roles") if payload is not None else None
    if not isinstance(event, dict) or not isinstance(days, list) or not isinstance(roles, list):
        raise RuntimeError("Accepted event mutation has an invalid outcome payload")
    try:
        location = event["location"]
        general_note = event["general_note"]
        if (location is not None and not isinstance(location, str)) or (
            general_note is not None and not isinstance(general_note, str)
        ):
            raise TypeError
        attendance = event["base_expected_attendance"]
        if not isinstance(attendance, int) or isinstance(attendance, bool):
            raise TypeError
        result_days = tuple(
            EventDayResult(
                UUID(_required_str(item, "id")),
                date.fromisoformat(_required_str(item, "calendar_date")),
                "range_generated",
            )
            for item in days
            if isinstance(item, dict)
        )
        result_roles = tuple(
            EventMealRoleResult(
                id=UUID(_required_str(item, "id")),
                source_preset_id=_optional_uuid(item, "source_preset_id"),
                position_key=_required_str(item, "position_key"),
                built_in_translation_key=item.get("built_in_translation_key"),
                custom_name=item.get("custom_name"),
            )
            for item in roles
            if isinstance(item, dict)
        )
        if len(result_days) != len(days) or len(result_roles) != len(roles):
            raise TypeError
        for role in result_roles:
            if (role.built_in_translation_key is None) == (role.custom_name is None):
                raise TypeError
            if not all(
                value is None or isinstance(value, str)
                for value in (role.built_in_translation_key, role.custom_name)
            ):
                raise TypeError
        first_change_sequence = mutation.first_change_sequence
        last_change_sequence = mutation.last_change_sequence
        if first_change_sequence is None or last_change_sequence is None:
            raise TypeError
        return CreateEventResult(
            mutation_id=mutation.id,
            event_id=UUID(_required_str(event, "id")),
            organization_id=UUID(_required_str(event, "organization_id")),
            name=_required_str(event, "name"),
            start_date=date.fromisoformat(_required_str(event, "start_date")),
            end_date=date.fromisoformat(_required_str(event, "end_date")),
            base_expected_attendance=attendance,
            budget_amount=Decimal(_required_str(event, "budget_amount")),
            currency=_required_str(event, "currency"),
            location=location,
            general_note=general_note,
            days=result_days,
            meal_roles=result_roles,
            first_change_sequence=first_change_sequence,
            last_change_sequence=last_change_sequence,
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Accepted event mutation has an invalid outcome payload") from error


def _validation_error(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
    )


def _error_payload(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": violation.path, "code": violation.code}
                for violation in error.field_violations
            ],
        }
    }


def _event_change_records(
    event: Event,
    days: tuple[EventDay, ...],
    meal_roles: tuple[EventMealRole, ...],
    attendance_clock: FieldClock,
) -> tuple[tuple[str, UUID, dict[str, object]], ...]:
    event_record = _event_change_record(event, attendance_clock)[2]
    day_records: tuple[tuple[str, UUID, dict[str, object]], ...] = tuple(
        (
            "event_day",
            day.id,
            {
                "id": str(day.id),
                "event_id": str(event.id),
                "calendar_date": day.calendar_date.isoformat(),
                "note": day.note,
                "is_visible": day.is_visible,
                "provenance": day.provenance,
                "created_at": day.created_at.isoformat(),
                "created_by_user_id": str(day.created_by_user_id),
                "retired_at": day.retired_at.isoformat() if day.retired_at else None,
                "retired_by_user_id": (
                    str(day.retired_by_user_id) if day.retired_by_user_id else None
                ),
                "field_clocks": {"note": None, "is_visible": None},
            },
        )
        for day in days
    )
    role_change_records: tuple[tuple[str, UUID, dict[str, object]], ...] = tuple(
        (
            "event_meal_role",
            meal_role.id,
            {
                "id": str(meal_role.id),
                "event_id": str(event.id),
                "source_preset_id": (
                    str(meal_role.source_preset_id)
                    if meal_role.source_preset_id is not None
                    else None
                ),
                "built_in_translation_key": meal_role.built_in_translation_key,
                "custom_name": meal_role.custom_name,
                "normalized_custom_name": meal_role.normalized_custom_name,
                "position_key": meal_role.position_key,
                "created_at": meal_role.created_at.isoformat(),
                "created_by_user_id": str(meal_role.created_by_user_id),
                "retired_at": meal_role.retired_at.isoformat() if meal_role.retired_at else None,
                "retired_by_user_id": (
                    str(meal_role.retired_by_user_id) if meal_role.retired_by_user_id else None
                ),
                "field_clocks": {"position_key": None},
            },
        )
        for meal_role in meal_roles
    )
    return (("event", event.id, event_record), *day_records, *role_change_records)


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload
    error = payload.get("error") if payload is not None else None
    if not isinstance(error, dict):
        raise RuntimeError("Rejected event mutation has an invalid outcome payload")
    try:
        code = _required_str(error, "code")
        if code not in ("archived_event", "client_time_too_far_ahead", "validation_failed"):
            raise TypeError
        raw_violations = error.get("field_violations")
        if raw_violations is None and code != "validation_failed":
            raw_violations = []
        if not isinstance(raw_violations, list):
            raise TypeError
        violations = tuple(
            FieldViolation(_required_str(item, "path"), _required_str(item, "code"))
            for item in raw_violations
            if isinstance(item, dict)
        )
        if len(violations) != len(raw_violations):
            raise TypeError
    except TypeError as error_value:
        raise RuntimeError(
            "Rejected event mutation has an invalid outcome payload"
        ) from error_value
    return ApplicationServiceError(
        cast(
            Literal["archived_event", "client_time_too_far_ahead", "validation_failed"],
            code,
        ),
        field_violations=violations,
        retry_same_identity=False,
    )


def _mutation(
    *,
    command: _PreparedCommand | _PreparedAttendanceCommand,
    context: ExecutionContext,
    actor_role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "partially_superseded", "rejected"],
    outcome_payload: dict[str, object],
    first_change_sequence: int | None = None,
    last_change_sequence: int | None = None,
) -> Mutation:
    return Mutation(
        id=command.mutation_id,
        logical_operation_id=command.logical_operation_id,
        organization_id=command.organization_id,
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=actor_role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=(
            COMMAND_KIND
            if isinstance(command, _PreparedCommand)
            else UPDATE_BASE_ATTENDANCE_COMMAND_KIND
        ),
        target_identities=[{"entity_kind": "event", "entity_id": str(command.event_id)}],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=outcome_payload,
        first_change_sequence=first_change_sequence,
        last_change_sequence=last_change_sequence,
    )


def _field_clock_wins(clock: FieldClock | None, command: _PreparedAttendanceCommand) -> bool:
    """Apply the protocol's total ordering without depending on receive order."""

    if clock is None:
        return True
    return (command.client_wall_time, command.mutation_id) > (
        clock.winning_client_wall_time,
        clock.winning_mutation_id,
    )


def _event_change_record(
    event: Event, attendance_clock: FieldClock | None = None
) -> tuple[str, UUID, dict[str, object]]:
    return (
        "event",
        event.id,
        {
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
            "current_archive_snapshot_id": (
                str(event.current_archive_snapshot_id)
                if event.current_archive_snapshot_id is not None
                else None
            ),
            "archived_at": event.archived_at.isoformat() if event.archived_at is not None else None,
            "archived_by_user_id": (
                str(event.archived_by_user_id) if event.archived_by_user_id is not None else None
            ),
            "created_by_user_id": str(event.created_by_user_id),
            "field_clocks": {
                "base_expected_attendance": (
                    {
                        "winning_client_wall_time": (
                            attendance_clock.winning_client_wall_time.isoformat()
                        ),
                        "winning_mutation_id": str(attendance_clock.winning_mutation_id),
                    }
                    if attendance_clock is not None
                    else None
                )
            },
        },
    )


async def _reserve_change_range(
    session: AsyncSession,
    organization_id: UUID,
    mutation_id: UUID,
    change_count: int,
) -> tuple[int, int]:
    row = (
        await session.execute(
            text(
                "SELECT first_change_sequence, last_change_sequence "
                "FROM reserve_organization_change_transaction("
                ":organization_id, :mutation_id, :change_count)"
            ),
            {
                "organization_id": organization_id,
                "mutation_id": mutation_id,
                "change_count": change_count,
            },
        )
    ).one()
    return int(row.first_change_sequence), int(row.last_change_sequence)


async def create_event(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateEventCommand,
) -> CreateEventResult:
    """Create an active event with every range day and current role preset copied locally."""

    prepared = _prepare_command(command)
    request_hash = _request_hash(prepared)
    deferred_error: ApplicationServiceError | None = None
    result: CreateEventResult | None = None

    async with session_factory() as session, session.begin():
        actor_role, currency = await _authorize_and_lock_organization(
            session, context, prepared.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key("mutation", prepared.mutation_id)},
        )
        retained = await session.get(Mutation, prepared.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "accepted":
                return _retained_result(retained)
            if retained.outcome == "rejected":
                deferred_error = _retained_error(retained)
            else:
                raise RuntimeError("Event creation retained an unsupported outcome")
        elif prepared.violations:
            deferred_error = _validation_error(prepared.violations)
            session.add(
                _mutation(
                    command=prepared,
                    context=context,
                    actor_role=actor_role,
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error_payload(deferred_error),
                )
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _advisory_lock_key("event", prepared.event_id)},
            )
            event_exists = await session.scalar(
                select(Event.id).where(Event.id == prepared.event_id)
            )
            if event_exists is not None:
                deferred_error = _validation_error((FieldViolation("event_id", "already_exists"),))
                session.add(
                    _mutation(
                        command=prepared,
                        context=context,
                        actor_role=actor_role,
                        request_hash=request_hash,
                        outcome="rejected",
                        outcome_payload=_error_payload(deferred_error),
                    )
                )
            else:
                presets = tuple(
                    (
                        preset.id,
                        preset.built_in_translation_key,
                        preset.custom_name,
                        preset.normalized_custom_name,
                        preset.position_key,
                    )
                    for preset in (
                        await session.execute(
                            select(OrganizationMealRolePreset)
                            .where(
                                OrganizationMealRolePreset.organization_id
                                == prepared.organization_id,
                                OrganizationMealRolePreset.retired_at.is_(None),
                            )
                            .order_by(
                                OrganizationMealRolePreset.position_key,
                                OrganizationMealRolePreset.id,
                            )
                            .with_for_update(read=True, of=OrganizationMealRolePreset)
                        )
                    ).scalars()
                )
                days = tuple(
                    EventDayResult(
                        uuid4(), prepared.start_date + timedelta(days=index), "range_generated"
                    )
                    for index in range((prepared.end_date - prepared.start_date).days + 1)
                )
                role_records = tuple(
                    (
                        EventMealRoleResult(
                            uuid4(), source_preset_id, position, built_in_key, custom_name
                        ),
                        normalized_custom_name,
                    )
                    for (
                        source_preset_id,
                        built_in_key,
                        custom_name,
                        normalized_custom_name,
                        position,
                    ) in presets
                )
                roles = tuple(role for role, _ in role_records)
                event = Event(
                    id=prepared.event_id,
                    organization_id=prepared.organization_id,
                    name=prepared.name,
                    start_date=prepared.start_date,
                    end_date=prepared.end_date,
                    location=prepared.location,
                    general_note=prepared.general_note,
                    base_expected_attendance=prepared.base_expected_attendance,
                    budget_amount=prepared.budget_amount,
                    currency=currency,
                    created_by_user_id=context.actor_user_id,
                )
                session.add(event)
                await session.flush()
                attendance_clock = FieldClock(
                    organization_id=prepared.organization_id,
                    entity_kind="event",
                    entity_id=event.id,
                    field_name="base_expected_attendance",
                    winning_client_wall_time=prepared.client_wall_time,
                    winning_mutation_id=prepared.mutation_id,
                )
                session.add(attendance_clock)
                day_entities = tuple(
                    EventDay(
                        id=day.id,
                        event_id=prepared.event_id,
                        calendar_date=day.calendar_date,
                        is_visible=True,
                        provenance=day.provenance,
                        created_by_user_id=context.actor_user_id,
                    )
                    for day in days
                )
                meal_role_entities = tuple(
                    EventMealRole(
                        id=role.id,
                        event_id=prepared.event_id,
                        source_preset_id=role.source_preset_id,
                        built_in_translation_key=role.built_in_translation_key,
                        custom_name=role.custom_name,
                        normalized_custom_name=normalized_custom_name,
                        position_key=role.position_key,
                        created_by_user_id=context.actor_user_id,
                    )
                    for role, normalized_custom_name in role_records
                )
                session.add_all((*day_entities, *meal_role_entities))
                await session.flush()
                change_records = _event_change_records(
                    event,
                    day_entities,
                    meal_role_entities,
                    attendance_clock,
                )
                first_change_sequence, last_change_sequence = await _reserve_change_range(
                    session,
                    prepared.organization_id,
                    prepared.mutation_id,
                    len(change_records),
                )
                result = CreateEventResult(
                    mutation_id=prepared.mutation_id,
                    event_id=prepared.event_id,
                    organization_id=prepared.organization_id,
                    name=prepared.name,
                    start_date=prepared.start_date,
                    end_date=prepared.end_date,
                    base_expected_attendance=prepared.base_expected_attendance,
                    budget_amount=prepared.budget_amount,
                    currency=currency,
                    location=prepared.location,
                    general_note=prepared.general_note,
                    days=days,
                    meal_roles=roles,
                    first_change_sequence=first_change_sequence,
                    last_change_sequence=last_change_sequence,
                    replayed=False,
                )
                session.add_all(
                    OrganizationChange(
                        organization_id=prepared.organization_id,
                        sequence=first_change_sequence + index,
                        mutation_id=prepared.mutation_id,
                        entity_id=entity_id,
                        entity_kind=entity_kind,
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": record},
                    )
                    for index, (entity_kind, entity_id, record) in enumerate(change_records)
                )
                session.add(
                    _mutation(
                        command=prepared,
                        context=context,
                        actor_role=actor_role,
                        request_hash=request_hash,
                        outcome="accepted",
                        outcome_payload=_result_payload(result),
                        first_change_sequence=first_change_sequence,
                        last_change_sequence=last_change_sequence,
                    )
                )

    if deferred_error is not None:
        raise deferred_error
    if result is None:
        raise RuntimeError("Event creation produced no outcome")
    return result


def _attendance_result_payload(result: UpdateEventBaseAttendanceResult) -> dict[str, object]:
    return {
        "event": {
            "id": str(result.event_id),
            "organization_id": str(result.organization_id),
            "base_expected_attendance": result.base_expected_attendance,
        },
        "updated_scheduled_recipe_ids": [
            str(scheduled_recipe_id) for scheduled_recipe_id in result.updated_scheduled_recipe_ids
        ],
        "outcome": result.outcome,
    }


def _retained_attendance_result(mutation: Mutation) -> UpdateEventBaseAttendanceResult:
    payload = mutation.outcome_payload
    event = payload.get("event") if payload is not None else None
    scheduled_recipe_ids = (
        payload.get("updated_scheduled_recipe_ids") if payload is not None else None
    )
    outcome = payload.get("outcome") if payload is not None else None
    if (
        not isinstance(event, dict)
        or not isinstance(scheduled_recipe_ids, list)
        or outcome not in ("accepted", "partially_superseded")
        or outcome != mutation.outcome
    ):
        raise RuntimeError("Accepted attendance mutation has an invalid outcome payload")
    try:
        attendance = event.get("base_expected_attendance")
        if not isinstance(attendance, int) or isinstance(attendance, bool):
            raise TypeError
        updated_ids = tuple(UUID(value) for value in scheduled_recipe_ids if isinstance(value, str))
        if len(updated_ids) != len(scheduled_recipe_ids):
            raise TypeError
        first_change_sequence = mutation.first_change_sequence
        last_change_sequence = mutation.last_change_sequence
        if first_change_sequence is None or last_change_sequence is None:
            raise TypeError
        return UpdateEventBaseAttendanceResult(
            mutation_id=mutation.id,
            event_id=UUID(_required_str(event, "id")),
            organization_id=UUID(_required_str(event, "organization_id")),
            base_expected_attendance=attendance,
            updated_scheduled_recipe_ids=updated_ids,
            first_change_sequence=first_change_sequence,
            last_change_sequence=last_change_sequence,
            replayed=True,
            outcome=outcome,
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("Accepted attendance mutation has an invalid outcome payload") from error


def _scheduled_recipe_change_record(
    scheduled_recipe: ScheduledRecipe,
) -> tuple[str, UUID, dict[str, object]]:
    return (
        "scheduled_recipe",
        scheduled_recipe.id,
        {
            "id": str(scheduled_recipe.id),
            "organization_id": str(scheduled_recipe.organization_id),
            "event_id": str(scheduled_recipe.event_id),
            "event_day_id": str(scheduled_recipe.event_day_id),
            "event_meal_role_id": str(scheduled_recipe.event_meal_role_id),
            "recipe_id": str(scheduled_recipe.recipe_id),
            "recipe_version_id": str(scheduled_recipe.recipe_version_id),
            "diner_count": scheduled_recipe.diner_count,
            "attendance_mode": scheduled_recipe.attendance_mode,
            "consumption_percentage": _canonical_decimal_string(
                scheduled_recipe.consumption_percentage
            ),
            "selected_scale_amount": _canonical_decimal_string(
                scheduled_recipe.selected_scale_amount
            ),
            "scale_mode": scheduled_recipe.scale_mode,
            "note": scheduled_recipe.note,
            "position_key": scheduled_recipe.position_key,
            "retired_at": (
                scheduled_recipe.retired_at.isoformat()
                if scheduled_recipe.retired_at is not None
                else None
            ),
            "retired_by_user_id": (
                str(scheduled_recipe.retired_by_user_id)
                if scheduled_recipe.retired_by_user_id is not None
                else None
            ),
            "created_by_user_id": str(scheduled_recipe.created_by_user_id),
        },
    )


async def update_event_base_attendance(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: UpdateEventBaseAttendanceCommand,
) -> UpdateEventBaseAttendanceResult:
    """Atomically update event attendance and every non-retired following scheduled recipe."""

    prepared = _prepare_attendance_command(command)
    request_hash = _attendance_request_hash(command)
    deferred_error: ApplicationServiceError | None = None
    result: UpdateEventBaseAttendanceResult | None = None

    async with session_factory() as session, session.begin():
        actor_role = await _authorize_member_and_lock_organization(
            session, context, prepared.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key("mutation", prepared.mutation_id)},
        )
        retained = await session.get(Mutation, prepared.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != UPDATE_BASE_ATTENDANCE_COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome in ("accepted", "partially_superseded"):
                return _retained_attendance_result(retained)
            if retained.outcome == "rejected":
                deferred_error = _retained_error(retained)
            else:
                raise RuntimeError("Event attendance retained an unsupported outcome")
        elif prepared.violations:
            deferred_error = _validation_error(prepared.violations)
            session.add(
                _mutation(
                    command=prepared,
                    context=context,
                    actor_role=actor_role,
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error_payload(deferred_error),
                )
            )
        elif prepared.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            deferred_error = ApplicationServiceError(
                "client_time_too_far_ahead", retry_same_identity=False
            )
            session.add(
                _mutation(
                    command=prepared,
                    context=context,
                    actor_role=actor_role,
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error_payload(deferred_error),
                )
            )
        else:
            event = await session.scalar(
                select(Event)
                .where(
                    Event.id == prepared.event_id,
                    Event.organization_id == prepared.organization_id,
                )
                .with_for_update(of=Event)
            )
            if event is None:
                deferred_error = _validation_error((FieldViolation("event_id", "not_found"),))
                session.add(
                    _mutation(
                        command=prepared,
                        context=context,
                        actor_role=actor_role,
                        request_hash=request_hash,
                        outcome="rejected",
                        outcome_payload=_error_payload(deferred_error),
                    )
                )
            elif event.lifecycle != "active":
                deferred_error = ApplicationServiceError(
                    "archived_event", retry_same_identity=False
                )
                session.add(
                    _mutation(
                        command=prepared,
                        context=context,
                        actor_role=actor_role,
                        request_hash=request_hash,
                        outcome="rejected",
                        outcome_payload=_error_payload(deferred_error),
                    )
                )
            else:
                clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == prepared.organization_id,
                        FieldClock.entity_kind == "event",
                        FieldClock.entity_id == event.id,
                        FieldClock.field_name == "base_expected_attendance",
                    )
                    .with_for_update(of=FieldClock)
                )
                following_recipes = tuple(
                    (
                        await session.execute(
                            select(ScheduledRecipe)
                            .where(
                                ScheduledRecipe.event_id == event.id,
                                ScheduledRecipe.organization_id == prepared.organization_id,
                                ScheduledRecipe.attendance_mode == "follows_event",
                                ScheduledRecipe.retired_at.is_(None),
                            )
                            .order_by(ScheduledRecipe.id)
                            .with_for_update(of=ScheduledRecipe)
                        )
                    ).scalars()
                )
                clock_wins = _field_clock_wins(clock, prepared)
                if clock_wins:
                    event.base_expected_attendance = prepared.base_expected_attendance
                    for scheduled_recipe in following_recipes:
                        scheduled_recipe.diner_count = prepared.base_expected_attendance
                    if clock is None:
                        clock = FieldClock(
                            organization_id=prepared.organization_id,
                            entity_kind="event",
                            entity_id=event.id,
                            field_name="base_expected_attendance",
                            winning_client_wall_time=prepared.client_wall_time,
                            winning_mutation_id=prepared.mutation_id,
                        )
                        session.add(clock)
                    else:
                        clock.winning_client_wall_time = prepared.client_wall_time
                        clock.winning_mutation_id = prepared.mutation_id
                    change_records = (
                        _event_change_record(event, clock),
                        *(_scheduled_recipe_change_record(recipe) for recipe in following_recipes),
                    )
                    outcome: Literal["accepted", "partially_superseded"] = "accepted"
                else:
                    # Publish the canonical event record so an outbox reconciliation has
                    # a concrete field value even though this action lost the LWW race.
                    following_recipes = ()
                    change_records = (_event_change_record(event, clock),)
                    outcome = "partially_superseded"
                first_change_sequence, last_change_sequence = await _reserve_change_range(
                    session,
                    prepared.organization_id,
                    prepared.mutation_id,
                    len(change_records),
                )
                result = UpdateEventBaseAttendanceResult(
                    mutation_id=prepared.mutation_id,
                    event_id=event.id,
                    organization_id=prepared.organization_id,
                    base_expected_attendance=event.base_expected_attendance,
                    updated_scheduled_recipe_ids=tuple(recipe.id for recipe in following_recipes),
                    first_change_sequence=first_change_sequence,
                    last_change_sequence=last_change_sequence,
                    replayed=False,
                    outcome=outcome,
                )
                session.add_all(
                    OrganizationChange(
                        organization_id=prepared.organization_id,
                        sequence=first_change_sequence + index,
                        mutation_id=prepared.mutation_id,
                        entity_id=entity_id,
                        entity_kind=entity_kind,
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": record},
                    )
                    for index, (entity_kind, entity_id, record) in enumerate(change_records)
                )
                session.add(
                    _mutation(
                        command=prepared,
                        context=context,
                        actor_role=actor_role,
                        request_hash=request_hash,
                        outcome=outcome,
                        outcome_payload=_attendance_result_payload(result),
                        first_change_sequence=first_change_sequence,
                        last_change_sequence=last_change_sequence,
                    )
                )

    if deferred_error is not None:
        raise deferred_error
    if result is None:
        raise RuntimeError("Event attendance update produced no outcome")
    return result
