import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update
from test_schedule_recipe_service import ServiceDatabase

from cookops.application.event_prices import (
    UpdateEventPriceEstimatesCommand,
    update_event_price_estimates,
)
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.application.scheduled_recipe_overrides import (
    SetScheduledIngredientOverrideCommand,
    set_scheduled_ingredient_override,
)
from cookops.application.scheduled_recipes import (
    ScheduleRecipeCommand,
    ScheduleRecipeResult,
    schedule_recipe,
)
from cookops.persistence.models import (
    Event,
    EventArchiveSnapshot,
    EventIngredientPrice,
    EventIngredientPriceSnapshot,
    Ingredient,
    IngredientPriceEstimate,
    Mutation,
    Organization,
    OrganizationChange,
    ScheduledRecipe,
    UnitDefinition,
)

pytest_plugins = ("test_schedule_recipe_service",)
pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


def _context(database: ServiceDatabase) -> ExecutionContext:
    return ExecutionContext(database.actor_id, database.installation_id)


async def _scheduled(database: ServiceDatabase) -> ScheduleRecipeResult:
    return await schedule_recipe(
        database.sessions,
        _context(database),
        ScheduleRecipeCommand(
            mutation_id=uuid4(),
            scheduled_recipe_id=uuid4(),
            organization_id=database.organization_id,
            event_id=database.event_id,
            event_day_id=database.event_day_id,
            event_meal_role_id=database.event_role_id,
            recipe_id=database.recipe_id,
            recipe_version_id=database.recipe_version_id,
            client_wall_time=datetime.now(UTC),
            position_key="a",
        ),
    )


def _command(database: ServiceDatabase) -> UpdateEventPriceEstimatesCommand:
    return UpdateEventPriceEstimatesCommand(
        mutation_id=uuid4(),
        organization_id=database.organization_id,
        event_id=database.event_id,
        client_wall_time=datetime.now(UTC),
    )


def _publish_price(
    database: ServiceDatabase, ingredient_id: UUID, amount: Decimal, currency: str = "CZK"
) -> UUID:
    estimate_id = uuid4()
    with database.sync_engine.begin() as connection:
        unit_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        assert unit_id is not None
        connection.execute(
            insert(IngredientPriceEstimate).values(
                id=estimate_id,
                organization_id=database.organization_id,
                ingredient_id=ingredient_id,
                state="available",
                price_amount=amount,
                priced_quantity=Decimal("1000"),
                priced_unit_id=unit_id,
                currency=currency,
                published_by_user_id=database.actor_id,
            )
        )
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == ingredient_id)
            .values(current_price_estimate_id=estimate_id)
        )
    return estimate_id


