"""Recipe-catalog application services."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import _reserve_change_range
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import (
    ClientInstallation,
    IngredientVersion,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    Recipe,
    RecipeTag,
    RecipeVersion,
    RecipeVersionIngredientLine,
    RecipeVersionTag,
    SystemRoleAssignment,
    UnitDefinition,
    User,
)

COMMAND_KIND = "recipe.create"
COMMAND_SCHEMA_VERSION = 1
RECIPE_VERSION_TAG_CHANGE_NAMESPACE = UUID("82baf1fe-cee8-4306-b6a8-4d92c10f5c4a")


@dataclass(frozen=True, slots=True)
class RecipeIngredientLineInput:
    id: UUID
    line_key: UUID
    ingredient_version_id: UUID
    base_quantity: Decimal
    position_key: str
    scaling_behavior: Literal["proportional", "fixed"] = "proportional"
    include_in_portion_weight: bool = True
    preferred_display_unit_id: UUID | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class CreateRecipeCommand:
    mutation_id: UUID
    recipe_id: UUID
    recipe_version_id: UUID
    organization_id: UUID
    name: str
    scaling_unit_id: UUID
    base_scaling_amount: Decimal
    client_wall_time: datetime
    ingredient_lines: tuple[RecipeIngredientLineInput, ...]
    description: str | None = None
    recipe_tag_ids: tuple[UUID, ...] = ()
    estimated_diners_per_scaling_unit: Decimal | None = None
    round_suggestions_up: bool = False
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RecipeIngredientLineResult:
    id: UUID
    line_key: UUID
    ingredient_version_id: UUID
    base_quantity: Decimal
    preferred_display_unit_id: UUID | None
    note: str | None
    position_key: str
    scaling_behavior: Literal["proportional", "fixed"]
    include_in_portion_weight: bool


@dataclass(frozen=True, slots=True)
class CreateRecipeResult:
    mutation_id: UUID
    recipe_id: UUID
    recipe_version_id: UUID
    organization_id: UUID
    name: str
    description: str | None
    scaling_unit_id: UUID
    base_scaling_amount: Decimal
    estimated_diners_per_scaling_unit: Decimal | None
    round_suggestions_up: bool
    recipe_tag_ids: tuple[UUID, ...]
    ingredient_lines: tuple[RecipeIngredientLineResult, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


@dataclass(frozen=True, slots=True)
class _PreparedLine:
    id: UUID
    line_key: UUID
    ingredient_version_id: UUID
    base_quantity: Decimal
    preferred_display_unit_id: UUID | None
    note: str | None
    position_key: str
    scaling_behavior: Literal["proportional", "fixed"]
    include_in_portion_weight: bool


@dataclass(frozen=True, slots=True)
class _PreparedCommand:
    mutation_id: UUID
    recipe_id: UUID
    recipe_version_id: UUID
    organization_id: UUID
    name: str
    description: str | None
    scaling_unit_id: UUID
    base_scaling_amount: Decimal
    estimated_diners_per_scaling_unit: Decimal | None
    round_suggestions_up: bool
    client_wall_time: datetime
    recipe_tag_ids: tuple[UUID, ...]
    ingredient_lines: tuple[_PreparedLine, ...]
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


def _canonical_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _canonical_note(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _is_uuid(value: object) -> bool:
    return isinstance(value, UUID)


def _finite_decimal(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) and value.is_finite() else None


def _canonical_decimal_string(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def recipe_version_tag_change_id(recipe_version_id: UUID, recipe_tag_id: UUID) -> UUID:
    """Return the stable sync-record identity for a composite tag association."""

    return uuid5(RECIPE_VERSION_TAG_CHANGE_NAMESPACE, f"{recipe_version_id}:{recipe_tag_id}")


def _prepare_command(command: CreateRecipeCommand) -> _PreparedCommand:
    violations: list[FieldViolation] = []
    name = _canonical_text(command.name) if isinstance(command.name, str) else ""
    if not isinstance(command.name, str) or not name or len(name) > 200:
        violations.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
    description = (
        _canonical_note(command.description) if isinstance(command.description, str) else None
    )
    if command.description is not None and not isinstance(command.description, str):
        violations.append(FieldViolation("description", "must_be_string_or_null"))
    if description == "":
        description = None

    base_scaling_amount = _finite_decimal(command.base_scaling_amount)
    if base_scaling_amount is None or base_scaling_amount <= 0:
        violations.append(FieldViolation("base_scaling_amount", "must_be_positive_finite_decimal"))
        base_scaling_amount = Decimal(1)
    estimated = _finite_decimal(command.estimated_diners_per_scaling_unit)
    if command.estimated_diners_per_scaling_unit is not None and (
        estimated is None or estimated <= 0
    ):
        violations.append(
            FieldViolation(
                "estimated_diners_per_scaling_unit", "must_be_positive_finite_decimal_or_null"
            )
        )
        estimated = None
    if not isinstance(command.round_suggestions_up, bool):
        violations.append(FieldViolation("round_suggestions_up", "must_be_boolean"))
    round_suggestions_up = (
        command.round_suggestions_up if isinstance(command.round_suggestions_up, bool) else False
    )

    wall_time_has_timezone = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not wall_time_has_timezone:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))

    for path, value in (
        ("mutation_id", command.mutation_id),
        ("recipe_id", command.recipe_id),
        ("recipe_version_id", command.recipe_version_id),
        ("organization_id", command.organization_id),
        ("scaling_unit_id", command.scaling_unit_id),
    ):
        if not _is_uuid(value):
            violations.append(FieldViolation(path, "must_be_uuid"))
    if command.logical_operation_id is not None and not _is_uuid(command.logical_operation_id):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))

    tag_ids: list[UUID] = []
    if not isinstance(command.recipe_tag_ids, tuple):
        violations.append(FieldViolation("recipe_tag_ids", "must_be_uuid_tuple"))
    else:
        for index, tag_id in enumerate(command.recipe_tag_ids):
            if not _is_uuid(tag_id):
                violations.append(FieldViolation(f"recipe_tag_ids[{index}]", "must_be_uuid"))
            else:
                tag_ids.append(tag_id)
        if len(set(tag_ids)) != len(tag_ids):
            violations.append(FieldViolation("recipe_tag_ids", "must_not_contain_duplicates"))

    lines: list[_PreparedLine] = []
    if not isinstance(command.ingredient_lines, tuple):
        violations.append(FieldViolation("ingredient_lines", "must_be_tuple"))
    else:
        line_ids: set[UUID] = set()
        line_keys: set[UUID] = set()
        for index, line in enumerate(command.ingredient_lines):
            path = f"ingredient_lines[{index}]"
            if not isinstance(line, RecipeIngredientLineInput):
                violations.append(FieldViolation(path, "must_be_ingredient_line"))
                continue
            invalid_identity = False
            for name_part, value in (
                ("id", line.id),
                ("line_key", line.line_key),
                ("ingredient_version_id", line.ingredient_version_id),
            ):
                if not _is_uuid(value):
                    violations.append(FieldViolation(f"{path}.{name_part}", "must_be_uuid"))
                    invalid_identity = True
            if line.preferred_display_unit_id is not None and not _is_uuid(
                line.preferred_display_unit_id
            ):
                violations.append(
                    FieldViolation(f"{path}.preferred_display_unit_id", "must_be_uuid_or_null")
                )
            quantity = _finite_decimal(line.base_quantity)
            if quantity is None or quantity < 0:
                violations.append(
                    FieldViolation(f"{path}.base_quantity", "must_be_nonnegative_finite_decimal")
                )
                quantity = Decimal(0)
            position_key = line.position_key if isinstance(line.position_key, str) else ""
            if (
                not isinstance(line.position_key, str)
                or not position_key
                or len(position_key) > 255
                or not position_key.isascii()
                or not position_key.isalnum()
            ):
                violations.append(
                    FieldViolation(f"{path}.position_key", "must_be_alphanumeric_at_most_255")
                )
            if line.scaling_behavior not in ("proportional", "fixed"):
                violations.append(
                    FieldViolation(f"{path}.scaling_behavior", "must_be_proportional_or_fixed")
                )
            if not isinstance(line.include_in_portion_weight, bool):
                violations.append(
                    FieldViolation(f"{path}.include_in_portion_weight", "must_be_boolean")
                )
            note = _canonical_note(line.note) if isinstance(line.note, str) else None
            if line.note is not None and not isinstance(line.note, str):
                violations.append(FieldViolation(f"{path}.note", "must_be_string_or_null"))
            if invalid_identity:
                continue
            assert isinstance(line.id, UUID)
            assert isinstance(line.line_key, UUID)
            assert isinstance(line.ingredient_version_id, UUID)
            if line.id in line_ids:
                violations.append(FieldViolation(f"{path}.id", "must_be_unique"))
            if line.line_key in line_keys:
                violations.append(FieldViolation(f"{path}.line_key", "must_be_unique"))
            line_ids.add(line.id)
            line_keys.add(line.line_key)
            lines.append(
                _PreparedLine(
                    id=line.id,
                    line_key=line.line_key,
                    ingredient_version_id=line.ingredient_version_id,
                    base_quantity=quantity,
                    preferred_display_unit_id=(
                        line.preferred_display_unit_id
                        if isinstance(line.preferred_display_unit_id, UUID)
                        else None
                    ),
                    note=note or None,
                    position_key=position_key,
                    scaling_behavior=(
                        line.scaling_behavior
                        if line.scaling_behavior in ("proportional", "fixed")
                        else "proportional"
                    ),
                    include_in_portion_weight=(
                        line.include_in_portion_weight
                        if isinstance(line.include_in_portion_weight, bool)
                        else True
                    ),
                )
            )

    return _PreparedCommand(
        mutation_id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        recipe_id=command.recipe_id if isinstance(command.recipe_id, UUID) else UUID(int=0),
        recipe_version_id=(
            command.recipe_version_id
            if isinstance(command.recipe_version_id, UUID)
            else UUID(int=0)
        ),
        organization_id=(
            command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0)
        ),
        name=name,
        description=description,
        scaling_unit_id=(
            command.scaling_unit_id if isinstance(command.scaling_unit_id, UUID) else UUID(int=0)
        ),
        base_scaling_amount=base_scaling_amount,
        estimated_diners_per_scaling_unit=estimated,
        round_suggestions_up=round_suggestions_up,
        client_wall_time=(
            command.client_wall_time.astimezone(UTC)
            if wall_time_has_timezone
            else datetime(1970, 1, 1, tzinfo=UTC)
        ),
        recipe_tag_ids=tuple(tag_ids),
        ingredient_lines=tuple(lines),
        logical_operation_id=(
            command.logical_operation_id if isinstance(command.logical_operation_id, UUID) else None
        ),
        violations=tuple(violations),
    )


def _request_hash(command: _PreparedCommand) -> bytes:
    semantic_request = {
        "base_scaling_amount": _canonical_decimal_string(command.base_scaling_amount),
        "client_wall_time": command.client_wall_time.isoformat().replace("+00:00", "Z"),
        "command_kind": COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "description": command.description,
        "estimated_diners_per_scaling_unit": (
            _canonical_decimal_string(command.estimated_diners_per_scaling_unit)
            if command.estimated_diners_per_scaling_unit is not None
            else None
        ),
        "ingredient_lines": [
            {
                "base_quantity": _canonical_decimal_string(line.base_quantity),
                "id": str(line.id),
                "include_in_portion_weight": line.include_in_portion_weight,
                "ingredient_version_id": str(line.ingredient_version_id),
                "line_key": str(line.line_key),
                "note": line.note,
                "position_key": line.position_key,
                "preferred_display_unit_id": (
                    str(line.preferred_display_unit_id)
                    if line.preferred_display_unit_id is not None
                    else None
                ),
                "scaling_behavior": line.scaling_behavior,
            }
            for line in command.ingredient_lines
        ],
        "logical_operation_id": (
            str(command.logical_operation_id) if command.logical_operation_id is not None else None
        ),
        "name": command.name,
        "organization_id": str(command.organization_id),
        "recipe_id": str(command.recipe_id),
        "recipe_tag_ids": [str(tag_id) for tag_id in command.recipe_tag_ids],
        "recipe_version_id": str(command.recipe_version_id),
        "round_suggestions_up": command.round_suggestions_up,
        "scaling_unit_id": str(command.scaling_unit_id),
    }
    return hashlib.sha256(
        json.dumps(
            semantic_request, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).digest()


async def _authorize_and_lock_organization(
    session: AsyncSession, context: ExecutionContext, organization_id: UUID
) -> Literal["member", "organization_admin", "system_admin"]:
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
    organization = await session.scalar(
        select(Organization.id)
        .where(Organization.id == organization_id, Organization.retired_at.is_(None))
        .with_for_update(of=Organization)
    )
    if organization is None:
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
    membership = await session.execute(
        select(OrganizationMembership.role)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == context.actor_user_id,
            OrganizationMembership.state == "active",
        )
        .with_for_update(of=OrganizationMembership)
    )
    role = membership.scalar_one_or_none()
    if role not in ("member", "organization_admin"):
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    return cast(Literal["member", "organization_admin"], role)


def _line_result(line: _PreparedLine) -> RecipeIngredientLineResult:
    return RecipeIngredientLineResult(
        id=line.id,
        line_key=line.line_key,
        ingredient_version_id=line.ingredient_version_id,
        base_quantity=line.base_quantity,
        preferred_display_unit_id=line.preferred_display_unit_id,
        note=line.note,
        position_key=line.position_key,
        scaling_behavior=line.scaling_behavior,
        include_in_portion_weight=line.include_in_portion_weight,
    )


def _result_payload(result: CreateRecipeResult) -> dict[str, object]:
    return {
        "recipe": {
            "id": str(result.recipe_id),
            "organization_id": str(result.organization_id),
            "current_version_id": str(result.recipe_version_id),
        },
        "version": {
            "id": str(result.recipe_version_id),
            "name": result.name,
            "description": result.description,
            "scaling_unit_id": str(result.scaling_unit_id),
            "base_scaling_amount": _canonical_decimal_string(result.base_scaling_amount),
            "estimated_diners_per_scaling_unit": (
                _canonical_decimal_string(result.estimated_diners_per_scaling_unit)
                if result.estimated_diners_per_scaling_unit is not None
                else None
            ),
            "round_suggestions_up": result.round_suggestions_up,
        },
        "recipe_tag_ids": [str(tag_id) for tag_id in result.recipe_tag_ids],
        "ingredient_lines": [
            {
                "id": str(line.id),
                "line_key": str(line.line_key),
                "ingredient_version_id": str(line.ingredient_version_id),
                "base_quantity": _canonical_decimal_string(line.base_quantity),
                "preferred_display_unit_id": (
                    str(line.preferred_display_unit_id)
                    if line.preferred_display_unit_id is not None
                    else None
                ),
                "note": line.note,
                "position_key": line.position_key,
                "scaling_behavior": line.scaling_behavior,
                "include_in_portion_weight": line.include_in_portion_weight,
            }
            for line in result.ingredient_lines
        ],
    }


def _required_str(values: dict[object, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise TypeError
    return value


def _retained_result(mutation: Mutation) -> CreateRecipeResult:
    payload = mutation.outcome_payload
    recipe = payload.get("recipe") if payload is not None else None
    version = payload.get("version") if payload is not None else None
    tag_ids = payload.get("recipe_tag_ids") if payload is not None else None
    lines = payload.get("ingredient_lines") if payload is not None else None
    if (
        not isinstance(recipe, dict)
        or not isinstance(version, dict)
        or not isinstance(tag_ids, list)
        or not isinstance(lines, list)
    ):
        raise RuntimeError("Accepted recipe mutation has an invalid outcome payload")
    try:
        description = version["description"]
        estimated = version["estimated_diners_per_scaling_unit"]
        rounded = version["round_suggestions_up"]
        if (
            description is not None
            and not isinstance(description, str)
            or estimated is not None
            and not isinstance(estimated, str)
            or not isinstance(rounded, bool)
        ):
            raise TypeError
        result_tags = tuple(UUID(tag_id) for tag_id in tag_ids if isinstance(tag_id, str))
        if len(result_tags) != len(tag_ids):
            raise TypeError
        result_lines = tuple(
            RecipeIngredientLineResult(
                id=UUID(_required_str(item, "id")),
                line_key=UUID(_required_str(item, "line_key")),
                ingredient_version_id=UUID(_required_str(item, "ingredient_version_id")),
                base_quantity=Decimal(_required_str(item, "base_quantity")),
                preferred_display_unit_id=(
                    UUID(item["preferred_display_unit_id"])
                    if item.get("preferred_display_unit_id") is not None
                    and isinstance(item.get("preferred_display_unit_id"), str)
                    else None
                ),
                note=item.get("note"),
                position_key=_required_str(item, "position_key"),
                scaling_behavior=cast(
                    Literal["proportional", "fixed"], _required_str(item, "scaling_behavior")
                ),
                include_in_portion_weight=item["include_in_portion_weight"],
            )
            for item in lines
            if isinstance(item, dict)
        )
        if len(result_lines) != len(lines):
            raise TypeError
        for line in result_lines:
            if (
                line.note is not None
                and not isinstance(line.note, str)
                or line.scaling_behavior not in ("proportional", "fixed")
                or not isinstance(line.include_in_portion_weight, bool)
            ):
                raise TypeError
        first_change_sequence = mutation.first_change_sequence
        last_change_sequence = mutation.last_change_sequence
        if first_change_sequence is None or last_change_sequence is None:
            raise TypeError
        return CreateRecipeResult(
            mutation_id=mutation.id,
            recipe_id=UUID(_required_str(recipe, "id")),
            recipe_version_id=UUID(_required_str(version, "id")),
            organization_id=UUID(_required_str(recipe, "organization_id")),
            name=_required_str(version, "name"),
            description=description,
            scaling_unit_id=UUID(_required_str(version, "scaling_unit_id")),
            base_scaling_amount=Decimal(_required_str(version, "base_scaling_amount")),
            estimated_diners_per_scaling_unit=Decimal(estimated) if estimated is not None else None,
            round_suggestions_up=rounded,
            recipe_tag_ids=result_tags,
            ingredient_lines=result_lines,
            first_change_sequence=first_change_sequence,
            last_change_sequence=last_change_sequence,
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Accepted recipe mutation has an invalid outcome payload") from error


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


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload
    error = payload.get("error") if payload is not None else None
    if not isinstance(error, dict):
        raise RuntimeError("Rejected recipe mutation has an invalid outcome payload")
    try:
        if _required_str(error, "code") != "validation_failed":
            raise TypeError
        raw_violations = error.get("field_violations")
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
            "Rejected recipe mutation has an invalid outcome payload"
        ) from error_value
    return _validation_error(violations)


def _mutation(
    *,
    command: _PreparedCommand,
    context: ExecutionContext,
    actor_role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
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
        command_kind=COMMAND_KIND,
        target_identities=[
            {"entity_kind": "recipe", "entity_id": str(command.recipe_id)},
            {"entity_kind": "recipe_version", "entity_id": str(command.recipe_version_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=outcome_payload,
        first_change_sequence=first_change_sequence,
        last_change_sequence=last_change_sequence,
    )


async def _validate_catalog_references(session: AsyncSession, command: _PreparedCommand) -> bool:
    """Validate references while holding them stable through publication.

    False is a deterministic validation rejection, not an authorization signal.
    """

    scaling_unit = await session.scalar(
        select(UnitDefinition.id)
        .where(
            UnitDefinition.id == command.scaling_unit_id,
            UnitDefinition.allows_recipe_scaling.is_(True),
            UnitDefinition.retired_at.is_(None),
            (UnitDefinition.organization_id.is_(None))
            | (UnitDefinition.organization_id == command.organization_id),
        )
        .with_for_update(of=UnitDefinition)
    )
    if scaling_unit is None:
        return False
    if command.recipe_tag_ids:
        tags = (
            await session.execute(
                select(RecipeTag.id)
                .where(
                    RecipeTag.organization_id == command.organization_id,
                    RecipeTag.id.in_(command.recipe_tag_ids),
                    RecipeTag.retired_at.is_(None),
                )
                .with_for_update(of=RecipeTag)
            )
        ).scalars()
        if set(tags) != set(command.recipe_tag_ids):
            return False
    if command.ingredient_lines:
        version_ids = tuple(line.ingredient_version_id for line in command.ingredient_lines)
        found_versions = (
            await session.execute(
                select(
                    IngredientVersion.id,
                    IngredientVersion.canonical_unit_id,
                    UnitDefinition.dimension,
                )
                .join(UnitDefinition, UnitDefinition.id == IngredientVersion.canonical_unit_id)
                .where(
                    IngredientVersion.organization_id == command.organization_id,
                    IngredientVersion.id.in_(version_ids),
                )
                .with_for_update(of=IngredientVersion)
            )
        ).all()
        version_by_id = {row.id: row for row in found_versions}
        if set(version_by_id) != set(version_ids):
            return False
        display_ids = tuple(
            line.preferred_display_unit_id
            for line in command.ingredient_lines
            if line.preferred_display_unit_id is not None
        )
        if display_ids:
            display_units = (
                await session.execute(
                    select(
                        UnitDefinition.id,
                        UnitDefinition.organization_id,
                        UnitDefinition.dimension,
                        UnitDefinition.allows_ingredient_quantity,
                        UnitDefinition.retired_at,
                    )
                    .where(UnitDefinition.id.in_(display_ids))
                    .with_for_update(of=UnitDefinition)
                )
            ).all()
            display_by_id = {row.id: row for row in display_units}
            if set(display_by_id) != set(display_ids):
                return False
            for line in command.ingredient_lines:
                if line.preferred_display_unit_id is None:
                    continue
                ingredient_version = version_by_id[line.ingredient_version_id]
                display = display_by_id[line.preferred_display_unit_id]
                if (
                    not display.allows_ingredient_quantity
                    or display.retired_at is not None
                    or (
                        display.organization_id is not None
                        and display.organization_id != command.organization_id
                    )
                    or display.dimension != ingredient_version.dimension
                    or (
                        ingredient_version.dimension in ("count", "custom")
                        and line.preferred_display_unit_id != ingredient_version.canonical_unit_id
                    )
                ):
                    return False
        existing_line_id = await session.scalar(
            select(RecipeVersionIngredientLine.id).where(
                RecipeVersionIngredientLine.id.in_(
                    tuple(line.id for line in command.ingredient_lines)
                )
            )
        )
        if existing_line_id is not None:
            return False
    return True


def _change_records(
    command: _PreparedCommand, actor_user_id: UUID
) -> tuple[tuple[str, UUID, dict[str, object]], ...]:
    recipe_record: dict[str, object] = {
        "id": str(command.recipe_id),
        "organization_id": str(command.organization_id),
        "current_version_id": str(command.recipe_version_id),
        "retired_at": None,
        "created_by_user_id": str(actor_user_id),
    }
    version_record: dict[str, object] = {
        "id": str(command.recipe_version_id),
        "recipe_id": str(command.recipe_id),
        "organization_id": str(command.organization_id),
        "based_on_version_id": None,
        "name": command.name,
        "description": command.description,
        "scaling_model": "single_variable",
        "scaling_unit_id": str(command.scaling_unit_id),
        "base_scaling_amount": _canonical_decimal_string(command.base_scaling_amount),
        "estimated_diners_per_scaling_unit": (
            _canonical_decimal_string(command.estimated_diners_per_scaling_unit)
            if command.estimated_diners_per_scaling_unit is not None
            else None
        ),
        "round_suggestions_up": command.round_suggestions_up,
        "published_by_user_id": str(actor_user_id),
    }
    tag_records = tuple(
        (
            "recipe_version_tag",
            recipe_version_tag_change_id(command.recipe_version_id, tag_id),
            {
                "id": str(recipe_version_tag_change_id(command.recipe_version_id, tag_id)),
                "recipe_version_id": str(command.recipe_version_id),
                "recipe_tag_id": str(tag_id),
                "organization_id": str(command.organization_id),
            },
        )
        for tag_id in command.recipe_tag_ids
    )
    line_records = tuple(
        (
            "recipe_ingredient_line",
            line.id,
            {
                "id": str(line.id),
                "recipe_id": str(command.recipe_id),
                "recipe_version_id": str(command.recipe_version_id),
                "organization_id": str(command.organization_id),
                "line_key": str(line.line_key),
                "ingredient_version_id": str(line.ingredient_version_id),
                "base_quantity": _canonical_decimal_string(line.base_quantity),
                "preferred_display_unit_id": (
                    str(line.preferred_display_unit_id)
                    if line.preferred_display_unit_id is not None
                    else None
                ),
                "note": line.note,
                "position_key": line.position_key,
                "scaling_behavior": line.scaling_behavior,
                "include_in_portion_weight": line.include_in_portion_weight,
            },
        )
        for line in command.ingredient_lines
    )
    return (
        ("recipe", command.recipe_id, recipe_record),
        ("recipe_version", command.recipe_version_id, version_record),
        *cast(tuple[tuple[str, UUID, dict[str, object]], ...], tag_records),
        *cast(tuple[tuple[str, UUID, dict[str, object]], ...], line_records),
    )


async def create_recipe(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateRecipeCommand,
) -> CreateRecipeResult:
    """Create a recipe root and its first immutable version atomically."""

    prepared = _prepare_command(command)
    request_hash = _request_hash(prepared)
    deferred_error: ApplicationServiceError | None = None
    result: CreateRecipeResult | None = None
    async with session_factory() as session, session.begin():
        actor_role = await _authorize_and_lock_organization(
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
                raise RuntimeError("Recipe creation retained an unsupported outcome")
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
                {"lock_key": _advisory_lock_key("recipe", prepared.recipe_id)},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _advisory_lock_key("recipe_version", prepared.recipe_version_id)},
            )
            for line_id in sorted(line.id for line in prepared.ingredient_lines):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": _advisory_lock_key("recipe_ingredient_line", line_id)},
                )
            recipe_exists = await session.scalar(
                select(Recipe.id).where(Recipe.id == prepared.recipe_id)
            )
            version_exists = await session.scalar(
                select(RecipeVersion.id).where(RecipeVersion.id == prepared.recipe_version_id)
            )
            if recipe_exists is not None:
                deferred_error = _validation_error((FieldViolation("recipe_id", "already_exists"),))
            elif version_exists is not None:
                deferred_error = _validation_error(
                    (FieldViolation("recipe_version_id", "already_exists"),)
                )
            elif not await _validate_catalog_references(session, prepared):
                deferred_error = _validation_error(
                    (
                        FieldViolation(
                            "catalog_references", "must_be_active_and_owned_by_organization"
                        ),
                    )
                )
            if deferred_error is not None:
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
                change_records = _change_records(prepared, context.actor_user_id)
                first_change_sequence, last_change_sequence = await _reserve_change_range(
                    session, prepared.organization_id, prepared.mutation_id, len(change_records)
                )
                result = CreateRecipeResult(
                    mutation_id=prepared.mutation_id,
                    recipe_id=prepared.recipe_id,
                    recipe_version_id=prepared.recipe_version_id,
                    organization_id=prepared.organization_id,
                    name=prepared.name,
                    description=prepared.description,
                    scaling_unit_id=prepared.scaling_unit_id,
                    base_scaling_amount=prepared.base_scaling_amount,
                    estimated_diners_per_scaling_unit=prepared.estimated_diners_per_scaling_unit,
                    round_suggestions_up=prepared.round_suggestions_up,
                    recipe_tag_ids=prepared.recipe_tag_ids,
                    ingredient_lines=tuple(
                        _line_result(line) for line in prepared.ingredient_lines
                    ),
                    first_change_sequence=first_change_sequence,
                    last_change_sequence=last_change_sequence,
                    replayed=False,
                )
                session.add(
                    Recipe(
                        id=prepared.recipe_id,
                        organization_id=prepared.organization_id,
                        current_version_id=prepared.recipe_version_id,
                        created_by_user_id=context.actor_user_id,
                    )
                )
                await session.flush()
                session.add_all(
                    RecipeVersionTag(
                        recipe_version_id=prepared.recipe_version_id,
                        recipe_tag_id=tag_id,
                        organization_id=prepared.organization_id,
                    )
                    for tag_id in prepared.recipe_tag_ids
                )
                session.add_all(
                    RecipeVersionIngredientLine(
                        id=line.id,
                        organization_id=prepared.organization_id,
                        recipe_id=prepared.recipe_id,
                        recipe_version_id=prepared.recipe_version_id,
                        line_key=line.line_key,
                        ingredient_version_id=line.ingredient_version_id,
                        base_quantity=line.base_quantity,
                        preferred_display_unit_id=line.preferred_display_unit_id,
                        note=line.note,
                        position_key=line.position_key,
                        scaling_behavior=line.scaling_behavior,
                        include_in_portion_weight=line.include_in_portion_weight,
                    )
                    for line in prepared.ingredient_lines
                )
                await session.flush()
                session.add(
                    RecipeVersion(
                        id=prepared.recipe_version_id,
                        organization_id=prepared.organization_id,
                        recipe_id=prepared.recipe_id,
                        name=prepared.name,
                        description=prepared.description,
                        scaling_unit_id=prepared.scaling_unit_id,
                        base_scaling_amount=prepared.base_scaling_amount,
                        estimated_diners_per_scaling_unit=prepared.estimated_diners_per_scaling_unit,
                        round_suggestions_up=prepared.round_suggestions_up,
                        published_by_user_id=context.actor_user_id,
                    )
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
        raise RuntimeError("Recipe creation produced no outcome")
    return result
