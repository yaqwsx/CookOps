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
        version = None
        if source is not None:
            version = await session.scalar(
                select(IngredientVersion).where(
                    IngredientVersion.id == source.current_version_id,
                    IngredientVersion.ingredient_id == source.id,
                    IngredientVersion.organization_id == command.source_organization_id,
                )
            )
        if source is None or version is None:
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)

        tag_ids = tuple(
            (
                await session.execute(
                    select(IngredientVersionDietaryTag.dietary_tag_id)
                    .where(
                        IngredientVersionDietaryTag.ingredient_version_id == version.id,
                        IngredientVersionDietaryTag.organization_id
                        == command.source_organization_id,
                    )
                    .order_by(IngredientVersionDietaryTag.dietary_tag_id)
                )
            ).scalars()
        )
        source_unit = await session.get(UnitDefinition, version.canonical_unit_id)
        source_section = (
            await session.scalar(
                select(StoreSection).where(
                    StoreSection.id == version.default_store_section_id,
                    StoreSection.organization_id == command.source_organization_id,
                    StoreSection.retired_at.is_(None),
                )
            )
            if version.default_store_section_id is not None
            else None
        )
        source_tags = tuple(
            (
                await session.execute(
                    select(DietaryTag).where(
                        DietaryTag.id.in_(tag_ids),
                        DietaryTag.organization_id == command.source_organization_id,
                    )
                )
            ).scalars()
        )
        if (
            source_unit is None
            or (
                source_unit.retired_at is not None
                or (
                    source_unit.organization_id is not None
                    and source_unit.organization_id != command.source_organization_id
                )
            )
            or (version.default_store_section_id is not None and source_section is None)
        ):
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)
        if len(source_tags) != len(tag_ids):
            raise ApplicationServiceError("stale_precondition", retry_same_identity=False)

        requirements: list[IngredientCopyMappingRequirement] = []
        destination_unit = await session.scalar(
            select(UnitDefinition).where(
                UnitDefinition.id == source_unit.id,
                UnitDefinition.organization_id.is_(None),
                UnitDefinition.retired_at.is_(None),
            )
        )
        if source_unit.organization_id is not None or destination_unit is None:
            requirements.append(IngredientCopyMappingRequirement("canonical_unit", source_unit.id))
        destination_section = None
        if source_section is not None:
            destination_section = await session.scalar(
                select(StoreSection).where(
                    StoreSection.organization_id == command.destination_organization_id,
                    StoreSection.id == source_section.id,
                    StoreSection.retired_at.is_(None),
                )
            )
            if destination_section is None:
                requirements.append(
                    IngredientCopyMappingRequirement("default_store_section", source_section.id)
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
            "source": [str(source.id), str(version.id), source.retired_at, version.name],
            "version": [
                str(version.canonical_unit_id),
                str(version.default_store_section_id) if version.default_store_section_id else None,
                str(version.mass_per_canonical_quantity),
                [str(tag_id) for tag_id in tag_ids],
            ],
            "destination": [
                str(destination_unit.id) if destination_unit else None,
                str(destination_section.id) if destination_section else None,
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
