import asyncio
import base64
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import PostgresDsn
from sqlalchemy import Engine, create_engine, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command as alembic_command
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.development_seed import (
    _ADVISORY_LOCK_KEY,
    DEVELOPMENT_EVENT_ID,
    DEVELOPMENT_IDENTITIES,
    MEMBER,
    MULTI_ORGANIZATION_MEMBER,
    NO_ACCESS,
    ORGANIZATION_ADMIN,
    PRIMARY_ORGANIZATION_ID,
    SECONDARY_ORGANIZATION_ID,
    SYSTEM_ADMIN,
    DevelopmentSeedConflict,
    DevelopmentSeedForbidden,
    provision_dummy_development_identities,
)
from cookops.main import create_app
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

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()


@dataclass
class SeedDatabase:
    sync_engine: Engine
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture
def seed_database() -> Iterator[SeedDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    sync_engine = create_engine(database_url)
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    database = SeedDatabase(
        sync_engine=sync_engine,
        sessions=async_sessionmaker(async_engine, expire_on_commit=False),
    )
    try:
        yield database
    finally:
        asyncio.run(async_engine.dispose())
        sync_engine.dispose()
        alembic_command.downgrade(configuration, "base")


def dummy_settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        human_auth_provider=HumanAuthProvider.DUMMY,
        database_url=PostgresDsn(os.environ["TEST_DATABASE_URL"]),
        browser_session_hmac_key=KEY,
    )


