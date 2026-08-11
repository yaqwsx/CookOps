import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command as alembic_command
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
)
from cookops.application.recipe_lifecycle import (
    SetRecipeLifecycleCommand,
    set_recipe_lifecycle,
)
from cookops.application.recipes import (
    CreateRecipeCommand,
    PublishRecipeVersionCommand,
    RecipeIngredientLineInput,
    create_recipe,
    publish_recipe_version,
    recipe_version_tag_change_id,
)
from cookops.persistence.models import (
    ClientInstallation,
    FieldClock,
    Ingredient,
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

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


@dataclass
class ServiceDatabase:
    sync_engine: Engine
    sessions: async_sessionmaker[AsyncSession]
    actor_id: UUID
    installation_id: UUID
    organization_id: UUID
    other_organization_id: UUID
    grams_id: UUID
    person_id: UUID
    ingredient_version_id: UUID
    tag_id: UUID


@pytest.fixture
def service_database() -> Iterator[ServiceDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    sync_engine = create_engine(database_url)
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    actor_id, installation_id, organization_id, other_organization_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    tag_id, ingredient_id, ingredient_version_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    with sync_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Recipe member",
                verified_email="member@example.test",
                normalized_email="member@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id, user_id=actor_id, installation_kind="browser"
            )
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": organization_id,
                    "name": "Kitchen crew",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_organization_id,
                    "name": "Other kitchen crew",
                    "created_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=organization_id,
                user_id=actor_id,
                invited_email="member@example.test",
                role="member",
                state="active",
                invited_by_user_id=actor_id,
                claimed_at=now,
            )
        )
        connection.execute(
            insert(RecipeTag).values(
                id=tag_id,
                organization_id=organization_id,
                name="Vegetarian",
                normalized_name="vegetarian",
                color="#228B22",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(Ingredient).values(
                id=ingredient_id,
                organization_id=organization_id,
                current_version_id=ingredient_version_id,
                created_by_user_id=actor_id,
            )
        )
        grams_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        person_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "person"
            )
        )
        assert grams_id is not None and person_id is not None
        connection.execute(
            insert(IngredientVersion).values(
                id=ingredient_version_id,
                organization_id=organization_id,
                ingredient_id=ingredient_id,
                name="Tomatoes",
                normalized_name="tomatoes",
                canonical_unit_id=grams_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            )
        )
    database = ServiceDatabase(
        sync_engine=sync_engine,
        sessions=async_sessionmaker(async_engine, expire_on_commit=False),
        actor_id=actor_id,
        installation_id=installation_id,
        organization_id=organization_id,
        other_organization_id=other_organization_id,
        grams_id=grams_id,
        person_id=person_id,
        ingredient_version_id=ingredient_version_id,
        tag_id=tag_id,
    )
    try:
        yield database
    finally:
        asyncio.run(async_engine.dispose())
        sync_engine.dispose()
        alembic_command.downgrade(configuration, "base")


def context(database: ServiceDatabase) -> ExecutionContext:
    return ExecutionContext(database.actor_id, database.installation_id)


def recipe_command(
    database: ServiceDatabase,
    *,
    mutation_id: UUID | None = None,
    recipe_id: UUID | None = None,
    recipe_version_id: UUID | None = None,
    ingredient_version_id: UUID | None = None,
    name: str = "  Tomato soup  ",
    base_scaling_amount: Decimal = Decimal("10"),
    lines: tuple[RecipeIngredientLineInput, ...] | None = None,
    wall_time: datetime | None = None,
) -> CreateRecipeCommand:
    return CreateRecipeCommand(
        mutation_id=mutation_id or uuid4(),
        recipe_id=recipe_id or uuid4(),
        recipe_version_id=recipe_version_id or uuid4(),
        organization_id=database.organization_id,
        name=name,
        description="Cook slowly\r\n[Recipe](https://example.test/tomato-soup)",
        scaling_unit_id=database.person_id,
        base_scaling_amount=base_scaling_amount,
        estimated_diners_per_scaling_unit=Decimal("1"),
        round_suggestions_up=True,
        recipe_tag_ids=(database.tag_id,),
        ingredient_lines=lines
        or (
            RecipeIngredientLineInput(
                id=uuid4(),
                line_key=uuid4(),
                ingredient_version_id=ingredient_version_id or database.ingredient_version_id,
                base_quantity=Decimal("750.50"),
                preferred_display_unit_id=database.grams_id,
                note="  diced\r\nwell  ",
                position_key="a",
                scaling_behavior="proportional",
                include_in_portion_weight=True,
            ),
            RecipeIngredientLineInput(
                id=uuid4(),
                line_key=uuid4(),
                ingredient_version_id=ingredient_version_id or database.ingredient_version_id,
                base_quantity=Decimal("20"),
                preferred_display_unit_id=database.grams_id,
                note=None,
                position_key="b",
                scaling_behavior="fixed",
                include_in_portion_weight=False,
            ),
        ),
        client_wall_time=wall_time or datetime.now(UTC),
    )


