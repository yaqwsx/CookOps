import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError

from alembic import command
from cookops.persistence.models import (
    DietaryTag,
    Organization,
    OrganizationMealRolePreset,
    RecipeTag,
    StoreSection,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

CONFIGURATION_TABLES = {
    "dietary_tags",
    "organization_meal_role_presets",
    "recipe_tags",
    "store_sections",
}
SEED_KEYS = ("vegetarian", "vegan", "gluten", "lactose")


@dataclass
class MigrationDatabase:
    configuration: Config
    engine: Engine


@pytest.fixture
def migration_database() -> Iterator[MigrationDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    command.downgrade(configuration, "base")
    engine = create_engine(database_url)
    try:
        yield MigrationDatabase(configuration, engine)
    finally:
        engine.dispose()
        command.downgrade(configuration, "base")


def seed_organization(engine: Engine) -> tuple[UUID, UUID, UUID]:
    actor_id = uuid4()
    organization_id = uuid4()
    other_organization_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Configuration editor",
                verified_email="editor@example.test",
                normalized_email="editor@example.test",
            )
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": organization_id,
                    "name": "Primary organization",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_organization_id,
                    "name": "Other organization",
                    "created_by_user_id": actor_id,
                },
            ],
        )
    return actor_id, organization_id, other_organization_id


