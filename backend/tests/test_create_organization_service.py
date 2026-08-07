import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from alembic import command as alembic_command
from cookops.application.organizations import (
    ApplicationServiceError,
    CreateOrganizationCommand,
    ExecutionContext,
    create_organization,
)
from cookops.persistence.models import (
    ClientInstallation,
    DietaryTag,
    Mutation,
    Organization,
    OrganizationMealRolePreset,
    SystemRoleAssignment,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

EXPECTED_MEAL_ROLES = (
    "meal_role.breakfast",
    "meal_role.morning_snack",
    "meal_role.soup",
    "meal_role.lunch",
    "meal_role.afternoon_snack",
    "meal_role.dinner",
)
EXPECTED_DIETARY_TAGS = {"vegetarian", "vegan", "gluten", "lactose"}


@dataclass
class ServiceDatabase:
    sync_engine: Engine
    sessions: async_sessionmaker[AsyncSession]
    actor_id: UUID
    installation_id: UUID
    assignment_id: UUID


@pytest.fixture
def service_database() -> Iterator[ServiceDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")

    sync_engine = create_engine(database_url)
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    actor_id = uuid4()
    installation_id = uuid4()
    assignment_id = uuid4()
    now = datetime.now(UTC)
    with sync_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="System administrator",
                verified_email="admin@example.test",
                normalized_email="admin@example.test",
            )
        )
        connection.execute(
            insert(SystemRoleAssignment).values(
                id=assignment_id,
                user_id=actor_id,
                invited_email="admin@example.test",
                role="system_admin",
                granted_by_user_id=actor_id,
                claimed_at=now,
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id,
                user_id=actor_id,
                installation_kind="agent",
            )
        )

    database = ServiceDatabase(
        sync_engine=sync_engine,
        sessions=async_sessionmaker(async_engine, expire_on_commit=False),
        actor_id=actor_id,
        installation_id=installation_id,
        assignment_id=assignment_id,
    )
    try:
        yield database
    finally:
        asyncio.run(async_engine.dispose())
        sync_engine.dispose()
        alembic_command.downgrade(configuration, "base")


def context(database: ServiceDatabase) -> ExecutionContext:
    return ExecutionContext(
        actor_user_id=database.actor_id,
        client_installation_id=database.installation_id,
        oauth_client_id="mcp-client",
        oauth_grant_id="mcp-grant",
    )


def organization_command(
    *,
    mutation_id: UUID | None = None,
    organization_id: UUID | None = None,
    name: str = "Kitchen crew",
    default_currency: str = "CZK",
    client_wall_time: datetime | None = None,
    description: str | None = "First line\r\nSecond line",
) -> CreateOrganizationCommand:
    return CreateOrganizationCommand(
        mutation_id=mutation_id or uuid4(),
        organization_id=organization_id or uuid4(),
        name=name,
        description=description,
        default_currency=default_currency,
        client_wall_time=client_wall_time or datetime.now(UTC),
    )


def test_create_organization_seeds_configuration_and_persists_attribution(
    service_database: ServiceDatabase,
) -> None:
    command = organization_command(name="  Výprava  ", default_currency="czk")

    result = asyncio.run(
        create_organization(service_database.sessions, context(service_database), command)
    )

    assert result.outcome == "accepted"
    assert result.replayed is False
    assert result.name == "Výprava"
    assert result.description == "First line\nSecond line"
    assert result.default_currency == "CZK"
    assert tuple(preset.translation_key for preset in result.meal_role_presets) == (
        EXPECTED_MEAL_ROLES
    )
    assert {tag.seed_key for tag in result.dietary_tags} == EXPECTED_DIETARY_TAGS

    with service_database.sync_engine.connect() as connection:
        organization = connection.execute(
            select(
                Organization.name,
                Organization.description,
                Organization.default_currency,
                Organization.created_by_user_id,
            ).where(Organization.id == command.organization_id)
        ).one()
        assert organization == (
            "Výprava",
            "First line\nSecond line",
            "CZK",
            service_database.actor_id,
        )

        meal_roles = connection.execute(
            select(
                OrganizationMealRolePreset.id,
                OrganizationMealRolePreset.built_in_translation_key,
                OrganizationMealRolePreset.position_key,
            )
            .where(OrganizationMealRolePreset.organization_id == command.organization_id)
            .order_by(OrganizationMealRolePreset.position_key)
        ).all()
        assert tuple(row.built_in_translation_key for row in meal_roles) == EXPECTED_MEAL_ROLES
        assert tuple(row.position_key for row in meal_roles) == tuple("abcdef")
        assert {row.id for row in meal_roles} == {preset.id for preset in result.meal_role_presets}

        dietary_tags = connection.execute(
            select(DietaryTag.id, DietaryTag.seed_key, DietaryTag.name).where(
                DietaryTag.organization_id == command.organization_id
            )
        ).all()
        assert {(row.seed_key, row.name) for row in dietary_tags} == {
            (seed_key, None) for seed_key in EXPECTED_DIETARY_TAGS
        }
        assert {row.id for row in dietary_tags} == {tag.id for tag in result.dietary_tags}

        mutation = connection.execute(
            select(
                Mutation.organization_id,
                Mutation.is_system_administration_scope,
                Mutation.actor_user_id,
                Mutation.actor_role,
                Mutation.client_installation_id,
                Mutation.oauth_client_id,
                Mutation.oauth_grant_id,
                Mutation.command_kind,
                Mutation.command_schema_version,
                Mutation.target_identities,
                Mutation.request_hash,
                Mutation.outcome,
            ).where(Mutation.id == command.mutation_id)
        ).one()
        assert mutation.organization_id is None
        assert mutation.is_system_administration_scope is True
        assert mutation.actor_user_id == service_database.actor_id
        assert mutation.actor_role == "system_admin"
        assert mutation.client_installation_id == service_database.installation_id
        assert (mutation.oauth_client_id, mutation.oauth_grant_id) == (
            "mcp-client",
            "mcp-grant",
        )
        assert mutation.command_kind == "organization.create"
        assert mutation.command_schema_version == 1
        assert mutation.target_identities == [
            {"entity_kind": "organization", "entity_id": str(command.organization_id)}
        ]
        assert len(mutation.request_hash) == 32
        assert mutation.outcome == "accepted"


