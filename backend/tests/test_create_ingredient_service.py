import asyncio
import os
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from multiprocessing import get_context
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command as alembic_command
from cookops.application.ingredient_lifecycle import (
    SetIngredientLifecycleCommand,
    set_ingredient_lifecycle,
)
from cookops.application.ingredient_prices import (
    PublishIngredientPriceEstimateCommand,
    publish_ingredient_price_estimate,
)
from cookops.application.ingredient_versions import (
    PublishIngredientVersionCommand,
    publish_ingredient_version,
)
from cookops.application.ingredients import (
    CreateIngredientCommand,
    InitialPrice,
    create_ingredient,
)
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.persistence.models import (
    ClientInstallation,
    DietaryTag,
    FieldClock,
    Ingredient,
    IngredientPriceEstimate,
    IngredientVersion,
    IngredientVersionDietaryTag,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    StoreSection,
    UnitDefinition,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


@dataclass
class Database:
    sync: Engine
    sessions: async_sessionmaker[AsyncSession]
    actor_id: UUID
    installation_id: UUID
    organization_id: UUID
    gram_id: UUID
    kilogram_id: UUID
    section_id: UUID
    tag_id: UUID


@pytest.fixture
def database() -> Iterator[Database]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    sync = create_engine(database_url)
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    actor_id, installation_id, organization_id, section_id, tag_id = (uuid4() for _ in range(5))
    now = datetime.now(UTC)
    with sync.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Member",
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
            insert(Organization).values(
                id=organization_id,
                name="Kitchen",
                default_currency="CZK",
                created_by_user_id=actor_id,
            )
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
            insert(StoreSection).values(
                id=section_id,
                organization_id=organization_id,
                name="Produce",
                normalized_name="produce",
                position_key="a",
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=tag_id,
                organization_id=organization_id,
                name="Vegan",
                normalized_name="vegan",
                created_by_user_id=actor_id,
            )
        )
        gram_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        kilogram_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "kg"
            )
        )
    assert gram_id is not None and kilogram_id is not None
    result = Database(
        sync,
        async_sessionmaker(async_engine, expire_on_commit=False),
        actor_id,
        installation_id,
        organization_id,
        gram_id,
        kilogram_id,
        section_id,
        tag_id,
    )
    try:
        yield result
    finally:
        asyncio.run(async_engine.dispose())
        sync.dispose()
        alembic_command.downgrade(configuration, "base")


def context(database: Database) -> ExecutionContext:
    return ExecutionContext(database.actor_id, database.installation_id)


def publish_command(database: Database, **changes: object) -> PublishIngredientVersionCommand:
    values: dict[str, object] = {
        "mutation_id": uuid4(),
        "ingredient_id": uuid4(),
        "based_on_version_id": uuid4(),
        "ingredient_version_id": uuid4(),
        "organization_id": database.organization_id,
        "name": "Tomatoes",
        "canonical_unit_id": database.gram_id,
        "mass_per_canonical_quantity": Decimal("1"),
        "client_wall_time": datetime.now(UTC),
        "dietary_tag_ids": (),
    }
    values.update(changes)
    return PublishIngredientVersionCommand(**values)  # type: ignore[arg-type]


def command(database: Database, **changes: object) -> CreateIngredientCommand:
    values: dict[str, object] = {
        "mutation_id": uuid4(),
        "ingredient_id": uuid4(),
        "ingredient_version_id": uuid4(),
        "organization_id": database.organization_id,
        "name": "  Tomatoes ",
        "canonical_unit_id": database.gram_id,
        "mass_per_canonical_quantity": Decimal("1"),
        "client_wall_time": datetime.now(UTC),
        "dietary_tag_ids": (database.tag_id,),
        "default_store_section_id": database.section_id,
        "initial_price": InitialPrice(
            uuid4(), Decimal("35"), Decimal("1"), database.kilogram_id, "czk"
        ),
    }
    values.update(changes)
    return CreateIngredientCommand(**values)  # type: ignore[arg-type]


