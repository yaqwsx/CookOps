import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update
from test_create_shopping_list_service import _command as list_command
from test_create_shopping_list_service import _context, _scheduled
from test_schedule_recipe_service import ServiceDatabase

from cookops.application.organizations import ApplicationServiceError
from cookops.application.shopping_lists import (
    CreateShoppingListResult,
    SetShoppingAvailableSupplyCommand,
    SetShoppingContributionFulfilmentCommand,
    SetShoppingManualPurchaseTargetCommand,
    SetShoppingRowFulfilmentCommand,
    SetShoppingRowNoteCommand,
    ShoppingOperationResult,
    create_shopping_list,
    set_shopping_available_supply,
    set_shopping_contribution_fulfilment,
    set_shopping_manual_purchase_target,
    set_shopping_row_fulfilment,
    set_shopping_row_note,
)
from cookops.persistence.models import (
    Event,
    EventArchiveSnapshot,
    FieldClock,
    Mutation,
    OrganizationChange,
    OrganizationMembership,
    ShoppingContribution,
    ShoppingIngredientRow,
)

pytest_plugins = ("test_schedule_recipe_service",)
pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


def _list(database: ServiceDatabase) -> tuple[CreateShoppingListResult, UUID, UUID]:
    scheduled = asyncio.run(_scheduled(database))
    result = asyncio.run(
        create_shopping_list(
            database.sessions,
            _context(database),
            list_command(database, (scheduled.scheduled_recipe_id,)),
        )
    )
    with database.sync_engine.connect() as connection:
        row_id = connection.scalar(
            select(ShoppingIngredientRow.id).where(
                ShoppingIngredientRow.shopping_list_id == result.shopping_list_id
            )
        )
        contribution_id = connection.scalar(
            select(ShoppingContribution.id).where(
                ShoppingContribution.shopping_list_id == result.shopping_list_id
            )
        )
    assert row_id is not None and contribution_id is not None
    return result, row_id, contribution_id


def test_member_operates_supply_target_and_contribution_idempotently(
    service_database: ServiceDatabase,
) -> None:
    result, row_id, contribution_id = _list(service_database)
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == service_database.organization_id,
                OrganizationMembership.user_id == service_database.actor_id,
            )
            .values(role="member")
        )
    now = datetime.now(UTC)
    supply = SetShoppingAvailableSupplyCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        Decimal("200"),
        now,
    )
    target = SetShoppingManualPurchaseTargetCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        Decimal("1400"),
        now,
    )
    fulfilled = SetShoppingContributionFulfilmentCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        contribution_id,
        True,
        now,
    )
    assert (
        asyncio.run(
            set_shopping_available_supply(
                service_database.sessions, _context(service_database), supply
            )
        ).outcome
        == "accepted"
    )
    assert (
        asyncio.run(
            set_shopping_manual_purchase_target(
                service_database.sessions, _context(service_database), target
            )
        ).outcome
        == "accepted"
    )
    first = asyncio.run(
        set_shopping_contribution_fulfilment(
            service_database.sessions, _context(service_database), fulfilled
        )
    )
    replay = asyncio.run(
        set_shopping_contribution_fulfilment(
            service_database.sessions, _context(service_database), fulfilled
        )
    )
    assert replay.replayed and replay.first_change_sequence == first.first_change_sequence
    with service_database.sync_engine.connect() as connection:
        supply_amount, target_amount = connection.execute(
            select(
                ShoppingIngredientRow.available_supply_quantity,
                ShoppingIngredientRow.manual_purchase_target,
            ).where(ShoppingIngredientRow.id == row_id)
        ).one()
        contribution_credit = connection.scalar(
            select(ShoppingContribution.fulfilment_credit).where(
                ShoppingContribution.id == contribution_id
            )
        )
        assert supply_amount == Decimal("200")
        assert target_amount == Decimal("1400")
        assert contribution_credit == Decimal("1500")