def test_refresh_captures_current_price_for_resolved_recipe_and_replays(
    service_database: ServiceDatabase,
) -> None:
    scheduled = asyncio.run(_scheduled(service_database))
    estimate_id = _publish_price(
        service_database, service_database.recipe_ingredient_id, Decimal("78")
    )
    command = _command(service_database)
    result = asyncio.run(
        update_event_price_estimates(service_database.sessions, _context(service_database), command)
    )
    replay = asyncio.run(
        update_event_price_estimates(service_database.sessions, _context(service_database), command)
    )
    assert replay.replayed is True
    assert replay.price_snapshot_ids == result.price_snapshot_ids
    with service_database.sync_engine.connect() as connection:
        snapshot = connection.execute(
            select(
                EventIngredientPriceSnapshot.state,
                EventIngredientPriceSnapshot.source_ingredient_price_estimate_id,
                EventIngredientPriceSnapshot.price_amount,
                EventIngredientPriceSnapshot.currency,
            ).where(EventIngredientPriceSnapshot.id == result.price_snapshot_ids[0])
        ).one()
        stream = connection.scalar(
            select(EventIngredientPrice.current_snapshot_id).where(
                EventIngredientPrice.event_id == service_database.event_id,
                EventIngredientPrice.ingredient_id == service_database.recipe_ingredient_id,
            )
        )
        price_created_at, price_created_by_user_id = connection.execute(
            select(
                EventIngredientPrice.created_at,
                EventIngredientPrice.created_by_user_id,
            ).where(
                EventIngredientPrice.event_id == service_database.event_id,
                EventIngredientPrice.ingredient_id == service_database.recipe_ingredient_id,
            )
        ).one()
        changes = connection.execute(
            select(
                OrganizationChange.sequence,
                OrganizationChange.entity_id,
                OrganizationChange.entity_kind,
                OrganizationChange.payload,
            ).where(OrganizationChange.mutation_id == command.mutation_id)
        ).all()
    assert scheduled.scheduled_recipe_id
    assert snapshot == ("available", estimate_id, Decimal("78"), "CZK")
    assert stream == result.price_snapshot_ids[0]
    assert len(changes) == 2
    by_sequence = {change.sequence: change for change in changes}
    assert [change.entity_kind for _, change in sorted(by_sequence.items())] == [
        "event_ingredient_price_snapshot",
        "event_ingredient_price",
    ]
    snapshot_change, pointer_change = (
        by_sequence[result.first_change_sequence],
        by_sequence[result.first_change_sequence + 1],
    )
    assert snapshot_change.payload["record"] == {
        "id": str(result.price_snapshot_ids[0]),
        "organization_id": str(service_database.organization_id),
        "event_id": str(service_database.event_id),
        "ingredient_id": str(service_database.recipe_ingredient_id),
        "event_ingredient_price_id": str(pointer_change.entity_id),
        "previous_snapshot_id": snapshot_change.payload["record"]["previous_snapshot_id"],
        "source_ingredient_price_estimate_id": str(estimate_id),
        "state": "available",
        "price_amount": "78",
        "priced_quantity": "1000",
        "priced_unit_id": snapshot_change.payload["record"]["priced_unit_id"],
        "currency": "CZK",
        "captured_by_user_id": str(service_database.actor_id),
        "effective_client_action_time": snapshot_change.payload["record"][
            "effective_client_action_time"
        ],
        "server_received_at": snapshot_change.payload["record"]["server_received_at"],
        "originating_mutation_id": str(command.mutation_id),
    }
    assert pointer_change.payload["record"] == {
        "id": str(pointer_change.entity_id),
        "organization_id": str(service_database.organization_id),
        "event_id": str(service_database.event_id),
        "ingredient_id": str(service_database.recipe_ingredient_id),
        "current_snapshot_id": str(result.price_snapshot_ids[0]),
        "created_at": price_created_at.isoformat(),
        "created_by_user_id": str(price_created_by_user_id),
    }


def test_empty_refresh_publishes_complete_canonical_event_record(
    service_database: ServiceDatabase,
) -> None:
    command = _command(service_database)
    result = asyncio.run(
        update_event_price_estimates(service_database.sessions, _context(service_database), command)
    )
    with service_database.sync_engine.connect() as connection:
        change = connection.execute(
            select(
                OrganizationChange.entity_id,
                OrganizationChange.entity_kind,
                OrganizationChange.operation,
                OrganizationChange.payload,
            ).where(OrganizationChange.mutation_id == command.mutation_id)
        ).one()
        event_created_at = connection.scalar(
            select(Event.created_at).where(Event.id == service_database.event_id)
        )
    assert event_created_at is not None
    assert result.price_snapshot_ids == ()
    assert result.unavailable_ingredient_ids == ()
    assert change.entity_id == service_database.event_id
    assert change.entity_kind == "event"
    assert change.operation == "upsert"
    assert change.payload == {
        "record_schema_version": 1,
        "record": {
            "id": str(service_database.event_id),
            "organization_id": str(service_database.organization_id),
            "name": "Camp",
            "start_date": "2026-07-01",
            "end_date": "2026-07-01",
            "location": None,
            "general_note": None,
            "base_expected_attendance": 42,
            "budget_amount": "0",
            "currency": "CZK",
            "created_at": event_created_at.isoformat(),
            "lifecycle": "active",
            "current_archive_snapshot_id": None,
            "archived_at": None,
            "archived_by_user_id": None,
            "created_by_user_id": str(service_database.actor_id),
            "field_clocks": {"base_expected_attendance": None},
        },
    }