def price_command(
    database: Database, ingredient_id: UUID, **changes: object
) -> PublishIngredientPriceEstimateCommand:
    values: dict[str, object] = {
        "mutation_id": uuid4(),
        "ingredient_id": ingredient_id,
        "ingredient_price_estimate_id": uuid4(),
        "organization_id": database.organization_id,
        "amount": Decimal("12.50"),
        "priced_quantity": Decimal("1"),
        "unit_id": database.kilogram_id,
        "currency": "CZK",
        "client_wall_time": datetime.now(UTC),
    }
    values.update(changes)
    return PublishIngredientPriceEstimateCommand(**values)  # type: ignore[arg-type]


def _price_seed(database: Database) -> tuple[UUID, CreateIngredientCommand]:
    initial = command(database, initial_price=None, name=f"Tomatoes {uuid4().hex[:8]}")
    asyncio.run(create_ingredient(database.sessions, context(database), initial))
    return initial.ingredient_id, initial


def test_publish_ingredient_price_estimate_accepts_replays_and_rejects_invalid_values(
    database: Database,
) -> None:
    ingredient_id, _ = _price_seed(database)
    attempted = price_command(database, ingredient_id)
    accepted = asyncio.run(
        publish_ingredient_price_estimate(database.sessions, context(database), attempted)
    )
    replay = asyncio.run(
        publish_ingredient_price_estimate(database.sessions, context(database), attempted)
    )
    assert accepted.replayed is False and replay.replayed is True
    assert accepted.ingredient_price_estimate_id == replay.ingredient_price_estimate_id
    free = asyncio.run(
        publish_ingredient_price_estimate(
            database.sessions,
            context(database),
            price_command(database, ingredient_id, amount=Decimal("0")),
        )
    )
    assert free.outcome == "accepted"
    lww_ingredient_id, _ = _price_seed(database)
    older = price_command(
        database,
        lww_ingredient_id,
        client_wall_time=datetime.now(UTC) + timedelta(seconds=1),
    )
    newer = price_command(
        database,
        lww_ingredient_id,
        client_wall_time=datetime.now(UTC) + timedelta(seconds=2),
    )
    second = asyncio.run(
        publish_ingredient_price_estimate(database.sessions, context(database), newer)
    )
    first = asyncio.run(
        publish_ingredient_price_estimate(database.sessions, context(database), older)
    )
    retry = asyncio.run(
        publish_ingredient_price_estimate(database.sessions, context(database), older)
    )
    assert first.outcome == "partially_superseded"
    assert second.outcome == "accepted"
    assert retry.outcome == "partially_superseded" and retry.replayed
    with database.sync.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(IngredientPriceEstimate)
                .where(IngredientPriceEstimate.ingredient_id == ingredient_id)
            )
            == 2
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(IngredientPriceEstimate)
                .where(IngredientPriceEstimate.ingredient_id == lww_ingredient_id)
            )
            == 2
        )
    invalid = price_command(database, ingredient_id, amount=Decimal("1e101"))
    with pytest.raises(ApplicationServiceError) as first_error:
        asyncio.run(
            publish_ingredient_price_estimate(database.sessions, context(database), invalid)
        )
    with pytest.raises(ApplicationServiceError) as replay_error:
        asyncio.run(
            publish_ingredient_price_estimate(database.sessions, context(database), invalid)
        )
    assert replay_error.value.code == first_error.value.code
    assert replay_error.value.field_violations == first_error.value.field_violations
    retired_id, _ = _price_seed(database)
    lifecycle = SetIngredientLifecycleCommand(
        mutation_id=uuid4(),
        ingredient_id=retired_id,
        organization_id=database.organization_id,
        operation="retire",
        client_wall_time=datetime.now(UTC),
    )
    asyncio.run(set_ingredient_lifecycle(database.sessions, context(database), lifecycle))
    stale = price_command(database, retired_id)
    with pytest.raises(ApplicationServiceError) as stale_first:
        asyncio.run(publish_ingredient_price_estimate(database.sessions, context(database), stale))
    with pytest.raises(ApplicationServiceError) as stale_replay:
        asyncio.run(publish_ingredient_price_estimate(database.sessions, context(database), stale))
    assert stale_first.value.code == stale_replay.value.code == "stale_precondition"
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(
            publish_ingredient_price_estimate(
                database.sessions,
                context(database),
                price_command(database, ingredient_id, currency="EUR"),
            )
        )
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(
            publish_ingredient_price_estimate(
                database.sessions,
                context(database),
                price_command(database, ingredient_id, unit_id=uuid4()),
            )
        )