def test_member_creates_immutable_recipe_version_and_change_feed(
    service_database: ServiceDatabase,
) -> None:
    command = recipe_command(service_database, name="  Rajčatová polévka  ")
    result = asyncio.run(
        create_recipe(service_database.sessions, context(service_database), command)
    )

    assert result.replayed is False
    assert result.name == "Rajčatová polévka"
    assert result.description == "Cook slowly\n[Recipe](https://example.test/tomato-soup)"
    assert result.recipe_tag_ids == (service_database.tag_id,)
    assert (result.first_change_sequence, result.last_change_sequence) == (1, 5)
    assert [line.scaling_behavior for line in result.ingredient_lines] == ["proportional", "fixed"]
    assert result.ingredient_lines[1].include_in_portion_weight is False

    with service_database.sync_engine.connect() as connection:
        recipe = connection.execute(
            select(
                Recipe.organization_id, Recipe.current_version_id, Recipe.created_by_user_id
            ).where(Recipe.id == command.recipe_id)
        ).one()
        assert recipe == (
            service_database.organization_id,
            command.recipe_version_id,
            service_database.actor_id,
        )
        version = connection.execute(
            select(
                RecipeVersion.name,
                RecipeVersion.description,
                RecipeVersion.scaling_model,
                RecipeVersion.base_scaling_amount,
                RecipeVersion.round_suggestions_up,
            ).where(RecipeVersion.id == command.recipe_version_id)
        ).one()
        assert version == (
            "Rajčatová polévka",
            "Cook slowly\n[Recipe](https://example.test/tomato-soup)",
            "single_variable",
            Decimal("10"),
            True,
        )
        lines = connection.execute(
            select(
                RecipeVersionIngredientLine.base_quantity,
                RecipeVersionIngredientLine.note,
                RecipeVersionIngredientLine.scaling_behavior,
                RecipeVersionIngredientLine.include_in_portion_weight,
            )
            .where(RecipeVersionIngredientLine.recipe_version_id == command.recipe_version_id)
            .order_by(RecipeVersionIngredientLine.position_key)
        ).all()
        assert list(lines) == [
            (Decimal("750.50"), "  diced\nwell  ", "proportional", True),
            (Decimal("20"), None, "fixed", False),
        ]
        assert (
            connection.scalar(
                select(func.count())
                .select_from(RecipeVersionTag)
                .where(RecipeVersionTag.recipe_version_id == command.recipe_version_id)
            )
            == 1
        )
        mutation = connection.execute(
            select(
                Mutation.actor_role,
                Mutation.outcome,
                Mutation.first_change_sequence,
                Mutation.last_change_sequence,
            ).where(Mutation.id == command.mutation_id)
        ).one()
        assert mutation == ("member", "accepted", 1, 5)
        changes = connection.execute(
            select(
                OrganizationChange.entity_kind,
                OrganizationChange.entity_id,
                OrganizationChange.payload,
            )
            .where(OrganizationChange.organization_id == service_database.organization_id)
            .order_by(OrganizationChange.sequence)
        ).all()
        assert [row.entity_kind for row in changes] == [
            "recipe",
            "recipe_version",
            "recipe_version_tag",
            "recipe_ingredient_line",
            "recipe_ingredient_line",
        ]
        tag_change_id = recipe_version_tag_change_id(
            command.recipe_version_id, service_database.tag_id
        )
        assert changes[2].entity_id == tag_change_id
        assert changes[2].payload["record"]["id"] == str(tag_change_id)
        assert tag_change_id != recipe_version_tag_change_id(uuid4(), service_database.tag_id)
        assert changes[4].payload["record"]["include_in_portion_weight"] is False