def test_provisioning_is_idempotent_and_covers_all_required_dummy_authorities(
    seed_database: SeedDatabase,
) -> None:
    first = asyncio.run(
        provision_dummy_development_identities(dummy_settings(), seed_database.sessions)
    )
    second = asyncio.run(
        provision_dummy_development_identities(dummy_settings(), seed_database.sessions)
    )

    assert first == second
    assert first.primary_organization_id == PRIMARY_ORGANIZATION_ID
    assert first.secondary_organization_id == SECONDARY_ORGANIZATION_ID
    assert first.identities == DEVELOPMENT_IDENTITIES
    with seed_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(User)) == 5
        assert connection.scalar(select(func.count()).select_from(ExternalIdentity)) == 5
        assert connection.scalar(select(func.count()).select_from(Organization)) == 2
        assert connection.scalar(select(func.count()).select_from(OrganizationMembership)) == 4
        assert connection.scalar(select(func.count()).select_from(SystemRoleAssignment)) == 1
        assert connection.scalar(
            select(func.count()).select_from(Event).where(Event.id == DEVELOPMENT_EVENT_ID)
        ) == 1
        event_day_id = connection.scalar(
            select(EventDay.id).where(EventDay.event_id == DEVELOPMENT_EVENT_ID)
        )
        meal_role_id = connection.scalar(
            select(EventMealRole.id).where(EventMealRole.event_id == DEVELOPMENT_EVENT_ID)
        )
        ingredient_id = connection.scalar(
            select(Ingredient.id).where(Ingredient.organization_id == PRIMARY_ORGANIZATION_ID)
        )
        ingredient_version_id = connection.scalar(
            select(IngredientVersion.id).where(IngredientVersion.ingredient_id == ingredient_id)
        )
        recipe_id = connection.scalar(
            select(Recipe.id).where(Recipe.organization_id == PRIMARY_ORGANIZATION_ID)
        )
        recipe_version_id = connection.scalar(
            select(RecipeVersion.id).where(RecipeVersion.recipe_id == recipe_id)
        )
        assert None not in (event_day_id, meal_role_id, ingredient_id, ingredient_version_id)
        assert None not in (recipe_id, recipe_version_id)
        assert connection.scalar(
            select(func.count()).select_from(ScheduledRecipe).where(
                ScheduledRecipe.event_id == DEVELOPMENT_EVENT_ID,
                ScheduledRecipe.event_day_id == event_day_id,
                ScheduledRecipe.event_meal_role_id == meal_role_id,
                ScheduledRecipe.recipe_id == recipe_id,
                ScheduledRecipe.recipe_version_id == recipe_version_id,
            )
        ) == 1
        scheduled_recipe_id = connection.scalar(
            select(ScheduledRecipe.id).where(ScheduledRecipe.event_id == DEVELOPMENT_EVENT_ID)
        )
        assert scheduled_recipe_id is not None
        assert connection.scalar(
            select(func.count()).select_from(RecipeVersionIngredientLine).where(
                RecipeVersionIngredientLine.recipe_version_id == recipe_version_id,
                RecipeVersionIngredientLine.recipe_id == recipe_id,
                RecipeVersionIngredientLine.ingredient_version_id == ingredient_version_id,
            )
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(StoreSection).where(
                StoreSection.organization_id == PRIMARY_ORGANIZATION_ID
            )
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(UnitDefinition).where(
                UnitDefinition.code == "portion",
                UnitDefinition.organization_id == PRIMARY_ORGANIZATION_ID,
            )
        ) == 1
        scaling_unit_id = connection.scalar(
            select(RecipeVersion.scaling_unit_id).where(RecipeVersion.id == recipe_version_id)
        )
        assert connection.scalar(
            select(UnitDefinition.organization_id).where(UnitDefinition.id == scaling_unit_id)
        ) == PRIMARY_ORGANIZATION_ID
        shopping_list_id = connection.scalar(
            select(ShoppingList.id).where(ShoppingList.event_id == DEVELOPMENT_EVENT_ID)
        )
        assert shopping_list_id is not None
        revision_id = connection.scalar(
            select(ShoppingList.current_generation_revision_id).where(
                ShoppingList.id == shopping_list_id
            )
        )
        assert revision_id is not None
        assert connection.scalar(
            select(func.count()).select_from(ShoppingGenerationRevision).where(
                ShoppingGenerationRevision.id == revision_id,
                ShoppingGenerationRevision.shopping_list_id == shopping_list_id,
                ShoppingGenerationRevision.event_id == DEVELOPMENT_EVENT_ID,
            )
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(ShoppingRevisionSource).where(
                ShoppingRevisionSource.generation_revision_id == revision_id,
                ShoppingRevisionSource.shopping_list_id == shopping_list_id,
                ShoppingRevisionSource.event_id == DEVELOPMENT_EVENT_ID,
                ShoppingRevisionSource.scheduled_recipe_id == scheduled_recipe_id,
            )
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(ShoppingIngredientRow).where(
                ShoppingIngredientRow.shopping_list_id == shopping_list_id
            )
        ) == 1
        contribution_id = connection.scalar(
            select(ShoppingContribution.id).where(
                ShoppingContribution.shopping_list_id == shopping_list_id
            )
        )
        assert contribution_id is not None
        assert connection.scalar(
            select(func.count()).select_from(ShoppingContributionSnapshot).where(
                ShoppingContributionSnapshot.shopping_contribution_id == contribution_id
            )
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(ShoppingList).where(
                ShoppingList.organization_id == SECONDARY_ORGANIZATION_ID
            )
        ) == 0

        system_role = connection.execute(
            select(SystemRoleAssignment.user_id, SystemRoleAssignment.invited_email).where(
                SystemRoleAssignment.revoked_at.is_(None)
            )
        ).one()
        assert system_role == (SYSTEM_ADMIN.id, SYSTEM_ADMIN.verified_email)

        organization_admin_role = connection.scalar(
            select(OrganizationMembership.role).where(
                OrganizationMembership.organization_id == PRIMARY_ORGANIZATION_ID,
                OrganizationMembership.user_id == ORGANIZATION_ADMIN.id,
            )
        )
        assert organization_admin_role == "organization_admin"

        member_organizations = connection.scalars(
            select(OrganizationMembership.organization_id).where(
                OrganizationMembership.user_id == MULTI_ORGANIZATION_MEMBER.id,
                OrganizationMembership.state == "active",
            )
        ).all()
        assert set(member_organizations) == {PRIMARY_ORGANIZATION_ID, SECONDARY_ORGANIZATION_ID}
        assert (
            connection.scalar(
                select(func.count())
                .select_from(OrganizationMembership)
                .where(OrganizationMembership.user_id == MEMBER.id)
            )
            == 1
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(OrganizationMembership)
                .where(OrganizationMembership.user_id == NO_ACCESS.id)
            )
            == 0
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(SystemRoleAssignment)
                .where(SystemRoleAssignment.user_id == NO_ACCESS.id)
            )
            == 0
        )


def test_concurrent_provisioning_has_exact_idempotent_result(
    seed_database: SeedDatabase,
) -> None:
    async def provision_concurrently() -> None:
        await asyncio.gather(
            *(
                provision_dummy_development_identities(dummy_settings(), seed_database.sessions)
                for _ in range(8)
            )
        )

    asyncio.run(provision_concurrently())

    with seed_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(User)) == 5
        assert connection.scalar(select(func.count()).select_from(ExternalIdentity)) == 5
        assert connection.scalar(select(func.count()).select_from(Organization)) == 2
        assert connection.scalar(select(func.count()).select_from(OrganizationMembership)) == 4
        assert connection.scalar(select(func.count()).select_from(SystemRoleAssignment)) == 1