def _create_ingredient_in_process(
    database_url: str, context: ExecutionContext, attempted: CreateIngredientCommand
) -> tuple[str, str]:
    """Run one command with a process- and loop-local SQLAlchemy runtime."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        result = asyncio.run(create_ingredient(sessions, context, attempted))
        return "accepted", str(result.ingredient_id)
    except ApplicationServiceError as error:
        return error.code, ""
    finally:
        asyncio.run(engine.dispose())


def test_member_publishes_atomic_ingredient_version_price_and_change_feed(
    database: Database,
) -> None:
    created = asyncio.run(
        create_ingredient(database.sessions, context(database), command(database))
    )
    assert created.name == "Tomatoes"
    assert created.normalized_name == "tomatoes"
    assert created.initial_price_id is not None
    assert created.first_change_sequence == 1
    assert created.last_change_sequence == 3
    with database.sync.connect() as connection:
        assert (
            connection.scalar(
                select(Ingredient.current_version_id).where(Ingredient.id == created.ingredient_id)
            )
            == created.ingredient_version_id
        )
        assert (
            connection.scalar(
                select(Ingredient.current_price_estimate_id).where(
                    Ingredient.id == created.ingredient_id
                )
            )
            == created.initial_price_id
        )
        assert connection.scalar(
            select(IngredientVersion.mass_per_canonical_quantity).where(
                IngredientVersion.id == created.ingredient_version_id
            )
        ) == Decimal("1")
        assert connection.scalar(select(func.count()).select_from(IngredientVersionDietaryTag)) == 1
        assert (
            connection.scalar(
                select(IngredientPriceEstimate.currency).where(
                    IngredientPriceEstimate.id == created.initial_price_id
                )
            )
            == "CZK"
        )
        changes = (
            connection.execute(
                select(OrganizationChange.entity_kind).order_by(OrganizationChange.sequence)
            )
            .scalars()
            .all()
        )
        assert changes == ["ingredient", "ingredient_version", "ingredient_price_estimate"]


def test_member_retires_and_restores_ingredient_root_without_mutating_version(
    database: Database,
) -> None:
    created = asyncio.run(
        create_ingredient(database.sessions, context(database), command(database))
    )
    version_before = created.ingredient_version_id
    retired = SetIngredientLifecycleCommand(
        mutation_id=uuid4(),
        ingredient_id=created.ingredient_id,
        organization_id=database.organization_id,
        operation="retire",
        client_wall_time=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )
    first = asyncio.run(set_ingredient_lifecycle(database.sessions, context(database), retired))
    replay = asyncio.run(set_ingredient_lifecycle(database.sessions, context(database), retired))
    restored = SetIngredientLifecycleCommand(
        mutation_id=uuid4(),
        ingredient_id=created.ingredient_id,
        organization_id=database.organization_id,
        operation="restore",
        client_wall_time=datetime(2026, 8, 10, 12, 0, 0, 1, tzinfo=UTC),
    )
    second = asyncio.run(set_ingredient_lifecycle(database.sessions, context(database), restored))
    with database.sync.connect() as connection:
        assert (
            connection.scalar(
                select(Ingredient.retired_at).where(Ingredient.id == created.ingredient_id)
            )
            is None
        )
        assert (
            connection.scalar(
                select(Ingredient.current_version_id).where(Ingredient.id == created.ingredient_id)
            )
            == version_before
        )
        assert (
            connection.scalar(
                select(FieldClock.winning_mutation_id).where(
                    FieldClock.entity_kind == "ingredient",
                    FieldClock.entity_id == created.ingredient_id,
                    FieldClock.field_name == "lifecycle",
                )
            )
            == restored.mutation_id
        )
        records = (
            connection.execute(
                select(OrganizationChange.payload)
                .where(OrganizationChange.entity_kind == "ingredient")
                .order_by(OrganizationChange.sequence)
            )
            .scalars()
            .all()
        )
        assert cast(dict[str, str], cast(dict[str, object], records[-2])["record"])[
            "lifecycle"
        ] == "retired"
        assert cast(dict[str, str], cast(dict[str, object], records[-1])["record"])[
            "lifecycle"
        ] == "active"
    assert first.replayed is False
    assert replay.replayed is True
    assert second.outcome == "accepted"


def test_member_may_publish_an_ingredient_without_an_initial_price(database: Database) -> None:
    created = asyncio.run(
        create_ingredient(
            database.sessions, context(database), command(database, initial_price=None)
        )
    )
    assert created.initial_price_id is None
    assert created.first_change_sequence == 1
    assert created.last_change_sequence == 2
    with database.sync.connect() as connection:
        assert (
            connection.scalar(
                select(Ingredient.current_price_estimate_id).where(
                    Ingredient.id == created.ingredient_id
                )
            )
            is None
        )
        assert connection.scalar(select(func.count()).select_from(IngredientPriceEstimate)) == 0


def test_replay_and_exact_normalized_collision_are_deterministic(database: Database) -> None:
    initial = command(database)
    first = asyncio.run(create_ingredient(database.sessions, context(database), initial))
    replay = asyncio.run(create_ingredient(database.sessions, context(database), initial))
    assert replay.replayed is True
    assert replay.ingredient_id == first.ingredient_id
    with pytest.raises(ApplicationServiceError) as rejected:
        asyncio.run(
            create_ingredient(
                database.sessions, context(database), command(database, name="tOMATOES")
            )
        )
    assert rejected.value.code == "validation_failed"
    assert rejected.value.field_violations[0].path == "name"
    with database.sync.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Ingredient)) == 1
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 2


def test_replay_rechecks_current_authority_before_returning_retained_outcome(
    database: Database,
) -> None:
    attempted = command(database)
    asyncio.run(create_ingredient(database.sessions, context(database), attempted))
    with database.sync.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == database.organization_id,
                OrganizationMembership.user_id == database.actor_id,
            )
            .values(
                state="removed", removed_at=datetime.now(UTC), removed_by_user_id=database.actor_id
            )
        )
    with pytest.raises(ApplicationServiceError) as denied:
        asyncio.run(create_ingredient(database.sessions, context(database), attempted))
    assert denied.value.code == "forbidden"
    assert denied.value.retry_same_identity is True


def test_same_mutation_with_changed_input_is_an_idempotency_mismatch(database: Database) -> None:
    attempted = command(database)
    asyncio.run(create_ingredient(database.sessions, context(database), attempted))
    changed = command(
        database,
        mutation_id=attempted.mutation_id,
        ingredient_id=attempted.ingredient_id,
        ingredient_version_id=attempted.ingredient_version_id,
        name="Different tomatoes",
    )
    with pytest.raises(ApplicationServiceError) as mismatch:
        asyncio.run(create_ingredient(database.sessions, context(database), changed))
    assert mismatch.value.code == "idempotency_mismatch"
    assert mismatch.value.retry_same_identity is False


def test_preexisting_client_supplied_version_or_price_id_is_a_retained_rejection(
    database: Database,
) -> None:
    published = asyncio.run(
        create_ingredient(database.sessions, context(database), command(database))
    )
    attempted = command(
        database,
        ingredient_version_id=published.ingredient_version_id,
        initial_price=InitialPrice(
            published.initial_price_id or uuid4(),
            Decimal("1"),
            Decimal("1"),
            database.gram_id,
            "CZK",
        ),
    )
    with pytest.raises(ApplicationServiceError) as rejected:
        asyncio.run(create_ingredient(database.sessions, context(database), attempted))
    assert rejected.value.code == "validation_failed"
    assert {item.path for item in rejected.value.field_violations} >= {
        "ingredient_version_id",
        "initial_price.id",
    }


@pytest.mark.parametrize(
    "bad_price",
    [
        InitialPrice(uuid4(), cast(Decimal, "not-a-decimal"), Decimal("1"), uuid4(), "CZK"),
        InitialPrice(uuid4(), Decimal("1"), cast(Decimal, []), uuid4(), "CZK"),
        InitialPrice(uuid4(), Decimal("1"), Decimal("1"), cast(UUID, "not-a-uuid"), cast(str, 3)),
    ],
)
def test_malformed_initial_price_types_are_retained_validation_rejections(
    database: Database, bad_price: InitialPrice
) -> None:
    attempted = command(database, initial_price=bad_price)
    with pytest.raises(ApplicationServiceError) as first:
        asyncio.run(create_ingredient(database.sessions, context(database), attempted))
    with pytest.raises(ApplicationServiceError) as replay:
        asyncio.run(create_ingredient(database.sessions, context(database), attempted))
    assert first.value.code == replay.value.code == "validation_failed"
    assert first.value.field_violations == replay.value.field_violations
    with database.sync.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


def test_corrupt_retained_payloads_fail_closed(database: Database) -> None:
    accepted = command(database)
    asyncio.run(create_ingredient(database.sessions, context(database), accepted))
    with database.sync.begin() as connection:
        connection.execute(
            update(Mutation)
            .where(Mutation.id == accepted.mutation_id)
            .values(outcome_payload={"ingredient": {"id": 42}})
        )
    with pytest.raises(RuntimeError, match="invalid outcome payload"):
        asyncio.run(create_ingredient(database.sessions, context(database), accepted))

    rejected = command(database, mass_per_canonical_quantity=Decimal("0"))
    with pytest.raises(ApplicationServiceError):
        asyncio.run(create_ingredient(database.sessions, context(database), rejected))
    with database.sync.begin() as connection:
        connection.execute(
            update(Mutation)
            .where(Mutation.id == rejected.mutation_id)
            .values(
                outcome_payload={
                    "error": {
                        "code": "validation_failed",
                        "field_violations": [{"path": 1, "code": "x"}],
                    }
                }
            )
        )
    with pytest.raises(RuntimeError, match="invalid outcome payload"):
        asyncio.run(create_ingredient(database.sessions, context(database), rejected))


@pytest.mark.parametrize(
    ("override", "path"),
    [
        ({"mass_per_canonical_quantity": Decimal("0")}, "mass_per_canonical_quantity"),
        ({"mass_per_canonical_quantity": Decimal("999")}, "mass_per_canonical_quantity"),
        ({"name": "İ" * 200}, "name"),
        (
            {"initial_price": InitialPrice(uuid4(), Decimal("2"), Decimal("0"), uuid4(), "CZK")},
            "initial_price.quantity",
        ),
        (
            {"initial_price": InitialPrice(uuid4(), Decimal("2"), Decimal("1"), uuid4(), "EUR")},
            "initial_price.currency",
        ),
        ({"dietary_tag_ids": (uuid4(),)}, "dietary_tag_ids"),
    ],
)
def test_invalid_catalog_references_and_values_are_retained_rejections(
    database: Database, override: dict[str, object], path: str
) -> None:
    attempted = command(database, **override)
    with pytest.raises(ApplicationServiceError) as rejected:
        asyncio.run(create_ingredient(database.sessions, context(database), attempted))
    assert rejected.value.code == "validation_failed"
    assert path in {item.path for item in rejected.value.field_violations}
    with database.sync.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Ingredient)) == 0
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 1


def test_concurrent_normalized_name_creates_exactly_one_ingredient(database: Database) -> None:
    commands = (command(database, name=" Tomatoes "), command(database, name="TOMATOES"))
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as executor:
        futures = [
            executor.submit(
                _create_ingredient_in_process,
                os.environ["TEST_DATABASE_URL"],
                context(database),
                item,
            )
            for item in commands
        ]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(outcome[0] for outcome in outcomes) == ["accepted", "validation_failed"]
    with database.sync.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Ingredient)) == 1
        assert connection.scalar(select(func.count()).select_from(Mutation)) == 2


def test_publish_ingredient_version_is_atomic_idempotent_and_boundary_safe(
    database: Database,
) -> None:
    initial = command(database, dietary_tag_ids=(database.tag_id,), initial_price=None)
    created = asyncio.run(create_ingredient(database.sessions, context(database), initial))
    with database.sync.begin() as connection:
        connection.execute(
            update(DietaryTag)
            .where(DietaryTag.id == database.tag_id)
            .values(retired_at=datetime.now(UTC), retired_by_user_id=database.actor_id)
        )
    published = publish_command(
        database,
        ingredient_id=created.ingredient_id,
        based_on_version_id=created.ingredient_version_id,
        dietary_tag_ids=(database.tag_id,),
    )
    result = asyncio.run(
        publish_ingredient_version(database.sessions, context(database), published)
    )
    replay = asyncio.run(
        publish_ingredient_version(database.sessions, context(database), published)
    )
    assert result.replayed is False and replay.replayed is True
    with database.sync.connect() as connection:
        assert (
            connection.scalar(
                select(Ingredient.current_version_id).where(Ingredient.id == created.ingredient_id)
            )
            == result.ingredient_version_id
        )
        assert (
            connection.scalar(
                select(IngredientVersion.id).where(
                    IngredientVersion.id == created.ingredient_version_id
                )
            )
            == created.ingredient_version_id
        )

    stale = publish_command(
        database,
        ingredient_id=created.ingredient_id,
        based_on_version_id=uuid4(),
    )
    with pytest.raises(ApplicationServiceError, match="stale"):
        asyncio.run(publish_ingredient_version(database.sessions, context(database), stale))
    with pytest.raises(ApplicationServiceError, match="stale"):
        asyncio.run(publish_ingredient_version(database.sessions, context(database), stale))

    invalid_mass = publish_command(
        database,
        ingredient_id=created.ingredient_id,
        based_on_version_id=result.ingredient_version_id,
        mass_per_canonical_quantity=Decimal("0"),
    )
    with pytest.raises(ApplicationServiceError):
        asyncio.run(publish_ingredient_version(database.sessions, context(database), invalid_mass))

    retired_new_tag = uuid4()
    with database.sync.begin() as connection:
        connection.execute(
            insert(DietaryTag).values(
                id=retired_new_tag,
                organization_id=database.organization_id,
                name="Retired",
                normalized_name="retired",
                created_by_user_id=database.actor_id,
                retired_at=datetime.now(UTC),
                retired_by_user_id=database.actor_id,
            )
        )
    rejected_tag = publish_command(
        database,
        ingredient_id=created.ingredient_id,
        based_on_version_id=result.ingredient_version_id,
        dietary_tag_ids=(database.tag_id, retired_new_tag),
    )
    with pytest.raises(ApplicationServiceError):
        asyncio.run(publish_ingredient_version(database.sessions, context(database), rejected_tag))