def test_migration_upgrades_empty_database_and_matches_orm_metadata(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine

    command.upgrade(configuration, "head")

    table_names = set(inspect(engine).get_table_names())
    assert table_names >= CONFIGURATION_TABLES
    command.check(configuration)

    command.downgrade(configuration, "0002_identity_organizations")
    assert CONFIGURATION_TABLES.isdisjoint(inspect(engine).get_table_names())


def test_migration_upgrades_previous_schema_and_preserves_it_on_downgrade(
    migration_database: MigrationDatabase,
) -> None:
    configuration = migration_database.configuration
    engine = migration_database.engine
    command.upgrade(configuration, "0002_identity_organizations")
    actor_id, organization_id, _ = seed_organization(engine)

    command.upgrade(configuration, "head")
    with engine.begin() as connection:
        connection.execute(
            insert(StoreSection).values(
                organization_id=organization_id,
                name="Produce",
                normalized_name="produce",
                position_key="a",
                created_by_user_id=actor_id,
            )
        )

    command.downgrade(configuration, "0002_identity_organizations")

    assert CONFIGURATION_TABLES.isdisjoint(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(select(Organization.id).where(Organization.id == organization_id))


def test_configuration_constraints_lifecycle_and_localized_seed_identity(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    engine = migration_database.engine
    actor_id, organization_id, other_organization_id = seed_organization(engine)
    now = datetime.now(UTC)
    recipe_tag_id = uuid4()

    with engine.begin() as connection:
        connection.execute(
            insert(StoreSection),
            [
                {
                    "organization_id": organization_id,
                    "name": "Produce",
                    "normalized_name": "produce",
                    "position_key": "a",
                    "created_by_user_id": actor_id,
                },
                {
                    "organization_id": organization_id,
                    "name": "Bakery",
                    "normalized_name": "bakery",
                    "position_key": "a",
                    "created_by_user_id": actor_id,
                },
                {
                    "organization_id": other_organization_id,
                    "name": "Produce",
                    "normalized_name": "produce",
                    "position_key": "a",
                    "created_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(
            insert(OrganizationMealRolePreset).values(
                organization_id=organization_id,
                built_in_translation_key="meal_role.breakfast",
                position_key="a",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(OrganizationMealRolePreset).values(
                organization_id=organization_id,
                custom_name="Second breakfast",
                normalized_custom_name="second breakfast",
                position_key="b",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(RecipeTag).values(
                id=recipe_tag_id,
                organization_id=organization_id,
                name="Quick",
                normalized_name="quick",
                color="#12aBcD",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(DietaryTag),
            [
                {
                    "organization_id": organization_id,
                    "seed_key": seed_key,
                    "created_by_user_id": actor_id,
                }
                for seed_key in SEED_KEYS
            ],
        )
        connection.execute(
            insert(DietaryTag).values(
                organization_id=organization_id,
                name="Nut allergy",
                normalized_name="nut allergy",
                color="#CC8800",
                created_by_user_id=actor_id,
            )
        )

        seed_rows = connection.execute(
            select(DietaryTag.seed_key, DietaryTag.name).where(
                DietaryTag.organization_id == organization_id,
                DietaryTag.seed_key.is_not(None),
            )
        ).all()
        assert set(seed_rows) == {(seed_key, None) for seed_key in SEED_KEYS}

        connection.execute(
            update(DietaryTag)
            .where(
                DietaryTag.organization_id == organization_id,
                DietaryTag.seed_key == "vegan",
            )
            .values(name="Plant-based", normalized_name="plant-based")
        )
        renamed_seed = connection.execute(
            select(DietaryTag.seed_key, DietaryTag.name).where(
                DietaryTag.organization_id == organization_id,
                DietaryTag.seed_key == "vegan",
            )
        ).one()
        assert renamed_seed == ("vegan", "Plant-based")

        invalid_statements = [
            insert(StoreSection).values(
                organization_id=organization_id,
                name=" ",
                normalized_name="",
                position_key="b",
                created_by_user_id=actor_id,
            ),
            insert(StoreSection).values(
                organization_id=organization_id,
                name="Produce",
                normalized_name="PRODUCE",
                position_key="b",
                created_by_user_id=actor_id,
            ),
            insert(StoreSection).values(
                organization_id=organization_id,
                name="Frozen",
                normalized_name="frozen",
                position_key="č",
                created_by_user_id=actor_id,
            ),
            insert(StoreSection).values(
                organization_id=organization_id,
                name="Retirement without actor",
                normalized_name="retirement without actor",
                position_key="c",
                created_by_user_id=actor_id,
                retired_at=now,
            ),
            insert(StoreSection).values(
                organization_id=organization_id,
                name="PRODUCE",
                normalized_name="produce",
                position_key="c",
                created_by_user_id=actor_id,
                retired_at=now,
                retired_by_user_id=actor_id,
            ),
            insert(OrganizationMealRolePreset).values(
                organization_id=organization_id,
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(OrganizationMealRolePreset).values(
                organization_id=organization_id,
                custom_name="Missing normalization",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(OrganizationMealRolePreset).values(
                organization_id=organization_id,
                built_in_translation_key="meal_role.lunch",
                custom_name="Lunch",
                normalized_custom_name="lunch",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(OrganizationMealRolePreset).values(
                organization_id=organization_id,
                built_in_translation_key="Meal role",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(OrganizationMealRolePreset).values(
                organization_id=organization_id,
                built_in_translation_key="meal_role.breakfast",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(OrganizationMealRolePreset).values(
                organization_id=organization_id,
                custom_name="SECOND BREAKFAST",
                normalized_custom_name="second breakfast",
                position_key="c",
                created_by_user_id=actor_id,
            ),
            insert(RecipeTag).values(
                organization_id=organization_id,
                name="Slow",
                normalized_name="slow",
                color="red",
                created_by_user_id=actor_id,
            ),
            insert(RecipeTag).values(
                organization_id=organization_id,
                name="QUICK",
                normalized_name="quick",
                color="#000000",
                created_by_user_id=actor_id,
            ),
            insert(DietaryTag).values(
                organization_id=organization_id,
                created_by_user_id=actor_id,
            ),
            insert(DietaryTag).values(
                organization_id=organization_id,
                name="Missing normalization",
                created_by_user_id=actor_id,
            ),
            insert(DietaryTag).values(
                organization_id=organization_id,
                seed_key="kosher",
                created_by_user_id=actor_id,
            ),
            insert(DietaryTag).values(
                organization_id=organization_id,
                name="Bad color",
                normalized_name="bad color",
                color="#xyzxyz",
                created_by_user_id=actor_id,
            ),
            insert(DietaryTag).values(
                organization_id=organization_id,
                seed_key="vegan",
                created_by_user_id=actor_id,
            ),
            insert(DietaryTag).values(
                organization_id=organization_id,
                name="NUT ALLERGY",
                normalized_name="nut allergy",
                created_by_user_id=actor_id,
            ),
            insert(StoreSection).values(
                organization_id=uuid4(),
                name="Unknown organization",
                normalized_name="unknown organization",
                position_key="z",
                created_by_user_id=actor_id,
            ),
        ]
        for statement in invalid_statements:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(statement)

        connection.execute(
            update(RecipeTag)
            .where(RecipeTag.id == recipe_tag_id)
            .values(retired_at=now, retired_by_user_id=actor_id)
        )
        connection.execute(
            update(RecipeTag)
            .where(RecipeTag.id == recipe_tag_id)
            .values(retired_at=None, retired_by_user_id=None)
        )
        restored = connection.execute(
            select(RecipeTag.retired_at, RecipeTag.retired_by_user_id).where(
                RecipeTag.id == recipe_tag_id
            )
        ).one()
        assert restored == (None, None)

        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(delete(Organization).where(Organization.id == organization_id))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(delete(User).where(User.id == actor_id))

    rollback_section_id = uuid4()
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            insert(StoreSection).values(
                id=rollback_section_id,
                organization_id=organization_id,
                name="Rolled back",
                normalized_name="rolled back",
                position_key="z",
                created_by_user_id=actor_id,
            )
        )
        transaction.rollback()

    with engine.connect() as connection:
        assert (
            connection.scalar(select(StoreSection.id).where(StoreSection.id == rollback_section_id))
            is None
        )
        created_at = connection.scalar(
            select(StoreSection.created_at).where(
                StoreSection.organization_id == organization_id,
                StoreSection.normalized_name == "produce",
            )
        )
        assert created_at is not None
        assert created_at.tzinfo is not None