def test_semantic_retry_is_replayed_and_changed_input_is_rejected(
    service_database: ServiceDatabase,
) -> None:
    action_time = datetime.now(UTC)
    command = organization_command(
        name="Cafe\u0301",
        client_wall_time=action_time,
        description="First line\rSecond line",
    )
    first = asyncio.run(
        create_organization(service_database.sessions, context(service_database), command)
    )
    equivalent_retry = organization_command(
        mutation_id=command.mutation_id,
        organization_id=command.organization_id,
        name="  Café ",
        default_currency="czk",
        client_wall_time=action_time,
        description="First line\nSecond line",
    )

    replayed = asyncio.run(
        create_organization(
            service_database.sessions,
            context(service_database),
            equivalent_retry,
        )
    )
    assert replayed.replayed is True
    assert replayed.name == "Café"
    assert replayed.description == "First line\nSecond line"
    assert replayed.organization_id == first.organization_id
    assert replayed.meal_role_presets == first.meal_role_presets
    assert replayed.dietary_tags == first.dietary_tags

    changed_retry = organization_command(
        mutation_id=command.mutation_id,
        organization_id=command.organization_id,
        name="Different organization",
        client_wall_time=action_time,
        description="First line\nSecond line",
    )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            create_organization(
                service_database.sessions,
                context(service_database),
                changed_retry,
            )
        )
    assert error.value.code == "idempotency_mismatch"
    assert error.value.retry_same_identity is False

    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Organization)) == 1
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


def test_retained_outcome_with_invalid_scalar_types_fails_closed(
    service_database: ServiceDatabase,
) -> None:
    command = organization_command()
    asyncio.run(create_organization(service_database.sessions, context(service_database), command))
    with service_database.sync_engine.begin() as connection:
        payload = connection.scalar(
            select(Mutation.outcome_payload).where(Mutation.id == command.mutation_id)
        )
        assert payload is not None
        organization = payload["organization"]
        assert isinstance(organization, dict)
        organization["name"] = 42
        connection.execute(
            update(Mutation)
            .where(Mutation.id == command.mutation_id)
            .values(outcome_payload=payload)
        )

    with pytest.raises(RuntimeError, match="invalid outcome payload"):
        asyncio.run(
            create_organization(service_database.sessions, context(service_database), command)
        )


def test_authorization_is_current_and_failures_are_not_cached(
    service_database: ServiceDatabase,
) -> None:
    command = organization_command()
    now = datetime.now(UTC)
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(SystemRoleAssignment)
            .where(SystemRoleAssignment.id == service_database.assignment_id)
            .values(revoked_at=now, revoked_by_user_id=service_database.actor_id)
        )

    with pytest.raises(ApplicationServiceError) as denied:
        asyncio.run(
            create_organization(service_database.sessions, context(service_database), command)
        )
    assert denied.value.code == "forbidden"
    assert denied.value.retry_same_identity is True
    with service_database.sync_engine.connect() as connection:
        assert connection.get_isolation_level() == "READ COMMITTED"
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 0
        assert connection.scalar(select(func.count()).select_from(Organization)) == 0

    replacement_assignment_id = uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(SystemRoleAssignment).values(
                id=replacement_assignment_id,
                user_id=service_database.actor_id,
                invited_email="admin@example.test",
                role="system_admin",
                granted_by_user_id=service_database.actor_id,
                claimed_at=now,
            )
        )
    asyncio.run(create_organization(service_database.sessions, context(service_database), command))

    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(SystemRoleAssignment)
            .where(SystemRoleAssignment.id == replacement_assignment_id)
            .values(revoked_at=now, revoked_by_user_id=service_database.actor_id)
        )
    with pytest.raises(ApplicationServiceError) as revoked_retry:
        asyncio.run(
            create_organization(service_database.sessions, context(service_database), command)
        )
    assert revoked_retry.value.code == "forbidden"
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


