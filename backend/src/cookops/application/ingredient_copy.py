"""Read-only guarded preview for copying one ingredient across organizations."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.persistence.models import (
    ClientInstallation,
    DietaryTag,
    Ingredient,
    IngredientVersion,
    IngredientVersionDietaryTag,
    Organization,
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


async def preview_ingredient_copy(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: PreviewIngredientCopyCommand,
) -> IngredientCopyPreview:
    async with session_factory() as session:
        if not await _authorized(session, context, command):
            raise ApplicationServiceError("forbidden", retry_same_identity=True)

        source = await session.scalar(
            select(Ingredient).where(
                Ingredient.id == command.ingredient_id,
                Ingredient.organization_id == command.source_organization_id,
                Ingredient.retired_at.is_(None),
                Ingredient.current_version_id.is_not(None),
            )
        )
        versions = []
        if source is not None:
            versions = list(
                (
                    await session.execute(
                        select(IngredientVersion).where(
                            IngredientVersion.ingredient_id == source.id,
                            IngredientVersion.organization_id == command.source_organization_id,
                        )
                    )
                ).scalars()
            )
        version = (
            next((item for item in versions if item.id == source.current_version_id), None)
            if source
            else None
        )
        version_by_id = {item.id: item for item in versions}
        if source is None or version is None or len(version_by_id) != len(versions):
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)

        if any(
            item.based_on_version_id is not None
            and (
                item.based_on_version_id not in version_by_id
                or version_by_id[item.based_on_version_id].ingredient_id != source.id
                or version_by_id[item.based_on_version_id].organization_id
                != command.source_organization_id
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

        version_tags = list(
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
            or item.organization_id != command.source_organization_id
            for item in version_tags
        ):
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
        tag_ids = tuple(
            sorted(
                {
                    item.dietary_tag_id
                    for item in version_tags
                    if item.ingredient_version_id == version.id
                }
            )
        )
        unit_ids = {item.canonical_unit_id for item in versions}
        source_units = {
            item.id: item
            for item in (
                await session.execute(select(UnitDefinition).where(UnitDefinition.id.in_(unit_ids)))
            ).scalars()
        }
        section_ids = {
            item.default_store_section_id for item in versions if item.default_store_section_id
        }
        source_sections = {
            item.id: item
            for item in (
                await session.execute(
                    select(StoreSection).where(
                        StoreSection.id.in_(section_ids),
                        StoreSection.organization_id == command.source_organization_id,
                    )
                )
            ).scalars()
        }
        source_tags = tuple(
            (
                await session.execute(
                    select(DietaryTag).where(
                        DietaryTag.id.in_({item.dietary_tag_id for item in version_tags}),
                        DietaryTag.organization_id == command.source_organization_id,
                    )
                )
            ).scalars()
        )
        if (
            len(source_units) != len(unit_ids)
            or any(
                item.organization_id is not None
                and item.organization_id != command.source_organization_id
                for item in source_units.values()
            )
            or len(source_sections) != len(section_ids)
        ):
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
        if len(source_tags) != len({item.dietary_tag_id for item in version_tags}):
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)

        requirements: list[IngredientCopyMappingRequirement] = []
        destination_units = {
            item.id: item
            for item in (
                await session.execute(
                    select(UnitDefinition).where(
                        UnitDefinition.id.in_(unit_ids),
                        UnitDefinition.organization_id.is_(None),
                        UnitDefinition.retired_at.is_(None),
                    )
                )
            ).scalars()
        }
        for unit in sorted(source_units.values(), key=lambda item: item.id):
            if unit.organization_id is not None or unit.id not in destination_units:
                requirements.append(IngredientCopyMappingRequirement("canonical_unit", unit.id))
        destination_sections = {
            item.id: item
            for item in (
                await session.execute(
                    select(StoreSection).where(
                        StoreSection.organization_id == command.destination_organization_id,
                        StoreSection.id.in_(section_ids),
                        StoreSection.retired_at.is_(None),
                    )
                )
            ).scalars()
        }
        for section in sorted(source_sections.values(), key=lambda item: item.id):
            if section.id not in destination_sections:
                requirements.append(
                    IngredientCopyMappingRequirement("default_store_section", section.id)
                )
        destination_seed_keys = {
            key
            for key in (
                await session.execute(
                    select(DietaryTag.seed_key).where(
                        DietaryTag.organization_id == command.destination_organization_id,
                        DietaryTag.seed_key.is_not(None),
                        DietaryTag.retired_at.is_(None),
                    )
                )
            ).scalars()
            if key is not None
        }
        for tag in sorted(source_tags, key=lambda item: item.id):
            if tag.seed_key is None or tag.seed_key not in destination_seed_keys:
                requirements.append(
                    IngredientCopyMappingRequirement("dietary_tag", tag.id, tag.seed_key)
                )

        fingerprint_value = {
            "source": [str(source.id), source.retired_at],
            "versions": [
                [
                    str(item.id),
                    str(item.based_on_version_id) if item.based_on_version_id else None,
                    item.name,
                    str(item.canonical_unit_id),
                    str(item.default_store_section_id) if item.default_store_section_id else None,
                    str(item.mass_per_canonical_quantity),
                    sorted(
                        str(tag.dietary_tag_id)
                        for tag in version_tags
                        if tag.ingredient_version_id == item.id
                    ),
                ]
                for item in sorted(versions, key=lambda item: item.id)
            ],
            "source_dependencies": [
                [
                    str(item.id),
                    item.organization_id,
                    item.retired_at,
                    getattr(item, "seed_key", None),
                    getattr(item, "code", None),
                    getattr(item, "normalized_name", None),
                ]
                for item in sorted(source_units.values(), key=lambda item: item.id)
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
                for item in sorted(source_sections.values(), key=lambda item: item.id)
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
                for item in sorted(source_tags, key=lambda item: item.id)
            ],
            "destination": [
                sorted(str(item.id) for item in destination_units.values()),
                sorted(str(item.id) for item in destination_sections.values()),
                sorted(destination_seed_keys),
            ],
            "requirements": [
                [item.kind, str(item.source_id), item.seed_key] for item in requirements
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_value, default=str, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        return IngredientCopyPreview(
            command.source_organization_id,
            command.destination_organization_id,
            source.id,
            version.id,
            version.name,
            version.canonical_unit_id,
            version.default_store_section_id,
            tag_ids,
            fingerprint,
            tuple(requirements),
        )


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