def test_member_retires_and_restores_recipe_without_mutating_immutable_version(
    service_database: ServiceDatabase,
) -> None:
    created = asyncio.run(
        create_recipe(
            service_database.sessions, context(service_database), recipe_command(service_database)
        )
    )
    retire = SetRecipeLifecycleCommand(
        mutation_id=uuid4(),
        recipe_id=created.recipe_id,
        organization_id=service_database.organization_id,
        operation="retire",
        client_wall_time=datetime.now(UTC),
    )
    retired = asyncio.run(
        set_recipe_lifecycle(service_database.sessions, context(service_database), retire)
    )
    restore = replace(
        retire, mutation_id=uuid4(), operation="restore", client_wall_time=datetime.now(UTC)
    )
    restored = asyncio.run(
        set_recipe_lifecycle(service_database.sessions, context(service_database), restore)
    )
    assert retired.replayed is False and restored.replayed is False
    with service_database.sync_engine.connect() as connection:
        recipe = connection.execute(
            select(Recipe.current_version_id, Recipe.retired_at).where(
                Recipe.id == created.recipe_id
            )
        ).one()
        assert recipe == (created.recipe_version_id, None)
        assert (
            connection.scalar(
                select(func.count())
                .select_from(RecipeVersion)
                .where(RecipeVersion.id == created.recipe_version_id)
            )
            == 1
        )
        clock = connection.execute(
            select(FieldClock.winning_mutation_id).where(
                FieldClock.entity_kind == "recipe",
                FieldClock.entity_id == created.recipe_id,
                FieldClock.field_name == "lifecycle",
            )
        ).scalar_one()
        assert clock == restore.mutation_id


def test_semantic_retry_replays_and_changed_input_is_rejected(
    service_database: ServiceDatabase,
) -> None:
    action_time = datetime.now(UTC)
    command = recipe_command(service_database, name="Cafe\u0301 soup", wall_time=action_time)
    first = asyncio.run(
        create_recipe(service_database.sessions, context(service_database), command)
    )
    equivalent = replace(
        command,
        name="  Café soup  ",
        description="Cook slowly\n[Recipe](https://example.test/tomato-soup)",
        base_scaling_amount=Decimal("10.0"),
        ingredient_lines=tuple(
            replace(
                line,
                note=line.note.replace("\r\n", "\n") if line.note else None,
                base_quantity=line.base_quantity * Decimal("1.0"),
            )
            for line in command.ingredient_lines
        ),
    )
    replayed = asyncio.run(
        create_recipe(service_database.sessions, context(service_database), equivalent)
    )
    assert replayed.replayed is True
    assert replayed.recipe_id == first.recipe_id
    assert replayed.name == "Café soup"

    changed = replace(command, name="Different soup")
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(create_recipe(service_database.sessions, context(service_database), changed))
    assert error.value.code == "idempotency_mismatch"
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Recipe)) == 1
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


@pytest.mark.parametrize(
    ("base_scaling_amount", "line_quantity", "expected_path"),
    [
        (Decimal("NaN"), Decimal("1"), "base_scaling_amount"),
        (Decimal("0"), Decimal("1"), "base_scaling_amount"),
        (Decimal("1"), Decimal("-0.01"), "ingredient_lines[0].base_quantity"),
        (Decimal("1"), Decimal("Infinity"), "ingredient_lines[0].base_quantity"),
    ],
)
def test_invalid_decimals_are_deterministically_rejected(
    service_database: ServiceDatabase,
    base_scaling_amount: Decimal,
    line_quantity: Decimal,
    expected_path: str,
) -> None:
    original = recipe_command(service_database)
    command = replace(
        original,
        base_scaling_amount=base_scaling_amount,
        ingredient_lines=(replace(original.ingredient_lines[0], base_quantity=line_quantity),),
    )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(create_recipe(service_database.sessions, context(service_database), command))
    assert error.value.code == "validation_failed"
    assert any(violation.path == expected_path for violation in error.value.field_violations)
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == command.mutation_id))
            == "rejected"
        )
        assert connection.scalar(select(func.count()).select_from(Recipe)) == 0


def test_cross_organization_ingredient_version_is_rejected(
    service_database: ServiceDatabase,
) -> None:
    other_ingredient_id, other_version_id = uuid4(), uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(Ingredient).values(
                id=other_ingredient_id,
                organization_id=service_database.other_organization_id,
                current_version_id=other_version_id,
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=other_version_id,
                organization_id=service_database.other_organization_id,
                ingredient_id=other_ingredient_id,
                name="Other tomatoes",
                normalized_name="other tomatoes",
                canonical_unit_id=service_database.grams_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=service_database.actor_id,
            )
        )
    command = recipe_command(service_database, ingredient_version_id=other_version_id)
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(create_recipe(service_database.sessions, context(service_database), command))
    assert error.value.code == "validation_failed"
    assert error.value.field_violations == (error.value.field_violations[0],)
    assert error.value.field_violations[0].path == "catalog_references"