def test_invalid_refresh_replay_retains_field_violations(
    service_database: ServiceDatabase,
) -> None:
    command = UpdateEventPriceEstimatesCommand(
        mutation_id=uuid4(),
        organization_id=service_database.organization_id,
        event_id=cast(UUID, "not-a-uuid"),
        client_wall_time=datetime.now(UTC),
    )
    observed: list[ApplicationServiceError] = []
    for _ in range(2):
        with pytest.raises(ApplicationServiceError) as error:
            asyncio.run(
                update_event_price_estimates(
                    service_database.sessions, _context(service_database), command
                )
            )
        observed.append(error.value)
    assert [error.code for error in observed] == ["validation_failed", "validation_failed"]
    assert [
        [(violation.path, violation.code) for violation in error.field_violations]
        for error in observed
    ] == [[("event_id", "must_be_uuid")], [("event_id", "must_be_uuid")]]


def test_first_nonzero_override_captures_once_and_readd_keeps_the_stream(
    service_database: ServiceDatabase,
) -> None:
    scheduled = asyncio.run(
        schedule_recipe(
            service_database.sessions,
            _context(service_database),
            ScheduleRecipeCommand(
                mutation_id=uuid4(),
                scheduled_recipe_id=uuid4(),
                organization_id=service_database.organization_id,
                event_id=service_database.event_id,
                event_day_id=service_database.event_day_id,
                event_meal_role_id=service_database.event_role_id,
                recipe_id=service_database.recipe_id,
                recipe_version_id=service_database.recipe_version_id,
                client_wall_time=datetime.now(UTC),
                consumption_percentage=Decimal("0"),
                position_key="a",
            ),
        )
    )
    override_id = uuid4()

    def override(
        quantity: Decimal, operation: Literal["set", "clear"] = "set"
    ) -> SetScheduledIngredientOverrideCommand:
        return SetScheduledIngredientOverrideCommand(
            mutation_id=uuid4(),
            override_id=override_id,
            organization_id=service_database.organization_id,
            event_id=service_database.event_id,
            scheduled_recipe_id=scheduled.scheduled_recipe_id,
            operation=operation,
            override_kind="add",
            ingredient_id=service_database.added_ingredient_id if operation == "set" else None,
            ingredient_version_id=(
                service_database.added_ingredient_version_id if operation == "set" else None
            ),
            quantity=quantity if operation == "set" else None,
            include_in_portion_weight=True if operation == "set" else None,
            position_key="z" if operation == "set" else None,
            client_wall_time=datetime.now(UTC),
        )

    asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, _context(service_database), override(Decimal("0"))
        )
    )
    first_nonzero = asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, _context(service_database), override(Decimal("3"))
        )
    )
    asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions,
            _context(service_database),
            override(Decimal("0"), "clear"),
        )
    )
    readded = asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions, _context(service_database), override(Decimal("3"))
        )
    )
    with service_database.sync_engine.connect() as connection:
        snapshots = connection.execute(
            select(EventIngredientPriceSnapshot.id).where(
                EventIngredientPriceSnapshot.event_id == service_database.event_id,
                EventIngredientPriceSnapshot.ingredient_id == service_database.added_ingredient_id,
            )
        ).all()
        first_changes = connection.execute(
            select(OrganizationChange.entity_kind)
            .where(OrganizationChange.mutation_id == first_nonzero.mutation_id)
            .order_by(OrganizationChange.sequence)
        ).all()
        readded_changes = connection.execute(
            select(OrganizationChange.entity_kind)
            .where(OrganizationChange.mutation_id == readded.mutation_id)
            .order_by(OrganizationChange.sequence)
        ).all()
    assert len(snapshots) == 1
    assert [change.entity_kind for change in first_changes] == [
        "scheduled_ingredient_override",
        "event_ingredient_price_snapshot",
        "event_ingredient_price",
    ]
    assert [change.entity_kind for change in readded_changes] == ["scheduled_ingredient_override"]