def test_disabled_actor_or_installation_is_forbidden_without_cached_mutation(
    service_database: ServiceDatabase,
) -> None:
    now = datetime.now(UTC)
    disabled_installation_command = organization_command()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(ClientInstallation)
            .where(ClientInstallation.id == service_database.installation_id)
            .values(disabled_at=now, disabled_by_user_id=service_database.actor_id)
        )
    with pytest.raises(ApplicationServiceError) as disabled_installation:
        asyncio.run(
            create_organization(
                service_database.sessions,
                context(service_database),
                disabled_installation_command,
            )
        )
    assert disabled_installation.value.code == "forbidden"

    disabled_actor_command = organization_command()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(ClientInstallation)
            .where(ClientInstallation.id == service_database.installation_id)
            .values(disabled_at=None, disabled_by_user_id=None)
        )
        connection.execute(
            update(User)
            .where(User.id == service_database.actor_id)
            .values(disabled_at=now, disabled_by_user_id=service_database.actor_id)
        )
    with pytest.raises(ApplicationServiceError) as disabled_actor:
        asyncio.run(
            create_organization(
                service_database.sessions,
                context(service_database),
                disabled_actor_command,
            )
        )
    assert disabled_actor.value.code == "forbidden"

    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 0
        assert connection.scalar(select(func.count()).select_from(Organization)) == 0


def test_installation_must_belong_to_actor_and_match_authentication_channel(
    service_database: ServiceDatabase,
) -> None:
    other_user_id = uuid4()
    foreign_agent_id = uuid4()
    browser_id = uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=other_user_id,
                display_name="Other user",
                verified_email="other@example.test",
                normalized_email="other@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation),
            [
                {
                    "id": foreign_agent_id,
                    "user_id": other_user_id,
                    "installation_kind": "agent",
                },
                {
                    "id": browser_id,
                    "user_id": service_database.actor_id,
                    "installation_kind": "browser",
                },
            ],
        )

    contexts = (
        ExecutionContext(
            actor_user_id=service_database.actor_id,
            client_installation_id=foreign_agent_id,
            oauth_client_id="mcp-client",
            oauth_grant_id="mcp-grant",
        ),
        ExecutionContext(
            actor_user_id=service_database.actor_id,
            client_installation_id=browser_id,
            oauth_client_id="mcp-client",
            oauth_grant_id="mcp-grant",
        ),
        ExecutionContext(
            actor_user_id=service_database.actor_id,
            client_installation_id=service_database.installation_id,
        ),
    )
    for execution_context in contexts:
        with pytest.raises(ApplicationServiceError) as error:
            asyncio.run(
                create_organization(
                    service_database.sessions,
                    execution_context,
                    organization_command(),
                )
            )
        assert error.value.code == "forbidden"

    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 0


@pytest.mark.parametrize("membership_role", ["member", "organization_admin"])
def test_organization_roles_cannot_create_organizations(
    service_database: ServiceDatabase,
    membership_role: str,
) -> None:
    from cookops.persistence.models import OrganizationMembership

    member_id = uuid4()
    installation_id = uuid4()
    existing_organization_id = uuid4()
    now = datetime.now(UTC)
    normalized_email = f"{membership_role}@example.test"
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=member_id,
                display_name=membership_role,
                verified_email=normalized_email,
                normalized_email=normalized_email,
            )
        )
        connection.execute(
            insert(Organization).values(
                id=existing_organization_id,
                name="Existing organization",
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=existing_organization_id,
                user_id=member_id,
                invited_email=normalized_email,
                role=membership_role,
                state="active",
                invited_by_user_id=service_database.actor_id,
                claimed_at=now,
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id,
                user_id=member_id,
                installation_kind="browser",
            )
        )

    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            create_organization(
                service_database.sessions,
                ExecutionContext(member_id, installation_id),
                organization_command(),
            )
        )
    assert error.value.code == "forbidden"
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 0