def test_system_administrator_can_publish_without_membership(
    service_database: ServiceDatabase,
) -> None:
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == service_database.organization_id,
                OrganizationMembership.user_id == service_database.actor_id,
            )
            .values(
                state="removed",
                removed_at=datetime.now(UTC),
                removed_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            insert(SystemRoleAssignment).values(
                user_id=service_database.actor_id,
                invited_email="member@example.test",
                role="system_admin",
                granted_by_user_id=service_database.actor_id,
                claimed_at=datetime.now(UTC),
            )
        )
    result = asyncio.run(
        create_recipe(
            service_database.sessions, context(service_database), recipe_command(service_database)
        )
    )
    assert result.replayed is False
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.actor_role).where(Mutation.id == result.mutation_id))
            == "system_admin"
        )


def test_concurrent_creates_for_one_recipe_identity_admit_exactly_one(
    service_database: ServiceDatabase,
) -> None:
    recipe_id = uuid4()
    first = recipe_command(service_database, recipe_id=recipe_id)
    second = recipe_command(service_database, recipe_id=recipe_id)

    async def race() -> tuple[object, object]:
        return await asyncio.gather(
            create_recipe(service_database.sessions, context(service_database), first),
            create_recipe(service_database.sessions, context(service_database), second),
            return_exceptions=True,
        )

    outcomes = asyncio.run(race())
    accepted = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, ApplicationServiceError)]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0].code == "validation_failed"
    assert rejected[0].field_violations == (FieldViolation("recipe_id", "already_exists"),)
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(Recipe).where(Recipe.id == recipe_id)
            )
            == 1
        )


def test_publish_recipe_version_advances_only_the_current_pointer(
    service_database: ServiceDatabase,
) -> None:
    created = asyncio.run(
        create_recipe(
            service_database.sessions, context(service_database), recipe_command(service_database)
        )
    )
    published = asyncio.run(
        publish_recipe_version(
            service_database.sessions,
            context(service_database),
            PublishRecipeVersionCommand(
                mutation_id=uuid4(),
                recipe_id=created.recipe_id,
                recipe_version_id=uuid4(),
                based_on_version_id=created.recipe_version_id,
                organization_id=service_database.organization_id,
                name="Updated tomato soup",
                scaling_unit_id=service_database.person_id,
                base_scaling_amount=Decimal("12"),
                client_wall_time=datetime.now(UTC),
                ingredient_lines=(),
            ),
        )
    )
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(Recipe.current_version_id).where(Recipe.id == created.recipe_id)
            )
            == published.recipe_version_id
        )
        assert (
            connection.scalar(
                select(RecipeVersion.based_on_version_id).where(
                    RecipeVersion.id == published.recipe_version_id
                )
            )
            == created.recipe_version_id
        )


def test_publish_preserves_recipe_root_author_for_a_different_member(
    service_database: ServiceDatabase,
) -> None:
    created = asyncio.run(
        create_recipe(
            service_database.sessions, context(service_database), recipe_command(service_database)
        )
    )
    publisher_id, installation_id = uuid4(), uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=publisher_id,
                display_name="Second recipe member",
                verified_email="second@example.test",
                normalized_email="second@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id, user_id=publisher_id, installation_kind="browser"
            )
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=service_database.organization_id,
                user_id=publisher_id,
                invited_email="second@example.test",
                role="member",
                state="active",
                invited_by_user_id=service_database.actor_id,
                claimed_at=datetime.now(UTC),
            )
        )
    result = asyncio.run(
        publish_recipe_version(
            service_database.sessions,
            ExecutionContext(publisher_id, installation_id),
            PublishRecipeVersionCommand(
                mutation_id=uuid4(),
                recipe_id=created.recipe_id,
                recipe_version_id=uuid4(),
                based_on_version_id=created.recipe_version_id,
                organization_id=service_database.organization_id,
                name="Second member version",
                scaling_unit_id=service_database.person_id,
                base_scaling_amount=Decimal("12"),
                client_wall_time=datetime.now(UTC),
                ingredient_lines=(),
            ),
        )
    )
    with service_database.sync_engine.connect() as connection:
        record = connection.scalar(
            select(OrganizationChange.payload).where(
                OrganizationChange.mutation_id == result.mutation_id,
                OrganizationChange.entity_kind == "recipe",
            )
        )
    assert record is not None
    assert record["record"]["created_by_user_id"] == str(service_database.actor_id)