def test_lww_is_deterministic_and_rejected_future_mutation_is_retained(
    service_database: ServiceDatabase,
) -> None:
    result, row_id, _ = _list(service_database)
    later = datetime.now(UTC)
    winning = SetShoppingAvailableSupplyCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        Decimal("9"),
        later,
    )
    losing = SetShoppingAvailableSupplyCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        Decimal("2"),
        later - timedelta(seconds=1),
    )
    assert (
        asyncio.run(
            set_shopping_available_supply(
                service_database.sessions, _context(service_database), winning
            )
        ).outcome
        == "accepted"
    )
    assert (
        asyncio.run(
            set_shopping_available_supply(
                service_database.sessions, _context(service_database), losing
            )
        ).outcome
        == "partially_superseded"
    )
    future = SetShoppingAvailableSupplyCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        Decimal("3"),
        datetime.now(UTC) + timedelta(hours=25),
    )
    with pytest.raises(ApplicationServiceError, match="client_time_too_far_ahead"):
        asyncio.run(
            set_shopping_available_supply(
                service_database.sessions, _context(service_database), future
            )
        )
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(
            select(ShoppingIngredientRow.available_supply_quantity).where(
                ShoppingIngredientRow.id == row_id
            )
        ) == Decimal("9")
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == future.mutation_id))
            == "rejected"
        )
        assert (
            connection.scalar(
                select(FieldClock.winning_mutation_id).where(
                    FieldClock.entity_id == row_id,
                    FieldClock.field_name == "available_supply_quantity",
                )
            )
            == winning.mutation_id
        )


@pytest.mark.parametrize("fulfilled", [True, False])
def test_aggregate_fulfilment_sets_every_credit_atomically(
    service_database: ServiceDatabase, fulfilled: bool
) -> None:
    result, row_id, contribution_id = _list(service_database)
    command = SetShoppingRowFulfilmentCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        fulfilled,
        datetime.now(UTC),
    )
    operation = asyncio.run(
        set_shopping_row_fulfilment(service_database.sessions, _context(service_database), command)
    )
    assert operation.shopping_contribution_ids == (contribution_id,)
    with service_database.sync_engine.connect() as connection:
        credit = connection.scalar(
            select(ShoppingContribution.fulfilment_credit).where(
                ShoppingContribution.id == contribution_id
            )
        )
        assert credit == (Decimal("1500") if fulfilled else Decimal(0))


def test_archived_event_rejects_operations_and_retains_the_rejection(
    service_database: ServiceDatabase,
) -> None:
    result, row_id, _ = _list(service_database)
    snapshot_id = uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=snapshot_id,
                event_id=result.event_id,
                archive_schema_version=1,
                payload={"event": {}},
                attachment_manifest=[],
                content_hash=hashlib.sha256(b"shopping-operation-test").digest(),
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            update(Event)
            .where(Event.id == result.event_id)
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=snapshot_id,
                archived_at=datetime.now(UTC),
                archived_by_user_id=service_database.actor_id,
            )
        )
    command = SetShoppingAvailableSupplyCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        Decimal("12"),
        datetime.now(UTC),
    )
    for _ in range(2):
        with pytest.raises(ApplicationServiceError, match="archived_event"):
            asyncio.run(
                set_shopping_available_supply(
                    service_database.sessions, _context(service_database), command
                )
            )
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == command.mutation_id))
            == "rejected"
        )
        assert connection.scalar(
            select(ShoppingIngredientRow.available_supply_quantity).where(
                ShoppingIngredientRow.id == row_id
            )
        ) == Decimal(0)


def test_aggregate_fulfilment_rejects_as_one_action_when_an_individual_clock_wins(
    service_database: ServiceDatabase,
) -> None:
    result, row_id, contribution_id = _list(service_database)
    individual = SetShoppingContributionFulfilmentCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        contribution_id,
        True,
        datetime.now(UTC),
    )
    asyncio.run(
        set_shopping_contribution_fulfilment(
            service_database.sessions, _context(service_database), individual
        )
    )
    aggregate = SetShoppingRowFulfilmentCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        False,
        individual.client_wall_time - timedelta(seconds=1),
    )
    with pytest.raises(ApplicationServiceError, match="validation_failed") as error:
        asyncio.run(
            set_shopping_row_fulfilment(
                service_database.sessions, _context(service_database), aggregate
            )
        )
    assert error.value.field_violations[0].code == "superseded_by_newer_change"
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(
            select(ShoppingContribution.fulfilment_credit).where(
                ShoppingContribution.id == contribution_id
            )
        ) == Decimal("1500")
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == aggregate.mutation_id))
            == "rejected"
        )
        assert (
            connection.scalar(
                select(OrganizationChange.sequence).where(
                    OrganizationChange.mutation_id == aggregate.mutation_id
                )
            )
            is None
        )


