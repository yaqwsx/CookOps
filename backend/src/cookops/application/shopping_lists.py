"""Materialize immutable revisions and operate a shopping list."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.engine import RowMapping
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
    AdHocShoppingItem,
    Event,
    EventDay,
    EventIngredientPrice,
    EventIngredientPriceSnapshot,
    EventMealRole,
    FieldClock,
    IngredientVersion,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    RecipeVersion,
    RecipeVersionIngredientLine,
    ScheduledIngredientOverride,
    ScheduledRecipe,
    ShoppingContribution,
    ShoppingContributionSnapshot,
    ShoppingGenerationRevision,
    ShoppingIngredientRow,
    ShoppingList,
    ShoppingRevisionSource,
    StoreSection,
    SystemRoleAssignment,
    UnitDefinition,
    User,
)

COMMAND_KIND = "shopping_list.create"
COMMAND_SCHEMA_VERSION = 1
MAX_SERIALIZED_NAME_BYTES = 800


def _canonical_row_note(value: str | None) -> str | None:
    if value is None:
        return None
    note = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return note or None


def _row_note_hash_value(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return _invalid(value)
    note = _canonical_row_note(value)
    if note is None:
        return None
    if "\x00" in note or any(0xD800 <= ord(char) <= 0xDFFF for char in note):
        return _invalid(note)
    if len(note) > 4000:
        return _invalid(note)
    return note


REFRESH_COMMAND_KIND = "shopping_list.refresh"
RENAME_COMMAND_KIND = "shopping_list.rename"
_REVISION_SOURCE_ID_NAMESPACE = UUID("df740018-c0d2-4790-a314-cf4180a1c2c9")


class ShoppingListQueryDenied(PermissionError):
    """The current actor may not inspect the requested shopping-list scope."""


class ShoppingListQueryNotFound(LookupError):
    """A shopping-list resource is absent or outside its declared scope."""


@dataclass(frozen=True, slots=True)
class ShoppingListSummary:
    """Compact, materialized-shopping metadata safe for an event list screen."""

    id: UUID
    organization_id: UUID
    event_id: UUID
    name: str
    current_generation_revision_id: UUID | None
    generated_at: datetime | None
    source_scheduled_recipe_count: int
    ingredient_row_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ShoppingListSummaryPage:
    summaries: tuple[ShoppingListSummary, ...]
    has_more: bool


class ShoppingListQueryService:
    """Read materialized shopping-list summaries without exposing ORM records."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_summaries(
        self,
        *,
        actor_user_id: UUID,
        organization_id: UUID,
        event_id: UUID,
        limit: int,
        before_created_at: datetime | None = None,
        before_id: UUID | None = None,
    ) -> ShoppingListSummaryPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if (before_created_at is None) != (before_id is None):
            raise ValueError("cursor fields must be supplied together")
        async with self._session_factory() as session:
            await self._authorize_read(session, actor_user_id, organization_id)
            event_exists = await session.scalar(
                select(Event.id).where(
                    Event.id == event_id,
                    Event.organization_id == organization_id,
                )
            )
            if event_exists is None:
                raise ShoppingListQueryNotFound
            statement = self._summary_statement().where(
                ShoppingList.organization_id == organization_id,
                ShoppingList.event_id == event_id,
            )
            if before_created_at is not None and before_id is not None:
                statement = statement.where(
                    or_(
                        ShoppingList.created_at < before_created_at,
                        (ShoppingList.created_at == before_created_at)
                        & (ShoppingList.id < before_id),
                    )
                )
            rows = (await session.execute(statement.limit(limit + 1))).mappings().all()
        return ShoppingListSummaryPage(
            summaries=tuple(self._summary_from_row(row) for row in rows[:limit]),
            has_more=len(rows) > limit,
        )

    async def get_summary(
        self,
        *,
        actor_user_id: UUID,
        organization_id: UUID,
        event_id: UUID,
        shopping_list_id: UUID,
    ) -> ShoppingListSummary:
        async with self._session_factory() as session:
            await self._authorize_read(session, actor_user_id, organization_id)
            row = (
                (
                    await session.execute(
                        self._summary_statement().where(
                            ShoppingList.id == shopping_list_id,
                            ShoppingList.organization_id == organization_id,
                            ShoppingList.event_id == event_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ShoppingListQueryNotFound
        return self._summary_from_row(row)

    @staticmethod
    def _summary_statement() -> Select[
        tuple[UUID, UUID, UUID, str, UUID | None, datetime | None, int, int, datetime]
    ]:
        source_count = (
            select(func.count())
            .select_from(ShoppingRevisionSource)
            .where(
                ShoppingRevisionSource.generation_revision_id
                == ShoppingList.current_generation_revision_id
            )
            .scalar_subquery()
        )
        row_count = (
            select(func.count())
            .select_from(ShoppingIngredientRow)
            .where(ShoppingIngredientRow.shopping_list_id == ShoppingList.id)
            .scalar_subquery()
        )
        return (
            select(
                ShoppingList.id,
                ShoppingList.organization_id,
                ShoppingList.event_id,
                ShoppingList.name,
                ShoppingList.current_generation_revision_id,
                ShoppingGenerationRevision.generated_at,
                source_count.label("source_scheduled_recipe_count"),
                row_count.label("ingredient_row_count"),
                ShoppingList.created_at,
            )
            .outerjoin(
                ShoppingGenerationRevision,
                ShoppingGenerationRevision.id == ShoppingList.current_generation_revision_id,
            )
            .order_by(ShoppingList.created_at.desc(), ShoppingList.id.desc())
        )

    @staticmethod
    def _summary_from_row(values: RowMapping) -> ShoppingListSummary:
        return ShoppingListSummary(
            id=cast(UUID, values["id"]),
            organization_id=cast(UUID, values["organization_id"]),
            event_id=cast(UUID, values["event_id"]),
            name=cast(str, values["name"]),
            current_generation_revision_id=cast(
                UUID | None, values["current_generation_revision_id"]
            ),
            generated_at=cast(datetime | None, values["generated_at"]),
            source_scheduled_recipe_count=cast(int, values["source_scheduled_recipe_count"]),
            ingredient_row_count=cast(int, values["ingredient_row_count"]),
            created_at=cast(datetime, values["created_at"]),
        )

    @staticmethod
    async def _authorize_read(
        session: AsyncSession, actor_user_id: UUID, organization_id: UUID
    ) -> None:
        actor = await session.scalar(
            select(User.id).where(User.id == actor_user_id, User.disabled_at.is_(None))
        )
        organization = await session.scalar(
            select(Organization.id).where(
                Organization.id == organization_id, Organization.retired_at.is_(None)
            )
        )
        if actor is None or organization is None:
            raise ShoppingListQueryDenied
        system_admin = await session.scalar(
            select(SystemRoleAssignment.id).where(
                SystemRoleAssignment.user_id == actor_user_id,
                SystemRoleAssignment.role == "system_admin",
                SystemRoleAssignment.revoked_at.is_(None),
            )
        )
        if system_admin is not None:
            return
        membership = await session.scalar(
            select(OrganizationMembership.id).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == actor_user_id,
                OrganizationMembership.state == "active",
                OrganizationMembership.role.in_(("member", "organization_admin")),
            )
        )
        if membership is None:
            raise ShoppingListQueryDenied


@dataclass(frozen=True, slots=True)
class CreateShoppingListCommand:
    mutation_id: UUID
    shopping_list_id: UUID
    generation_revision_id: UUID
    organization_id: UUID
    event_id: UUID
    name: str
    scheduled_recipe_ids: tuple[UUID, ...]
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CreateShoppingListResult:
    mutation_id: UUID
    shopping_list_id: UUID
    generation_revision_id: UUID
    organization_id: UUID
    event_id: UUID
    name: str
    scheduled_recipe_ids: tuple[UUID, ...]
    ingredient_row_ids: tuple[UUID, ...]
    contribution_ids: tuple[UUID, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


@dataclass(frozen=True, slots=True)
class RenameShoppingListCommand:
    mutation_id: UUID
    shopping_list_id: UUID
    organization_id: UUID
    name: str
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RenameShoppingListResult:
    mutation_id: UUID
    shopping_list_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


@dataclass(frozen=True, slots=True)
class _Prepared:
    mutation_id: UUID
    shopping_list_id: UUID
    generation_revision_id: UUID
    organization_id: UUID
    event_id: UUID
    name: str
    scheduled_recipe_ids: tuple[UUID, ...]
    client_wall_time: datetime
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedLine:
    ingredient_id: UUID
    ingredient_version_id: UUID
    ingredient_name: str
    calculation_unit_id: UUID
    default_store_section_id: UUID | None
    default_store_section_name: str | None
    quantity: Decimal
    note: str | None


@dataclass(frozen=True, slots=True)
class _Source:
    scheduled_recipe_id: UUID
    recipe_name: str
    recipe_description: str | None
    calendar_date: str
    meal_role: str
    selected_scale_amount: Decimal
    recipe_base_scaling_amount: Decimal


def _canonical_decimal(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _canonical_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _invalid(value: object) -> dict[str, str]:
    return {"invalid_type": type(value).__qualname__, "repr": repr(value)}


def _raw_uuid(value: object) -> str | dict[str, str]:
    return str(value) if isinstance(value, UUID) else _invalid(value)


def _raw_time(value: object) -> str | dict[str, str]:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _invalid(value)


def _raw_source_ids(value: object) -> object:
    if not isinstance(value, tuple):
        return _invalid(value)
    encoded = [_raw_uuid(item) for item in value]
    if all(isinstance(item, str) for item in encoded):
        return sorted(str(item) for item in encoded)
    return encoded


def _hash(command: CreateShoppingListCommand) -> bytes:
    value = {
        "command_kind": COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "mutation_id": _raw_uuid(command.mutation_id),
        "shopping_list_id": _raw_uuid(command.shopping_list_id),
        "generation_revision_id": _raw_uuid(command.generation_revision_id),
        "organization_id": _raw_uuid(command.organization_id),
        "event_id": _raw_uuid(command.event_id),
        "name": _canonical_name(command.name)
        if isinstance(command.name, str)
        else _invalid(command.name),
        "scheduled_recipe_ids": _raw_source_ids(command.scheduled_recipe_ids),
        "client_wall_time": _raw_time(command.client_wall_time),
        "logical_operation_id": (
            _raw_uuid(command.logical_operation_id)
            if command.logical_operation_id is not None
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _prepare(command: CreateShoppingListCommand) -> _Prepared:
    violations: list[FieldViolation] = []
    values = (
        ("mutation_id", command.mutation_id),
        ("shopping_list_id", command.shopping_list_id),
        ("generation_revision_id", command.generation_revision_id),
        ("organization_id", command.organization_id),
        ("event_id", command.event_id),
    )
    for path, value in values:
        if not isinstance(value, UUID):
            violations.append(FieldViolation(path, "must_be_uuid"))
    name = _canonical_name(command.name) if isinstance(command.name, str) else ""
    if (
        not isinstance(command.name, str)
        or not name
        or len(name) > 200
        or len(json.dumps(name, ensure_ascii=False).encode()) > MAX_SERIALIZED_NAME_BYTES
    ):
        violations.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
    if not isinstance(command.scheduled_recipe_ids, tuple):
        violations.append(FieldViolation("scheduled_recipe_ids", "must_be_uuid_tuple"))
        source_ids: tuple[UUID, ...] = ()
    else:
        source_ids = tuple(item for item in command.scheduled_recipe_ids if isinstance(item, UUID))
        if len(source_ids) != len(command.scheduled_recipe_ids):
            violations.append(FieldViolation("scheduled_recipe_ids", "must_contain_only_uuids"))
        if len(set(source_ids)) != len(source_ids):
            violations.append(FieldViolation("scheduled_recipe_ids", "must_not_contain_duplicates"))
    has_time = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not has_time:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    return _Prepared(
        mutation_id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        shopping_list_id=command.shopping_list_id
        if isinstance(command.shopping_list_id, UUID)
        else UUID(int=0),
        generation_revision_id=command.generation_revision_id
        if isinstance(command.generation_revision_id, UUID)
        else UUID(int=0),
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        event_id=command.event_id if isinstance(command.event_id, UUID) else UUID(int=0),
        name=name,
        scheduled_recipe_ids=source_ids,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if has_time
        else datetime(1970, 1, 1, tzinfo=UTC),
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        violations=tuple(violations),
    )


def _error(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
    )


def _error_payload(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": item.path, "code": item.code} for item in error.field_violations
            ],
        }
    }


def _mutation(
    prepared: _Prepared,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=prepared.mutation_id,
        logical_operation_id=prepared.logical_operation_id,
        organization_id=prepared.organization_id,
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=prepared.client_wall_time,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=COMMAND_KIND,
        target_identities=[
            {"entity_kind": "event", "entity_id": str(prepared.event_id)},
            {"entity_kind": "shopping_list", "entity_id": str(prepared.shopping_list_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


def _payload(result: CreateShoppingListResult) -> dict[str, object]:
    return {
        "shopping_list": {
            "id": str(result.shopping_list_id),
            "generation_revision_id": str(result.generation_revision_id),
            "organization_id": str(result.organization_id),
            "event_id": str(result.event_id),
            "name": result.name,
            "scheduled_recipe_ids": [str(item) for item in result.scheduled_recipe_ids],
            "ingredient_row_ids": [str(item) for item in result.ingredient_row_ids],
            "contribution_ids": [str(item) for item in result.contribution_ids],
        }
    }


def _retained(mutation: Mutation) -> CreateShoppingListResult:
    try:
        payload = mutation.outcome_payload
        if payload is None:
            raise TypeError
        record = payload["shopping_list"]
        if (
            not isinstance(record, dict)
            or mutation.first_change_sequence is None
            or mutation.last_change_sequence is None
        ):
            raise TypeError
        return CreateShoppingListResult(
            mutation_id=mutation.id,
            shopping_list_id=UUID(str(record["id"])),
            generation_revision_id=UUID(str(record["generation_revision_id"])),
            organization_id=UUID(str(record["organization_id"])),
            event_id=UUID(str(record["event_id"])),
            name=str(record["name"]),
            scheduled_recipe_ids=tuple(UUID(str(item)) for item in record["scheduled_recipe_ids"]),
            ingredient_row_ids=tuple(UUID(str(item)) for item in record["ingredient_row_ids"]),
            contribution_ids=tuple(UUID(str(item)) for item in record["contribution_ids"]),
            first_change_sequence=mutation.first_change_sequence,
            last_change_sequence=mutation.last_change_sequence,
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Accepted shopping-list mutation has invalid outcome payload") from exc


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload
    try:
        error = payload["error"] if payload is not None else None
        violations = error["field_violations"] if isinstance(error, dict) else None
        if not isinstance(error, dict) or error.get("code") not in (
            "validation_failed",
            "archived_event",
            "client_time_too_far_ahead",
            "stale_precondition",
        ):
            raise TypeError
        if violations is None and error.get("code") in (
            "archived_event",
            "client_time_too_far_ahead",
            "stale_precondition",
        ):
            violations = []
        if not isinstance(violations, list):
            raise TypeError
        parsed = tuple(
            FieldViolation(item["path"], item["code"])
            for item in violations
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("code"), str)
        )
        if len(parsed) != len(violations):
            raise TypeError
        if error["code"] == "client_time_too_far_ahead":
            return ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        if error["code"] == "archived_event":
            return ApplicationServiceError("archived_event", retry_same_identity=False)
        if error["code"] == "stale_precondition":
            return ApplicationServiceError("stale_precondition", retry_same_identity=False)
        return _error(parsed)
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Rejected shopping-list mutation has invalid outcome payload") from exc


async def _resolve_source(
    session: AsyncSession,
    prepared: _Prepared | _RefreshSourceScope,
    source_id: UUID,
) -> tuple[_Source, list[_ResolvedLine]] | None:
    source = (
        await session.execute(
            select(ScheduledRecipe, RecipeVersion, EventDay, EventMealRole)
            .join(RecipeVersion, RecipeVersion.id == ScheduledRecipe.recipe_version_id)
            .join(EventDay, EventDay.id == ScheduledRecipe.event_day_id)
            .join(EventMealRole, EventMealRole.id == ScheduledRecipe.event_meal_role_id)
            .where(
                ScheduledRecipe.id == source_id,
                ScheduledRecipe.event_id == prepared.event_id,
                ScheduledRecipe.organization_id == prepared.organization_id,
                ScheduledRecipe.retired_at.is_(None),
            )
            .with_for_update(of=ScheduledRecipe)
        )
    ).one_or_none()
    if source is None:
        return None
    scheduled, recipe, day, role = source
    label = role.custom_name or role.built_in_translation_key or ""
    details = _Source(
        source_id,
        recipe.name,
        recipe.description,
        day.calendar_date.isoformat(),
        label,
        scheduled.selected_scale_amount,
        recipe.base_scaling_amount,
    )
    base_lines = list(
        (
            await session.scalars(
                select(RecipeVersionIngredientLine)
                .where(RecipeVersionIngredientLine.recipe_version_id == scheduled.recipe_version_id)
                .order_by(RecipeVersionIngredientLine.position_key)
                .with_for_update(of=RecipeVersionIngredientLine)
            )
        ).all()
    )
    overrides = list(
        (
            await session.scalars(
                select(ScheduledIngredientOverride)
                .where(
                    ScheduledIngredientOverride.scheduled_recipe_id == source_id,
                    ScheduledIngredientOverride.retired_at.is_(None),
                )
                .with_for_update(of=ScheduledIngredientOverride)
            )
        ).all()
    )
    replacements = {
        override.target_line_key: override
        for override in overrides
        if override.override_kind == "replace"
    }
    resolved: list[tuple[UUID, Decimal, str | None]] = []
    for line in base_lines:
        override = replacements.get(line.line_key)
        raw_quantity = (
            override.quantity
            if override is not None
            else line.base_quantity * scheduled.selected_scale_amount / recipe.base_scaling_amount
            if line.scaling_behavior == "proportional"
            else line.base_quantity
        )
        resolved.append(
            (
                line.ingredient_version_id,
                raw_quantity,
                override.note if override is not None else line.note,
            )
        )
    for override in overrides:
        if override.override_kind == "add":
            resolved.append(
                (
                    override.ingredient_version_id,
                    override.quantity,
                    override.note,
                )
            )
    version_ids = [version_id for version_id, *_ in resolved]
    versions = {
        item.id: item
        for item in (
            await session.scalars(
                select(IngredientVersion).where(IngredientVersion.id.in_(version_ids))
            )
        ).all()
    }
    sections = {
        item.id: item.name
        for item in (
            await session.scalars(
                select(StoreSection).where(
                    StoreSection.id.in_(
                        [
                            value.default_store_section_id
                            for value in versions.values()
                            if value.default_store_section_id is not None
                        ]
                    )
                )
            )
        ).all()
    }
    lines: list[_ResolvedLine] = []
    for version_id, quantity, note in resolved:
        version = versions.get(version_id)
        if version is None or quantity < 0:
            return None
        if quantity == 0:
            continue
        lines.append(
            _ResolvedLine(
                version.ingredient_id,
                version.id,
                version.name,
                version.canonical_unit_id,
                version.default_store_section_id,
                (
                    sections.get(version.default_store_section_id)
                    if version.default_store_section_id is not None
                    else None
                ),
                quantity,
                note,
            )
        )
    return details, lines


async def create_shopping_list(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateShoppingListCommand,
) -> CreateShoppingListResult:
    """Create a shopping aggregate and materialize its initial immutable revision."""
    prepared, request_hash = _prepare(command), _hash(command)
    error: ApplicationServiceError | None = None
    result: CreateShoppingListResult | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_member_and_lock_organization(
            session, context, prepared.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", prepared.mutation_id)},
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
                return _retained(retained)
            if retained.outcome == "rejected":
                error = _retained_error(retained)
            else:
                raise RuntimeError("Unsupported retained shopping mutation")
        elif prepared.violations:
            error = _error(prepared.violations)
            session.add(
                _mutation(prepared, context, role, request_hash, "rejected", _error_payload(error))
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("shopping_list", prepared.shopping_list_id)},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        "shopping_generation_revision", prepared.generation_revision_id
                    )
                },
            )
            collision = await session.scalar(
                select(ShoppingList.id).where(ShoppingList.id == prepared.shopping_list_id)
            )
            revision_collision = await session.scalar(
                select(ShoppingGenerationRevision.id).where(
                    ShoppingGenerationRevision.id == prepared.generation_revision_id
                )
            )
            event = await session.scalar(
                select(Event.id)
                .where(
                    Event.id == prepared.event_id,
                    Event.organization_id == prepared.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=Event)
            )
            materialized: list[tuple[_Source, list[_ResolvedLine]]] = []
            if collision is not None or revision_collision is not None or event is None:
                error = (
                    ApplicationServiceError("archived_event", retry_same_identity=False)
                    if event is None
                    else _error((FieldViolation("identity", "must_not_already_exist"),))
                )
            else:
                for source_id in prepared.scheduled_recipe_ids:
                    loaded = await _resolve_source(session, prepared, source_id)
                    if loaded is None:
                        error = _error(
                            (
                                FieldViolation(
                                    "scheduled_recipe_ids",
                                    "must_reference_active_recipes_from_event",
                                ),
                            )
                        )
                        break
                    materialized.append(loaded)
            if error is not None:
                session.add(
                    _mutation(
                        prepared, context, role, request_hash, "rejected", _error_payload(error)
                    )
                )
            else:
                rows: dict[UUID, ShoppingIngredientRow] = {}
                contributions: list[ShoppingContribution] = []
                snapshots: list[ShoppingContributionSnapshot] = []
                price_snapshots = await _event_price_snapshots(
                    session, prepared.organization_id, prepared.event_id
                )
                shopping_list = ShoppingList(
                    id=prepared.shopping_list_id,
                    organization_id=prepared.organization_id,
                    event_id=prepared.event_id,
                    name=prepared.name,
                    created_by_user_id=context.actor_user_id,
                )
                session.add(shopping_list)
                await session.flush()
                revision = ShoppingGenerationRevision(
                    id=prepared.generation_revision_id,
                    organization_id=prepared.organization_id,
                    event_id=prepared.event_id,
                    shopping_list_id=prepared.shopping_list_id,
                    generated_by_user_id=context.actor_user_id,
                )
                session.add(revision)
                await session.flush()
                revision_sources: list[ShoppingRevisionSource] = []
                for source, lines in materialized:
                    revision_source = ShoppingRevisionSource(
                        generation_revision_id=prepared.generation_revision_id,
                        shopping_list_id=prepared.shopping_list_id,
                        organization_id=prepared.organization_id,
                        event_id=prepared.event_id,
                        scheduled_recipe_id=source.scheduled_recipe_id,
                    )
                    revision_sources.append(revision_source)
                    session.add(revision_source)
                    grouped: dict[UUID, list[_ResolvedLine]] = defaultdict(list)
                    for line in lines:
                        grouped[line.ingredient_id].append(line)
                    for ingredient_id, items in grouped.items():
                        representative = items[-1]
                        row = rows.get(ingredient_id)
                        if row is None:
                            row = ShoppingIngredientRow(
                                id=uuid4(),
                                organization_id=prepared.organization_id,
                                event_id=prepared.event_id,
                                shopping_list_id=prepared.shopping_list_id,
                                ingredient_id=ingredient_id,
                                ingredient_name=representative.ingredient_name,
                                calculation_unit_id=representative.calculation_unit_id,
                                default_store_section_id=representative.default_store_section_id,
                                default_store_section_name=representative.default_store_section_name,
                                created_by_user_id=context.actor_user_id,
                            )
                            rows[ingredient_id] = row
                            session.add(row)
                            await session.flush()
                        quantity = sum((item.quantity for item in items), Decimal(0))
                        contribution = ShoppingContribution(
                            id=uuid4(),
                            organization_id=prepared.organization_id,
                            event_id=prepared.event_id,
                            shopping_list_id=prepared.shopping_list_id,
                            shopping_ingredient_row_id=row.id,
                            ingredient_id=ingredient_id,
                            scheduled_recipe_id=source.scheduled_recipe_id,
                        )
                        contributions.append(contribution)
                        session.add(contribution)
                        notes = [item.note for item in items if item.note]
                        snapshots.append(
                            ShoppingContributionSnapshot(
                                id=uuid4(),
                                organization_id=prepared.organization_id,
                                event_id=prepared.event_id,
                                shopping_list_id=prepared.shopping_list_id,
                                generation_revision_id=prepared.generation_revision_id,
                                shopping_contribution_id=contribution.id,
                                ingredient_id=ingredient_id,
                                active_in_revision=True,
                                generated_quantity=quantity,
                                **_captured_price(price_snapshots.get(ingredient_id)),
                                ingredient_version_id=representative.ingredient_version_id,
                                ingredient_name=representative.ingredient_name,
                                source_details={
                                    "recipe_name": source.recipe_name,
                                    "recipe_description": source.recipe_description,
                                    "day": source.calendar_date,
                                    "meal_role": source.meal_role,
                                    "selected_scale_amount": _canonical_decimal(
                                        source.selected_scale_amount
                                    ),
                                    "recipe_base_scaling_amount": _canonical_decimal(
                                        source.recipe_base_scaling_amount
                                    ),
                                    "line_notes": notes,
                                    "line_count": len(items),
                                },
                            )
                        )
                for snapshot in snapshots:
                    session.add(snapshot)
                shopping_list.current_generation_revision_id = prepared.generation_revision_id
                session.add(
                    FieldClock(
                        organization_id=prepared.organization_id,
                        entity_kind="shopping_list",
                        entity_id=shopping_list.id,
                        field_name="current_generation_revision_id",
                        winning_client_wall_time=prepared.client_wall_time,
                        winning_mutation_id=prepared.mutation_id,
                    )
                )
                await session.flush()
                records: list[tuple[str, UUID, dict[str, object]]] = [
                    _generation_revision_record(revision)
                ]
                records.extend([await _row_record(session, row) for row in rows.values()])
                records.extend(
                    [
                        await _contribution_record(session, contribution)
                        for contribution in contributions
                    ]
                )
                records.extend(_revision_source_record(source) for source in revision_sources)
                records.extend(_contribution_snapshot_record(snapshot) for snapshot in snapshots)
                records.append(
                    (
                        "shopping_list",
                        shopping_list.id,
                        await _shopping_list_record(session, shopping_list),
                    )
                )
                first, last = await _reserve_change_range(
                    session, prepared.organization_id, prepared.mutation_id, len(records)
                )
                result = CreateShoppingListResult(
                    prepared.mutation_id,
                    prepared.shopping_list_id,
                    prepared.generation_revision_id,
                    prepared.organization_id,
                    prepared.event_id,
                    prepared.name,
                    prepared.scheduled_recipe_ids,
                    tuple(row.id for row in rows.values()),
                    tuple(item.id for item in contributions),
                    first,
                    last,
                    False,
                )
                payload = _payload(result)
                session.add_all(
                    [
                        OrganizationChange(
                            organization_id=prepared.organization_id,
                            sequence=first + offset,
                            mutation_id=prepared.mutation_id,
                            entity_id=entity_id,
                            entity_kind=entity_kind,
                            operation="upsert",
                            payload={"record_schema_version": 1, "record": record},
                        )
                        for offset, (entity_kind, entity_id, record) in enumerate(records)
                    ]
                )
                session.add(
                    _mutation(
                        prepared, context, role, request_hash, "accepted", payload, first, last
                    )
                )
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("Shopping-list creation produced no outcome")
    return result


def _rename_hash(command: RenameShoppingListCommand) -> bytes:
    value = {
        "command_kind": RENAME_COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "mutation_id": _raw_uuid(command.mutation_id),
        "shopping_list_id": _raw_uuid(command.shopping_list_id),
        "organization_id": _raw_uuid(command.organization_id),
        "name": (
            _canonical_name(command.name)
            if isinstance(command.name, str)
            else _invalid(command.name)
        ),
        "client_wall_time": _raw_time(command.client_wall_time),
        "logical_operation_id": _raw_uuid(command.logical_operation_id)
        if command.logical_operation_id is not None
        else None,
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


async def rename_shopping_list(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: RenameShoppingListCommand,
) -> RenameShoppingListResult:
    """Rename an active shopping list through its LWW name field."""
    request_hash = _rename_hash(command)
    name = _canonical_name(command.name) if isinstance(command.name, str) else ""
    violations = [
        FieldViolation(field, "must_be_uuid")
        for field in ("mutation_id", "shopping_list_id", "organization_id")
        if not isinstance(getattr(command, field), UUID)
    ]
    if (
        not isinstance(command.name, str)
        or not name
        or len(name) > 200
        or len(json.dumps(name, ensure_ascii=False).encode()) > MAX_SERIALIZED_NAME_BYTES
    ):
        violations.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
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
    mutation_id = command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0)
    organization_id = (
        command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
    )
    when = (
        command.client_wall_time.astimezone(UTC)
        if not violations
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    error: ApplicationServiceError | None = None
    result: RenameShoppingListResult | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_member_and_lock_organization(session, context, organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", mutation_id)},
        )
        retained = await session.get(Mutation, mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != RENAME_COMMAND_KIND
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "rejected":
                error = _retained_error(retained)
            elif (
                retained.first_change_sequence is not None
                and retained.last_change_sequence is not None
            ):
                return RenameShoppingListResult(
                    command.mutation_id,
                    command.shopping_list_id,
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                    retained.outcome,
                )
            else:
                raise RuntimeError("Invalid retained shopping-list rename outcome")
        elif violations:
            error = _error(tuple(violations))
        elif when > datetime.now(UTC) + timedelta(hours=24):
            error = ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("shopping_list", command.shopping_list_id)},
            )
            shopping_list = await session.scalar(
                select(ShoppingList)
                .join(Event, Event.id == ShoppingList.event_id)
                .where(
                    ShoppingList.id == command.shopping_list_id,
                    ShoppingList.organization_id == command.organization_id,
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ShoppingList, Event))
            )
            if shopping_list is None:
                error = ApplicationServiceError("archived_event", retry_same_identity=False)
            else:
                clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == command.organization_id,
                        FieldClock.entity_kind == "shopping_list",
                        FieldClock.entity_id == shopping_list.id,
                        FieldClock.field_name == "name",
                    )
                    .with_for_update(of=FieldClock)
                )
                wins = clock is None or (when, command.mutation_id) > (
                    clock.winning_client_wall_time,
                    clock.winning_mutation_id,
                )
                if wins:
                    shopping_list.name = name
                    if clock is None:
                        session.add(
                            FieldClock(
                                organization_id=command.organization_id,
                                entity_kind="shopping_list",
                                entity_id=shopping_list.id,
                                field_name="name",
                                winning_client_wall_time=when,
                                winning_mutation_id=command.mutation_id,
                            )
                        )
                    else:
                        clock.winning_client_wall_time, clock.winning_mutation_id = (
                            when,
                            command.mutation_id,
                        )
                outcome = "accepted" if wins else "partially_superseded"
                await session.flush()
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                session.add(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first,
                        mutation_id=command.mutation_id,
                        entity_id=shopping_list.id,
                        entity_kind="shopping_list",
                        operation="upsert",
                        payload={
                            "record_schema_version": 1,
                            "record": await _shopping_list_record(session, shopping_list),
                        },
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
                        command_kind=RENAME_COMMAND_KIND,
                        target_identities=[
                            {"entity_kind": "shopping_list", "entity_id": str(shopping_list.id)}
                        ],
                        request_hash=request_hash,
                        outcome=outcome,
                        outcome_payload={"outcome": outcome},
                        first_change_sequence=first,
                        last_change_sequence=last,
                    )
                )
                result = RenameShoppingListResult(
                    command.mutation_id, shopping_list.id, first, last, False, outcome
                )
        if error is not None and retained is None:
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
                    command_kind=RENAME_COMMAND_KIND,
                    target_identities=[
                        {"entity_kind": "shopping_list", "entity_id": str(command.shopping_list_id)}
                    ],
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error_payload(error),
                    first_change_sequence=None,
                    last_change_sequence=None,
                )
            )
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("Shopping-list rename produced no outcome")
    return result


@dataclass(frozen=True, slots=True)
class RefreshShoppingListCommand:
    """Replace the generated projection of one list without touching its operations."""

    mutation_id: UUID
    generation_revision_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    parent_generation_revision_id: UUID
    scheduled_recipe_ids: tuple[UUID, ...]
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RefreshShoppingListResult:
    mutation_id: UUID
    shopping_list_id: UUID
    generation_revision_id: UUID
    parent_generation_revision_id: UUID
    scheduled_recipe_ids: tuple[UUID, ...]
    ingredient_row_ids: tuple[UUID, ...]
    contribution_ids: tuple[UUID, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


@dataclass(frozen=True, slots=True)
class _PreparedRefresh:
    mutation_id: UUID
    generation_revision_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    parent_generation_revision_id: UUID
    scheduled_recipe_ids: tuple[UUID, ...]
    client_wall_time: datetime
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


@dataclass(frozen=True, slots=True)
class _RefreshSourceScope:
    organization_id: UUID
    event_id: UUID


def _refresh_hash(command: RefreshShoppingListCommand) -> bytes:
    value = {
        "command_kind": REFRESH_COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "mutation_id": _raw_uuid(command.mutation_id),
        "generation_revision_id": _raw_uuid(command.generation_revision_id),
        "organization_id": _raw_uuid(command.organization_id),
        "shopping_list_id": _raw_uuid(command.shopping_list_id),
        "parent_generation_revision_id": _raw_uuid(command.parent_generation_revision_id),
        "scheduled_recipe_ids": _raw_source_ids(command.scheduled_recipe_ids),
        "client_wall_time": _raw_time(command.client_wall_time),
        "logical_operation_id": (
            _raw_uuid(command.logical_operation_id)
            if command.logical_operation_id is not None
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _prepare_refresh(command: RefreshShoppingListCommand) -> _PreparedRefresh:
    violations: list[FieldViolation] = []
    values = (
        ("mutation_id", command.mutation_id),
        ("generation_revision_id", command.generation_revision_id),
        ("organization_id", command.organization_id),
        ("shopping_list_id", command.shopping_list_id),
        ("parent_generation_revision_id", command.parent_generation_revision_id),
    )
    for path, value in values:
        if not isinstance(value, UUID):
            violations.append(FieldViolation(path, "must_be_uuid"))
    if not isinstance(command.scheduled_recipe_ids, tuple):
        violations.append(FieldViolation("scheduled_recipe_ids", "must_be_uuid_tuple"))
        source_ids: tuple[UUID, ...] = ()
    else:
        source_ids = tuple(item for item in command.scheduled_recipe_ids if isinstance(item, UUID))
        if len(source_ids) != len(command.scheduled_recipe_ids):
            violations.append(FieldViolation("scheduled_recipe_ids", "must_contain_only_uuids"))
        if len(set(source_ids)) != len(source_ids):
            violations.append(FieldViolation("scheduled_recipe_ids", "must_not_contain_duplicates"))
    has_time = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not has_time:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    return _PreparedRefresh(
        command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        command.generation_revision_id
        if isinstance(command.generation_revision_id, UUID)
        else UUID(int=0),
        command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0),
        command.shopping_list_id if isinstance(command.shopping_list_id, UUID) else UUID(int=0),
        command.parent_generation_revision_id
        if isinstance(command.parent_generation_revision_id, UUID)
        else UUID(int=0),
        source_ids,
        command.client_wall_time.astimezone(UTC) if has_time else datetime(1970, 1, 1, tzinfo=UTC),
        command.logical_operation_id if isinstance(command.logical_operation_id, UUID) else None,
        tuple(violations),
    )


def _refresh_mutation(
    prepared: _PreparedRefresh,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "partially_superseded", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=prepared.mutation_id,
        logical_operation_id=prepared.logical_operation_id,
        organization_id=prepared.organization_id,
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=prepared.client_wall_time,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=REFRESH_COMMAND_KIND,
        target_identities=[
            {"entity_kind": "shopping_list", "entity_id": str(prepared.shopping_list_id)},
            {
                "entity_kind": "shopping_generation_revision",
                "entity_id": str(prepared.generation_revision_id),
            },
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


def _refresh_payload(result: RefreshShoppingListResult) -> dict[str, object]:
    return {
        "shopping_refresh": {
            "shopping_list_id": str(result.shopping_list_id),
            "generation_revision_id": str(result.generation_revision_id),
            "parent_generation_revision_id": str(result.parent_generation_revision_id),
            "scheduled_recipe_ids": [str(item) for item in result.scheduled_recipe_ids],
            "ingredient_row_ids": [str(item) for item in result.ingredient_row_ids],
            "contribution_ids": [str(item) for item in result.contribution_ids],
            "outcome": result.outcome,
        }
    }


def _retained_refresh(mutation: Mutation) -> RefreshShoppingListResult:
    try:
        item = mutation.outcome_payload["shopping_refresh"] if mutation.outcome_payload else None
        if (
            not isinstance(item, dict)
            or mutation.outcome not in ("accepted", "partially_superseded")
            or item["outcome"] != mutation.outcome
            or mutation.first_change_sequence is None
            or mutation.last_change_sequence is None
        ):
            raise TypeError
        return RefreshShoppingListResult(
            mutation.id,
            UUID(str(item["shopping_list_id"])),
            UUID(str(item["generation_revision_id"])),
            UUID(str(item["parent_generation_revision_id"])),
            tuple(UUID(str(value)) for value in item["scheduled_recipe_ids"]),
            tuple(UUID(str(value)) for value in item["ingredient_row_ids"]),
            tuple(UUID(str(value)) for value in item["contribution_ids"]),
            mutation.first_change_sequence,
            mutation.last_change_sequence,
            True,
            item["outcome"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Retained shopping refresh has invalid outcome payload") from exc


async def _shopping_list_record(
    session: AsyncSession,
    shopping_list: ShoppingList,
    preloaded_clocks: dict[tuple[str, UUID, str], FieldClock] | None = None,
) -> dict[str, object]:
    clocks = await _field_clock_metadata(
        session,
        shopping_list.organization_id,
        "shopping_list",
        shopping_list.id,
        ("name", "current_generation_revision_id"),
        preloaded_clocks,
    )
    return {
        "id": str(shopping_list.id),
        "organization_id": str(shopping_list.organization_id),
        "event_id": str(shopping_list.event_id),
        "name": shopping_list.name,
        "current_generation_revision_id": str(shopping_list.current_generation_revision_id)
        if shopping_list.current_generation_revision_id
        else None,
        "created_at": shopping_list.created_at.isoformat(),
        "created_by_user_id": str(shopping_list.created_by_user_id),
        "field_clocks": clocks,
    }


def _revision_source_entity_id(source: ShoppingRevisionSource) -> UUID:
    """Give a composite immutable source a deterministic protocol identity."""

    return uuid5(
        _REVISION_SOURCE_ID_NAMESPACE,
        f"{source.generation_revision_id}/{source.scheduled_recipe_id}",
    )


def _generation_revision_record(
    revision: ShoppingGenerationRevision,
) -> tuple[str, UUID, dict[str, object]]:
    return (
        "shopping_generation_revision",
        revision.id,
        {
            "id": str(revision.id),
            "organization_id": str(revision.organization_id),
            "event_id": str(revision.event_id),
            "shopping_list_id": str(revision.shopping_list_id),
            "parent_revision_id": str(revision.parent_revision_id)
            if revision.parent_revision_id
            else None,
            "generated_at": revision.generated_at.isoformat(),
            "generated_by_user_id": str(revision.generated_by_user_id),
            "immutable": True,
        },
    )


def _revision_source_record(
    source: ShoppingRevisionSource,
) -> tuple[str, UUID, dict[str, object]]:
    return (
        "shopping_revision_source",
        _revision_source_entity_id(source),
        {
            "id": str(_revision_source_entity_id(source)),
            "generation_revision_id": str(source.generation_revision_id),
            "shopping_list_id": str(source.shopping_list_id),
            "organization_id": str(source.organization_id),
            "event_id": str(source.event_id),
            "scheduled_recipe_id": str(source.scheduled_recipe_id),
            "immutable": True,
        },
    )


def _contribution_snapshot_record(
    snapshot: ShoppingContributionSnapshot,
) -> tuple[str, UUID, dict[str, object]]:
    return (
        "shopping_contribution_snapshot",
        snapshot.id,
        {
            "id": str(snapshot.id),
            "organization_id": str(snapshot.organization_id),
            "event_id": str(snapshot.event_id),
            "shopping_list_id": str(snapshot.shopping_list_id),
            "generation_revision_id": str(snapshot.generation_revision_id),
            "shopping_contribution_id": str(snapshot.shopping_contribution_id),
            "ingredient_id": str(snapshot.ingredient_id),
            "active_in_revision": snapshot.active_in_revision,
            "generated_quantity": _canonical_decimal(snapshot.generated_quantity),
            "event_price_snapshot_id": str(snapshot.event_price_snapshot_id)
            if snapshot.event_price_snapshot_id
            else None,
            "price_amount": _canonical_decimal(snapshot.price_amount)
            if snapshot.price_amount is not None
            else None,
            "priced_quantity": _canonical_decimal(snapshot.priced_quantity)
            if snapshot.priced_quantity is not None
            else None,
            "priced_unit_id": str(snapshot.priced_unit_id) if snapshot.priced_unit_id else None,
            "currency": snapshot.currency,
            "ingredient_version_id": str(snapshot.ingredient_version_id),
            "ingredient_name": snapshot.ingredient_name,
            "source_details": snapshot.source_details,
            "immutable": True,
        },
    )


async def _event_price_snapshots(
    session: AsyncSession, organization_id: UUID, event_id: UUID
) -> dict[UUID, EventIngredientPriceSnapshot]:
    return {
        snapshot.ingredient_id: snapshot
        for snapshot in (
            await session.scalars(
                select(EventIngredientPriceSnapshot)
                .join(
                    EventIngredientPrice,
                    EventIngredientPrice.current_snapshot_id == EventIngredientPriceSnapshot.id,
                )
                .where(
                    EventIngredientPrice.organization_id == organization_id,
                    EventIngredientPrice.event_id == event_id,
                )
            )
        ).all()
    }


def _captured_price(snapshot: EventIngredientPriceSnapshot | None) -> dict[str, object]:
    if snapshot is None or snapshot.state != "available":
        return {
            "event_price_snapshot_id": None,
            "price_amount": None,
            "priced_quantity": None,
            "priced_unit_id": None,
            "currency": None,
        }
    return {
        "event_price_snapshot_id": snapshot.id,
        "price_amount": snapshot.price_amount,
        "priced_quantity": snapshot.priced_quantity,
        "priced_unit_id": snapshot.priced_unit_id,
        "currency": snapshot.currency,
    }


async def refresh_shopping_list(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: RefreshShoppingListCommand,
) -> RefreshShoppingListResult:
    """Append an immutable shopping revision and LWW-select it when it wins."""

    prepared, request_hash = _prepare_refresh(command), _refresh_hash(command)
    error: ApplicationServiceError | None = None
    result: RefreshShoppingListResult | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_member_and_lock_organization(
            session, context, prepared.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", prepared.mutation_id)},
        )
        retained = await session.get(Mutation, prepared.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != REFRESH_COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome in ("accepted", "partially_superseded"):
                return _retained_refresh(retained)
            raise _retained_error(retained)
        if prepared.violations:
            error = _error(prepared.violations)
        elif prepared.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            error = ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        if error is not None:
            session.add(
                _refresh_mutation(
                    prepared, context, role, request_hash, "rejected", _error_payload(error)
                )
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        "shopping_generation_revision", prepared.generation_revision_id
                    )
                },
            )
            shopping_list = await session.scalar(
                select(ShoppingList)
                .join(Event, Event.id == ShoppingList.event_id)
                .where(
                    ShoppingList.id == prepared.shopping_list_id,
                    ShoppingList.organization_id == prepared.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ShoppingList, Event))
            )
            if shopping_list is None:
                existing = await session.scalar(
                    select(ShoppingList.id).where(
                        ShoppingList.id == prepared.shopping_list_id,
                        ShoppingList.organization_id == prepared.organization_id,
                    )
                )
                error = (
                    ApplicationServiceError("archived_event", retry_same_identity=False)
                    if existing is not None
                    else _error((FieldViolation("shopping_list_id", "not_found"),))
                )
            else:
                parent = await session.scalar(
                    select(ShoppingGenerationRevision)
                    .where(
                        ShoppingGenerationRevision.id == prepared.parent_generation_revision_id,
                        ShoppingGenerationRevision.shopping_list_id == shopping_list.id,
                    )
                    .with_for_update(of=ShoppingGenerationRevision)
                )
                collision = await session.scalar(
                    select(ShoppingGenerationRevision.id).where(
                        ShoppingGenerationRevision.id == prepared.generation_revision_id
                    )
                )
                if parent is None:
                    error = ApplicationServiceError("stale_precondition", retry_same_identity=False)
                elif collision is not None:
                    error = _error(
                        (FieldViolation("generation_revision_id", "must_not_already_exist"),)
                    )
                else:
                    materialized: list[tuple[_Source, list[_ResolvedLine]]] = []
                    source_scope = _RefreshSourceScope(
                        prepared.organization_id, shopping_list.event_id
                    )
                    for source_id in prepared.scheduled_recipe_ids:
                        loaded = await _resolve_source(session, source_scope, source_id)
                        if loaded is None:
                            error = _error(
                                (
                                    FieldViolation(
                                        "scheduled_recipe_ids",
                                        "must_reference_active_recipes_from_event",
                                    ),
                                )
                            )
                            break
                        materialized.append(loaded)
            if error is not None:
                session.add(
                    _refresh_mutation(
                        prepared, context, role, request_hash, "rejected", _error_payload(error)
                    )
                )
            else:
                assert shopping_list is not None
                assert parent is not None
                existing_rows = {
                    row.ingredient_id: row
                    for row in (
                        await session.scalars(
                            select(ShoppingIngredientRow)
                            .where(ShoppingIngredientRow.shopping_list_id == shopping_list.id)
                            .order_by(ShoppingIngredientRow.id)
                            .with_for_update(of=ShoppingIngredientRow)
                        )
                    ).all()
                }
                existing_contributions = {
                    (item.scheduled_recipe_id, item.ingredient_id): item
                    for item in (
                        await session.scalars(
                            select(ShoppingContribution)
                            .where(ShoppingContribution.shopping_list_id == shopping_list.id)
                            .order_by(ShoppingContribution.id)
                            .with_for_update(of=ShoppingContribution)
                        )
                    ).all()
                }
                revision = ShoppingGenerationRevision(
                    id=prepared.generation_revision_id,
                    organization_id=prepared.organization_id,
                    event_id=shopping_list.event_id,
                    shopping_list_id=shopping_list.id,
                    parent_revision_id=parent.id,
                    generated_by_user_id=context.actor_user_id,
                )
                session.add(revision)
                await session.flush()
                price_snapshots = await _event_price_snapshots(
                    session, prepared.organization_id, shopping_list.event_id
                )
                generated: dict[tuple[UUID, UUID], tuple[_Source, list[_ResolvedLine]]] = {}
                revision_sources: list[ShoppingRevisionSource] = []
                for source, lines in materialized:
                    revision_source = ShoppingRevisionSource(
                        generation_revision_id=prepared.generation_revision_id,
                        shopping_list_id=shopping_list.id,
                        organization_id=prepared.organization_id,
                        event_id=shopping_list.event_id,
                        scheduled_recipe_id=source.scheduled_recipe_id,
                    )
                    revision_sources.append(revision_source)
                    session.add(revision_source)
                    grouped: dict[UUID, list[_ResolvedLine]] = defaultdict(list)
                    for line in lines:
                        grouped[line.ingredient_id].append(line)
                    for ingredient_id, items in grouped.items():
                        generated[(source.scheduled_recipe_id, ingredient_id)] = (source, items)
                contribution_ids: list[UUID] = []
                snapshots: list[ShoppingContributionSnapshot] = []
                for key, (source, items) in generated.items():
                    scheduled_recipe_id, ingredient_id = key
                    representative = items[-1]
                    row = existing_rows.get(ingredient_id)
                    if row is None:
                        row = ShoppingIngredientRow(
                            id=uuid4(),
                            organization_id=prepared.organization_id,
                            event_id=shopping_list.event_id,
                            shopping_list_id=shopping_list.id,
                            ingredient_id=ingredient_id,
                            ingredient_name=representative.ingredient_name,
                            calculation_unit_id=representative.calculation_unit_id,
                            default_store_section_id=representative.default_store_section_id,
                            default_store_section_name=representative.default_store_section_name,
                            created_by_user_id=context.actor_user_id,
                        )
                        existing_rows[ingredient_id] = row
                        session.add(row)
                        await session.flush()
                    contribution = existing_contributions.get(key)
                    if contribution is None:
                        contribution = ShoppingContribution(
                            id=uuid4(),
                            organization_id=prepared.organization_id,
                            event_id=shopping_list.event_id,
                            shopping_list_id=shopping_list.id,
                            shopping_ingredient_row_id=row.id,
                            ingredient_id=ingredient_id,
                            scheduled_recipe_id=scheduled_recipe_id,
                        )
                        existing_contributions[key] = contribution
                        session.add(contribution)
                    contribution_ids.append(contribution.id)
                    notes = [item.note for item in items if item.note]
                    snapshots.append(
                        ShoppingContributionSnapshot(
                            id=uuid4(),
                            organization_id=prepared.organization_id,
                            event_id=shopping_list.event_id,
                            shopping_list_id=shopping_list.id,
                            generation_revision_id=prepared.generation_revision_id,
                            shopping_contribution_id=contribution.id,
                            ingredient_id=ingredient_id,
                            active_in_revision=True,
                            generated_quantity=sum((item.quantity for item in items), Decimal(0)),
                            **_captured_price(price_snapshots.get(ingredient_id)),
                            ingredient_version_id=representative.ingredient_version_id,
                            ingredient_name=representative.ingredient_name,
                            source_details={
                                "recipe_name": source.recipe_name,
                                "recipe_description": source.recipe_description,
                                "day": source.calendar_date,
                                "meal_role": source.meal_role,
                                "selected_scale_amount": _canonical_decimal(
                                    source.selected_scale_amount
                                ),
                                "recipe_base_scaling_amount": _canonical_decimal(
                                    source.recipe_base_scaling_amount
                                ),
                                "line_notes": notes,
                                "line_count": len(items),
                            },
                        )
                    )
                prior = {
                    snapshot.shopping_contribution_id: snapshot
                    for snapshot in (
                        await session.scalars(
                            select(ShoppingContributionSnapshot).where(
                                ShoppingContributionSnapshot.generation_revision_id == parent.id
                            )
                        )
                    ).all()
                }
                # A sibling offline refresh may have introduced a contribution that
                # is absent from this command's parent branch.  It is nevertheless
                # already a stable identity of this list and must receive an
                # explanatory retired snapshot, rather than disappearing with its
                # retained fulfilment credit.
                missing_prior_ids = [
                    contribution.id
                    for contribution in existing_contributions.values()
                    if contribution.id not in prior
                ]
                if missing_prior_ids:
                    historical = (
                        await session.execute(
                            select(ShoppingContributionSnapshot, ShoppingGenerationRevision)
                            .join(
                                ShoppingGenerationRevision,
                                ShoppingGenerationRevision.id
                                == ShoppingContributionSnapshot.generation_revision_id,
                            )
                            .where(
                                ShoppingContributionSnapshot.shopping_contribution_id.in_(
                                    missing_prior_ids
                                )
                            )
                            .order_by(
                                ShoppingContributionSnapshot.shopping_contribution_id,
                                ShoppingGenerationRevision.generated_at.desc(),
                                ShoppingGenerationRevision.id.desc(),
                            )
                        )
                    ).all()
                    for snapshot, _revision in historical:
                        prior.setdefault(snapshot.shopping_contribution_id, snapshot)
                for contribution in existing_contributions.values():
                    if contribution.id in contribution_ids:
                        continue
                    previous = prior.get(contribution.id)
                    if previous is None:
                        continue
                    snapshots.append(
                        ShoppingContributionSnapshot(
                            id=uuid4(),
                            organization_id=prepared.organization_id,
                            event_id=shopping_list.event_id,
                            shopping_list_id=shopping_list.id,
                            generation_revision_id=prepared.generation_revision_id,
                            shopping_contribution_id=contribution.id,
                            ingredient_id=contribution.ingredient_id,
                            active_in_revision=False,
                            generated_quantity=previous.generated_quantity,
                            event_price_snapshot_id=previous.event_price_snapshot_id,
                            price_amount=previous.price_amount,
                            priced_quantity=previous.priced_quantity,
                            priced_unit_id=previous.priced_unit_id,
                            currency=previous.currency,
                            ingredient_version_id=previous.ingredient_version_id,
                            ingredient_name=previous.ingredient_name,
                            source_details=previous.source_details,
                        )
                    )
                session.add_all(snapshots)
                pointer_clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == prepared.organization_id,
                        FieldClock.entity_kind == "shopping_list",
                        FieldClock.entity_id == shopping_list.id,
                        FieldClock.field_name == "current_generation_revision_id",
                    )
                    .with_for_update(of=FieldClock)
                )
                pointer_wins = pointer_clock is None or (
                    prepared.client_wall_time,
                    prepared.mutation_id,
                ) > (pointer_clock.winning_client_wall_time, pointer_clock.winning_mutation_id)
                outcome: Literal["accepted", "partially_superseded"] = "accepted"
                if pointer_wins:
                    shopping_list.current_generation_revision_id = prepared.generation_revision_id
                    if pointer_clock is None:
                        session.add(
                            FieldClock(
                                organization_id=prepared.organization_id,
                                entity_kind="shopping_list",
                                entity_id=shopping_list.id,
                                field_name="current_generation_revision_id",
                                winning_client_wall_time=prepared.client_wall_time,
                                winning_mutation_id=prepared.mutation_id,
                            )
                        )
                    else:
                        pointer_clock.winning_client_wall_time = prepared.client_wall_time
                        pointer_clock.winning_mutation_id = prepared.mutation_id
                else:
                    outcome = "partially_superseded"
                await session.flush()
                snapshot_contribution_ids = {
                    snapshot.shopping_contribution_id for snapshot in snapshots
                }
                snapshot_contributions = [
                    contribution
                    for contribution in existing_contributions.values()
                    if contribution.id in snapshot_contribution_ids
                ]
                rows_by_id = {row.id: row for row in existing_rows.values()}
                snapshot_rows = list(
                    {
                        contribution.shopping_ingredient_row_id: rows_by_id[
                            contribution.shopping_ingredient_row_id
                        ]
                        for contribution in snapshot_contributions
                    }.values()
                )
                records: list[tuple[str, UUID, dict[str, object]]] = [
                    _generation_revision_record(revision)
                ]
                records.extend([await _row_record(session, row) for row in snapshot_rows])
                records.extend(
                    [
                        await _contribution_record(session, contribution)
                        for contribution in snapshot_contributions
                    ]
                )
                records.extend(_revision_source_record(source) for source in revision_sources)
                records.extend(_contribution_snapshot_record(snapshot) for snapshot in snapshots)
                records.append(
                    (
                        "shopping_list",
                        shopping_list.id,
                        await _shopping_list_record(session, shopping_list),
                    )
                )
                first, last = await _reserve_change_range(
                    session, prepared.organization_id, prepared.mutation_id, len(records)
                )
                result = RefreshShoppingListResult(
                    prepared.mutation_id,
                    shopping_list.id,
                    prepared.generation_revision_id,
                    parent.id,
                    prepared.scheduled_recipe_ids,
                    tuple(row.id for row in existing_rows.values()),
                    tuple(contribution_ids),
                    first,
                    last,
                    False,
                    outcome,
                )
                payload = _refresh_payload(result)
                session.add_all(
                    [
                        OrganizationChange(
                            organization_id=prepared.organization_id,
                            sequence=first + offset,
                            mutation_id=prepared.mutation_id,
                            entity_id=entity_id,
                            entity_kind=entity_kind,
                            operation="upsert",
                            payload={"record_schema_version": 1, "record": record},
                        )
                        for offset, (entity_kind, entity_id, record) in enumerate(records)
                    ]
                )
                session.add(
                    _refresh_mutation(
                        prepared, context, role, request_hash, outcome, payload, first, last
                    )
                )
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("Shopping-list refresh produced no outcome")
    return result


# Operational shopping mutations deliberately share one small implementation.  They
# are separate public commands because each represents one user intent in the sync
# outbox, while their authorization/idempotency/LWW mechanics are identical.
@dataclass(frozen=True, slots=True)
class SetShoppingAvailableSupplyCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    shopping_ingredient_row_id: UUID
    quantity: Decimal
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SetShoppingManualPurchaseTargetCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    shopping_ingredient_row_id: UUID
    quantity: Decimal | None
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SetShoppingStoreSectionOverrideCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    shopping_ingredient_row_id: UUID
    store_section_id: UUID | None
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SetShoppingRowNoteCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    shopping_ingredient_row_id: UUID
    note: str | None
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SetShoppingContributionFulfilmentCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    shopping_contribution_id: UUID
    fulfilled: bool
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SetShoppingRowFulfilmentCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    shopping_ingredient_row_id: UUID
    fulfilled: bool
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


ShoppingOperationCommand = (
    SetShoppingAvailableSupplyCommand
    | SetShoppingManualPurchaseTargetCommand
    | SetShoppingStoreSectionOverrideCommand
    | SetShoppingRowNoteCommand
    | SetShoppingContributionFulfilmentCommand
    | SetShoppingRowFulfilmentCommand
)
_OPERATION_KIND = {
    SetShoppingAvailableSupplyCommand: "shopping_list.set_available_supply",
    SetShoppingManualPurchaseTargetCommand: "shopping_list.set_manual_purchase_target",
    SetShoppingStoreSectionOverrideCommand: "shopping_list.set_store_section_override",
    SetShoppingRowNoteCommand: "shopping_list.set_row_note",
    SetShoppingContributionFulfilmentCommand: "shopping_list.set_contribution_fulfilment",
    SetShoppingRowFulfilmentCommand: "shopping_list.set_row_fulfilment",
}


@dataclass(frozen=True, slots=True)
class ShoppingOperationResult:
    mutation_id: UUID
    shopping_list_id: UUID
    shopping_ingredient_row_id: UUID
    shopping_contribution_ids: tuple[UUID, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


def _operation_kind(command: ShoppingOperationCommand) -> str:
    return _OPERATION_KIND[type(command)]


def _operation_hash(command: ShoppingOperationCommand) -> bytes:
    raw_quantity = getattr(command, "quantity", None)
    raw_fulfilled = getattr(command, "fulfilled", None)
    raw_section_id = getattr(command, "store_section_id", None)
    raw_note = getattr(command, "note", None)
    value: object
    if isinstance(raw_quantity, Decimal):
        value = (
            _canonical_decimal(raw_quantity) if raw_quantity.is_finite() else _invalid(raw_quantity)
        )
    elif raw_quantity is None and isinstance(command, SetShoppingManualPurchaseTargetCommand):
        value = None
    elif isinstance(command, SetShoppingStoreSectionOverrideCommand):
        value = _raw_uuid(raw_section_id) if raw_section_id is not None else None
    elif isinstance(command, SetShoppingRowNoteCommand):
        value = _row_note_hash_value(raw_note)
    elif isinstance(raw_fulfilled, bool):
        value = raw_fulfilled
    else:
        value = _invalid(raw_quantity if hasattr(command, "quantity") else raw_fulfilled)
    target = getattr(command, "shopping_ingredient_row_id", None)
    if isinstance(command, SetShoppingContributionFulfilmentCommand):
        target = command.shopping_contribution_id
    return hashlib.sha256(
        json.dumps(
            {
                "command_kind": _operation_kind(command),
                "command_schema_version": COMMAND_SCHEMA_VERSION,
                "mutation_id": _raw_uuid(command.mutation_id),
                "organization_id": _raw_uuid(command.organization_id),
                "shopping_list_id": _raw_uuid(command.shopping_list_id),
                "target_id": _raw_uuid(target),
                "value": value,
                "client_wall_time": _raw_time(command.client_wall_time),
                "logical_operation_id": _raw_uuid(command.logical_operation_id)
                if command.logical_operation_id is not None
                else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _operation_violations(command: ShoppingOperationCommand) -> tuple[FieldViolation, ...]:
    violations: list[FieldViolation] = []
    for name in ("mutation_id", "organization_id", "shopping_list_id"):
        if not isinstance(getattr(command, name), UUID):
            violations.append(FieldViolation(name, "must_be_uuid"))
    target_name = (
        "shopping_contribution_id"
        if isinstance(command, SetShoppingContributionFulfilmentCommand)
        else "shopping_ingredient_row_id"
    )
    if not isinstance(getattr(command, target_name), UUID):
        violations.append(FieldViolation(target_name, "must_be_uuid"))
    if isinstance(
        command, (SetShoppingAvailableSupplyCommand, SetShoppingManualPurchaseTargetCommand)
    ):
        if command.quantity is not None and (
            not isinstance(command.quantity, Decimal)
            or not command.quantity.is_finite()
            or command.quantity < 0
        ):
            violations.append(
                FieldViolation("quantity", "must_be_nonnegative_finite_decimal_or_null")
            )
        if isinstance(command, SetShoppingAvailableSupplyCommand) and command.quantity is None:
            violations.append(FieldViolation("quantity", "must_be_nonnegative_finite_decimal"))
    elif isinstance(command, SetShoppingStoreSectionOverrideCommand):
        if command.store_section_id is not None and not isinstance(command.store_section_id, UUID):
            violations.append(FieldViolation("store_section_id", "must_be_uuid_or_null"))
    elif isinstance(command, SetShoppingRowNoteCommand):
        if command.note is not None and not isinstance(command.note, str):
            violations.append(FieldViolation("note", "must_be_string_or_null"))
        elif isinstance(command.note, str):
            note = _canonical_row_note(command.note)
            if note is not None and (
                "\x00" in note or any(0xD800 <= ord(char) <= 0xDFFF for char in note)
            ):
                violations.append(FieldViolation("note", "must_be_valid_unicode_text"))
            elif note is not None and len(note) > 4000:
                violations.append(FieldViolation("note", "must_fit_change_record"))
    else:
        if not isinstance(command.fulfilled, bool):
            violations.append(FieldViolation("fulfilled", "must_be_boolean"))
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
    return tuple(violations)


def _operation_result_payload(result: ShoppingOperationResult) -> dict[str, object]:
    return {
        "shopping_operation": {
            "shopping_list_id": str(result.shopping_list_id),
            "shopping_ingredient_row_id": str(result.shopping_ingredient_row_id),
            "shopping_contribution_ids": [str(item) for item in result.shopping_contribution_ids],
            "outcome": result.outcome,
        }
    }


def _retained_operation(mutation: Mutation) -> ShoppingOperationResult:
    try:
        item = mutation.outcome_payload["shopping_operation"] if mutation.outcome_payload else None
        if not isinstance(item, dict) or mutation.outcome not in (
            "accepted",
            "partially_superseded",
        ):
            raise TypeError
        ids = item["shopping_contribution_ids"]
        if (
            not isinstance(ids, list)
            or item["outcome"] != mutation.outcome
            or mutation.first_change_sequence is None
            or mutation.last_change_sequence is None
        ):
            raise TypeError
        return ShoppingOperationResult(
            mutation.id,
            UUID(str(item["shopping_list_id"])),
            UUID(str(item["shopping_ingredient_row_id"])),
            tuple(UUID(str(value)) for value in ids),
            mutation.first_change_sequence,
            mutation.last_change_sequence,
            True,
            item["outcome"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Retained shopping operation has invalid outcome payload") from exc


def _operation_mutation(
    command: ShoppingOperationCommand,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "partially_superseded", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    target_id = (
        command.shopping_contribution_id
        if isinstance(command, SetShoppingContributionFulfilmentCommand)
        else command.shopping_ingredient_row_id
    )
    return Mutation(
        id=command.mutation_id,
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        else datetime(1970, 1, 1, tzinfo=UTC),
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=_operation_kind(command),
        target_identities=[
            {"entity_kind": "shopping_list", "entity_id": str(command.shopping_list_id)},
            {"entity_kind": "shopping_target", "entity_id": str(target_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


_ROW_SYNCHRONIZABLE_FIELDS = (
    "available_supply_quantity",
    "manual_purchase_target",
    "store_section_override_id",
    "note",
    "aggregate_fulfilment_credit",
)
_CONTRIBUTION_SYNCHRONIZABLE_FIELDS = ("fulfilment_credit",)
_AD_HOC_SYNCHRONIZABLE_FIELDS = (
    "name",
    "target_amount",
    "unit_id",
    "store_section_id",
    "note",
    "fulfilment_credit",
    "lifecycle",
)


async def _field_clock_metadata(
    session: AsyncSession,
    organization_id: UUID,
    entity_kind: str,
    entity_id: UUID,
    field_names: tuple[str, ...],
    preloaded_clocks: dict[tuple[str, UUID, str], FieldClock] | None = None,
) -> dict[str, object]:
    if preloaded_clocks is None:
        clocks = {
            clock.field_name: clock
            for clock in (
                await session.scalars(
                    select(FieldClock).where(
                        FieldClock.organization_id == organization_id,
                        FieldClock.entity_kind == entity_kind,
                        FieldClock.entity_id == entity_id,
                        FieldClock.field_name.in_(field_names),
                    )
                )
            ).all()
        }
    else:
        clocks = {
            field_name: clock
            for field_name in field_names
            if (clock := preloaded_clocks.get((entity_kind, entity_id, field_name))) is not None
        }
    return {
        field_name: (
            {
                "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                "winning_mutation_id": str(clock.winning_mutation_id),
            }
            if (clock := clocks.get(field_name)) is not None
            else None
        )
        for field_name in field_names
    }


async def _row_record(
    session: AsyncSession,
    row: ShoppingIngredientRow,
    preloaded_clocks: dict[tuple[str, UUID, str], FieldClock] | None = None,
) -> tuple[str, UUID, dict[str, object]]:
    return (
        "shopping_ingredient_row",
        row.id,
        {
            "id": str(row.id),
            "organization_id": str(row.organization_id),
            "event_id": str(row.event_id),
            "shopping_list_id": str(row.shopping_list_id),
            "ingredient_id": str(row.ingredient_id),
            "ingredient_name": row.ingredient_name,
            "calculation_unit_id": str(row.calculation_unit_id),
            "available_supply_quantity": _canonical_decimal(row.available_supply_quantity),
            "manual_purchase_target": _canonical_decimal(row.manual_purchase_target)
            if row.manual_purchase_target is not None
            else None,
            "manual_target_automatic_value": _canonical_decimal(row.manual_target_automatic_value)
            if row.manual_target_automatic_value is not None
            else None,
            "manual_target_generation_revision_id": str(row.manual_target_generation_revision_id)
            if row.manual_target_generation_revision_id
            else None,
            "default_store_section_id": str(row.default_store_section_id)
            if row.default_store_section_id
            else None,
            "default_store_section_name": row.default_store_section_name,
            "store_section_override_id": str(row.store_section_override_id)
            if row.store_section_override_id
            else None,
            "note": row.note,
            "aggregate_fulfilment_credit": _canonical_decimal(row.aggregate_fulfilment_credit),
            "aggregate_credit_updated_at": row.aggregate_credit_updated_at.isoformat()
            if row.aggregate_credit_updated_at
            else None,
            "aggregate_credit_updated_by_user_id": str(row.aggregate_credit_updated_by_user_id)
            if row.aggregate_credit_updated_by_user_id
            else None,
            "aggregate_credit_updated_by_installation_id": str(
                row.aggregate_credit_updated_by_installation_id
            )
            if row.aggregate_credit_updated_by_installation_id
            else None,
            "created_at": row.created_at.isoformat(),
            "created_by_user_id": str(row.created_by_user_id),
            "field_clocks": await _field_clock_metadata(
                session,
                row.organization_id,
                "shopping_ingredient_row",
                row.id,
                _ROW_SYNCHRONIZABLE_FIELDS,
                preloaded_clocks,
            ),
        },
    )


async def _contribution_record(
    session: AsyncSession,
    contribution: ShoppingContribution,
    preloaded_clocks: dict[tuple[str, UUID, str], FieldClock] | None = None,
) -> tuple[str, UUID, dict[str, object]]:
    return (
        "shopping_contribution",
        contribution.id,
        {
            "id": str(contribution.id),
            "organization_id": str(contribution.organization_id),
            "event_id": str(contribution.event_id),
            "shopping_list_id": str(contribution.shopping_list_id),
            "shopping_ingredient_row_id": str(contribution.shopping_ingredient_row_id),
            "ingredient_id": str(contribution.ingredient_id),
            "scheduled_recipe_id": str(contribution.scheduled_recipe_id),
            "fulfilment_credit": _canonical_decimal(contribution.fulfilment_credit),
            "fulfilment_updated_at": contribution.fulfilment_updated_at.isoformat()
            if contribution.fulfilment_updated_at
            else None,
            "fulfilment_updated_by_user_id": str(contribution.fulfilment_updated_by_user_id)
            if contribution.fulfilment_updated_by_user_id
            else None,
            "fulfilment_updated_by_installation_id": str(
                contribution.fulfilment_updated_by_installation_id
            )
            if contribution.fulfilment_updated_by_installation_id
            else None,
            "field_clocks": await _field_clock_metadata(
                session,
                contribution.organization_id,
                "shopping_contribution",
                contribution.id,
                _CONTRIBUTION_SYNCHRONIZABLE_FIELDS,
                preloaded_clocks,
            ),
        },
    )


async def _current_generated_quantity(
    session: AsyncSession, contribution: ShoppingContribution, revision_id: UUID
) -> Decimal:
    quantity = await session.scalar(
        select(ShoppingContributionSnapshot.generated_quantity).where(
            ShoppingContributionSnapshot.generation_revision_id == revision_id,
            ShoppingContributionSnapshot.shopping_contribution_id == contribution.id,
            ShoppingContributionSnapshot.active_in_revision.is_(True),
        )
    )
    return quantity or Decimal(0)


async def _row_generated_quantity(
    session: AsyncSession, row: ShoppingIngredientRow, revision_id: UUID
) -> Decimal:
    quantity = await session.scalar(
        select(func.coalesce(func.sum(ShoppingContributionSnapshot.generated_quantity), 0))
        .join(
            ShoppingContribution,
            ShoppingContribution.id == ShoppingContributionSnapshot.shopping_contribution_id,
        )
        .where(
            ShoppingContributionSnapshot.generation_revision_id == revision_id,
            ShoppingContributionSnapshot.active_in_revision.is_(True),
            ShoppingContribution.shopping_ingredient_row_id == row.id,
        )
    )
    return Decimal(quantity or 0)


async def _apply_shopping_operation(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: ShoppingOperationCommand,
) -> ShoppingOperationResult:
    violations = _operation_violations(command)
    request_hash = _operation_hash(command)
    error: ApplicationServiceError | None = None
    result: ShoppingOperationResult | None = None
    async with session_factory() as session, session.begin():
        org_id = (
            command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
        )
        role = await _authorize_member_and_lock_organization(session, context, org_id)
        mutation_id = command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", mutation_id)},
        )
        retained = await session.get(Mutation, mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != _operation_kind(command)
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome in ("accepted", "partially_superseded"):
                return _retained_operation(retained)
            raise _retained_error(retained)
        if violations:
            error = _error(violations)
        elif command.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            error = ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        if error is not None:
            session.add(
                _operation_mutation(
                    command, context, role, request_hash, "rejected", _error_payload(error)
                )
            )
        else:
            shopping_list = await session.scalar(
                select(ShoppingList)
                .join(Event, Event.id == ShoppingList.event_id)
                .where(
                    ShoppingList.id == command.shopping_list_id,
                    ShoppingList.organization_id == command.organization_id,
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ShoppingList, Event))
            )
            if shopping_list is None:
                archived_list_id = await session.scalar(
                    select(ShoppingList.id).where(
                        ShoppingList.id == command.shopping_list_id,
                        ShoppingList.organization_id == command.organization_id,
                    )
                )
                error = (
                    ApplicationServiceError("archived_event", retry_same_identity=False)
                    if archived_list_id is not None
                    else _error((FieldViolation("shopping_list_id", "not_found"),))
                )
                session.add(
                    _operation_mutation(
                        command, context, role, request_hash, "rejected", _error_payload(error)
                    )
                )
            elif shopping_list.current_generation_revision_id is None:
                error = _error((FieldViolation("shopping_list_id", "not_found"),))
                session.add(
                    _operation_mutation(
                        command, context, role, request_hash, "rejected", _error_payload(error)
                    )
                )
            else:
                row_id = (
                    command.shopping_contribution_id
                    if isinstance(command, SetShoppingContributionFulfilmentCommand)
                    else command.shopping_ingredient_row_id
                )
                row = None
                contribution: ShoppingContribution | None = None
                if isinstance(command, SetShoppingContributionFulfilmentCommand):
                    # All paths lock the aggregate row before any contribution.  The
                    # initial lookup is intentionally unlocked: it only discovers
                    # the immutable parent identity used to acquire that first lock.
                    contribution = await session.scalar(
                        select(ShoppingContribution).where(
                            ShoppingContribution.id == row_id,
                            ShoppingContribution.shopping_list_id == shopping_list.id,
                            ShoppingContribution.organization_id == command.organization_id,
                        )
                    )
                    if contribution is not None:
                        row = await session.scalar(
                            select(ShoppingIngredientRow)
                            .where(
                                ShoppingIngredientRow.id == contribution.shopping_ingredient_row_id
                            )
                            .with_for_update(of=ShoppingIngredientRow)
                        )
                        if row is not None:
                            contribution = await session.scalar(
                                select(ShoppingContribution)
                                .where(
                                    ShoppingContribution.id == contribution.id,
                                    ShoppingContribution.shopping_ingredient_row_id == row.id,
                                    ShoppingContribution.shopping_list_id == shopping_list.id,
                                    ShoppingContribution.organization_id == command.organization_id,
                                )
                                .with_for_update(of=ShoppingContribution)
                            )
                            if contribution is None:
                                row = None
                else:
                    row = await session.scalar(
                        select(ShoppingIngredientRow)
                        .where(
                            ShoppingIngredientRow.id == row_id,
                            ShoppingIngredientRow.shopping_list_id == shopping_list.id,
                            ShoppingIngredientRow.organization_id == command.organization_id,
                        )
                        .with_for_update(of=ShoppingIngredientRow)
                    )
                if row is None:
                    error = _error((FieldViolation("shopping_target_id", "not_found"),))
                    session.add(
                        _operation_mutation(
                            command, context, role, request_hash, "rejected", _error_payload(error)
                        )
                    )
                else:
                    if (
                        isinstance(command, SetShoppingStoreSectionOverrideCommand)
                        and command.store_section_id is not None
                    ):
                        section = await session.get(
                            StoreSection, command.store_section_id, with_for_update=True
                        )
                        if (
                            section is None
                            or section.organization_id != command.organization_id
                            or section.retired_at is not None
                        ):
                            error = _error(
                                (
                                    FieldViolation(
                                        "store_section_id", "not_available_in_organization"
                                    ),
                                )
                            )
                            session.add(
                                _operation_mutation(
                                    command,
                                    context,
                                    request_hash=request_hash,
                                    role=role,
                                    outcome="rejected",
                                    payload=_error_payload(error),
                                )
                            )
                    records: list[tuple[str, UUID, dict[str, object]]] = []
                    affected: tuple[UUID, ...] = ()
                    outcome: Literal["accepted", "partially_superseded"] = "accepted"
                    if error is not None:
                        pass
                    elif isinstance(
                        command,
                        (
                            SetShoppingAvailableSupplyCommand,
                            SetShoppingManualPurchaseTargetCommand,
                            SetShoppingStoreSectionOverrideCommand,
                            SetShoppingRowNoteCommand,
                        ),
                    ):
                        field = (
                            "available_supply_quantity"
                            if isinstance(command, SetShoppingAvailableSupplyCommand)
                            else (
                                "manual_purchase_target"
                                if isinstance(command, SetShoppingManualPurchaseTargetCommand)
                                else "store_section_override_id"
                                if isinstance(command, SetShoppingStoreSectionOverrideCommand)
                                else "note"
                            )
                        )
                        clock = await session.scalar(
                            select(FieldClock)
                            .where(
                                FieldClock.organization_id == command.organization_id,
                                FieldClock.entity_kind == "shopping_ingredient_row",
                                FieldClock.entity_id == row.id,
                                FieldClock.field_name == field,
                            )
                            .with_for_update(of=FieldClock)
                        )
                        wins = clock is None or (command.client_wall_time, command.mutation_id) > (
                            clock.winning_client_wall_time,
                            clock.winning_mutation_id,
                        )
                        if wins:
                            if isinstance(command, SetShoppingAvailableSupplyCommand):
                                row.available_supply_quantity = command.quantity
                            elif isinstance(command, SetShoppingManualPurchaseTargetCommand):
                                automatic_value = (
                                    max(
                                        Decimal(0),
                                        await _row_generated_quantity(
                                            session,
                                            row,
                                            shopping_list.current_generation_revision_id,
                                        )
                                        - row.available_supply_quantity,
                                    )
                                    if command.quantity is not None
                                    else None
                                )
                                row.manual_purchase_target = command.quantity
                                row.manual_target_automatic_value = automatic_value
                                row.manual_target_generation_revision_id = (
                                    shopping_list.current_generation_revision_id
                                    if command.quantity is not None
                                    else None
                                )
                            else:
                                if isinstance(command, SetShoppingStoreSectionOverrideCommand):
                                    row.store_section_override_id = command.store_section_id
                                else:
                                    row.note = _canonical_row_note(command.note)
                            if clock is None:
                                session.add(
                                    FieldClock(
                                        organization_id=command.organization_id,
                                        entity_kind="shopping_ingredient_row",
                                        entity_id=row.id,
                                        field_name=field,
                                        winning_client_wall_time=command.client_wall_time,
                                        winning_mutation_id=command.mutation_id,
                                    )
                                )
                            else:
                                clock.winning_client_wall_time, clock.winning_mutation_id = (
                                    command.client_wall_time,
                                    command.mutation_id,
                                )
                        else:
                            outcome = "partially_superseded"
                        records.append(await _row_record(session, row))
                    elif isinstance(command, SetShoppingContributionFulfilmentCommand):
                        assert contribution is not None
                        clock = await session.scalar(
                            select(FieldClock)
                            .where(
                                FieldClock.organization_id == command.organization_id,
                                FieldClock.entity_kind == "shopping_contribution",
                                FieldClock.entity_id == contribution.id,
                                FieldClock.field_name == "fulfilment_credit",
                            )
                            .with_for_update(of=FieldClock)
                        )
                        wins = clock is None or (command.client_wall_time, command.mutation_id) > (
                            clock.winning_client_wall_time,
                            clock.winning_mutation_id,
                        )
                        if wins:
                            contribution.fulfilment_credit = (
                                await _current_generated_quantity(
                                    session,
                                    contribution,
                                    shopping_list.current_generation_revision_id,
                                )
                                if command.fulfilled
                                else Decimal(0)
                            )
                            (
                                contribution.fulfilment_updated_at,
                                contribution.fulfilment_updated_by_user_id,
                                contribution.fulfilment_updated_by_installation_id,
                            ) = (
                                command.client_wall_time,
                                context.actor_user_id,
                                context.client_installation_id,
                            )
                            if clock is None:
                                session.add(
                                    FieldClock(
                                        organization_id=command.organization_id,
                                        entity_kind="shopping_contribution",
                                        entity_id=contribution.id,
                                        field_name="fulfilment_credit",
                                        winning_client_wall_time=command.client_wall_time,
                                        winning_mutation_id=command.mutation_id,
                                    )
                                )
                            else:
                                clock.winning_client_wall_time, clock.winning_mutation_id = (
                                    command.client_wall_time,
                                    command.mutation_id,
                                )
                        else:
                            outcome = "partially_superseded"
                        affected, records = (
                            (contribution.id,),
                            [
                                await _contribution_record(session, contribution),
                                await _row_record(session, row),
                            ],
                        )
                    else:
                        contributions = tuple(
                            (
                                await session.scalars(
                                    select(ShoppingContribution)
                                    .where(
                                        ShoppingContribution.shopping_ingredient_row_id == row.id
                                    )
                                    .order_by(ShoppingContribution.id)
                                    .with_for_update(of=ShoppingContribution)
                                )
                            ).all()
                        )
                        # Do not let an older aggregate action overwrite a newer individual
                        # checkbox.  It is all-or-nothing: when any involved field lost its
                        # LWW comparison, this aggregate action changes no credit at all.
                        generated_quantities = {
                            item.id: await _current_generated_quantity(
                                session, item, shopping_list.current_generation_revision_id
                            )
                            for item in contributions
                        }
                        active_contribution_ids = set(
                            (
                                await session.scalars(
                                    select(
                                        ShoppingContributionSnapshot.shopping_contribution_id
                                    ).where(
                                        ShoppingContributionSnapshot.generation_revision_id
                                        == shopping_list.current_generation_revision_id,
                                        ShoppingContributionSnapshot.active_in_revision.is_(True),
                                        ShoppingContributionSnapshot.shopping_contribution_id.in_(
                                            [item.id for item in contributions]
                                        ),
                                    )
                                )
                            ).all()
                        )
                        touched = (
                            contributions
                            if not command.fulfilled
                            else tuple(
                                item for item in contributions if item.id in active_contribution_ids
                            )
                        )
                        clocks = {
                            clock.entity_id: clock
                            for clock in (
                                await session.scalars(
                                    select(FieldClock)
                                    .where(
                                        FieldClock.organization_id == command.organization_id,
                                        FieldClock.entity_kind == "shopping_contribution",
                                        FieldClock.field_name == "fulfilment_credit",
                                        FieldClock.entity_id.in_([item.id for item in touched]),
                                    )
                                    .with_for_update(of=FieldClock)
                                )
                            ).all()
                        }
                        aggregate_clock = await session.scalar(
                            select(FieldClock)
                            .where(
                                FieldClock.organization_id == command.organization_id,
                                FieldClock.entity_kind == "shopping_ingredient_row",
                                FieldClock.entity_id == row.id,
                                FieldClock.field_name == "aggregate_fulfilment_credit",
                            )
                            .with_for_update(of=FieldClock)
                        )
                        wins_all = all(
                            clock is None
                            or (command.client_wall_time, command.mutation_id)
                            > (clock.winning_client_wall_time, clock.winning_mutation_id)
                            for clock in (
                                *(clocks.get(item.id) for item in touched),
                                aggregate_clock,
                            )
                        )
                        if not wins_all:
                            error = _error(
                                (FieldViolation("fulfilment", "superseded_by_newer_change"),)
                            )
                            session.add(
                                _operation_mutation(
                                    command,
                                    context,
                                    role,
                                    request_hash,
                                    "rejected",
                                    _error_payload(error),
                                )
                            )
                        for item in touched if wins_all else ():
                            item.fulfilment_credit = (
                                generated_quantities[item.id] if command.fulfilled else Decimal(0)
                            )
                            (
                                item.fulfilment_updated_at,
                                item.fulfilment_updated_by_user_id,
                                item.fulfilment_updated_by_installation_id,
                            ) = (
                                command.client_wall_time,
                                context.actor_user_id,
                                context.client_installation_id,
                            )
                            clock = clocks.get(item.id)
                            if clock is None:
                                session.add(
                                    FieldClock(
                                        organization_id=command.organization_id,
                                        entity_kind="shopping_contribution",
                                        entity_id=item.id,
                                        field_name="fulfilment_credit",
                                        winning_client_wall_time=command.client_wall_time,
                                        winning_mutation_id=command.mutation_id,
                                    )
                                )
                            else:
                                clock.winning_client_wall_time, clock.winning_mutation_id = (
                                    command.client_wall_time,
                                    command.mutation_id,
                                )
                        if wins_all:
                            contribution_credit = sum(
                                (item.fulfilment_credit for item in contributions), Decimal(0)
                            )
                            automatic_target = max(
                                Decimal(0),
                                await _row_generated_quantity(
                                    session, row, shopping_list.current_generation_revision_id
                                )
                                - row.available_supply_quantity,
                            )
                            target = (
                                row.manual_purchase_target
                                if row.manual_purchase_target is not None
                                else automatic_target
                            )
                            row.aggregate_fulfilment_credit = (
                                max(Decimal(0), target - contribution_credit)
                                if command.fulfilled
                                else Decimal(0)
                            )
                            (
                                row.aggregate_credit_updated_at,
                                row.aggregate_credit_updated_by_user_id,
                                row.aggregate_credit_updated_by_installation_id,
                            ) = (
                                command.client_wall_time,
                                context.actor_user_id,
                                context.client_installation_id,
                            )
                            if aggregate_clock is None:
                                session.add(
                                    FieldClock(
                                        organization_id=command.organization_id,
                                        entity_kind="shopping_ingredient_row",
                                        entity_id=row.id,
                                        field_name="aggregate_fulfilment_credit",
                                        winning_client_wall_time=command.client_wall_time,
                                        winning_mutation_id=command.mutation_id,
                                    )
                                )
                            else:
                                aggregate_clock.winning_client_wall_time = command.client_wall_time
                                aggregate_clock.winning_mutation_id = command.mutation_id
                        affected, records = (
                            tuple(item.id for item in contributions),
                            [
                                await _row_record(session, row),
                                *[
                                    await _contribution_record(session, item)
                                    for item in contributions
                                ],
                            ],
                        )
                    if error is None:
                        first, last = await _reserve_change_range(
                            session, command.organization_id, command.mutation_id, len(records)
                        )
                        result = ShoppingOperationResult(
                            command.mutation_id,
                            shopping_list.id,
                            row.id,
                            affected,
                            first,
                            last,
                            False,
                            outcome,
                        )
                        session.add_all(
                            OrganizationChange(
                                organization_id=command.organization_id,
                                sequence=first + index,
                                mutation_id=command.mutation_id,
                                entity_id=entity_id,
                                entity_kind=kind,
                                operation="upsert",
                                payload={"record_schema_version": 1, "record": record},
                            )
                            for index, (kind, entity_id, record) in enumerate(records)
                        )
                        session.add(
                            _operation_mutation(
                                command,
                                context,
                                role,
                                request_hash,
                                outcome,
                                _operation_result_payload(result),
                                first,
                                last,
                            )
                        )
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("Shopping operation produced no outcome")
    return result


async def set_shopping_available_supply(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetShoppingAvailableSupplyCommand,
) -> ShoppingOperationResult:
    return await _apply_shopping_operation(session_factory, context, command)


async def set_shopping_manual_purchase_target(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetShoppingManualPurchaseTargetCommand,
) -> ShoppingOperationResult:
    return await _apply_shopping_operation(session_factory, context, command)


async def set_shopping_store_section_override(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetShoppingStoreSectionOverrideCommand,
) -> ShoppingOperationResult:
    return await _apply_shopping_operation(session_factory, context, command)


async def set_shopping_row_note(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetShoppingRowNoteCommand,
) -> ShoppingOperationResult:
    return await _apply_shopping_operation(session_factory, context, command)


async def set_shopping_contribution_fulfilment(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetShoppingContributionFulfilmentCommand,
) -> ShoppingOperationResult:
    return await _apply_shopping_operation(session_factory, context, command)


async def set_shopping_row_fulfilment(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetShoppingRowFulfilmentCommand,
) -> ShoppingOperationResult:
    return await _apply_shopping_operation(session_factory, context, command)


AD_HOC_CREATE_COMMAND_KIND = "shopping_list.create_ad_hoc_item"
AD_HOC_FULFILMENT_COMMAND_KIND = "shopping_list.set_ad_hoc_item_fulfilment"
AD_HOC_LIFECYCLE_COMMAND_KIND = "shopping_list.ad_hoc_item_lifecycle"
AD_HOC_UPDATE_COMMAND_KIND = "shopping_list.update_ad_hoc_item"


@dataclass(frozen=True, slots=True)
class CreateAdHocShoppingItemCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    ad_hoc_shopping_item_id: UUID
    name: str
    target_amount: Decimal
    unit_id: UUID
    store_section_id: UUID
    client_wall_time: datetime
    note: str | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SetAdHocShoppingItemFulfilmentCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    ad_hoc_shopping_item_id: UUID
    fulfilled: bool
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SetAdHocShoppingItemLifecycleCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    ad_hoc_shopping_item_id: UUID
    operation: Literal["retire", "restore"]
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class UpdateAdHocShoppingItemCommand:
    mutation_id: UUID
    organization_id: UUID
    shopping_list_id: UUID
    ad_hoc_shopping_item_id: UUID
    name: str
    target_amount: Decimal
    unit_id: UUID
    store_section_id: UUID
    note: str | None
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CreateAdHocShoppingItemResult:
    mutation_id: UUID
    shopping_list_id: UUID
    ad_hoc_shopping_item_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


@dataclass(frozen=True, slots=True)
class SetAdHocShoppingItemFulfilmentResult:
    mutation_id: UUID
    shopping_list_id: UUID
    ad_hoc_shopping_item_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


@dataclass(frozen=True, slots=True)
class SetAdHocShoppingItemLifecycleResult:
    mutation_id: UUID
    shopping_list_id: UUID
    ad_hoc_shopping_item_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


@dataclass(frozen=True, slots=True)
class UpdateAdHocShoppingItemResult:
    mutation_id: UUID
    shopping_list_id: UUID
    ad_hoc_shopping_item_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


def _ad_hoc_record(item: AdHocShoppingItem) -> dict[str, object]:
    return {
        "id": str(item.id),
        "organization_id": str(item.organization_id),
        "event_id": str(item.event_id),
        "shopping_list_id": str(item.shopping_list_id),
        "name": item.name,
        "target_amount": _canonical_decimal(item.target_amount),
        "unit_id": str(item.unit_id),
        "store_section_id": str(item.store_section_id),
        "note": item.note,
        "fulfilment_credit": _canonical_decimal(item.fulfilment_credit),
        "fulfilment_updated_at": (
            item.fulfilment_updated_at.isoformat()
            if item.fulfilment_updated_at is not None
            else None
        ),
        "fulfilment_updated_by_user_id": (
            str(item.fulfilment_updated_by_user_id)
            if item.fulfilment_updated_by_user_id is not None
            else None
        ),
        "fulfilment_updated_by_installation_id": (
            str(item.fulfilment_updated_by_installation_id)
            if item.fulfilment_updated_by_installation_id is not None
            else None
        ),
        "created_at": item.created_at.isoformat(),
        "created_by_user_id": str(item.created_by_user_id),
        "retired_at": item.retired_at.isoformat() if item.retired_at is not None else None,
        "retired_by_user_id": (
            str(item.retired_by_user_id) if item.retired_by_user_id is not None else None
        ),
    }


def _ad_hoc_request_hash(command: CreateAdHocShoppingItemCommand) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {
                "command_kind": AD_HOC_CREATE_COMMAND_KIND,
                "command_schema_version": COMMAND_SCHEMA_VERSION,
                "mutation_id": _raw_uuid(command.mutation_id),
                "organization_id": _raw_uuid(command.organization_id),
                "shopping_list_id": _raw_uuid(command.shopping_list_id),
                "ad_hoc_shopping_item_id": _raw_uuid(command.ad_hoc_shopping_item_id),
                "name": _canonical_name(command.name)
                if isinstance(command.name, str)
                else _invalid(command.name),
                "target_amount": _canonical_decimal(command.target_amount)
                if isinstance(command.target_amount, Decimal) and command.target_amount.is_finite()
                else _invalid(command.target_amount),
                "unit_id": _raw_uuid(command.unit_id),
                "store_section_id": _raw_uuid(command.store_section_id),
                "note": command.note,
                "client_wall_time": _raw_time(command.client_wall_time),
                "logical_operation_id": _raw_uuid(command.logical_operation_id)
                if command.logical_operation_id is not None
                else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _ad_hoc_violations(command: CreateAdHocShoppingItemCommand) -> tuple[FieldViolation, ...]:
    violations: list[FieldViolation] = []
    for name in (
        "mutation_id",
        "organization_id",
        "shopping_list_id",
        "ad_hoc_shopping_item_id",
        "unit_id",
        "store_section_id",
    ):
        if not isinstance(getattr(command, name), UUID):
            violations.append(FieldViolation(name, "must_be_uuid"))
    name = _canonical_name(command.name) if isinstance(command.name, str) else ""
    if (
        not isinstance(command.name, str)
        or not name
        or len(name) > 200
        or len(json.dumps(name, ensure_ascii=False).encode()) > MAX_SERIALIZED_NAME_BYTES
    ):
        violations.append(FieldViolation("name", "must_be_nonempty_at_most_200_characters"))
    if (
        not isinstance(command.target_amount, Decimal)
        or not command.target_amount.is_finite()
        or command.target_amount < 0
    ):
        violations.append(FieldViolation("target_amount", "must_be_nonnegative_finite_decimal"))
    if command.note is not None and (not isinstance(command.note, str) or len(command.note) > 4000):
        violations.append(FieldViolation("note", "must_be_null_or_at_most_4000_characters"))
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
    return tuple(violations)


def _ad_hoc_mutation(
    command: CreateAdHocShoppingItemCommand,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=command.mutation_id,
        logical_operation_id=command.logical_operation_id,
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if isinstance(command.client_wall_time, datetime) and command.client_wall_time.tzinfo
        else datetime(1970, 1, 1, tzinfo=UTC),
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=AD_HOC_CREATE_COMMAND_KIND,
        target_identities=[
            {"entity_kind": "shopping_list", "entity_id": str(command.shopping_list_id)},
            {
                "entity_kind": "ad_hoc_shopping_item",
                "entity_id": str(command.ad_hoc_shopping_item_id),
            },
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def create_ad_hoc_shopping_item(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateAdHocShoppingItemCommand,
) -> CreateAdHocShoppingItemResult:
    request_hash = _ad_hoc_request_hash(command)
    error: ApplicationServiceError | None = None
    result: CreateAdHocShoppingItemResult | None = None
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
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != AD_HOC_CREATE_COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if (
                retained.outcome != "accepted"
                or retained.first_change_sequence is None
                or retained.last_change_sequence is None
                or not retained.outcome_payload
            ):
                raise _retained_error(retained)
            try:
                payload = retained.outcome_payload["ad_hoc_shopping_item"]
                assert isinstance(payload, dict)
                return CreateAdHocShoppingItemResult(
                    mutation_id,
                    UUID(str(payload["shopping_list_id"])),
                    UUID(str(payload["id"])),
                    retained.first_change_sequence,
                    retained.last_change_sequence,
                    True,
                )
            except (AssertionError, KeyError, ValueError, TypeError) as exc:
                raise RuntimeError("Retained ad-hoc item has invalid outcome payload") from exc
        if violations := _ad_hoc_violations(command):
            error = _error(violations)
        elif command.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            error = ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        if error is None:
            shopping_list = await session.scalar(
                select(ShoppingList)
                .join(Event, Event.id == ShoppingList.event_id)
                .where(
                    ShoppingList.id == command.shopping_list_id,
                    ShoppingList.organization_id == command.organization_id,
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ShoppingList, Event))
            )
            if shopping_list is None:
                error = _error((FieldViolation("shopping_list_id", "not_found"),))
            section = (
                await session.scalar(
                    select(StoreSection.id).where(
                        StoreSection.id == command.store_section_id,
                        StoreSection.organization_id == command.organization_id,
                        StoreSection.retired_at.is_(None),
                    )
                )
                if error is None
                else None
            )
            unit = (
                await session.scalar(
                    select(UnitDefinition.id).where(
                        UnitDefinition.id == command.unit_id,
                        UnitDefinition.retired_at.is_(None),
                        UnitDefinition.allows_ingredient_quantity.is_(True),
                        or_(
                            UnitDefinition.organization_id.is_(None),
                            UnitDefinition.organization_id == command.organization_id,
                        ),
                    )
                )
                if error is None
                else None
            )
            if error is None and section is None:
                error = _error((FieldViolation("store_section_id", "not_found"),))
            if error is None and unit is None:
                error = _error((FieldViolation("unit_id", "not_found"),))
            if (
                error is None
                and await session.get(AdHocShoppingItem, command.ad_hoc_shopping_item_id)
                is not None
            ):
                error = _error((FieldViolation("ad_hoc_shopping_item_id", "already_exists"),))
            if error is None:
                item = AdHocShoppingItem(
                    id=command.ad_hoc_shopping_item_id,
                    organization_id=command.organization_id,
                    event_id=shopping_list.event_id,
                    shopping_list_id=shopping_list.id,
                    name=_canonical_name(command.name),
                    target_amount=command.target_amount,
                    unit_id=command.unit_id,
                    store_section_id=command.store_section_id,
                    note=command.note,
                    created_by_user_id=context.actor_user_id,
                )
                session.add(item)
                await session.flush()
                record = _ad_hoc_record(item)
                record["field_clocks"] = await _field_clock_metadata(
                    session,
                    item.organization_id,
                    "ad_hoc_shopping_item",
                    item.id,
                    _AD_HOC_SYNCHRONIZABLE_FIELDS,
                )
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 1
                )
                result = CreateAdHocShoppingItemResult(
                    command.mutation_id, shopping_list.id, item.id, first, last, False
                )
                session.add(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first,
                        mutation_id=command.mutation_id,
                        entity_id=item.id,
                        entity_kind="ad_hoc_shopping_item",
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": record},
                    )
                )
                session.add(
                    _ad_hoc_mutation(
                        command,
                        context,
                        role,
                        request_hash,
                        "accepted",
                        {"ad_hoc_shopping_item": record},
                        first,
                        last,
                    )
                )
        if error is not None:
            session.add(
                _ad_hoc_mutation(
                    command, context, role, request_hash, "rejected", _error_payload(error)
                )
            )
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("Ad-hoc shopping item produced no outcome")
    return result


def _ad_hoc_fulfilment_hash(command: SetAdHocShoppingItemFulfilmentCommand) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {
                "command_kind": AD_HOC_FULFILMENT_COMMAND_KIND,
                "command_schema_version": COMMAND_SCHEMA_VERSION,
                "mutation_id": _raw_uuid(command.mutation_id),
                "organization_id": _raw_uuid(command.organization_id),
                "shopping_list_id": _raw_uuid(command.shopping_list_id),
                "ad_hoc_shopping_item_id": _raw_uuid(command.ad_hoc_shopping_item_id),
                "fulfilled": command.fulfilled
                if isinstance(command.fulfilled, bool)
                else _invalid(command.fulfilled),
                "client_wall_time": _raw_time(command.client_wall_time),
                "logical_operation_id": _raw_uuid(command.logical_operation_id)
                if command.logical_operation_id is not None
                else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _ad_hoc_fulfilment_violations(
    command: SetAdHocShoppingItemFulfilmentCommand,
) -> tuple[FieldViolation, ...]:
    violations = [
        FieldViolation(name, "must_be_uuid")
        for name in (
            "mutation_id",
            "organization_id",
            "shopping_list_id",
            "ad_hoc_shopping_item_id",
        )
        if not isinstance(getattr(command, name), UUID)
    ]
    if not isinstance(command.fulfilled, bool):
        violations.append(FieldViolation("fulfilled", "must_be_boolean"))
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
    return tuple(violations)


def _ad_hoc_fulfilment_mutation(
    command: SetAdHocShoppingItemFulfilmentCommand,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "partially_superseded", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        else datetime(1970, 1, 1, tzinfo=UTC),
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=AD_HOC_FULFILMENT_COMMAND_KIND,
        target_identities=[
            {
                "entity_kind": "shopping_list",
                "entity_id": str(command.shopping_list_id),
            },
            {
                "entity_kind": "ad_hoc_shopping_item",
                "entity_id": str(command.ad_hoc_shopping_item_id),
            },
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def set_ad_hoc_shopping_item_fulfilment(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetAdHocShoppingItemFulfilmentCommand,
) -> SetAdHocShoppingItemFulfilmentResult:
    request_hash = _ad_hoc_fulfilment_hash(command)
    error: ApplicationServiceError | None = None
    result: SetAdHocShoppingItemFulfilmentResult | None = None
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
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != AD_HOC_FULFILMENT_COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if (
                retained.outcome not in ("accepted", "partially_superseded")
                or retained.first_change_sequence is None
                or retained.last_change_sequence is None
            ):
                raise _retained_error(retained)
            outcome: Literal["accepted", "partially_superseded"] = (
                "accepted" if retained.outcome == "accepted" else "partially_superseded"
            )
            return SetAdHocShoppingItemFulfilmentResult(
                command.mutation_id,
                command.shopping_list_id,
                command.ad_hoc_shopping_item_id,
                retained.first_change_sequence,
                retained.last_change_sequence,
                True,
                outcome,
            )
        if violations := _ad_hoc_fulfilment_violations(command):
            error = _error(violations)
        elif command.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            error = ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        shopping_list: ShoppingList | None = None
        if error is None:
            shopping_list = await session.scalar(
                select(ShoppingList)
                .join(Event, Event.id == ShoppingList.event_id)
                .where(
                    ShoppingList.id == command.shopping_list_id,
                    ShoppingList.organization_id == command.organization_id,
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ShoppingList, Event))
            )
            if shopping_list is None:
                archived_list_id = await session.scalar(
                    select(ShoppingList.id).where(
                        ShoppingList.id == command.shopping_list_id,
                        ShoppingList.organization_id == command.organization_id,
                    )
                )
                error = (
                    ApplicationServiceError("archived_event", retry_same_identity=False)
                    if archived_list_id is not None
                    else _error((FieldViolation("shopping_list_id", "not_found"),))
                )
        item: AdHocShoppingItem | None = None
        if error is None and shopping_list is not None:
            item = await session.scalar(
                select(AdHocShoppingItem)
                .where(
                    AdHocShoppingItem.id == command.ad_hoc_shopping_item_id,
                    AdHocShoppingItem.shopping_list_id == shopping_list.id,
                    AdHocShoppingItem.organization_id == command.organization_id,
                    AdHocShoppingItem.retired_at.is_(None),
                )
                .with_for_update(of=AdHocShoppingItem)
            )
            if item is None:
                error = _error((FieldViolation("ad_hoc_shopping_item_id", "not_found"),))
        if error is None and item is not None:
            clock = await session.scalar(
                select(FieldClock)
                .where(
                    FieldClock.organization_id == command.organization_id,
                    FieldClock.entity_kind == "ad_hoc_shopping_item",
                    FieldClock.entity_id == item.id,
                    FieldClock.field_name == "fulfilment_credit",
                )
                .with_for_update(of=FieldClock)
            )
            wins = clock is None or (command.client_wall_time, command.mutation_id) > (
                clock.winning_client_wall_time,
                clock.winning_mutation_id,
            )
            if wins:
                item.fulfilment_credit = item.target_amount if command.fulfilled else Decimal(0)
                item.fulfilment_updated_at = command.client_wall_time
                item.fulfilment_updated_by_user_id = context.actor_user_id
                item.fulfilment_updated_by_installation_id = context.client_installation_id
                if clock is None:
                    session.add(
                        FieldClock(
                            organization_id=command.organization_id,
                            entity_kind="ad_hoc_shopping_item",
                            entity_id=item.id,
                            field_name="fulfilment_credit",
                            winning_client_wall_time=command.client_wall_time,
                            winning_mutation_id=command.mutation_id,
                        )
                    )
                else:
                    clock.winning_client_wall_time, clock.winning_mutation_id = (
                        command.client_wall_time,
                        command.mutation_id,
                    )
            record = _ad_hoc_record(item)
            record["field_clocks"] = await _field_clock_metadata(
                session,
                item.organization_id,
                "ad_hoc_shopping_item",
                item.id,
                _AD_HOC_SYNCHRONIZABLE_FIELDS,
            )
            first, last = await _reserve_change_range(
                session, command.organization_id, command.mutation_id, 1
            )
            outcome: Literal["accepted", "partially_superseded"] = (
                "accepted" if wins else "partially_superseded"
            )
            session.add(
                OrganizationChange(
                    organization_id=command.organization_id,
                    sequence=first,
                    mutation_id=command.mutation_id,
                    entity_id=item.id,
                    entity_kind="ad_hoc_shopping_item",
                    operation="upsert",
                    payload={"record_schema_version": 1, "record": record},
                )
            )
            session.add(
                _ad_hoc_fulfilment_mutation(
                    command,
                    context,
                    role,
                    request_hash,
                    outcome,
                    {"ad_hoc_shopping_item": record},
                    first,
                    last,
                )
            )
            result = SetAdHocShoppingItemFulfilmentResult(
                command.mutation_id,
                command.shopping_list_id,
                item.id,
                first,
                last,
                False,
                outcome,
            )
        if error is not None:
            session.add(
                _ad_hoc_fulfilment_mutation(
                    command, context, role, request_hash, "rejected", _error_payload(error)
                )
            )
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("Ad-hoc fulfilment produced no outcome")
    return result


def _ad_hoc_lifecycle_hash(command: SetAdHocShoppingItemLifecycleCommand) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {
                "command_kind": AD_HOC_LIFECYCLE_COMMAND_KIND,
                "command_schema_version": COMMAND_SCHEMA_VERSION,
                "mutation_id": _raw_uuid(command.mutation_id),
                "organization_id": _raw_uuid(command.organization_id),
                "shopping_list_id": _raw_uuid(command.shopping_list_id),
                "ad_hoc_shopping_item_id": _raw_uuid(command.ad_hoc_shopping_item_id),
                "operation": command.operation,
                "client_wall_time": _raw_time(command.client_wall_time),
                "logical_operation_id": _raw_uuid(command.logical_operation_id)
                if command.logical_operation_id is not None
                else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _ad_hoc_lifecycle_violations(
    command: SetAdHocShoppingItemLifecycleCommand,
) -> tuple[FieldViolation, ...]:
    violations = [
        FieldViolation(name, "must_be_uuid")
        for name in (
            "mutation_id",
            "organization_id",
            "shopping_list_id",
            "ad_hoc_shopping_item_id",
        )
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
    return tuple(violations)


def _ad_hoc_lifecycle_mutation(
    command: SetAdHocShoppingItemLifecycleCommand,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "partially_superseded", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        else datetime(1970, 1, 1, tzinfo=UTC),
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=AD_HOC_LIFECYCLE_COMMAND_KIND,
        target_identities=[
            {"entity_kind": "shopping_list", "entity_id": str(command.shopping_list_id)},
            {
                "entity_kind": "ad_hoc_shopping_item",
                "entity_id": str(command.ad_hoc_shopping_item_id),
            },
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def set_ad_hoc_shopping_item_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetAdHocShoppingItemLifecycleCommand,
) -> SetAdHocShoppingItemLifecycleResult:
    request_hash = _ad_hoc_lifecycle_hash(command)
    error: ApplicationServiceError | None = None
    result: SetAdHocShoppingItemLifecycleResult | None = None
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
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != AD_HOC_LIFECYCLE_COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if (
                retained.outcome not in ("accepted", "partially_superseded")
                or retained.first_change_sequence is None
                or retained.last_change_sequence is None
            ):
                raise _retained_error(retained)
            return SetAdHocShoppingItemLifecycleResult(
                command.mutation_id,
                command.shopping_list_id,
                command.ad_hoc_shopping_item_id,
                retained.first_change_sequence,
                retained.last_change_sequence,
                True,
                "accepted" if retained.outcome == "accepted" else "partially_superseded",
            )
        if violations := _ad_hoc_lifecycle_violations(command):
            error = _error(violations)
        elif command.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            error = ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        shopping_list: ShoppingList | None = None
        if error is None:
            shopping_list = await session.scalar(
                select(ShoppingList)
                .join(Event, Event.id == ShoppingList.event_id)
                .where(
                    ShoppingList.id == command.shopping_list_id,
                    ShoppingList.organization_id == command.organization_id,
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ShoppingList, Event))
            )
            if shopping_list is None:
                archived = await session.scalar(
                    select(ShoppingList.id).where(
                        ShoppingList.id == command.shopping_list_id,
                        ShoppingList.organization_id == command.organization_id,
                    )
                )
                error = (
                    ApplicationServiceError("archived_event", retry_same_identity=False)
                    if archived is not None
                    else _error((FieldViolation("shopping_list_id", "not_found"),))
                )
        item: AdHocShoppingItem | None = None
        if error is None and shopping_list is not None:
            item = await session.scalar(
                select(AdHocShoppingItem)
                .where(
                    AdHocShoppingItem.id == command.ad_hoc_shopping_item_id,
                    AdHocShoppingItem.shopping_list_id == shopping_list.id,
                    AdHocShoppingItem.organization_id == command.organization_id,
                )
                .with_for_update(of=AdHocShoppingItem)
            )
            if item is None:
                error = _error((FieldViolation("ad_hoc_shopping_item_id", "not_found"),))
        if error is None and item is not None:
            clock = await session.scalar(
                select(FieldClock)
                .where(
                    FieldClock.organization_id == command.organization_id,
                    FieldClock.entity_kind == "ad_hoc_shopping_item",
                    FieldClock.entity_id == item.id,
                    FieldClock.field_name == "lifecycle",
                )
                .with_for_update(of=FieldClock)
            )
            wins = clock is None or (command.client_wall_time, command.mutation_id) > (
                clock.winning_client_wall_time,
                clock.winning_mutation_id,
            )
            if wins:
                item.retired_at, item.retired_by_user_id = (
                    (datetime.now(UTC), context.actor_user_id)
                    if command.operation == "retire"
                    else (None, None)
                )
                if clock is None:
                    session.add(
                        FieldClock(
                            organization_id=command.organization_id,
                            entity_kind="ad_hoc_shopping_item",
                            entity_id=item.id,
                            field_name="lifecycle",
                            winning_client_wall_time=command.client_wall_time,
                            winning_mutation_id=command.mutation_id,
                        )
                    )
                else:
                    clock.winning_client_wall_time, clock.winning_mutation_id = (
                        command.client_wall_time,
                        command.mutation_id,
                    )
            record = _ad_hoc_record(item)
            record["field_clocks"] = await _field_clock_metadata(
                session,
                item.organization_id,
                "ad_hoc_shopping_item",
                item.id,
                _AD_HOC_SYNCHRONIZABLE_FIELDS,
            )
            first, last = await _reserve_change_range(
                session, command.organization_id, command.mutation_id, 1
            )
            outcome: Literal["accepted", "partially_superseded"] = (
                "accepted" if wins else "partially_superseded"
            )
            session.add(
                OrganizationChange(
                    organization_id=command.organization_id,
                    sequence=first,
                    mutation_id=command.mutation_id,
                    entity_id=item.id,
                    entity_kind="ad_hoc_shopping_item",
                    operation="upsert",
                    payload={"record_schema_version": 1, "record": record},
                )
            )
            session.add(
                _ad_hoc_lifecycle_mutation(
                    command,
                    context,
                    role,
                    request_hash,
                    outcome,
                    {"ad_hoc_shopping_item": record},
                    first,
                    last,
                )
            )
            result = SetAdHocShoppingItemLifecycleResult(
                command.mutation_id, command.shopping_list_id, item.id, first, last, False, outcome
            )
        if error is not None:
            session.add(
                _ad_hoc_lifecycle_mutation(
                    command, context, role, request_hash, "rejected", _error_payload(error)
                )
            )
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("Ad-hoc lifecycle produced no outcome")
    return result


def _ad_hoc_update_hash(command: UpdateAdHocShoppingItemCommand) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {
                "command_kind": AD_HOC_UPDATE_COMMAND_KIND,
                "command_schema_version": COMMAND_SCHEMA_VERSION,
                "mutation_id": _raw_uuid(command.mutation_id),
                "organization_id": _raw_uuid(command.organization_id),
                "shopping_list_id": _raw_uuid(command.shopping_list_id),
                "ad_hoc_shopping_item_id": _raw_uuid(command.ad_hoc_shopping_item_id),
                "name": _canonical_name(command.name)
                if isinstance(command.name, str)
                else _invalid(command.name),
                "target_amount": _canonical_decimal(command.target_amount)
                if isinstance(command.target_amount, Decimal) and command.target_amount.is_finite()
                else _invalid(command.target_amount),
                "unit_id": _raw_uuid(command.unit_id),
                "store_section_id": _raw_uuid(command.store_section_id),
                "note": command.note,
                "client_wall_time": _raw_time(command.client_wall_time),
                "logical_operation_id": _raw_uuid(command.logical_operation_id)
                if command.logical_operation_id is not None
                else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _ad_hoc_update_violations(
    command: UpdateAdHocShoppingItemCommand,
) -> tuple[FieldViolation, ...]:
    violations = [
        FieldViolation(name, "must_be_uuid")
        for name in (
            "mutation_id",
            "organization_id",
            "shopping_list_id",
            "ad_hoc_shopping_item_id",
            "unit_id",
            "store_section_id",
        )
        if not isinstance(getattr(command, name), UUID)
    ]
    name = _canonical_name(command.name) if isinstance(command.name, str) else ""
    if (
        not isinstance(command.name, str)
        or not name
        or len(name) > 200
        or len(json.dumps(name, ensure_ascii=False).encode()) > MAX_SERIALIZED_NAME_BYTES
    ):
        violations.append(FieldViolation("name", "must_be_nonempty_at_most_200_characters"))
    if (
        not isinstance(command.target_amount, Decimal)
        or not command.target_amount.is_finite()
        or command.target_amount < 0
    ):
        violations.append(FieldViolation("target_amount", "must_be_nonnegative_finite_decimal"))
    if command.note is not None and (not isinstance(command.note, str) or len(command.note) > 4000):
        violations.append(FieldViolation("note", "must_be_null_or_at_most_4000_characters"))
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
    return tuple(violations)


def _ad_hoc_update_mutation(
    command: UpdateAdHocShoppingItemCommand,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "partially_superseded", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        else datetime(1970, 1, 1, tzinfo=UTC),
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=AD_HOC_UPDATE_COMMAND_KIND,
        target_identities=[
            {"entity_kind": "shopping_list", "entity_id": str(command.shopping_list_id)},
            {
                "entity_kind": "ad_hoc_shopping_item",
                "entity_id": str(command.ad_hoc_shopping_item_id),
            },
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def update_ad_hoc_shopping_item(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: UpdateAdHocShoppingItemCommand,
) -> UpdateAdHocShoppingItemResult:
    request_hash = _ad_hoc_update_hash(command)
    error: ApplicationServiceError | None = None
    result: UpdateAdHocShoppingItemResult | None = None
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
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != AD_HOC_UPDATE_COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if (
                retained.outcome not in ("accepted", "partially_superseded")
                or retained.first_change_sequence is None
                or retained.last_change_sequence is None
            ):
                raise _retained_error(retained)
            return UpdateAdHocShoppingItemResult(
                command.mutation_id,
                command.shopping_list_id,
                command.ad_hoc_shopping_item_id,
                retained.first_change_sequence,
                retained.last_change_sequence,
                True,
                "accepted" if retained.outcome == "accepted" else "partially_superseded",
            )
        if violations := _ad_hoc_update_violations(command):
            error = _error(violations)
        elif command.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            error = ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        shopping_list: ShoppingList | None = None
        if error is None:
            shopping_list = await session.scalar(
                select(ShoppingList)
                .join(Event, Event.id == ShoppingList.event_id)
                .where(
                    ShoppingList.id == command.shopping_list_id,
                    ShoppingList.organization_id == command.organization_id,
                    Event.organization_id == command.organization_id,
                    Event.lifecycle == "active",
                )
                .with_for_update(of=(ShoppingList, Event))
            )
            if shopping_list is None:
                archived = await session.scalar(
                    select(ShoppingList.id).where(
                        ShoppingList.id == command.shopping_list_id,
                        ShoppingList.organization_id == command.organization_id,
                    )
                )
                error = (
                    ApplicationServiceError("archived_event", retry_same_identity=False)
                    if archived is not None
                    else _error((FieldViolation("shopping_list_id", "not_found"),))
                )
        section = (
            await session.scalar(
                select(StoreSection.id).where(
                    StoreSection.id == command.store_section_id,
                    StoreSection.organization_id == command.organization_id,
                    StoreSection.retired_at.is_(None),
                )
            )
            if error is None
            else None
        )
        unit = (
            await session.scalar(
                select(UnitDefinition.id).where(
                    UnitDefinition.id == command.unit_id,
                    UnitDefinition.retired_at.is_(None),
                    UnitDefinition.allows_ingredient_quantity.is_(True),
                    or_(
                        UnitDefinition.organization_id.is_(None),
                        UnitDefinition.organization_id == command.organization_id,
                    ),
                )
            )
            if error is None
            else None
        )
        if error is None and section is None:
            error = _error((FieldViolation("store_section_id", "not_found"),))
        if error is None and unit is None:
            error = _error((FieldViolation("unit_id", "not_found"),))
        item: AdHocShoppingItem | None = None
        if error is None and shopping_list is not None:
            item = await session.scalar(
                select(AdHocShoppingItem)
                .where(
                    AdHocShoppingItem.id == command.ad_hoc_shopping_item_id,
                    AdHocShoppingItem.shopping_list_id == shopping_list.id,
                    AdHocShoppingItem.organization_id == command.organization_id,
                    AdHocShoppingItem.retired_at.is_(None),
                )
                .with_for_update(of=AdHocShoppingItem)
            )
            if item is None:
                error = _error((FieldViolation("ad_hoc_shopping_item_id", "not_found"),))
        if error is None and item is not None:
            fields = {
                "name": _canonical_name(command.name),
                "target_amount": command.target_amount,
                "unit_id": command.unit_id,
                "store_section_id": command.store_section_id,
                "note": command.note,
            }
            clocks = {
                clock.field_name: clock
                for clock in (
                    await session.scalars(
                        select(FieldClock)
                        .where(
                            FieldClock.organization_id == command.organization_id,
                            FieldClock.entity_kind == "ad_hoc_shopping_item",
                            FieldClock.entity_id == item.id,
                            FieldClock.field_name.in_(tuple(fields)),
                        )
                        .with_for_update(of=FieldClock)
                    )
                ).all()
            }
            winners = tuple(
                name
                for name in fields
                if (clock := clocks.get(name)) is None
                or (command.client_wall_time, command.mutation_id)
                > (clock.winning_client_wall_time, clock.winning_mutation_id)
            )
            for name in winners:
                setattr(item, name, fields[name])
                if (clock := clocks.get(name)) is None:
                    session.add(
                        FieldClock(
                            organization_id=command.organization_id,
                            entity_kind="ad_hoc_shopping_item",
                            entity_id=item.id,
                            field_name=name,
                            winning_client_wall_time=command.client_wall_time,
                            winning_mutation_id=command.mutation_id,
                        )
                    )
                else:
                    clock.winning_client_wall_time, clock.winning_mutation_id = (
                        command.client_wall_time,
                        command.mutation_id,
                    )
            record = _ad_hoc_record(item)
            record["field_clocks"] = await _field_clock_metadata(
                session,
                item.organization_id,
                "ad_hoc_shopping_item",
                item.id,
                _AD_HOC_SYNCHRONIZABLE_FIELDS,
            )
            first, last = await _reserve_change_range(
                session, command.organization_id, command.mutation_id, 1
            )
            outcome: Literal["accepted", "partially_superseded"] = (
                "accepted" if len(winners) == len(fields) else "partially_superseded"
            )
            session.add(
                OrganizationChange(
                    organization_id=command.organization_id,
                    sequence=first,
                    mutation_id=command.mutation_id,
                    entity_id=item.id,
                    entity_kind="ad_hoc_shopping_item",
                    operation="upsert",
                    payload={"record_schema_version": 1, "record": record},
                )
            )
            session.add(
                _ad_hoc_update_mutation(
                    command,
                    context,
                    role,
                    request_hash,
                    outcome,
                    {"ad_hoc_shopping_item": record},
                    first,
                    last,
                )
            )
            result = UpdateAdHocShoppingItemResult(
                command.mutation_id, command.shopping_list_id, item.id, first, last, False, outcome
            )
        if error is not None:
            session.add(
                _ad_hoc_update_mutation(
                    command, context, role, request_hash, "rejected", _error_payload(error)
                )
            )
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("Ad-hoc item update produced no outcome")
    return result
