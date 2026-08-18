"""Guarded, atomic copy of one current recipe version between organizations."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import _reserve_change_range
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.application.recipes import (
    CreateRecipeCommand,
    RecipeIngredientLineInput,
    _change_records,
    _prepare_command,
    _validate_catalog_references,
)
from cookops.persistence.models import (
    ClientInstallation,
    Ingredient,
    IngredientVersion,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    Recipe,
    RecipeVersion,
    RecipeVersionIngredientLine,
    RecipeVersionTag,
    SystemRoleAssignment,
    User,
)

COPY_COMMAND_KIND = "recipe.copy"
COPY_COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CopyRecipeToOrganizationCommand:
    source_organization_id: UUID
    destination_organization_id: UUID
    source_recipe_id: UUID
    source_current_recipe_version_id: UUID
    destination_recipe_id: UUID
    destination_recipe_version_id: UUID
    ingredient_version_mappings: dict[UUID, UUID] = field(default_factory=dict)
    recipe_tag_mappings: dict[UUID, UUID] = field(default_factory=dict)
    scaling_unit_mappings: dict[UUID, UUID] = field(default_factory=dict)
    preferred_display_unit_mappings: dict[UUID, UUID] = field(default_factory=dict)
    mutation_id: UUID = field(default_factory=uuid4)
    client_wall_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CopyRecipeToOrganizationResult:
    mutation_id: UUID
    source_organization_id: UUID
    destination_organization_id: UUID
    source_recipe_id: UUID
    destination_recipe_id: UUID
    source_recipe_version_id: UUID
    destination_recipe_version_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool


def _request_hash(command: CopyRecipeToOrganizationCommand) -> bytes:
    value = {
        "command_kind": COPY_COMMAND_KIND,
        "command_schema_version": COPY_COMMAND_SCHEMA_VERSION,
        "source_organization_id": str(command.source_organization_id),
        "destination_organization_id": str(command.destination_organization_id),
        "source_recipe_id": str(command.source_recipe_id),
        "source_current_recipe_version_id": str(command.source_current_recipe_version_id),
        "destination_recipe_id": str(command.destination_recipe_id),
        "destination_recipe_version_id": str(command.destination_recipe_version_id),
        "ingredient_version_mappings": sorted(
            (str(k), str(v)) for k, v in command.ingredient_version_mappings.items()
        ),
        "recipe_tag_mappings": sorted(
            (str(k), str(v)) for k, v in command.recipe_tag_mappings.items()
        ),
        "scaling_unit_mappings": sorted(
            (str(k), str(v)) for k, v in command.scaling_unit_mappings.items()
        ),
        "preferred_display_unit_mappings": sorted(
            (str(k), str(v)) for k, v in command.preferred_display_unit_mappings.items()
        ),
        "mutation_id": str(command.mutation_id),
        "client_wall_time": command.client_wall_time.astimezone(UTC).isoformat(),
        "logical_operation_id": str(command.logical_operation_id)
        if command.logical_operation_id is not None
        else None,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


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


def _retained_copy_result(mutation: Mutation) -> CopyRecipeToOrganizationResult:
    payload = mutation.outcome_payload or {}
    item = payload.get("copy")
    if (
        not isinstance(item, dict)
        or mutation.first_change_sequence is None
        or mutation.last_change_sequence is None
    ):
        raise RuntimeError("Accepted recipe copy mutation has an invalid outcome payload")
    try:
        return CopyRecipeToOrganizationResult(
            mutation.id,
            UUID(str(item["source_organization_id"])),
            UUID(str(item["destination_organization_id"])),
            UUID(str(item["source_recipe_id"])),
            UUID(str(item["destination_recipe_id"])),
            UUID(str(item["source_recipe_version_id"])),
            UUID(str(item["destination_recipe_version_id"])),
            mutation.first_change_sequence,
            mutation.last_change_sequence,
            True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "Accepted recipe copy mutation has an invalid outcome payload"
        ) from error


def _retained_copy_error(mutation: Mutation) -> ApplicationServiceError:
    item = (mutation.outcome_payload or {}).get("error")
    if not isinstance(item, dict) or not isinstance(item.get("code"), str):
        raise RuntimeError("Rejected recipe copy mutation has an invalid outcome payload")
    violations = item.get("field_violations", [])
    if not isinstance(violations, list):
        raise RuntimeError("Rejected recipe copy mutation has an invalid outcome payload")
    return ApplicationServiceError(
        item["code"],
        field_violations=tuple(
            FieldViolation(value["path"], value["code"])
            for value in violations
            if isinstance(value, dict)
            and isinstance(value.get("path"), str)
            and isinstance(value.get("code"), str)
        ),
        retry_same_identity=False,
    )


async def _authorize_copy(
    session: AsyncSession, context: ExecutionContext, source_id: UUID, destination_id: UUID
) -> Literal["organization_admin", "system_admin"]:
    expected_kind = "agent" if context.oauth_client_id is not None else "browser"
    actor = await session.scalar(
        select(User.id)
        .join(ClientInstallation, ClientInstallation.user_id == User.id)
        .where(
            User.id == context.actor_user_id,
            User.disabled_at.is_(None),
            ClientInstallation.id == context.client_installation_id,
            ClientInstallation.disabled_at.is_(None),
            ClientInstallation.installation_kind == expected_kind,
        )
        .with_for_update(of=(User, ClientInstallation))
    )
    if actor is None:
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    system_admin = await session.scalar(
        select(SystemRoleAssignment.id)
        .where(
            SystemRoleAssignment.user_id == context.actor_user_id,
            SystemRoleAssignment.role == "system_admin",
            SystemRoleAssignment.revoked_at.is_(None),
        )
        .with_for_update(of=SystemRoleAssignment)
    )
    if system_admin is not None:
        return "system_admin"
    memberships = (
        await session.execute(
            select(OrganizationMembership.organization_id, OrganizationMembership.role)
            .where(
                OrganizationMembership.organization_id.in_((source_id, destination_id)),
                OrganizationMembership.user_id == context.actor_user_id,
                OrganizationMembership.state == "active",
            )
            .with_for_update(of=OrganizationMembership)
        )
    ).all()
    roles = {organization_id: role for organization_id, role in memberships}
    if roles.get(source_id) not in ("member", "organization_admin"):
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    if roles.get(destination_id) != "organization_admin":
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    return "organization_admin"


def _mutation(
    command: CopyRecipeToOrganizationCommand,
    context: ExecutionContext,
    role: str,
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=command.mutation_id,
        logical_operation_id=command.logical_operation_id,
        organization_id=command.destination_organization_id,
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time.astimezone(UTC),
        command_schema_version=COPY_COMMAND_SCHEMA_VERSION,
        command_kind=COPY_COMMAND_KIND,
        target_identities=[
            {"entity_kind": "recipe", "entity_id": str(command.destination_recipe_id)},
            {
                "entity_kind": "recipe_version",
                "entity_id": str(command.destination_recipe_version_id),
            },
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


def _copy_payload(command: CopyRecipeToOrganizationCommand) -> dict[str, object]:
    return {
        "copy": {
            "source_organization_id": str(command.source_organization_id),
            "destination_organization_id": str(command.destination_organization_id),
            "source_recipe_id": str(command.source_recipe_id),
            "destination_recipe_id": str(command.destination_recipe_id),
            "source_recipe_version_id": str(command.source_current_recipe_version_id),
            "destination_recipe_version_id": str(command.destination_recipe_version_id),
        }
    }


async def copy_recipe_to_organization(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CopyRecipeToOrganizationCommand,
) -> CopyRecipeToOrganizationResult:
    """Copy one current immutable version and its associations atomically."""
    request_hash = _request_hash(command)
    deferred_error: ApplicationServiceError | None = None
    result: CopyRecipeToOrganizationResult | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_copy(
            session, context, command.source_organization_id, command.destination_organization_id
        )
        for organization_id in sorted(
            (command.source_organization_id, command.destination_organization_id), key=str
        ):
            organization = await session.scalar(
                select(Organization.id)
                .where(Organization.id == organization_id, Organization.retired_at.is_(None))
                .with_for_update(of=Organization)
            )
            if organization is None:
                raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COPY_COMMAND_KIND
                or retained.command_schema_version != COPY_COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "accepted":
                return _retained_copy_result(retained)
            if retained.outcome == "rejected":
                raise _retained_copy_error(retained)
            raise RuntimeError("Recipe copy retained an unsupported outcome")

        if command.source_organization_id == command.destination_organization_id:
            deferred_error = ApplicationServiceError(
                "validation_failed",
                field_violations=(FieldViolation("destination_organization_id", "must_differ"),),
                retry_same_identity=False,
            )
            session.add(
                _mutation(
                    command, context, role, request_hash, "rejected", _error_payload(deferred_error)
                )
            )
            await session.commit()
            raise deferred_error

        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("recipe", command.destination_recipe_id)},
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("recipe_version", command.destination_recipe_version_id)},
        )
        source = await session.scalar(
            select(Recipe)
            .where(
                Recipe.id == command.source_recipe_id,
                Recipe.organization_id == command.source_organization_id,
                Recipe.retired_at.is_(None),
            )
            .with_for_update(of=Recipe)
        )
        source_version = await session.scalar(
            select(RecipeVersion)
            .where(
                RecipeVersion.id == command.source_current_recipe_version_id,
                RecipeVersion.recipe_id == command.source_recipe_id,
                RecipeVersion.organization_id == command.source_organization_id,
            )
            .with_for_update(of=RecipeVersion)
        )
        if (
            source is None
            or source_version is None
            or source.current_version_id != source_version.id
        ):
            deferred_error = ApplicationServiceError(
                "stale_precondition", retry_same_identity=False
            )
        else:
            source_lines = (
                (
                    await session.execute(
                        select(RecipeVersionIngredientLine)
                        .where(
                            RecipeVersionIngredientLine.recipe_version_id == source_version.id,
                            RecipeVersionIngredientLine.organization_id
                            == command.source_organization_id,
                        )
                        .order_by(
                            RecipeVersionIngredientLine.position_key,
                            RecipeVersionIngredientLine.line_key,
                        )
                        .with_for_update(of=RecipeVersionIngredientLine)
                    )
                )
                .scalars()
                .all()
            )
            source_tags = (
                (
                    await session.execute(
                        select(RecipeVersionTag)
                        .where(
                            RecipeVersionTag.recipe_version_id == source_version.id,
                            RecipeVersionTag.organization_id == command.source_organization_id,
                        )
                        .order_by(RecipeVersionTag.recipe_tag_id)
                        .with_for_update(of=RecipeVersionTag)
                    )
                )
                .scalars()
                .all()
            )
            ingredient_ids = {line.ingredient_version_id for line in source_lines}
            tag_ids = {item.recipe_tag_id for item in source_tags}
            display_ids = {
                line.preferred_display_unit_id
                for line in source_lines
                if line.preferred_display_unit_id is not None
            }
            if (
                set(command.ingredient_version_mappings) != ingredient_ids
                or set(command.recipe_tag_mappings) != tag_ids
                or set(command.scaling_unit_mappings) != {source_version.scaling_unit_id}
                or set(command.preferred_display_unit_mappings) != display_ids
            ):
                deferred_error = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(FieldViolation("mappings", "must_cover_all_dependencies"),),
                    retry_same_identity=False,
                )
            else:
                destination_lines = tuple(
                    RecipeIngredientLineInput(
                        id=uuid4(),
                        line_key=line.line_key,
                        ingredient_version_id=command.ingredient_version_mappings[
                            line.ingredient_version_id
                        ],
                        base_quantity=line.base_quantity,
                        position_key=line.position_key,
                        scaling_behavior=cast(
                            Literal["proportional", "fixed"], line.scaling_behavior
                        ),
                        include_in_portion_weight=line.include_in_portion_weight,
                        preferred_display_unit_id=(
                            command.preferred_display_unit_mappings[line.preferred_display_unit_id]
                            if line.preferred_display_unit_id is not None
                            else None
                        ),
                        note=line.note,
                    )
                    for line in source_lines
                )
                destination_command = CreateRecipeCommand(
                    mutation_id=command.mutation_id,
                    recipe_id=command.destination_recipe_id,
                    recipe_version_id=command.destination_recipe_version_id,
                    organization_id=command.destination_organization_id,
                    name=source_version.name,
                    description=source_version.description,
                    scaling_unit_id=command.scaling_unit_mappings[source_version.scaling_unit_id],
                    base_scaling_amount=source_version.base_scaling_amount,
                    estimated_diners_per_scaling_unit=source_version.estimated_diners_per_scaling_unit,
                    round_suggestions_up=source_version.round_suggestions_up,
                    recipe_tag_ids=tuple(
                        command.recipe_tag_mappings[item.recipe_tag_id] for item in source_tags
                    ),
                    ingredient_lines=destination_lines,
                    client_wall_time=command.client_wall_time,
                    logical_operation_id=command.logical_operation_id,
                )
                prepared = _prepare_command(destination_command)
                if prepared.violations:
                    deferred_error = ApplicationServiceError(
                        "validation_failed",
                        field_violations=prepared.violations,
                        retry_same_identity=False,
                    )
                else:
                    current_versions = (
                        (
                            await session.execute(
                                select(IngredientVersion.id)
                                .join(Ingredient, Ingredient.id == IngredientVersion.ingredient_id)
                                .where(
                                    IngredientVersion.organization_id
                                    == command.destination_organization_id,
                                    IngredientVersion.id.in_(
                                        tuple(command.ingredient_version_mappings.values())
                                    ),
                                    Ingredient.retired_at.is_(None),
                                    Ingredient.current_version_id == IngredientVersion.id,
                                )
                                .with_for_update(of=IngredientVersion)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if set(current_versions) != set(command.ingredient_version_mappings.values()):
                        deferred_error = ApplicationServiceError(
                            "validation_failed",
                            field_violations=(
                                FieldViolation("mappings", "must_reference_current_catalog"),
                            ),
                            retry_same_identity=False,
                        )
                    elif not await _validate_catalog_references(session, prepared):
                        deferred_error = ApplicationServiceError(
                            "validation_failed",
                            field_violations=(
                                FieldViolation(
                                    "catalog_references", "must_be_active_and_owned_by_organization"
                                ),
                            ),
                            retry_same_identity=False,
                        )
                    elif (
                        await session.scalar(
                            select(Recipe.id).where(Recipe.id == prepared.recipe_id)
                        )
                        is not None
                        or await session.scalar(
                            select(RecipeVersion.id).where(
                                RecipeVersion.id == prepared.recipe_version_id
                            )
                        )
                        is not None
                    ):
                        deferred_error = ApplicationServiceError(
                            "validation_failed",
                            field_violations=(FieldViolation("destination_ids", "already_exists"),),
                            retry_same_identity=False,
                        )
                    else:
                        records = _change_records(prepared, context.actor_user_id)
                        first, last = await _reserve_change_range(
                            session, prepared.organization_id, prepared.mutation_id, len(records)
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
                                based_on_version_id=None,
                                name=prepared.name,
                                description=prepared.description,
                                scaling_model="single_variable",
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
                                sequence=first + index,
                                mutation_id=prepared.mutation_id,
                                entity_id=entity_id,
                                entity_kind=kind,
                                operation="upsert",
                                payload={"record_schema_version": 1, "record": record},
                            )
                            for index, (kind, entity_id, record) in enumerate(records)
                        )
                        result = CopyRecipeToOrganizationResult(
                            command.mutation_id,
                            command.source_organization_id,
                            command.destination_organization_id,
                            command.source_recipe_id,
                            command.destination_recipe_id,
                            command.source_current_recipe_version_id,
                            command.destination_recipe_version_id,
                            first,
                            last,
                            False,
                        )
                        session.add(
                            _mutation(
                                command,
                                context,
                                role,
                                request_hash,
                                "accepted",
                                _copy_payload(command),
                                first,
                                last,
                            )
                        )
        if deferred_error is not None:
            session.add(
                _mutation(
                    command, context, role, request_hash, "rejected", _error_payload(deferred_error)
                )
            )
    if deferred_error is not None:
        raise deferred_error
    if result is None:
        raise RuntimeError("Recipe copy produced no outcome")
    return result
