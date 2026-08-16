import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from iso4217 import Currency
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.persistence.models import (
    ClientInstallation,
    DietaryTag,
    Mutation,
    Organization,
    OrganizationMealRolePreset,
    SystemRoleAssignment,
    User,
)

COMMAND_KIND = "organization.create"
COMMAND_SCHEMA_VERSION = 1
MEAL_ROLE_PRESETS = (
    ("meal_role.breakfast", "a"),
    ("meal_role.morning_snack", "b"),
    ("meal_role.soup", "c"),
    ("meal_role.lunch", "d"),
    ("meal_role.afternoon_snack", "e"),
    ("meal_role.dinner", "f"),
)
DIETARY_TAG_SEEDS = ("vegetarian", "vegan", "gluten", "lactose")


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Trusted authentication and client attribution supplied by an adapter."""

    actor_user_id: UUID
    client_installation_id: UUID
    oauth_client_id: str | None = None
    oauth_grant_id: str | None = None

    def __post_init__(self) -> None:
        oauth_values = (self.oauth_client_id, self.oauth_grant_id)
        if (oauth_values[0] is None) != (oauth_values[1] is None):
            raise ValueError("OAuth client and grant attribution must be provided together")
        if any(
            value is not None and (not value.strip() or value != value.strip())
            for value in oauth_values
        ):
            raise ValueError("OAuth attribution must be nonblank and trimmed")


@dataclass(frozen=True, slots=True)
class CreateOrganizationCommand:
    mutation_id: UUID
    organization_id: UUID
    name: str
    client_wall_time: datetime
    default_currency: str = "CZK"
    description: str | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class MealRolePresetResult:
    id: UUID
    translation_key: str
    position_key: str


@dataclass(frozen=True, slots=True)
class DietaryTagResult:
    id: UUID
    seed_key: str


@dataclass(frozen=True, slots=True)
class CreateOrganizationResult:
    mutation_id: UUID
    organization_id: UUID
    name: str
    description: str | None
    default_currency: str
    meal_role_presets: tuple[MealRolePresetResult, ...]
    dietary_tags: tuple[DietaryTagResult, ...]
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


@dataclass(frozen=True, slots=True)
class OrganizationLifecycleResult:
    organization_id: UUID
    name: str
    description: str | None
    default_currency: str
    retired_at: datetime | None
    retired_by_user_id: UUID | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class SetOrganizationLifecycleCommand:
    mutation_id: UUID
    organization_id: UUID
    operation: Literal["retire", "restore"]
    client_wall_time: datetime


@dataclass(frozen=True, slots=True)
class FieldViolation:
    path: str
    code: str


class ApplicationServiceError(Exception):
    def __init__(
        self,
        code: Literal[
            "archived_event",
            "client_time_too_far_ahead",
            "forbidden",
            "idempotency_mismatch",
            "stale_precondition",
            "validation_failed",
        ],
        *,
        field_violations: tuple[FieldViolation, ...] = (),
        retry_same_identity: bool,
        replayed: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.field_violations = field_violations
        self.retry_same_identity = retry_same_identity
        self.replayed = replayed


@dataclass(frozen=True, slots=True)
class _PreparedCommand:
    mutation_id: UUID
    organization_id: UUID
    name: str
    client_wall_time: datetime
    default_currency: str
    description: str | None
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


def _prepare_command(command: CreateOrganizationCommand) -> _PreparedCommand:
    name = unicodedata.normalize("NFC", command.name).strip()
    currency = command.default_currency.strip().upper()
    description = (
        unicodedata.normalize("NFC", command.description).replace("\r\n", "\n").replace("\r", "\n")
        if command.description is not None
        else None
    )
    violations: list[FieldViolation] = []
    if not name or len(name) > 200:
        violations.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
    if currency not in Currency.__members__:
        violations.append(FieldViolation("default_currency", "must_be_iso_4217_code"))
    wall_time_has_timezone = (
        command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not wall_time_has_timezone:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    return _PreparedCommand(
        mutation_id=command.mutation_id,
        organization_id=command.organization_id,
        name=name,
        client_wall_time=(
            command.client_wall_time.astimezone(UTC)
            if wall_time_has_timezone
            else command.client_wall_time.replace(tzinfo=UTC)
        ),
        default_currency=currency,
        description=description,
        logical_operation_id=command.logical_operation_id,
        violations=tuple(violations),
    )


def _request_hash(command: _PreparedCommand) -> bytes:
    semantic_request = {
        "client_wall_time": command.client_wall_time.isoformat().replace("+00:00", "Z"),
        "command_kind": COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "default_currency": command.default_currency,
        "description": command.description,
        "logical_operation_id": (
            str(command.logical_operation_id) if command.logical_operation_id else None
        ),
        "name": command.name,
        "organization_id": str(command.organization_id),
    }
    encoded = json.dumps(
        semantic_request,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).digest()


def _advisory_lock_key(namespace: str, identity: UUID) -> int:
    digest = hashlib.sha256(f"{namespace}:{identity}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _authorize(
    session: AsyncSession, context: ExecutionContext, *, require_installation: bool = True
) -> None:
    expected_installation_kind = "agent" if context.oauth_client_id is not None else "browser"
    statement = (
        select(User.id)
        .join(
            SystemRoleAssignment,
            SystemRoleAssignment.user_id == User.id,
        )
        .where(
            User.id == context.actor_user_id,
            User.disabled_at.is_(None),
            SystemRoleAssignment.role == "system_admin",
            SystemRoleAssignment.revoked_at.is_(None),
        )
    )
    if require_installation:
        statement = statement.join(
            ClientInstallation,
            (ClientInstallation.user_id == User.id)
            & (ClientInstallation.id == context.client_installation_id),
        ).where(
            ClientInstallation.disabled_at.is_(None),
            ClientInstallation.installation_kind == expected_installation_kind,
        )
    lock_targets = (User, SystemRoleAssignment, ClientInstallation) if require_installation else (
        User,
        SystemRoleAssignment,
    )
    authorized_actor = await session.scalar(
        statement.with_for_update(
            read=True,
            of=lock_targets,
        )
    )
    if authorized_actor is None:
        # Authority failures are deliberately not idempotency records. A retry must
        # re-evaluate a newly granted or revoked current role.
        raise ApplicationServiceError("forbidden", retry_same_identity=True)


def _outcome_payload(result: CreateOrganizationResult) -> dict[str, object]:
    return {
        "organization": {
            "id": str(result.organization_id),
            "name": result.name,
            "description": result.description,
            "default_currency": result.default_currency,
        },
        "meal_role_presets": [
            {
                "id": str(preset.id),
                "translation_key": preset.translation_key,
                "position_key": preset.position_key,
            }
            for preset in result.meal_role_presets
        ],
        "dietary_tags": [
            {"id": str(tag.id), "seed_key": tag.seed_key} for tag in result.dietary_tags
        ],
    }


def _required_str(values: dict[object, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise TypeError
    return value


def _retained_result(mutation: Mutation) -> CreateOrganizationResult:
    payload = mutation.outcome_payload
    if payload is None:
        raise RuntimeError("Accepted organization mutation has no outcome payload")
    organization = payload.get("organization")
    presets = payload.get("meal_role_presets")
    tags = payload.get("dietary_tags")
    if (
        not isinstance(organization, dict)
        or not isinstance(presets, list)
        or not isinstance(tags, list)
    ):
        raise RuntimeError("Accepted organization mutation has an invalid outcome payload")
    try:
        description = organization["description"]
        if description is not None and not isinstance(description, str):
            raise TypeError
        result_presets = tuple(
            MealRolePresetResult(
                id=UUID(_required_str(item, "id")),
                translation_key=_required_str(item, "translation_key"),
                position_key=_required_str(item, "position_key"),
            )
            for item in presets
            if isinstance(item, dict)
        )
        result_tags = tuple(
            DietaryTagResult(
                id=UUID(_required_str(item, "id")),
                seed_key=_required_str(item, "seed_key"),
            )
            for item in tags
            if isinstance(item, dict)
        )
        if len(result_presets) != len(presets) or len(result_tags) != len(tags):
            raise TypeError
        return CreateOrganizationResult(
            mutation_id=mutation.id,
            organization_id=UUID(_required_str(organization, "id")),
            name=_required_str(organization, "name"),
            description=description,
            default_currency=_required_str(organization, "default_currency"),
            meal_role_presets=result_presets,
            dietary_tags=result_tags,
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "Accepted organization mutation has an invalid outcome payload"
        ) from error


def _validation_error(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed",
        field_violations=violations,
        retry_same_identity=False,
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


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload
    error = payload.get("error") if payload is not None else None
    if not isinstance(error, dict):
        raise RuntimeError("Rejected organization mutation has an invalid outcome payload")
    try:
        if _required_str(error, "code") != "validation_failed":
            raise TypeError
        raw_violations = error.get("field_violations")
        if not isinstance(raw_violations, list):
            raise TypeError
        violations = tuple(
            FieldViolation(
                path=_required_str(item, "path"),
                code=_required_str(item, "code"),
            )
            for item in raw_violations
            if isinstance(item, dict)
        )
        if len(violations) != len(raw_violations):
            raise TypeError
    except TypeError as error_value:
        raise RuntimeError(
            "Rejected organization mutation has an invalid outcome payload"
        ) from error_value
    return _validation_error(violations)


def _mutation(
    *,
    command: _PreparedCommand,
    context: ExecutionContext,
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
    outcome_payload: dict[str, object],
) -> Mutation:
    return Mutation(
        id=command.mutation_id,
        logical_operation_id=command.logical_operation_id,
        organization_id=None,
        is_system_administration_scope=True,
        actor_user_id=context.actor_user_id,
        actor_role="system_admin",
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=COMMAND_KIND,
        target_identities=[
            {"entity_kind": "organization", "entity_id": str(command.organization_id)}
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=outcome_payload,
    )


async def create_organization(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateOrganizationCommand,
) -> CreateOrganizationResult:
    """Create an organization and its built-in configuration atomically."""

    prepared = _prepare_command(command)
    request_hash = _request_hash(prepared)
    deferred_error: ApplicationServiceError | None = None
    result: CreateOrganizationResult | None = None

    async with session_factory() as session, session.begin():
        if not prepared.violations:
            await session.execute(
                insert(ClientInstallation)
                .values(
                    id=context.client_installation_id,
                    user_id=context.actor_user_id,
                    installation_kind=(
                        "agent" if context.oauth_client_id is not None else "browser"
                    ),
                )
                .on_conflict_do_nothing(index_elements=("id",))
            )
        await _authorize(session, context, require_installation=not prepared.violations)

        # The lock is acquired before a separate SELECT. Under PostgreSQL's default
        # READ COMMITTED isolation, a waiter therefore observes the winner's commit.
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
                raise ApplicationServiceError(
                    "idempotency_mismatch",
                    retry_same_identity=False,
                )
            if retained.outcome == "accepted":
                return _retained_result(retained)
            if retained.outcome == "rejected":
                deferred_error = _retained_error(retained)
            else:
                raise RuntimeError("Organization creation retained an unsupported outcome")

        elif prepared.violations:
            deferred_error = _validation_error(prepared.violations)
            session.add(
                _mutation(
                    command=prepared,
                    context=context,
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error_payload(deferred_error),
                )
            )

        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _advisory_lock_key("organization", prepared.organization_id)},
            )
            organization_exists = await session.scalar(
                select(Organization.id).where(Organization.id == prepared.organization_id)
            )
            if organization_exists is not None:
                deferred_error = _validation_error(
                    (FieldViolation("organization_id", "already_exists"),)
                )
                session.add(
                    _mutation(
                        command=prepared,
                        context=context,
                        request_hash=request_hash,
                        outcome="rejected",
                        outcome_payload=_error_payload(deferred_error),
                    )
                )
            else:
                preset_results = tuple(
                    MealRolePresetResult(uuid4(), translation_key, position_key)
                    for translation_key, position_key in MEAL_ROLE_PRESETS
                )
                tag_results = tuple(
                    DietaryTagResult(uuid4(), seed_key) for seed_key in DIETARY_TAG_SEEDS
                )
                result = CreateOrganizationResult(
                    mutation_id=prepared.mutation_id,
                    organization_id=prepared.organization_id,
                    name=prepared.name,
                    description=prepared.description,
                    default_currency=prepared.default_currency,
                    meal_role_presets=preset_results,
                    dietary_tags=tag_results,
                    replayed=False,
                )

                session.add(
                    Organization(
                        id=prepared.organization_id,
                        name=prepared.name,
                        description=prepared.description,
                        default_currency=prepared.default_currency,
                        created_by_user_id=context.actor_user_id,
                    )
                )
                # These persistence models intentionally expose no ORM relationships,
                # so make the parent row visible before flushing its FK children.
                await session.flush()
                session.add_all(
                    OrganizationMealRolePreset(
                        id=preset.id,
                        organization_id=prepared.organization_id,
                        built_in_translation_key=preset.translation_key,
                        position_key=preset.position_key,
                        created_by_user_id=context.actor_user_id,
                    )
                    for preset in preset_results
                )
                session.add_all(
                    DietaryTag(
                        id=tag.id,
                        organization_id=prepared.organization_id,
                        seed_key=tag.seed_key,
                        created_by_user_id=context.actor_user_id,
                    )
                    for tag in tag_results
                )
                session.add(
                    _mutation(
                        command=prepared,
                        context=context,
                        request_hash=request_hash,
                        outcome="accepted",
                        outcome_payload=_outcome_payload(result),
                    )
                )

    if deferred_error is not None:
        raise deferred_error
    if result is None:
        raise RuntimeError("Organization creation produced no outcome")
    return result


def _lifecycle_hash_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        try:
            return value.isoformat()
        except Exception:
            pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"invalid_type": type(value).__qualname__}


def _lifecycle_request_hash(command: SetOrganizationLifecycleCommand) -> bytes:
    payload = {
        "client_wall_time": _lifecycle_hash_value(command.client_wall_time),
        "command_kind": "organization.lifecycle",
        "command_schema_version": 1,
        "operation": _lifecycle_hash_value(command.operation),
        "organization_id": _lifecycle_hash_value(command.organization_id),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()


def _lifecycle_result(mutation: Mutation, *, replayed: bool) -> OrganizationLifecycleResult:
    payload = mutation.outcome_payload
    record = payload.get("organization") if payload is not None else None
    if not isinstance(record, dict):
        raise RuntimeError("Accepted organization lifecycle mutation has invalid outcome payload")
    try:
        retired_at = record.get("retired_at")
        retired_by = record.get("retired_by_user_id")
        return OrganizationLifecycleResult(
            UUID(_required_str(record, "id")),
            _required_str(record, "name"),
            record.get("description") if isinstance(record.get("description"), str) else None,
            _required_str(record, "default_currency"),
            datetime.fromisoformat(retired_at) if isinstance(retired_at, str) else None,
            UUID(retired_by) if isinstance(retired_by, str) else None,
            replayed,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "Accepted organization lifecycle mutation has invalid outcome payload"
        ) from error


def _lifecycle_mutation(
    command: SetOrganizationLifecycleCommand,
    context: ExecutionContext,
    request_hash: bytes,
    payload: dict[str, object],
    *,
    outcome: Literal["accepted", "rejected"] = "accepted",
) -> Mutation:
    organization_id = (
        command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
    )
    wall_time = command.client_wall_time
    try:
        wall_time = (
            wall_time.astimezone(UTC)
            if isinstance(wall_time, datetime)
            and wall_time.tzinfo is not None
            and wall_time.utcoffset() is not None
            else datetime(1970, 1, 1, tzinfo=UTC)
        )
    except Exception:
        wall_time = datetime(1970, 1, 1, tzinfo=UTC)
    return Mutation(
        id=command.mutation_id,
        logical_operation_id=None,
        organization_id=None,
        is_system_administration_scope=True,
        actor_user_id=context.actor_user_id,
        actor_role="system_admin",
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=wall_time,
        command_schema_version=1,
        command_kind="organization.lifecycle",
        target_identities=[{"entity_kind": "organization", "entity_id": str(organization_id)}],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
    )


async def change_organization_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetOrganizationLifecycleCommand,
) -> OrganizationLifecycleResult:
    """Retire or restore an organization as a system-administration mutation."""
    violations: list[FieldViolation] = []
    if not isinstance(command.mutation_id, UUID):
        violations.append(FieldViolation("mutation_id", "must_be_uuid"))
    if not isinstance(command.organization_id, UUID):
        violations.append(FieldViolation("organization_id", "must_be_uuid"))
    if not isinstance(command.operation, str) or command.operation not in ("retire", "restore"):
        violations.append(FieldViolation("operation", "must_be_retire_or_restore"))
    try:
        wall_time_has_timezone = (
            isinstance(command.client_wall_time, datetime)
            and command.client_wall_time.tzinfo is not None
            and command.client_wall_time.utcoffset() is not None
        )
    except Exception:
        wall_time_has_timezone = False
    if not wall_time_has_timezone:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    request_hash = _lifecycle_request_hash(command)
    if not isinstance(command.mutation_id, UUID):
        raise _validation_error(tuple(violations))
    async with session_factory() as session, session.begin():
        await session.execute(
            insert(ClientInstallation)
            .values(
                id=context.client_installation_id,
                user_id=context.actor_user_id,
                installation_kind="agent" if context.oauth_client_id is not None else "browser",
            )
            .on_conflict_do_nothing(index_elements=("id",))
        )
        await _authorize(session, context)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id)
        deferred_error: ApplicationServiceError | None = None
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != "organization.lifecycle"
                or retained.command_schema_version != 1
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "accepted":
                return _lifecycle_result(retained, replayed=True)
            if retained.outcome == "rejected":
                deferred_error = _retained_error(retained)
            else:
                raise RuntimeError("Organization lifecycle retained an unsupported outcome")
        elif violations:
            deferred_error = _validation_error(tuple(violations))
            session.add(
                _lifecycle_mutation(
                    command,
                    context,
                    request_hash,
                    _error_payload(deferred_error),
                    outcome="rejected",
                )
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _advisory_lock_key("organization", command.organization_id)},
            )
            organization = await session.scalar(
                select(Organization)
                .where(Organization.id == command.organization_id)
                .with_for_update()
            )
            if organization is None:
                deferred_error = _validation_error(
                    (FieldViolation("organization_id", "not_found"),)
                )
                session.add(
                    _lifecycle_mutation(
                        command,
                        context,
                        request_hash,
                        _error_payload(deferred_error),
                        outcome="rejected",
                    )
                )
            else:
                if command.operation == "retire":
                    organization.retired_at = datetime.now(UTC)
                    organization.retired_by_user_id = context.actor_user_id
                else:
                    organization.retired_at = None
                    organization.retired_by_user_id = None
                record = {
                    "id": str(organization.id),
                    "name": organization.name,
                    "description": organization.description,
                    "default_currency": organization.default_currency,
                    "retired_at": (
                        organization.retired_at.isoformat() if organization.retired_at else None
                    ),
                    "retired_by_user_id": (
                        str(organization.retired_by_user_id)
                        if organization.retired_by_user_id
                        else None
                    ),
                }
                session.add(
                    _lifecycle_mutation(command, context, request_hash, {"organization": record})
                )
                return _lifecycle_result(
                    Mutation(id=command.mutation_id, outcome_payload={"organization": record}),
                    replayed=False,
                )
    if deferred_error is not None:
        raise deferred_error
    raise RuntimeError("Organization lifecycle produced no outcome")


async def list_organizations_for_system_admin(
    session_factory: async_sessionmaker[AsyncSession], context: ExecutionContext
) -> tuple[OrganizationLifecycleResult, ...]:
    async with session_factory() as session, session.begin():
        await _authorize(session, context, require_installation=False)
        rows = await session.scalars(
            select(Organization).order_by(Organization.name, Organization.id)
        )
        return tuple(
            OrganizationLifecycleResult(
                item.id, item.name, item.description, item.default_currency,
                item.retired_at, item.retired_by_user_id, False,
            )
            for item in rows
        )
