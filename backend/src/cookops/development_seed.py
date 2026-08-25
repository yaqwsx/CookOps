"""Provision fixed dummy identities for development and automated tests.

This module is a command/service boundary, not an HTTP adapter. It only accepts
development or test settings with the dummy authentication provider; production
deployments cannot run it successfully.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid5

from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.database import create_database_runtime
from cookops.persistence.models import (
    Event,
    EventDay,
    EventMealRole,
    ExternalIdentity,
    Ingredient,
    IngredientVersion,
    Organization,
    OrganizationMembership,
    Recipe,
    RecipeVersion,
    RecipeVersionIngredientLine,
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

_SEED_NAMESPACE = UUID("a962749c-2f5c-4f3a-a8b5-971ce1a94ab3")
_ADVISORY_LOCK_KEY = 4_217_384_208_779


def _seed_id(name: str) -> UUID:
    return uuid5(_SEED_NAMESPACE, name)


class DevelopmentSeedForbidden(RuntimeError):
    """The dummy seed command was attempted outside its trusted local boundary."""


class DevelopmentSeedConflict(RuntimeError):
    """Existing data occupies a deterministic development seed identifier."""


@dataclass(frozen=True, slots=True)
class DevelopmentIdentitySeed:
    """A selectable dummy identity with stable database and provider identifiers."""

    key: str
    display_name: str
    verified_email: str

    @property
    def id(self) -> UUID:
        return _seed_id(f"user:{self.key}")

    @property
    def subject(self) -> str:
        return f"dummy-{self.key}"


SYSTEM_ADMIN = DevelopmentIdentitySeed(
    key="system-admin",
    display_name="System Administrator",
    verified_email="dummy.system-admin@cookops.test",
)
ORGANIZATION_ADMIN = DevelopmentIdentitySeed(
    key="organization-admin",
    display_name="Organization Administrator",
    verified_email="dummy.organization-admin@cookops.test",
)
MEMBER = DevelopmentIdentitySeed(
    key="member",
    display_name="Organization Member",
    verified_email="dummy.member@cookops.test",
)
MULTI_ORGANIZATION_MEMBER = DevelopmentIdentitySeed(
    key="multi-organization-member",
    display_name="Multi-organization Member",
    verified_email="dummy.multi-organization-member@cookops.test",
)
NO_ACCESS = DevelopmentIdentitySeed(
    key="no-access",
    display_name="No Access",
    verified_email="dummy.no-access@cookops.test",
)

DEVELOPMENT_IDENTITIES = (
    SYSTEM_ADMIN,
    ORGANIZATION_ADMIN,
    MEMBER,
    MULTI_ORGANIZATION_MEMBER,
    NO_ACCESS,
)

PRIMARY_ORGANIZATION_ID = _seed_id("organization:primary")
SECONDARY_ORGANIZATION_ID = _seed_id("organization:secondary")
DEVELOPMENT_EVENT_ID = _seed_id("event:primary:shopping")


@dataclass(frozen=True, slots=True)
class DevelopmentSeedResult:
    """Stable identifiers that test code may use without rediscovering seed rows."""

    primary_organization_id: UUID
    secondary_organization_id: UUID
    identities: tuple[DevelopmentIdentitySeed, ...]


def _assert_allowed(settings: Settings) -> None:
    if settings.environment not in (Environment.DEVELOPMENT, Environment.TEST):
        raise DevelopmentSeedForbidden(
            "dummy identity seeding is only available in development or test"
        )
    if settings.human_auth_provider is not HumanAuthProvider.DUMMY:
        raise DevelopmentSeedForbidden(
            "dummy identity seeding requires the dummy authentication provider"
        )


async def _insert_if_absent(
    session: AsyncSession,
    *,
    table: Table,
    rows: tuple[Mapping[str, object], ...],
) -> None:
    """Insert deterministic rows by primary key without overwriting existing data."""

    await session.execute(
        insert(table).values(rows).on_conflict_do_nothing(index_elements=[table.c.id])
    )
    actual_rows = {
        row["id"]: row
        for row in (
            await session.execute(
                select(table).where(table.c.id.in_(tuple(row["id"] for row in rows)))
            )
        )
        .mappings()
        .all()
    }
    for expected in rows:
        actual = actual_rows.get(expected["id"])
        if actual is None or any(
            actual[name] != value for name, value in expected.items() if name != "claimed_at"
        ):
            raise DevelopmentSeedConflict(
                f"reserved development seed row conflicts in {table.name}"
            )


async def provision_dummy_development_identities(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> DevelopmentSeedResult:
    """Idempotently provision the development-only dummy identity fixture.

    The transaction-level advisory lock makes concurrent invocations converge on
    the fixed primary-key rows. Conflicting user-managed rows are left unchanged
    and PostgreSQL's existing uniqueness constraints reject them.
    """

    _assert_allowed(settings)
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        await session.execute(select(func.pg_advisory_xact_lock(_ADVISORY_LOCK_KEY)))
        await _insert_if_absent(
            session,
            table=cast(Table, User.__table__),
            rows=tuple(
                {
                    "id": identity.id,
                    "display_name": identity.display_name,
                    "verified_email": identity.verified_email,
                    "normalized_email": identity.verified_email,
                }
                for identity in DEVELOPMENT_IDENTITIES
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, ExternalIdentity.__table__),
            rows=tuple(
                {
                    "id": _seed_id(f"external-identity:{identity.key}"),
                    "user_id": identity.id,
                    "provider": "dummy",
                    "provider_subject": identity.subject,
                    "verified_email": identity.verified_email,
                    "normalized_verified_email": identity.verified_email,
                }
                for identity in DEVELOPMENT_IDENTITIES
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, Organization.__table__),
            rows=(
                {
                    "id": PRIMARY_ORGANIZATION_ID,
                    "name": "CookOps Development Primary",
                    "default_currency": "CZK",
                    "created_by_user_id": SYSTEM_ADMIN.id,
                },
                {
                    "id": SECONDARY_ORGANIZATION_ID,
                    "name": "CookOps Development Secondary",
                    "default_currency": "CZK",
                    "created_by_user_id": SYSTEM_ADMIN.id,
                },
            ),
        )
        grams_id = await session.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        if grams_id is None:
            raise DevelopmentSeedConflict("required system unit g is missing")
        scaling_unit_id = _seed_id("unit:primary:shopping:portion")
        await _insert_if_absent(
            session,
            table=cast(Table, UnitDefinition.__table__),
            rows=(
                {
                    "id": scaling_unit_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "code": "portion",
                    "custom_name": "Portion",
                    "normalized_custom_name": "portion",
                    "dimension": "count",
                    "rounds_up_to_whole_unit": True,
                    "allows_ingredient_quantity": False,
                    "allows_recipe_scaling": True,
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        event_day_id = _seed_id("event-day:primary:shopping")
        meal_role_id = _seed_id("event-meal-role:primary:shopping")
        ingredient_id = _seed_id("ingredient:primary:shopping")
        ingredient_version_id = _seed_id("ingredient-version:primary:shopping")
        recipe_id = _seed_id("recipe:primary:shopping")
        recipe_version_id = _seed_id("recipe-version:primary:shopping")
        recipe_line_id = _seed_id("recipe-line:primary:shopping")
        scheduled_recipe_id = _seed_id("scheduled-recipe:primary:shopping")
        section_id = _seed_id("store-section:primary:shopping")
        shopping_list_id = _seed_id("shopping-list:primary:shopping")
        revision_id = _seed_id("shopping-revision:primary:shopping")
        row_id = _seed_id("shopping-row:primary:shopping")
        contribution_id = _seed_id("shopping-contribution:primary:shopping")
        snapshot_id = _seed_id("shopping-snapshot:primary:shopping")
        await _insert_if_absent(
            session,
            table=cast(Table, Event.__table__),
            rows=(
                {
                    "id": DEVELOPMENT_EVENT_ID,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "name": "CookOps Development Shopping",
                    "start_date": date(2030, 1, 1),
                    "end_date": date(2030, 1, 1),
                    "base_expected_attendance": 10,
                    "budget_amount": Decimal("1000"),
                    "currency": "CZK",
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                    "lifecycle": "active",
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, EventDay.__table__),
            rows=(
                {
                    "id": event_day_id,
                    "event_id": DEVELOPMENT_EVENT_ID,
                    "calendar_date": date(2030, 1, 1),
                    "is_visible": True,
                    "provenance": "range_generated",
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, EventMealRole.__table__),
            rows=(
                {
                    "id": meal_role_id,
                    "event_id": DEVELOPMENT_EVENT_ID,
                    "built_in_translation_key": "meal_role.lunch",
                    "position_key": "a",
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, StoreSection.__table__),
            rows=(
                {
                    "id": section_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "name": "Produce",
                    "normalized_name": "produce",
                    "position_key": "a",
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, Ingredient.__table__),
            rows=(
                {
                    "id": ingredient_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "current_version_id": ingredient_version_id,
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, IngredientVersion.__table__),
            rows=(
                {
                    "id": ingredient_version_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "ingredient_id": ingredient_id,
                    "name": "Tomatoes",
                    "normalized_name": "tomatoes",
                    "canonical_unit_id": grams_id,
                    "mass_per_canonical_quantity": Decimal("1"),
                    "default_store_section_id": section_id,
                    "published_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, Recipe.__table__),
            rows=(
                {
                    "id": recipe_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "current_version_id": recipe_version_id,
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        recipe_line = {
            "id": recipe_line_id,
            "organization_id": PRIMARY_ORGANIZATION_ID,
            "recipe_id": recipe_id,
            "recipe_version_id": recipe_version_id,
            "line_key": recipe_line_id,
            "ingredient_version_id": ingredient_version_id,
            "base_quantity": Decimal("100"),
            "preferred_display_unit_id": grams_id,
            "position_key": "a",
            "scaling_behavior": "proportional",
            "include_in_portion_weight": True,
        }
        existing_recipe_line = (
            (
                await session.execute(
                    select(RecipeVersionIngredientLine.__table__).where(
                        RecipeVersionIngredientLine.id == recipe_line_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing_recipe_line is None:
            await session.execute(insert(RecipeVersionIngredientLine).values(recipe_line))
        elif any(existing_recipe_line[key] != value for key, value in recipe_line.items()):
            raise DevelopmentSeedConflict(
                "reserved development seed row conflicts in recipe_version_ingredient_lines"
            )
        await _insert_if_absent(
            session,
            table=cast(Table, RecipeVersion.__table__),
            rows=(
                {
                    "id": recipe_version_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "recipe_id": recipe_id,
                    "name": "Tomato Salad",
                    "scaling_unit_id": scaling_unit_id,
                    "base_scaling_amount": Decimal("100"),
                    "published_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, ScheduledRecipe.__table__),
            rows=(
                {
                    "id": scheduled_recipe_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "event_id": DEVELOPMENT_EVENT_ID,
                    "event_day_id": event_day_id,
                    "event_meal_role_id": meal_role_id,
                    "recipe_id": recipe_id,
                    "recipe_version_id": recipe_version_id,
                    "diner_count": 10,
                    "attendance_mode": "follows_event",
                    "consumption_percentage": Decimal("100"),
                    "selected_scale_amount": Decimal("100"),
                    "scale_mode": "suggested",
                    "position_key": "a",
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, ShoppingList.__table__),
            rows=(
                {
                    "id": shopping_list_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "event_id": DEVELOPMENT_EVENT_ID,
                    "name": "Development shopping",
                    "current_generation_revision_id": revision_id,
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, ShoppingGenerationRevision.__table__),
            rows=(
                {
                    "id": revision_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "event_id": DEVELOPMENT_EVENT_ID,
                    "shopping_list_id": shopping_list_id,
                    "generated_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, ShoppingIngredientRow.__table__),
            rows=(
                {
                    "id": row_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "event_id": DEVELOPMENT_EVENT_ID,
                    "shopping_list_id": shopping_list_id,
                    "ingredient_id": ingredient_id,
                    "ingredient_name": "Tomatoes",
                    "calculation_unit_id": grams_id,
                    "default_store_section_id": section_id,
                    "default_store_section_name": "Produce",
                    "created_by_user_id": ORGANIZATION_ADMIN.id,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, ShoppingContribution.__table__),
            rows=(
                {
                    "id": contribution_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "event_id": DEVELOPMENT_EVENT_ID,
                    "shopping_list_id": shopping_list_id,
                    "shopping_ingredient_row_id": row_id,
                    "ingredient_id": ingredient_id,
                    "scheduled_recipe_id": scheduled_recipe_id,
                    "fulfilment_credit": Decimal("0"),
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, ShoppingContributionSnapshot.__table__),
            rows=(
                {
                    "id": snapshot_id,
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "event_id": DEVELOPMENT_EVENT_ID,
                    "shopping_list_id": shopping_list_id,
                    "generation_revision_id": revision_id,
                    "shopping_contribution_id": contribution_id,
                    "ingredient_id": ingredient_id,
                    "active_in_revision": True,
                    "generated_quantity": Decimal("1000"),
                    "ingredient_version_id": ingredient_version_id,
                    "ingredient_name": "Tomatoes",
                    "source_details": {"recipe_name": "Tomato Salad"},
                },
            ),
        )
        await session.execute(
            insert(ShoppingRevisionSource)
            .values(
                generation_revision_id=revision_id,
                shopping_list_id=shopping_list_id,
                organization_id=PRIMARY_ORGANIZATION_ID,
                event_id=DEVELOPMENT_EVENT_ID,
                scheduled_recipe_id=scheduled_recipe_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ShoppingRevisionSource.generation_revision_id,
                    ShoppingRevisionSource.scheduled_recipe_id,
                ]
            )
        )
        expected_source = {
            "generation_revision_id": revision_id,
            "shopping_list_id": shopping_list_id,
            "organization_id": PRIMARY_ORGANIZATION_ID,
            "event_id": DEVELOPMENT_EVENT_ID,
            "scheduled_recipe_id": scheduled_recipe_id,
        }
        actual_source = (
            (
                await session.execute(
                    select(ShoppingRevisionSource.__table__).where(
                        ShoppingRevisionSource.generation_revision_id == revision_id,
                        ShoppingRevisionSource.scheduled_recipe_id == scheduled_recipe_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if actual_source is None or any(
            actual_source[key] != value for key, value in expected_source.items()
        ):
            raise DevelopmentSeedConflict(
                "reserved development seed row conflicts in shopping_revision_sources"
            )
        await _insert_if_absent(
            session,
            table=cast(Table, SystemRoleAssignment.__table__),
            rows=(
                {
                    "id": _seed_id("system-role:system-admin"),
                    "user_id": SYSTEM_ADMIN.id,
                    "invited_email": SYSTEM_ADMIN.verified_email,
                    "role": "system_admin",
                    "granted_by_user_id": SYSTEM_ADMIN.id,
                    "claimed_at": now,
                },
            ),
        )
        await _insert_if_absent(
            session,
            table=cast(Table, OrganizationMembership.__table__),
            rows=(
                {
                    "id": _seed_id(f"membership:{PRIMARY_ORGANIZATION_ID}:organization-admin"),
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "user_id": ORGANIZATION_ADMIN.id,
                    "invited_email": ORGANIZATION_ADMIN.verified_email,
                    "role": "organization_admin",
                    "state": "active",
                    "invited_by_user_id": SYSTEM_ADMIN.id,
                    "claimed_at": now,
                },
                {
                    "id": _seed_id(f"membership:{PRIMARY_ORGANIZATION_ID}:member"),
                    "organization_id": PRIMARY_ORGANIZATION_ID,
                    "user_id": MEMBER.id,
                    "invited_email": MEMBER.verified_email,
                    "role": "member",
                    "state": "active",
                    "invited_by_user_id": SYSTEM_ADMIN.id,
                    "claimed_at": now,
                },
                *(
                    {
                        "id": _seed_id(f"membership:{organization_id}:multi-organization-member"),
                        "organization_id": organization_id,
                        "user_id": MULTI_ORGANIZATION_MEMBER.id,
                        "invited_email": MULTI_ORGANIZATION_MEMBER.verified_email,
                        "role": "member",
                        "state": "active",
                        "invited_by_user_id": SYSTEM_ADMIN.id,
                        "claimed_at": now,
                    }
                    for organization_id in (
                        PRIMARY_ORGANIZATION_ID,
                        SECONDARY_ORGANIZATION_ID,
                    )
                ),
            ),
        )
    return DevelopmentSeedResult(
        primary_organization_id=PRIMARY_ORGANIZATION_ID,
        secondary_organization_id=SECONDARY_ORGANIZATION_ID,
        identities=DEVELOPMENT_IDENTITIES,
    )


async def _run_command() -> DevelopmentSeedResult:
    settings = Settings()
    _assert_allowed(settings)
    runtime = create_database_runtime(str(settings.database_url))
    try:
        return await provision_dummy_development_identities(settings, runtime.session_factory)
    finally:
        await runtime.close()


def main() -> None:
    """Run ``python -m cookops.development_seed`` after applying migrations."""

    result = asyncio.run(_run_command())
    print(
        "Provisioned deterministic development identities for "
        f"{result.primary_organization_id} and {result.secondary_organization_id}."
    )


if __name__ == "__main__":
    main()