def test_row_and_contribution_fulfilment_concurrently_acquire_locks_in_one_order(
    service_database: ServiceDatabase,
) -> None:
    result, row_id, contribution_id = _list(service_database)
    action_time = datetime.now(UTC)
    individual = SetShoppingContributionFulfilmentCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        contribution_id,
        True,
        action_time,
    )
    aggregate = SetShoppingRowFulfilmentCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        True,
        action_time,
    )

    async def run_concurrently() -> tuple[object, object]:
        return await asyncio.wait_for(
            asyncio.gather(
                set_shopping_contribution_fulfilment(
                    service_database.sessions, _context(service_database), individual
                ),
                set_shopping_row_fulfilment(
                    service_database.sessions, _context(service_database), aggregate
                ),
                return_exceptions=True,
            ),
            timeout=5,
        )

    outcomes = asyncio.run(run_concurrently())
    assert all(
        isinstance(outcome, (ShoppingOperationResult, ApplicationServiceError))
        for outcome in outcomes
    )


def test_operation_change_carries_full_row_record_and_authoritative_field_clock(
    service_database: ServiceDatabase,
) -> None:
    result, row_id, _ = _list(service_database)
    action_time = datetime.now(UTC)
    command = SetShoppingAvailableSupplyCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        Decimal("7.5"),
        action_time,
    )
    asyncio.run(
        set_shopping_available_supply(
            service_database.sessions, _context(service_database), command
        )
    )
    with service_database.sync_engine.connect() as connection:
        record = connection.scalar(
            select(OrganizationChange.payload["record"]).where(
                OrganizationChange.mutation_id == command.mutation_id,
                OrganizationChange.entity_kind == "shopping_ingredient_row",
            )
        )
    assert record is not None
    assert record["organization_id"] == str(service_database.organization_id)
    assert record["event_id"] == str(result.event_id)
    assert record["ingredient_name"]
    assert record["calculation_unit_id"]
    assert record["note"] is None
    clock = record["field_clocks"]["available_supply_quantity"]
    assert clock["winning_mutation_id"] == str(command.mutation_id)
    assert clock["winning_client_wall_time"] == action_time.isoformat()
    assert record["field_clocks"]["manual_purchase_target"] is None


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("0.000001"), Decimal("1E+20")])
def test_finite_decimal_supply_inputs_survive_without_float_rounding(
    service_database: ServiceDatabase, amount: Decimal
) -> None:
    result, row_id, _ = _list(service_database)
    command = SetShoppingAvailableSupplyCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        amount,
        datetime.now(UTC),
    )
    asyncio.run(
        set_shopping_available_supply(
            service_database.sessions, _context(service_database), command
        )
    )
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(ShoppingIngredientRow.available_supply_quantity).where(
                    ShoppingIngredientRow.id == row_id
                )
            )
            == amount
        )


def test_lone_surrogate_row_note_is_retained_as_validation_rejection(
    service_database: ServiceDatabase,
) -> None:
    result, row_id, _ = _list(service_database)
    command = SetShoppingRowNoteCommand(
        uuid4(),
        service_database.organization_id,
        result.shopping_list_id,
        row_id,
        "\ud800",
        datetime.now(UTC),
    )
    for _ in range(2):
        with pytest.raises(ApplicationServiceError) as error:
            asyncio.run(
                set_shopping_row_note(
                    service_database.sessions, _context(service_database), command
                )
            )
        assert error.value.code == "validation_failed"
        assert error.value.retry_same_identity is False
        assert error.value.field_violations

    for first_note, second_note in (
        ("\ud800", "\ud801"),
        ("x" * 4001, "y" * 4001),
        (123, None),
    ):
        mutation_id = uuid4()
        first = SetShoppingRowNoteCommand(
            mutation_id,
            service_database.organization_id,
            result.shopping_list_id,
            row_id,
            first_note,  # type: ignore[arg-type]
            datetime.now(UTC),
        )
        with pytest.raises(ApplicationServiceError) as first_error:
            asyncio.run(
                set_shopping_row_note(
                    service_database.sessions, _context(service_database), first
                )
            )
        assert first_error.value.code == "validation_failed"
        second = SetShoppingRowNoteCommand(
            mutation_id,
            service_database.organization_id,
            result.shopping_list_id,
            row_id,
            second_note,  # type: ignore[arg-type]
            first.client_wall_time,
        )
        with pytest.raises(ApplicationServiceError) as mismatch:
            asyncio.run(
                set_shopping_row_note(
                    service_database.sessions, _context(service_database), second
                )
            )
        assert mismatch.value.code == "idempotency_mismatch"