def test_provisioning_waits_for_the_seed_advisory_lock(seed_database: SeedDatabase) -> None:
    async def assert_lock_is_observed() -> None:
        seed_task: asyncio.Task[object] | None = None
        try:
            async with seed_database.sessions() as lock_holder, lock_holder.begin():
                await lock_holder.execute(select(func.pg_advisory_xact_lock(_ADVISORY_LOCK_KEY)))
                seed_task = asyncio.create_task(
                    provision_dummy_development_identities(dummy_settings(), seed_database.sessions)
                )
                async with seed_database.sessions() as observer:
                    class_id = _ADVISORY_LOCK_KEY >> 32
                    object_id = _ADVISORY_LOCK_KEY & 0xFFFF_FFFF
                    waiting = False
                    for _ in range(20):
                        waiting = bool(
                            await observer.scalar(
                                text(
                                    "SELECT EXISTS ("
                                    "SELECT 1 FROM pg_locks "
                                    "WHERE locktype = 'advisory' AND granted = false "
                                    "AND classid = :class_id AND objid = :object_id"
                                    ")"
                                ),
                                {"class_id": class_id, "object_id": object_id},
                            )
                        )
                        if waiting:
                            break
                        await asyncio.sleep(0.05)
                    assert waiting is True
            await asyncio.wait_for(seed_task, timeout=5)
        finally:
            if seed_task is not None:
                if not seed_task.done():
                    seed_task.cancel()
                await asyncio.gather(seed_task, return_exceptions=True)

    asyncio.run(assert_lock_is_observed())

    with seed_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(User)) == 5


def test_seed_rejects_a_conflicting_deterministic_user_id(seed_database: SeedDatabase) -> None:
    with seed_database.sync_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=SYSTEM_ADMIN.id,
                display_name="Unrelated user",
                verified_email="unrelated@cookops.test",
                normalized_email="unrelated@cookops.test",
            )
        )

    with pytest.raises(DevelopmentSeedConflict, match="users"):
        asyncio.run(
            provision_dummy_development_identities(dummy_settings(), seed_database.sessions)
        )

    with seed_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(ExternalIdentity)) == 0
        assert connection.scalar(select(func.count()).select_from(SystemRoleAssignment)) == 0


def test_seeded_dummy_identities_use_the_existing_browser_session_authentication_flow(
    seed_database: SeedDatabase,
) -> None:
    asyncio.run(provision_dummy_development_identities(dummy_settings(), seed_database.sessions))

    with TestClient(create_app(dummy_settings()), base_url="https://testserver") as client:
        listed = client.get("/auth/dummy/identities")
        assert listed.status_code == 200
        assert listed.json() == {
            "identities": [
                {"subject": identity.subject, "display_name": identity.display_name}
                for identity in sorted(
                    DEVELOPMENT_IDENTITIES, key=lambda identity: identity.display_name
                )
            ]
        }

        denied = client.post("/auth/dummy/session", json={"subject": NO_ACCESS.subject})
        assert denied.status_code == 403

        created = client.post("/auth/dummy/session", json={"subject": ORGANIZATION_ADMIN.subject})
        assert created.status_code == 204
        current = client.get("/auth/session")
        assert current.status_code == 200
        assert current.json() == {
            "id": str(ORGANIZATION_ADMIN.id),
            "display_name": ORGANIZATION_ADMIN.display_name,
            "verified_email": ORGANIZATION_ADMIN.verified_email,
        }


def test_provisioning_refuses_production_before_acquiring_a_database_session() -> None:
    production_settings = Settings(
        environment=Environment.PRODUCTION,
        human_auth_provider=HumanAuthProvider.GOOGLE,
        google_client_id="test-client.apps.googleusercontent.com",
        database_url=PostgresDsn(os.environ["TEST_DATABASE_URL"]),
        browser_session_hmac_key=KEY,
        browser_origin="https://testserver",
        oauth_interaction_details_api_credential_base64url=KEY[:-1] + "U",
        oauth_interaction_approval_api_credential_base64url=KEY,
        oauth_interaction_origin="https://testserver",
    )

    def unexpected_session_factory() -> AsyncSession:
        raise AssertionError("production seed guard acquired a database session")

    with pytest.raises(DevelopmentSeedForbidden, match="development or test"):
        asyncio.run(
            provision_dummy_development_identities(
                production_settings,
                cast(async_sessionmaker[AsyncSession], unexpected_session_factory),
            )
        )
