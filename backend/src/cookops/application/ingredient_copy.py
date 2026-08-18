"""Read-only guarded preview for copying one ingredient across organizations."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
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
from cookops.persistence.models import (
    ClientInstallation,
    DietaryTag,
    FieldClock,
    Ingredient,
    IngredientVersion,
    IngredientVersionDietaryTag,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    StoreSection,
    SystemRoleAssignment,
    UnitDefinition,
    User,
)


@dataclass(frozen=True, slots=True)
class PreviewIngredientCopyCommand:
    source_organization_id: UUID
    destination_organization_id: UUID
    ingredient_id: UUID


@dataclass(frozen=True, slots=True)
class IngredientCopyMappingRequirement:
    kind: Literal["canonical_unit", "default_store_section", "dietary_tag"]
    source_id: UUID
    seed_key: str | None = None


@dataclass(frozen=True, slots=True)
class IngredientCopyPreview:
    source_organization_id: UUID
    destination_organization_id: UUID
    source_ingredient_id: UUID
    source_version_id: UUID
    source_name: str
    canonical_unit_id: UUID
    default_store_section_id: UUID | None
    dietary_tag_ids: tuple[UUID, ...]
    precondition_fingerprint: str
    mapping_requirements: tuple[IngredientCopyMappingRequirement, ...]


@dataclass(frozen=True, slots=True)
class _IngredientCopyGraph:
    source_id: UUID
    source_current_version_id: UUID
    versions: tuple[IngredientVersion, ...]
    current_version: IngredientVersion
    version_tags: tuple[IngredientVersionDietaryTag, ...]
    source_units: tuple[UnitDefinition, ...]
    source_tags: tuple[DietaryTag, ...]
    current_version_tag_ids: tuple[UUID, ...]
    mapping_requirements: tuple[IngredientCopyMappingRequirement, ...]
    precondition_fingerprint: str


@dataclass(frozen=True, slots=True)
class IngredientCopyMapping:
    kind: Literal["canonical_unit", "default_store_section", "dietary_tag"]
    source_id: UUID
    destination_id: UUID | None


@dataclass(frozen=True, slots=True)
class CopyIngredientToOrganizationCommand:
    source_organization_id: UUID
    destination_organization_id: UUID
    ingredient_id: UUID
    precondition_fingerprint: str
    mappings: tuple[IngredientCopyMapping, ...] = ()
    mutation_id: UUID = field(default_factory=uuid4)
    client_wall_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CopyIngredientToOrganizationResult:
    mutation_id: UUID
    source_organization_id: UUID
    destination_organization_id: UUID
    source_ingredient_id: UUID
    destination_ingredient_id: UUID
    source_version_id: UUID
    destination_version_id: UUID
    source_name: str
    canonical_unit_id: UUID
    default_store_section_id: UUID | None
    dietary_tag_ids: tuple[UUID, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool


COPY_COMMAND_KIND = "ingredient.copy"
COPY_COMMAND_SCHEMA_VERSION = 1


def _copy_request_hash(command: CopyIngredientToOrganizationCommand) -> bytes:
    value = {
        "command_kind": COPY_COMMAND_KIND,
        "command_schema_version": COPY_COMMAND_SCHEMA_VERSION,
        "source_organization_id": str(command.source_organization_id),
        "destination_organization_id": str(command.destination_organization_id),
        "ingredient_id": str(command.ingredient_id),
        "precondition_fingerprint": command.precondition_fingerprint,
        "mappings": [
            [
                item.kind,
                str(item.source_id),
                str(item.destination_id) if item.destination_id else None,
            ]
            for item in command.mappings
        ],
        "mutation_id": str(command.mutation_id),
        "client_wall_time": command.client_wall_time.astimezone(UTC).isoformat(),
        "logical_operation_id": str(command.logical_operation_id)
        if command.logical_operation_id
        else None,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _copy_result_payload(result: CopyIngredientToOrganizationResult) -> dict[str, object]:
    return {
        "copy": {
            "source_organization_id": str(result.source_organization_id),
            "destination_organization_id": str(result.destination_organization_id),
            "source_ingredient_id": str(result.source_ingredient_id),
            "destination_ingredient_id": str(result.destination_ingredient_id),
            "source_version_id": str(result.source_version_id),
            "destination_version_id": str(result.destination_version_id),
            "source_name": result.source_name,
            "canonical_unit_id": str(result.canonical_unit_id),
            "default_store_section_id": str(result.default_store_section_id)
            if result.default_store_section_id
            else None,
            "dietary_tag_ids": [str(item) for item in result.dietary_tag_ids],
        }
    }


def _copy_retained_result(mutation: Mutation) -> CopyIngredientToOrganizationResult:
    payload = mutation.outcome_payload
    item = payload.get("copy") if isinstance(payload, dict) else None
    if (
        not isinstance(item, dict)
        or mutation.first_change_sequence is None
        or mutation.last_change_sequence is None
    ):
        raise RuntimeError("Accepted ingredient copy mutation has an invalid outcome payload")
    try:
        tags = item["dietary_tag_ids"]
        if not isinstance(tags, list) or not all(isinstance(value, str) for value in tags):
            raise TypeError
        return CopyIngredientToOrganizationResult(
            mutation.id,
            UUID(str(item["source_organization_id"])),
            UUID(str(item["destination_organization_id"])),
            UUID(str(item["source_ingredient_id"])),
            UUID(str(item["destination_ingredient_id"])),
            UUID(str(item["source_version_id"])),
            UUID(str(item["destination_version_id"])),
            str(item["source_name"]),
            UUID(str(item["canonical_unit_id"])),
            UUID(str(item["default_store_section_id"]))
            if item["default_store_section_id"]
            else None,
            tuple(UUID(value) for value in tags),
            mutation.first_change_sequence,
            mutation.last_change_sequence,
            True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "Accepted ingredient copy mutation has an invalid outcome payload"
        ) from error


def _copy_mutation(
    command: CopyIngredientToOrganizationCommand,
    context: ExecutionContext,
    actor_role: str,
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
        actor_role=actor_role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time.astimezone(UTC),
        command_schema_version=COPY_COMMAND_SCHEMA_VERSION,
        command_kind=COPY_COMMAND_KIND,
        target_identities=[{"entity_kind": "ingredient", "entity_id": str(command.ingredient_id)}],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


def _copy_error_payload(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": item.path, "code": item.code} for item in error.field_violations
            ],
        }
    }


def _copy_retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload
    item = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(item, dict) or not isinstance(item.get("code"), str):
        raise RuntimeError("Rejected ingredient copy mutation has an invalid outcome payload")
    violations = item.get("field_violations", ())
    if not isinstance(violations, list):
        raise RuntimeError("Rejected ingredient copy mutation has an invalid outcome payload")
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


async def _copy_actor_role(
    session: AsyncSession, context: ExecutionContext, destination_organization_id: UUID
) -> str:
    system_admin = await session.scalar(
        select(SystemRoleAssignment.id).where(
            SystemRoleAssignment.user_id == context.actor_user_id,
            SystemRoleAssignment.role == "system_admin",
            SystemRoleAssignment.revoked_at.is_(None),
        )
    )
    if system_admin is not None:
        return "system_admin"
    role = await session.scalar(
        select(OrganizationMembership.role).where(
            OrganizationMembership.organization_id == destination_organization_id,
            OrganizationMembership.user_id == context.actor_user_id,
            OrganizationMembership.role == "organization_admin",
            OrganizationMembership.state == "active",
        )
    )
    if role is None:
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    return role


async def preview_ingredient_copy(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: PreviewIngredientCopyCommand,
) -> IngredientCopyPreview:
    async with session_factory() as session:
        if not await _authorized(session, context, command):
            raise ApplicationServiceError("forbidden", retry_same_identity=True)
        graph = await _load_ingredient_copy_graph(
            session,
            command.source_organization_id,
            command.destination_organization_id,
            command.ingredient_id,
        )
        return IngredientCopyPreview(
            command.source_organization_id,
            command.destination_organization_id,
            graph.source_id,
            graph.current_version.id,
            graph.current_version.name,
            graph.current_version.canonical_unit_id,
            graph.current_version.default_store_section_id,
            graph.current_version_tag_ids,
            graph.precondition_fingerprint,
            graph.mapping_requirements,
        )


async def _load_ingredient_copy_graph(
    session: AsyncSession,
    source_organization_id: UUID,
    destination_organization_id: UUID,
    ingredient_id: UUID,
) -> _IngredientCopyGraph:
    source = await session.scalar(
        select(Ingredient).where(
            Ingredient.id == ingredient_id,
            Ingredient.organization_id == source_organization_id,
            Ingredient.retired_at.is_(None),
            Ingredient.current_version_id.is_not(None),
        )
    )
    versions: tuple[IngredientVersion, ...] = ()
    if source is not None:
        versions = tuple(
            (
                await session.execute(
                    select(IngredientVersion).where(
                        IngredientVersion.ingredient_id == source.id,
                        IngredientVersion.organization_id == source_organization_id,
                    )
                )
            ).scalars()
        )
    version: IngredientVersion | None = (
        next((item for item in versions if item.id == source.current_version_id), None)
        if source
        else None
    )
    version_by_id: dict[UUID, IngredientVersion] = {item.id: item for item in versions}
    if source is None or version is None or len(version_by_id) != len(versions):
        raise ApplicationServiceError("stale_precondition", retry_same_identity=False)

    if any(
        item.based_on_version_id is not None
        and (
            item.based_on_version_id not in version_by_id
            or version_by_id[item.based_on_version_id].ingredient_id != source.id
            or version_by_id[item.based_on_version_id].organization_id != source_organization_id
        )
        for item in versions
    ):
        raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
    for item in versions:
        seen: set[UUID] = set()
        cursor = item
        while cursor.based_on_version_id is not None:
            if cursor.id in seen:
                raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
            seen.add(cursor.id)
            cursor = version_by_id[cursor.based_on_version_id]

    version_tags = tuple(
        (
            await session.execute(
                select(IngredientVersionDietaryTag).where(
                    IngredientVersionDietaryTag.ingredient_version_id.in_(version_by_id),
                )
            )
        ).scalars()
    )
    if any(
        item.ingredient_version_id not in version_by_id
        or item.organization_id != source_organization_id
        for item in version_tags
    ):
        raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
    current_version_tag_ids = tuple(
        sorted(
            {
                item.dietary_tag_id
                for item in version_tags
                if item.ingredient_version_id == version.id
            }
        )
    )
    unit_ids = {item.canonical_unit_id for item in versions}
    source_units_by_id = {
        item.id: item
        for item in (
            await session.execute(select(UnitDefinition).where(UnitDefinition.id.in_(unit_ids)))
        ).scalars()
    }
    section_ids = {
        item.default_store_section_id for item in versions if item.default_store_section_id
    }
    source_sections_by_id = {
        item.id: item
        for item in (
            await session.execute(
                select(StoreSection).where(
                    StoreSection.id.in_(section_ids),
                    StoreSection.organization_id == source_organization_id,
                )
            )
        ).scalars()
    }
    source_tags = tuple(
        (
            await session.execute(
                select(DietaryTag).where(
                    DietaryTag.id.in_({item.dietary_tag_id for item in version_tags}),
                    DietaryTag.organization_id == source_organization_id,
                )
            )
        ).scalars()
    )
    if (
        len(source_units_by_id) != len(unit_ids)
        or any(
            item.organization_id is not None and item.organization_id != source_organization_id
            for item in source_units_by_id.values()
        )
        or len(source_sections_by_id) != len(section_ids)
    ):
        raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
    if len(source_tags) != len({item.dietary_tag_id for item in version_tags}):
        raise ApplicationServiceError("stale_precondition", retry_same_identity=False)

    destination_units = tuple(
        (
            await session.execute(
                select(UnitDefinition).where(
                    UnitDefinition.id.in_(unit_ids),
                    UnitDefinition.organization_id.is_(None),
                    UnitDefinition.retired_at.is_(None),
                )
            )
        ).scalars()
    )
    requirements: list[IngredientCopyMappingRequirement] = []
    destination_unit_ids = {item.id for item in destination_units}
    for unit in sorted(source_units_by_id.values(), key=lambda item: item.id):
        if unit.organization_id is not None or unit.id not in destination_unit_ids:
            requirements.append(IngredientCopyMappingRequirement("canonical_unit", unit.id))
    destination_sections = tuple(
        (
            await session.execute(
                select(StoreSection).where(
                    StoreSection.organization_id == destination_organization_id,
                    StoreSection.id.in_(section_ids),
                    StoreSection.retired_at.is_(None),
                )
            )
        ).scalars()
    )
    destination_section_ids = {item.id for item in destination_sections}
    for section in sorted(source_sections_by_id.values(), key=lambda item: item.id):
        if section.id not in destination_section_ids:
            requirements.append(
                IngredientCopyMappingRequirement("default_store_section", section.id)
            )
    destination_seed_keys = frozenset(
        key
        for key in (
            await session.execute(
                select(DietaryTag.seed_key).where(
                    DietaryTag.organization_id == destination_organization_id,
                    DietaryTag.seed_key.is_not(None),
                    DietaryTag.retired_at.is_(None),
                )
            )
        ).scalars()
        if key is not None
    )
    for tag in sorted(source_tags, key=lambda item: item.id):
        if tag.seed_key is None or tag.seed_key not in destination_seed_keys:
            requirements.append(
                IngredientCopyMappingRequirement("dietary_tag", tag.id, tag.seed_key)
            )

    source_units = tuple(sorted(source_units_by_id.values(), key=lambda item: item.id))
    source_sections = tuple(sorted(source_sections_by_id.values(), key=lambda item: item.id))
    ordered_versions = tuple(sorted(versions, key=lambda item: item.id))
    fingerprint_value = {
        "source": [
            str(source.id),
            source.current_version_id,
            source.retired_at,
        ],
        "versions": [
            [
                str(item.id),
                str(item.based_on_version_id) if item.based_on_version_id else None,
                item.name,
                str(item.canonical_unit_id),
                str(item.default_store_section_id) if item.default_store_section_id else None,
                str(item.mass_per_canonical_quantity),
                item.published_at,
                sorted(
                    str(tag.dietary_tag_id)
                    for tag in version_tags
                    if tag.ingredient_version_id == item.id
                ),
            ]
            for item in ordered_versions
        ],
        "source_dependencies": [
            [
                str(item.id),
                item.organization_id,
                item.retired_at,
                getattr(item, "seed_key", None),
                getattr(item, "code", None),
                getattr(item, "custom_name", None),
                getattr(item, "normalized_custom_name", None),
                getattr(item, "dimension", None),
                getattr(item, "base_unit_factor", None),
                getattr(item, "rounds_up_to_whole_unit", None),
                getattr(item, "allows_ingredient_quantity", None),
                getattr(item, "allows_recipe_scaling", None),
            ]
            for item in source_units
        ]
        + [
            [
                str(item.id),
                item.organization_id,
                item.retired_at,
                getattr(item, "seed_key", None),
                getattr(item, "code", None),
                getattr(item, "normalized_name", None),
            ]
            for item in source_sections
        ]
        + [
            [
                str(item.id),
                item.organization_id,
                item.retired_at,
                getattr(item, "seed_key", None),
                getattr(item, "name", None),
                getattr(item, "normalized_name", None),
                getattr(item, "color", None),
            ]
            for item in sorted(source_tags, key=lambda item: item.id)
        ],
        "destination": [
            sorted(str(item.id) for item in destination_units),
            sorted(str(item.id) for item in destination_sections),
            sorted(destination_seed_keys),
        ],
        "requirements": [[item.kind, str(item.source_id), item.seed_key] for item in requirements],
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_value, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return _IngredientCopyGraph(
        source_id=source.id,
        source_current_version_id=version.id,
        versions=ordered_versions,
        current_version=version,
        version_tags=version_tags,
        source_units=source_units,
        source_tags=tuple(sorted(source_tags, key=lambda item: item.id)),
        current_version_tag_ids=current_version_tag_ids,
        mapping_requirements=tuple(requirements),
        precondition_fingerprint=fingerprint,
    )


def _copy_error(*violations: FieldViolation) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed",
        field_violations=violations,
        retry_same_identity=False,
    )


async def _lock_copy_graph(
    session: AsyncSession,
    source_organization_id: UUID,
    ingredient_id: UUID,
) -> UUID | None:
    source = await session.scalar(
        select(Ingredient)
        .where(
            Ingredient.id == ingredient_id,
            Ingredient.organization_id == source_organization_id,
        )
        .with_for_update(of=Ingredient)
    )
    if source is None:
        return None
    versions = tuple(
        (
            await session.execute(
                select(IngredientVersion)
                .where(
                    IngredientVersion.ingredient_id == ingredient_id,
                    IngredientVersion.organization_id == source_organization_id,
                )
                .with_for_update(of=IngredientVersion)
            )
        ).scalars()
    )
    await session.execute(
        select(IngredientVersionDietaryTag)
        .where(
            IngredientVersionDietaryTag.ingredient_version_id.in_({item.id for item in versions})
        )
        .with_for_update(of=IngredientVersionDietaryTag)
    )
    unit_ids = {item.canonical_unit_id for item in versions}
    if unit_ids:
        await session.execute(
            select(UnitDefinition)
            .where(UnitDefinition.id.in_(unit_ids))
            .with_for_update(of=UnitDefinition)
        )
    section_ids = {
        item.default_store_section_id for item in versions if item.default_store_section_id
    }
    if section_ids:
        await session.execute(
            select(StoreSection)
            .where(StoreSection.id.in_(section_ids))
            .with_for_update(of=StoreSection)
        )
    tag_ids = set(
        (
            await session.execute(
                select(IngredientVersionDietaryTag.dietary_tag_id).where(
                    IngredientVersionDietaryTag.ingredient_version_id.in_(
                        {item.id for item in versions}
                    )
                )
            )
        ).scalars()
    )
    if tag_ids:
        await session.execute(
            select(DietaryTag).where(DietaryTag.id.in_(tag_ids)).with_for_update(of=DietaryTag)
        )
    return source.current_version_id


async def _resolve_copy_mappings(
    session: AsyncSession,
    graph: _IngredientCopyGraph,
    command: CopyIngredientToOrganizationCommand,
) -> tuple[dict[UUID, UUID], dict[UUID, UUID], dict[UUID, UUID]]:
    required = {(item.kind, item.source_id): item for item in graph.mapping_requirements}
    supplied = {(item.kind, item.source_id): item for item in command.mappings}
    if len(supplied) != len(command.mappings) or set(supplied) != set(required):
        raise _copy_error(FieldViolation("mappings", "must_match_requirements_exactly"))
    destination_unit_ids = {
        item.destination_id for item in command.mappings if item.kind == "canonical_unit"
    }
    destination_section_ids = {
        item.destination_id for item in command.mappings if item.kind == "default_store_section"
    }
    destination_tag_ids = {
        item.destination_id for item in command.mappings if item.kind == "dietary_tag"
    }
    destination_unit_ids.discard(None)
    destination_section_ids.discard(None)
    destination_tag_ids.discard(None)
    found_units = {
        item.id: item
        for item in (
            await session.execute(
                select(UnitDefinition)
                .where(UnitDefinition.id.in_(destination_unit_ids))
                .with_for_update(of=UnitDefinition)
            )
        ).scalars()
    }
    found_sections = {
        item.id: item
        for item in (
            await session.execute(
                select(StoreSection)
                .where(StoreSection.id.in_(destination_section_ids))
                .with_for_update(of=StoreSection)
            )
        ).scalars()
    }
    found_tags = {
        item.id: item
        for item in (
            await session.execute(
                select(DietaryTag)
                .where(DietaryTag.id.in_(destination_tag_ids))
                .with_for_update(of=DietaryTag)
            )
        ).scalars()
    }
    unit_map: dict[UUID, UUID] = {}
    section_map: dict[UUID, UUID] = {}
    tag_map: dict[UUID, UUID] = {}
    source_tags_by_id = {item.id: item for item in graph.source_tags}
    for requirement in graph.mapping_requirements:
        mapping = supplied[(requirement.kind, requirement.source_id)]
        if requirement.kind == "canonical_unit":
            if mapping.destination_id is None:
                raise _copy_error(
                    FieldViolation(
                        f"mappings[{requirement.source_id}].destination_id",
                        "must_be_destination_id",
                    )
                )
            unit_target = found_units.get(mapping.destination_id)
            if (
                unit_target is None
                or unit_target.retired_at is not None
                or (
                    unit_target.organization_id is not None
                    and unit_target.organization_id != command.destination_organization_id
                )
            ):
                raise _copy_error(
                    FieldViolation(
                        f"mappings[{requirement.source_id}].destination_id",
                        "not_available_in_destination",
                    )
                )
            unit_map[requirement.source_id] = mapping.destination_id
        elif requirement.kind == "default_store_section":
            if mapping.destination_id is None:
                raise _copy_error(
                    FieldViolation(
                        f"mappings[{requirement.source_id}].destination_id",
                        "must_be_destination_id",
                    )
                )
            section_target = found_sections.get(mapping.destination_id)
            if (
                section_target is None
                or section_target.organization_id != command.destination_organization_id
                or section_target.retired_at is not None
            ):
                raise _copy_error(
                    FieldViolation(
                        f"mappings[{requirement.source_id}].destination_id",
                        "not_available_in_destination",
                    )
                )
            section_map[requirement.source_id] = mapping.destination_id
        else:
            source_tag = source_tags_by_id[requirement.source_id]
            if mapping.destination_id is None:
                if source_tag.seed_key is not None:
                    raise _copy_error(
                        FieldViolation(
                            f"mappings[{requirement.source_id}].destination_id",
                            "seeded_tag_requires_destination_match",
                        )
                    )
                tag_map[requirement.source_id] = uuid4()
                continue
            tag_target = found_tags.get(mapping.destination_id)
            if (
                tag_target is None
                or tag_target.organization_id != command.destination_organization_id
                or tag_target.retired_at is not None
                or (source_tag.seed_key is not None and tag_target.seed_key != source_tag.seed_key)
            ):
                raise _copy_error(
                    FieldViolation(
                        f"mappings[{requirement.source_id}].destination_id",
                        "not_available_in_destination",
                    )
                )
            tag_map[requirement.source_id] = mapping.destination_id

    seeded_source_tags = {item.seed_key for item in graph.source_tags if item.seed_key is not None}
    resolved_seed_tags = {
        item.seed_key: item.id
        for item in (
            await session.execute(
                select(DietaryTag)
                .where(
                    DietaryTag.organization_id == command.destination_organization_id,
                    DietaryTag.seed_key.in_(seeded_source_tags),
                    DietaryTag.retired_at.is_(None),
                )
                .with_for_update(of=DietaryTag)
            )
        ).scalars()
        if item.seed_key is not None
    }
    for source_tag in graph.source_tags:
        if source_tag.id in tag_map or source_tag.seed_key is None:
            continue
        destination_id = resolved_seed_tags.get(source_tag.seed_key)
        if destination_id is None:
            raise _copy_error(
                FieldViolation(
                    f"mappings[{source_tag.id}]",
                    "seeded_tag_requires_current_destination_match",
                )
            )
        tag_map[source_tag.id] = destination_id
    return unit_map, section_map, tag_map


async def _validate_copy_units(
    session: AsyncSession,
    graph: _IngredientCopyGraph,
    command: CopyIngredientToOrganizationCommand,
    unit_map: dict[UUID, UUID],
) -> None:
    source_units = {item.id: item for item in graph.source_units}
    target_ids = {
        unit_map.get(item.canonical_unit_id, item.canonical_unit_id) for item in graph.versions
    }
    targets = {
        item.id: item
        for item in (
            await session.execute(
                select(UnitDefinition)
                .where(UnitDefinition.id.in_(target_ids))
                .with_for_update(of=UnitDefinition)
            )
        ).scalars()
    }
    dimensions: dict[str, set[UUID]] = {}
    for version in graph.versions:
        source_unit = source_units[version.canonical_unit_id]
        target_id = unit_map.get(version.canonical_unit_id, version.canonical_unit_id)
        target = targets.get(target_id)
        if (
            target is None
            or target.retired_at is not None
            or not target.allows_ingredient_quantity
            or (
                target.organization_id is not None
                and target.organization_id != command.destination_organization_id
            )
            or target.dimension != source_unit.dimension
            or (
                source_unit.dimension == "mass"
                and target.base_unit_factor != version.mass_per_canonical_quantity
            )
        ):
            raise _copy_error(
                FieldViolation(
                    "mappings",
                    "canonical_units_have_incompatible_semantics",
                )
            )
        dimensions.setdefault(target.dimension, set()).add(target.id)
    for dimension in ("count", "custom"):
        if len(dimensions.get(dimension, set())) > 1:
            raise _copy_error(
                FieldViolation("mappings", "count_and_custom_units_require_one_identity")
            )


async def _lock_active_copy_organizations(
    session: AsyncSession,
    source_organization_id: UUID,
    destination_organization_id: UUID,
) -> None:
    if source_organization_id == destination_organization_id:
        raise _copy_error(FieldViolation("destination_organization_id", "must_differ"))
    for organization_id in sorted((source_organization_id, destination_organization_id)):
        organization = await session.scalar(
            select(Organization)
            .where(Organization.id == organization_id)
            .with_for_update(of=Organization)
        )
        if organization is None or organization.retired_at is not None:
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)


async def _copy_ingredient_to_organization_once(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CopyIngredientToOrganizationCommand,
) -> CopyIngredientToOrganizationResult:
    async with session_factory() as session, session.begin():
        if not await _authorized(
            session,
            context,
            PreviewIngredientCopyCommand(
                command.source_organization_id,
                command.destination_organization_id,
                command.ingredient_id,
            ),
        ):
            raise ApplicationServiceError("forbidden", retry_same_identity=True)
        request_hash = _copy_request_hash(command)
        await _lock_active_copy_organizations(
            session, command.source_organization_id, command.destination_organization_id
        )
        locked_current_version_id = await _lock_copy_graph(
            session, command.source_organization_id, command.ingredient_id
        )
        if not await _authorized(
            session,
            context,
            PreviewIngredientCopyCommand(
                command.source_organization_id,
                command.destination_organization_id,
                command.ingredient_id,
            ),
        ):
            raise ApplicationServiceError("forbidden", retry_same_identity=True)
        actor_role = await _copy_actor_role(session, context, command.destination_organization_id)
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
                return _copy_retained_result(retained)
            if retained.outcome == "rejected":
                raise _copy_retained_error(retained)
            raise RuntimeError("Ingredient copy retained an unsupported outcome")
        graph = await _load_ingredient_copy_graph(
            session,
            command.source_organization_id,
            command.destination_organization_id,
            command.ingredient_id,
        )
        if (
            locked_current_version_id != graph.source_current_version_id
            or command.precondition_fingerprint != graph.precondition_fingerprint
        ):
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
        unit_map, section_map, tag_map = await _resolve_copy_mappings(session, graph, command)
        await _validate_copy_units(session, graph, command, unit_map)
        name_identity = UUID(
            bytes=hashlib.sha256(graph.current_version.normalized_name.encode()).digest()[:16]
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": _advisory_lock_key(
                    f"ingredient-name:{command.destination_organization_id}", name_identity
                )
            },
        )
        if not await _authorized(
            session,
            context,
            PreviewIngredientCopyCommand(
                command.source_organization_id,
                command.destination_organization_id,
                command.ingredient_id,
            ),
        ):
            raise ApplicationServiceError("forbidden", retry_same_identity=True)
        graph = await _load_ingredient_copy_graph(
            session,
            command.source_organization_id,
            command.destination_organization_id,
            command.ingredient_id,
        )
        if command.precondition_fingerprint != graph.precondition_fingerprint:
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
        duplicate = await session.scalar(
            select(Ingredient.id)
            .join(IngredientVersion, Ingredient.current_version_id == IngredientVersion.id)
            .where(
                Ingredient.organization_id == command.destination_organization_id,
                Ingredient.retired_at.is_(None),
                IngredientVersion.normalized_name == graph.current_version.normalized_name,
            )
            .with_for_update(of=Ingredient)
        )
        if duplicate is not None:
            raise _copy_error(FieldViolation("name", "already_exists"))

        explicit_tag_mappings = {
            item.source_id: item for item in command.mappings if item.kind == "dietary_tag"
        }
        new_destination_tags = {
            source_tag.id
            for source_tag in graph.source_tags
            if explicit_tag_mappings.get(source_tag.id) is not None
            and explicit_tag_mappings[source_tag.id].destination_id is None
        }
        new_tag_names = {
            graph_tag.normalized_name
            for graph_tag in graph.source_tags
            if graph_tag.id in new_destination_tags and graph_tag.normalized_name is not None
        }
        if len(new_tag_names) != len(new_destination_tags):
            raise _copy_error(FieldViolation("mappings", "custom_tag_names_must_be_unique"))
        for normalized_name in sorted(new_tag_names):
            name_identity = UUID(
                bytes=hashlib.sha256(normalized_name.encode()).digest()[:16]
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        f"dietary-tag-name:{command.destination_organization_id}",
                        name_identity,
                    )
                },
            )
        existing_tag = await session.scalar(
            select(DietaryTag.id)
            .where(
                DietaryTag.organization_id == command.destination_organization_id,
                DietaryTag.normalized_name.in_(new_tag_names),
            )
            .with_for_update(of=DietaryTag)
        )
        if existing_tag is not None:
            raise _copy_error(FieldViolation("mappings", "custom_tag_name_already_exists"))

        destination_ingredient_id = uuid4()
        destination_version_ids = {item.id: uuid4() for item in graph.versions}
        destination_tag_ids = {item.id: tag_map[item.id] for item in graph.source_tags}
        destination_current_version_id = destination_version_ids[graph.current_version.id]
        current_tag_ids = tuple(
            sorted(
                destination_tag_ids[item.dietary_tag_id]
                for item in graph.version_tags
                if item.ingredient_version_id == graph.current_version.id
            )
        )
        records: list[tuple[str, UUID, dict[str, object]]] = [
            (
                "ingredient",
                destination_ingredient_id,
                {
                    "id": str(destination_ingredient_id),
                    "organization_id": str(command.destination_organization_id),
                    "current_version_id": str(destination_current_version_id),
                    "current_price_estimate_id": None,
                    "created_at": None,
                    "retired_at": None,
                    "retired_by_user_id": None,
                    "lifecycle": "active",
                    "created_by_user_id": str(context.actor_user_id),
                    "field_clocks": {
                        "current_version_id": {
                            "winning_client_wall_time": command.client_wall_time.astimezone(
                                UTC
                            ).isoformat(),
                            "winning_mutation_id": str(command.mutation_id),
                        },
                        "current_price_estimate_id": {
                            "winning_client_wall_time": command.client_wall_time.astimezone(
                                UTC
                            ).isoformat(),
                            "winning_mutation_id": str(command.mutation_id),
                        },
                        "lifecycle": {
                            "winning_client_wall_time": command.client_wall_time.astimezone(
                                UTC
                            ).isoformat(),
                            "winning_mutation_id": str(command.mutation_id),
                        }
                    },
                },
            )
        ]
        for source_version in graph.versions:
            records.append(
                (
                    "ingredient_version",
                    destination_version_ids[source_version.id],
                    {
                        "id": str(destination_version_ids[source_version.id]),
                        "ingredient_id": str(destination_ingredient_id),
                        "organization_id": str(command.destination_organization_id),
                        "based_on_version_id": (
                            str(destination_version_ids[source_version.based_on_version_id])
                            if source_version.based_on_version_id
                            else None
                        ),
                        "name": source_version.name,
                        "normalized_name": source_version.normalized_name,
                        "canonical_unit_id": str(
                            unit_map.get(
                                source_version.canonical_unit_id, source_version.canonical_unit_id
                            )
                        ),
                        "mass_per_canonical_quantity": str(
                            source_version.mass_per_canonical_quantity
                        ),
                        "default_store_section_id": (
                            str(section_map[source_version.default_store_section_id])
                            if source_version.default_store_section_id
                            else None
                        ),
                        "published_at": None,
                        "dietary_tag_ids": [
                            str(destination_tag_ids[item.dietary_tag_id])
                            for item in graph.version_tags
                            if item.ingredient_version_id == source_version.id
                        ],
                        "published_by_user_id": str(context.actor_user_id),
                        "immutable": True,
                    },
                )
            )
        for source_tag in graph.source_tags:
            if source_tag.id in new_destination_tags:
                records.append(
                    (
                        "dietary_tag",
                        destination_tag_ids[source_tag.id],
                        {
                            "id": str(destination_tag_ids[source_tag.id]),
                            "organization_id": str(command.destination_organization_id),
                            "seed_key": None,
                            "name": source_tag.name,
                            "normalized_name": source_tag.normalized_name,
                            "color": source_tag.color,
                            "retired_at": None,
                            "retired_by_user_id": None,
                            "created_at": None,
                            "created_by_user_id": str(context.actor_user_id),
                            "field_clocks": {
                                "name": {
                                    "winning_client_wall_time": command.client_wall_time.astimezone(
                                        UTC
                                    ).isoformat(),
                                    "winning_mutation_id": str(command.mutation_id),
                                },
                                "color": {
                                    "winning_client_wall_time": command.client_wall_time.astimezone(
                                        UTC
                                    ).isoformat(),
                                    "winning_mutation_id": str(command.mutation_id),
                                },
                                "lifecycle": {
                                    "winning_client_wall_time": command.client_wall_time.astimezone(
                                        UTC
                                    ).isoformat(),
                                    "winning_mutation_id": str(command.mutation_id),
                                },
                            },
                        },
                    )
                )
        first, last = await _reserve_change_range(
            session, command.destination_organization_id, command.mutation_id, len(records)
        )
        result = CopyIngredientToOrganizationResult(
            command.mutation_id,
            command.source_organization_id,
            command.destination_organization_id,
            graph.source_id,
            destination_ingredient_id,
            graph.current_version.id,
            destination_current_version_id,
            graph.current_version.name,
            unit_map.get(
                graph.current_version.canonical_unit_id, graph.current_version.canonical_unit_id
            ),
            (
                section_map[graph.current_version.default_store_section_id]
                if graph.current_version.default_store_section_id is not None
                else None
            ),
            current_tag_ids,
            first,
            last,
            False,
        )
        session.add(
            _copy_mutation(
                command,
                context,
                actor_role,
                request_hash,
                "accepted",
                _copy_result_payload(result),
                first,
                last,
            )
        )
        await session.flush()
        destination_ingredient = Ingredient(
            id=destination_ingredient_id,
            organization_id=command.destination_organization_id,
            current_version_id=destination_current_version_id,
            created_by_user_id=context.actor_user_id,
        )
        session.add(destination_ingredient)
        destination_tags: dict[UUID, DietaryTag] = {}
        for source_tag in graph.source_tags:
            if source_tag.id in new_destination_tags:
                destination_tag = DietaryTag(
                    id=destination_tag_ids[source_tag.id],
                    organization_id=command.destination_organization_id,
                    seed_key=None,
                    name=source_tag.name,
                    normalized_name=source_tag.normalized_name,
                    color=source_tag.color,
                    created_by_user_id=context.actor_user_id,
                )
                destination_tags[source_tag.id] = destination_tag
                session.add(destination_tag)
        await session.flush()
        for tag_assignment in graph.version_tags:
            session.add(
                IngredientVersionDietaryTag(
                    ingredient_version_id=destination_version_ids[
                        tag_assignment.ingredient_version_id
                    ],
                    dietary_tag_id=destination_tag_ids[tag_assignment.dietary_tag_id],
                    organization_id=command.destination_organization_id,
                )
            )
        await session.flush()
        pending_versions = {item.id: item for item in graph.versions}
        inserted_version_ids: set[UUID] = set()
        versions_to_copy: list[IngredientVersion] = []
        while pending_versions:
            ready = [
                item
                for item in pending_versions.values()
                if item.based_on_version_id is None
                or item.based_on_version_id in inserted_version_ids
            ]
            if not ready:
                raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
            for source_version in sorted(ready, key=lambda item: item.id):
                versions_to_copy.append(source_version)
                inserted_version_ids.add(source_version.id)
                del pending_versions[source_version.id]
        destination_versions: dict[UUID, IngredientVersion] = {}
        for source_version in versions_to_copy:
            destination_version = IngredientVersion(
                id=destination_version_ids[source_version.id],
                organization_id=command.destination_organization_id,
                ingredient_id=destination_ingredient_id,
                based_on_version_id=(
                    destination_version_ids[source_version.based_on_version_id]
                    if source_version.based_on_version_id is not None
                    else None
                ),
                name=source_version.name,
                normalized_name=source_version.normalized_name,
                canonical_unit_id=unit_map.get(
                    source_version.canonical_unit_id, source_version.canonical_unit_id
                ),
                mass_per_canonical_quantity=source_version.mass_per_canonical_quantity,
                default_store_section_id=(
                    section_map[source_version.default_store_section_id]
                    if source_version.default_store_section_id is not None
                    else None
                ),
                published_by_user_id=context.actor_user_id,
            )
            destination_versions[source_version.id] = destination_version
            session.add(destination_version)
        await session.flush()
        records_by_id = {entity_id: record for _kind, entity_id, record in records}
        records_by_id[destination_ingredient_id]["created_at"] = (
            destination_ingredient.created_at.isoformat()
        )
        for source_version_id, destination_version in destination_versions.items():
            records_by_id[destination_version_ids[source_version_id]]["published_at"] = (
                destination_version.published_at.isoformat()
            )
        for source_tag_id, destination_tag in destination_tags.items():
            records_by_id[destination_tag_ids[source_tag_id]]["created_at"] = (
                destination_tag.created_at.isoformat()
            )
        session.add_all(
            OrganizationChange(
                organization_id=command.destination_organization_id,
                sequence=first + index,
                mutation_id=command.mutation_id,
                entity_id=entity_id,
                entity_kind=kind,
                operation="upsert",
                payload={"record_schema_version": 1, "record": record},
            )
            for index, (kind, entity_id, record) in enumerate(records)
        )
        for kind, entity_id, _record in records:
            fields = {
                "ingredient": ("lifecycle", "current_version_id", "current_price_estimate_id"),
                "dietary_tag": ("name", "color", "lifecycle"),
            }.get(kind, ())
            for field_name in fields:
                session.add(
                    FieldClock(
                        organization_id=command.destination_organization_id,
                        entity_kind=kind,
                        entity_id=entity_id,
                        field_name=field_name,
                        winning_client_wall_time=command.client_wall_time.astimezone(UTC),
                        winning_mutation_id=command.mutation_id,
                    )
                )
        return result


async def copy_ingredient_to_organization(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CopyIngredientToOrganizationCommand,
) -> CopyIngredientToOrganizationResult:
    try:
        return await _copy_ingredient_to_organization_once(session_factory, context, command)
    except ApplicationServiceError as error:
        if error.code not in {"validation_failed", "stale_precondition"}:
            raise
        async with session_factory() as session, session.begin():
            if not await _authorized(
                session,
                context,
                PreviewIngredientCopyCommand(
                    command.source_organization_id,
                    command.destination_organization_id,
                    command.ingredient_id,
                ),
            ):
                raise error
            request_hash = _copy_request_hash(command)
            await _lock_active_copy_organizations(
                session, command.source_organization_id, command.destination_organization_id
            )
            if not await _authorized(
                session,
                context,
                PreviewIngredientCopyCommand(
                    command.source_organization_id,
                    command.destination_organization_id,
                    command.ingredient_id,
                ),
            ):
                raise error
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
                    raise ApplicationServiceError(
                        "idempotency_mismatch", retry_same_identity=False
                    ) from None
                if retained.outcome == "accepted":
                    return _copy_retained_result(retained)
                if retained.outcome == "rejected":
                    raise _copy_retained_error(retained) from None
                raise RuntimeError("Ingredient copy retained an unsupported outcome") from None
            actor_role = await _copy_actor_role(
                session, context, command.destination_organization_id
            )
            session.add(
                _copy_mutation(
                    command,
                    context,
                    actor_role,
                    request_hash,
                    "rejected",
                    _copy_error_payload(error),
                )
            )
        raise error


async def _authorized(
    session: AsyncSession,
    context: ExecutionContext,
    command: PreviewIngredientCopyCommand,
) -> bool:
    kind = "agent" if context.oauth_client_id is not None else "browser"
    actor_statement = select(User.id).where(
        User.id == context.actor_user_id,
        User.disabled_at.is_(None),
    )
    if context.client_installation_id.int != 0:
        actor_statement = actor_statement.join(
            ClientInstallation, ClientInstallation.user_id == User.id
        ).where(
            ClientInstallation.id == context.client_installation_id,
            ClientInstallation.disabled_at.is_(None),
            ClientInstallation.installation_kind == kind,
        )
    actor = await session.scalar(actor_statement)
    organizations = set(
        (
            await session.execute(
                select(Organization.id).where(
                    Organization.id.in_(
                        (command.source_organization_id, command.destination_organization_id)
                    ),
                    Organization.retired_at.is_(None),
                )
            )
        ).scalars()
    )
    if actor is None or len(organizations) != 2:
        return False
    system_admin = await session.scalar(
        select(SystemRoleAssignment.id).where(
            SystemRoleAssignment.user_id == context.actor_user_id,
            SystemRoleAssignment.role == "system_admin",
            SystemRoleAssignment.revoked_at.is_(None),
        )
    )
    if system_admin is not None:
        return True
    rows = (
        await session.execute(
            select(
                OrganizationMembership.organization_id,
                OrganizationMembership.role,
            ).where(
                OrganizationMembership.user_id == context.actor_user_id,
                OrganizationMembership.organization_id.in_(organizations),
                OrganizationMembership.state == "active",
            )
        )
    ).all()
    roles: dict[UUID, str] = {row[0]: row[1] for row in rows}
    return (
        roles.get(command.source_organization_id) in ("member", "organization_admin")
        and roles.get(command.destination_organization_id) == "organization_admin"
    )