def test_domain_and_mutation_writes_roll_back_together(
    service_database: ServiceDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cookops.application import organizations

    command = organization_command()
    monkeypatch.setattr(
        organizations,
        "MEAL_ROLE_PRESETS",
        (("meal_role.breakfast", "a"), ("meal_role.breakfast", "b")),
    )

    with pytest.raises(IntegrityError):
        asyncio.run(
            create_organization(service_database.sessions, context(service_database), command)
        )

    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Organization)) == 0
        assert connection.scalar(select(func.count()).select_from(OrganizationMealRolePreset)) == 0
        assert connection.scalar(select(func.count()).select_from(DietaryTag)) == 0
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 0


def test_concurrent_same_mutation_creates_one_organization(
    service_database: ServiceDatabase,
) -> None:
    command = organization_command()

    async def run_concurrently() -> list[bool]:
        results = await asyncio.gather(
            *(
                create_organization(service_database.sessions, context(service_database), command)
                for _ in range(8)
            )
        )
        return [result.replayed for result in results]

    replay_flags = asyncio.run(run_concurrently())
    assert replay_flags.count(False) == 1
    assert replay_flags.count(True) == 7

    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Organization)) == 1
        assert connection.scalar(select(func.count()).select_from(OrganizationMealRolePreset)) == 6
        assert connection.scalar(select(func.count()).select_from(DietaryTag)) == 4
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


def test_concurrent_same_mutation_with_different_payload_rejects_one_as_mismatch(
    service_database: ServiceDatabase,
) -> None:
    mutation_id = uuid4()
    action_time = datetime.now(UTC)
    commands = (
        organization_command(
            mutation_id=mutation_id,
            name="First payload",
            client_wall_time=action_time,
        ),
        organization_command(
            mutation_id=mutation_id,
            name="Second payload",
            client_wall_time=action_time,
        ),
    )

    async def race() -> list[object]:
        return await asyncio.gather(
            *(
                create_organization(
                    service_database.sessions,
                    context(service_database),
                    command,
                )
                for command in commands
            ),
            return_exceptions=True,
        )

    outcomes = asyncio.run(race())
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    errors = [outcome for outcome in outcomes if isinstance(outcome, ApplicationServiceError)]
    assert len(errors) == 1
    assert errors[0].code == "idempotency_mismatch"
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Organization)) == 1
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


def test_concurrent_duplicate_organization_identity_is_a_retained_rejection(
    service_database: ServiceDatabase,
) -> None:
    organization_id = uuid4()
    commands = (
        organization_command(organization_id=organization_id, name="First command"),
        organization_command(organization_id=organization_id, name="Second command"),
    )

    async def race() -> list[object]:
        return await asyncio.gather(
            *(
                create_organization(
                    service_database.sessions,
                    context(service_database),
                    command,
                )
                for command in commands
            ),
            return_exceptions=True,
        )

    outcomes = asyncio.run(race())
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    errors = [outcome for outcome in outcomes if isinstance(outcome, ApplicationServiceError)]
    assert len(errors) == 1
    assert errors[0].code == "validation_failed"
    assert errors[0].field_violations[0].path == "organization_id"
    rejected_command = next(
        command
        for command, outcome in zip(commands, outcomes, strict=True)
        if isinstance(outcome, ApplicationServiceError)
    )
    with pytest.raises(ApplicationServiceError) as retained:
        asyncio.run(
            create_organization(
                service_database.sessions,
                context(service_database),
                rejected_command,
            )
        )
    assert retained.value.field_violations[0].code == "already_exists"
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Organization)) == 1
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 2


@pytest.mark.parametrize(
    ("name", "currency", "wall_time", "expected_path"),
    [
        ("   ", "CZK", datetime.now(UTC), "name"),
        ("Valid", "ZZZ", datetime.now(UTC), "default_currency"),
        ("Valid", "CZK", datetime.now(), "client_wall_time"),
    ],
)
def test_invalid_input_is_rejected_before_writes(
    service_database: ServiceDatabase,
    name: str,
    currency: str,
    wall_time: datetime,
    expected_path: str,
) -> None:
    command = organization_command(name=name, default_currency=currency, client_wall_time=wall_time)
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            create_organization(service_database.sessions, context(service_database), command)
        )
    assert error.value.code == "validation_failed"
    assert expected_path in {violation.path for violation in error.value.field_violations}
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1
        assert connection.scalar(select(func.count()).select_from(Organization)) == 0

    with pytest.raises(ApplicationServiceError) as retained:
        asyncio.run(
            create_organization(service_database.sessions, context(service_database), command)
        )
    assert retained.value.code == "validation_failed"

    changed_command = organization_command(
        mutation_id=command.mutation_id,
        organization_id=command.organization_id,
        name="Changed",
        default_currency=currency,
        client_wall_time=wall_time,
    )
    with pytest.raises(ApplicationServiceError) as mismatch:
        asyncio.run(
            create_organization(
                service_database.sessions,
                context(service_database),
                changed_command,
            )
        )
    assert mismatch.value.code == "idempotency_mismatch"