def test_refresh_keeps_known_prices_and_records_currency_mismatch_as_unavailable(
    service_database: ServiceDatabase,
) -> None:
    asyncio.run(_scheduled(service_database))
    with service_database.sync_engine.begin() as connection:
        # Catalog prices follow the organization default, while an existing event
        # retains its inherited currency and must mark this current catalog price unavailable.
        connection.execute(
            update(Organization)
            .where(Organization.id == service_database.organization_id)
            .values(default_currency="EUR")
        )
        connection.execute(
            update(Event).where(Event.id == service_database.event_id).values(currency="CZK")
        )
    estimate_id = _publish_price(
        service_database, service_database.recipe_ingredient_id, Decimal("12"), "EUR"
    )
    known_stream_id = uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(EventIngredientPrice).values(
                id=known_stream_id,
                organization_id=service_database.organization_id,
                event_id=service_database.event_id,
                ingredient_id=service_database.added_ingredient_id,
                created_by_user_id=service_database.actor_id,
            )
        )
    result = asyncio.run(
        update_event_price_estimates(
            service_database.sessions, _context(service_database), _command(service_database)
        )
    )
    with service_database.sync_engine.connect() as connection:
        snapshots = connection.execute(
            select(
                EventIngredientPriceSnapshot.ingredient_id,
                EventIngredientPriceSnapshot.state,
                EventIngredientPriceSnapshot.source_ingredient_price_estimate_id,
            ).where(EventIngredientPriceSnapshot.id.in_(result.price_snapshot_ids))
        ).all()
    by_ingredient = {row[0]: row[1:] for row in snapshots}
    assert set(result.unavailable_ingredient_ids) == set(by_ingredient)
    assert by_ingredient[service_database.recipe_ingredient_id] == ("unavailable", estimate_id)
    assert by_ingredient[service_database.added_ingredient_id] == ("unavailable", None)


def test_refresh_uses_nonzero_added_overrides_and_excludes_zero_recipe_scale(
    service_database: ServiceDatabase,
) -> None:
    scheduled = asyncio.run(_scheduled(service_database))
    asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions,
            _context(service_database),
            SetScheduledIngredientOverrideCommand(
                mutation_id=uuid4(),
                override_id=uuid4(),
                organization_id=service_database.organization_id,
                event_id=service_database.event_id,
                scheduled_recipe_id=scheduled.scheduled_recipe_id,
                operation="set",
                override_kind="add",
                ingredient_id=service_database.added_ingredient_id,
                ingredient_version_id=service_database.added_ingredient_version_id,
                quantity=Decimal("3"),
                include_in_portion_weight=True,
                position_key="z",
                client_wall_time=datetime.now(UTC),
            ),
        )
    )
    _publish_price(service_database, service_database.added_ingredient_id, Decimal("20"))
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(ScheduledRecipe)
            .where(ScheduledRecipe.id == scheduled.scheduled_recipe_id)
            .values(selected_scale_amount=Decimal("0"))
        )
    result = asyncio.run(
        update_event_price_estimates(
            service_database.sessions, _context(service_database), _command(service_database)
        )
    )
    with service_database.sync_engine.connect() as connection:
        ingredient_ids = set(
            connection.scalars(
                select(EventIngredientPriceSnapshot.ingredient_id).where(
                    EventIngredientPriceSnapshot.id.in_(result.price_snapshot_ids)
                )
            )
        )
    assert ingredient_ids == {
        service_database.recipe_ingredient_id,
        service_database.added_ingredient_id,
    }


def test_refresh_rejects_archived_event_without_mutating_streams(
    service_database: ServiceDatabase,
) -> None:
    with service_database.sync_engine.begin() as connection:
        archive_id = uuid4()
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=archive_id,
                event_id=service_database.event_id,
                archive_schema_version=1,
                payload={},
                content_hash=bytes(32),
                attachment_manifest=[],
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            update(Event)
            .where(Event.id == service_database.event_id)
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=archive_id,
                archived_at=datetime.now(UTC),
                archived_by_user_id=service_database.actor_id,
            )
        )
    command = _command(service_database)
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            update_event_price_estimates(
                service_database.sessions, _context(service_database), command
            )
        )
    assert error.value.code == "archived_event"
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(EventIngredientPrice.id).where(
                    EventIngredientPrice.event_id == command.event_id
                )
            )
            is None
        )
        assert (
            connection.scalar(select(Mutation.id).where(Mutation.id == command.mutation_id))
            == command.mutation_id
        )
