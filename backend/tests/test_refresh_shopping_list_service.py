import asyncio
import hashlib
import os
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update
from test_create_shopping_list_service import _command as create_command
from test_create_shopping_list_service import _context, _scheduled
from test_schedule_recipe_service import ServiceDatabase, override_command

from cookops.application.organizations import ApplicationServiceError
from cookops.application.scheduled_recipe_overrides import set_scheduled_ingredient_override
from cookops.application.shopping_lists import (
    CreateShoppingListResult,
    RefreshShoppingListCommand,
    SetShoppingAvailableSupplyCommand,
    SetShoppingContributionFulfilmentCommand,
    create_shopping_list,
    refresh_shopping_list,
    set_shopping_available_supply,
    set_shopping_contribution_fulfilment,
)
from cookops.application.synchronization import (
    PullRequest,
    SyncCursor,
    SyncCursorCodec,
    SynchronizationQueryService,
)
from cookops.persistence.models import (
    Event,
    EventArchiveSnapshot,
    FieldClock,
    Mutation,
    ScheduledRecipe,
    ShoppingContribution,
    ShoppingContributionSnapshot,
    ShoppingGenerationRevision,
    ShoppingIngredientRow,
    ShoppingList,
    ShoppingRevisionSource,
)

pytest_plugins = ("test_schedule_recipe_service",)
pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


def _initial(database: ServiceDatabase) -> tuple[CreateShoppingListResult, UUID]:
    scheduled = asyncio.run(_scheduled(database))
    result = asyncio.run(
        create_shopping_list(
            database.sessions,
            _context(database),
            create_command(database, (scheduled.scheduled_recipe_id,)),
        )
    )
    return result, scheduled.scheduled_recipe_id


def _refresh(
    database: ServiceDatabase,
    result: CreateShoppingListResult,
    source_ids: tuple[UUID, ...],
    *,
    parent: UUID | None = None,
    action_time: datetime | None = None,
) -> RefreshShoppingListCommand:
    return RefreshShoppingListCommand(
        mutation_id=uuid4(),
        generation_revision_id=uuid4(),
        organization_id=database.organization_id,
        shopping_list_id=result.shopping_list_id,
        parent_generation_revision_id=parent or result.generation_revision_id,
        scheduled_recipe_ids=source_ids,
        client_wall_time=action_time or datetime.now(UTC),
    )


def test_refresh_creates_immutable_revision_and_preserves_operational_state(
    service_database: ServiceDatabase,
) -> None:
    initial, scheduled_id = _initial(service_database)
    with service_database.sync_engine.connect() as connection:
        row_id = connection.scalar(
            select(ShoppingIngredientRow.id).where(
                ShoppingIngredientRow.shopping_list_id == initial.shopping_list_id
            )
        )
        contribution_id = connection.scalar(
            select(ShoppingContribution.id).where(
                ShoppingContribution.shopping_list_id == initial.shopping_list_id
            )
        )
    assert row_id is not None and contribution_id is not None
    now = datetime.now(UTC)
    asyncio.run(
        set_shopping_available_supply(
            service_database.sessions,
            _context(service_database),
            SetShoppingAvailableSupplyCommand(
                uuid4(),
                service_database.organization_id,
                initial.shopping_list_id,
                row_id,
                Decimal("9"),
                now,
            ),
        )
    )
    asyncio.run(
        set_shopping_contribution_fulfilment(
            service_database.sessions,
            _context(service_database),
            SetShoppingContributionFulfilmentCommand(
                uuid4(),
                service_database.organization_id,
                initial.shopping_list_id,
                contribution_id,
                True,
                now,
            ),
        )
    )
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(ScheduledRecipe)
            .where(ScheduledRecipe.id == scheduled_id)
            .values(selected_scale_amount=Decimal("2"))
        )
    command = _refresh(service_database, initial, (scheduled_id,))
    refreshed = asyncio.run(
        refresh_shopping_list(service_database.sessions, _context(service_database), command)
    )
    replay = asyncio.run(
        refresh_shopping_list(service_database.sessions, _context(service_database), command)
    )
    assert replay.replayed and replay.generation_revision_id == refreshed.generation_revision_id
    with service_database.sync_engine.connect() as connection:
        pointer = connection.scalar(
            select(ShoppingList.current_generation_revision_id).where(
                ShoppingList.id == initial.shopping_list_id
            )
        )
        revision_parent = connection.scalar(
            select(ShoppingGenerationRevision.parent_revision_id).where(
                ShoppingGenerationRevision.id == refreshed.generation_revision_id
            )
        )
        source_ids = connection.scalars(
            select(ShoppingRevisionSource.scheduled_recipe_id).where(
                ShoppingRevisionSource.generation_revision_id == refreshed.generation_revision_id
            )
        ).all()
        quantity, active = connection.execute(
            select(
                ShoppingContributionSnapshot.generated_quantity,
                ShoppingContributionSnapshot.active_in_revision,
            ).where(
                ShoppingContributionSnapshot.generation_revision_id
                == refreshed.generation_revision_id,
                ShoppingContributionSnapshot.shopping_contribution_id == contribution_id,
            )
        ).one()
        supply = connection.scalar(
            select(ShoppingIngredientRow.available_supply_quantity).where(
                ShoppingIngredientRow.id == row_id
            )
        )
        credit = connection.scalar(
            select(ShoppingContribution.fulfilment_credit).where(
                ShoppingContribution.id == contribution_id
            )
        )
        old_quantity = connection.scalar(
            select(ShoppingContributionSnapshot.generated_quantity).where(
                ShoppingContributionSnapshot.generation_revision_id
                == initial.generation_revision_id,
                ShoppingContributionSnapshot.shopping_contribution_id == contribution_id,
            )
        )
    assert pointer == refreshed.generation_revision_id
    assert revision_parent == initial.generation_revision_id
    assert source_ids == [scheduled_id]
    assert (quantity, active) == (Decimal("1000"), True)
    assert (supply, credit, old_quantity) == (Decimal("9"), Decimal("1500"), Decimal("1500"))


def test_refresh_retires_and_reactivates_stable_contribution_with_credit(
    service_database: ServiceDatabase,
) -> None:
    initial, scheduled_id = _initial(service_database)
    with service_database.sync_engine.connect() as connection:
        contribution_id = connection.scalar(
            select(ShoppingContribution.id).where(
                ShoppingContribution.shopping_list_id == initial.shopping_list_id
            )
        )
    assert contribution_id is not None
    action_time = datetime.now(UTC)
    asyncio.run(
        set_shopping_contribution_fulfilment(
            service_database.sessions,
            _context(service_database),
            SetShoppingContributionFulfilmentCommand(
                uuid4(),
                service_database.organization_id,
                initial.shopping_list_id,
                contribution_id,
                True,
                action_time,
            ),
        )
    )
    removed = asyncio.run(
        refresh_shopping_list(
            service_database.sessions,
            _context(service_database),
            _refresh(service_database, initial, ()),
        )
    )
    restored = asyncio.run(
        refresh_shopping_list(
            service_database.sessions,
            _context(service_database),
            _refresh(
                service_database, initial, (scheduled_id,), parent=removed.generation_revision_id
            ),
        )
    )
    with service_database.sync_engine.connect() as connection:
        retired = connection.execute(
            select(
                ShoppingContributionSnapshot.active_in_revision,
                ShoppingContributionSnapshot.generated_quantity,
            ).where(
                ShoppingContributionSnapshot.generation_revision_id
                == removed.generation_revision_id,
                ShoppingContributionSnapshot.shopping_contribution_id == contribution_id,
            )
        ).one()
        active = connection.scalar(
            select(ShoppingContributionSnapshot.active_in_revision).where(
                ShoppingContributionSnapshot.generation_revision_id
                == restored.generation_revision_id,
                ShoppingContributionSnapshot.shopping_contribution_id == contribution_id,
            )
        )
        credit = connection.scalar(
            select(ShoppingContribution.fulfilment_credit).where(
                ShoppingContribution.id == contribution_id
            )
        )
    assert retired == (False, Decimal("1500"))
    assert active is True and credit == Decimal("1500")


@pytest.mark.parametrize(
    "scale", [Decimal("0"), Decimal("0.001"), Decimal("1.25"), Decimal("999.999")]
)
def test_refresh_decimal_quantities_remain_exact(
    service_database: ServiceDatabase, scale: Decimal
) -> None:
    initial, scheduled_id = _initial(service_database)
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(ScheduledRecipe)
            .where(ScheduledRecipe.id == scheduled_id)
            .values(selected_scale_amount=scale)
        )
    refreshed = asyncio.run(
        refresh_shopping_list(
            service_database.sessions,
            _context(service_database),
            _refresh(service_database, initial, (scheduled_id,)),
        )
    )
    with service_database.sync_engine.connect() as connection:
        quantities = connection.scalars(
            select(ShoppingContributionSnapshot.generated_quantity).where(
                ShoppingContributionSnapshot.generation_revision_id
                == refreshed.generation_revision_id,
                ShoppingContributionSnapshot.active_in_revision.is_(True),
            )
        ).all()
    expected = Decimal("500") * scale
    assert quantities == ([] if expected == 0 else [expected])


def test_refresh_fuzzes_decimal_scaling_without_float_drift(
    service_database: ServiceDatabase,
) -> None:
    initial, scheduled_id = _initial(service_database)
    generator = random.Random(14_061)
    parent = initial.generation_revision_id
    for _ in range(48):
        scale = Decimal(generator.randrange(0, 1_000_000)).scaleb(-4)
        with service_database.sync_engine.begin() as connection:
            connection.execute(
                update(ScheduledRecipe)
                .where(ScheduledRecipe.id == scheduled_id)
                .values(selected_scale_amount=scale)
            )
        refreshed = asyncio.run(
            refresh_shopping_list(
                service_database.sessions,
                _context(service_database),
                _refresh(service_database, initial, (scheduled_id,), parent=parent),
            )
        )
        with service_database.sync_engine.connect() as connection:
            quantity = connection.scalar(
                select(ShoppingContributionSnapshot.generated_quantity).where(
                    ShoppingContributionSnapshot.generation_revision_id
                    == refreshed.generation_revision_id,
                    ShoppingContributionSnapshot.active_in_revision.is_(True),
                )
            )
        assert quantity == Decimal("500") * scale
        parent = refreshed.generation_revision_id


def test_refresh_rejects_unknown_parent_and_archived_event_idempotently(
    service_database: ServiceDatabase,
) -> None:
    initial, scheduled_id = _initial(service_database)
    stale = _refresh(service_database, initial, (scheduled_id,), parent=uuid4())
    for _ in range(2):
        with pytest.raises(ApplicationServiceError, match="stale_precondition"):
            asyncio.run(
                refresh_shopping_list(service_database.sessions, _context(service_database), stale)
            )
    future = _refresh(
        service_database,
        initial,
        (scheduled_id,),
        action_time=datetime.now(UTC) + timedelta(hours=25),
    )
    with pytest.raises(ApplicationServiceError, match="client_time_too_far_ahead"):
        asyncio.run(
            refresh_shopping_list(service_database.sessions, _context(service_database), future)
        )
    with service_database.sync_engine.begin() as connection:
        snapshot_id = uuid4()
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=snapshot_id,
                event_id=initial.event_id,
                archive_schema_version=1,
                payload={"event": {}},
                attachment_manifest=[],
                content_hash=hashlib.sha256(b"refresh-test").digest(),
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            update(Event)
            .where(Event.id == initial.event_id)
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=snapshot_id,
                archived_at=datetime.now(UTC),
                archived_by_user_id=service_database.actor_id,
            )
        )
    archived = _refresh(service_database, initial, (scheduled_id,))
    with pytest.raises(ApplicationServiceError, match="archived_event"):
        asyncio.run(
            refresh_shopping_list(service_database.sessions, _context(service_database), archived)
        )
    with service_database.sync_engine.connect() as connection:
        outcomes = connection.scalars(
            select(Mutation.outcome).where(
                Mutation.id.in_([stale.mutation_id, future.mutation_id, archived.mutation_id])
            )
        ).all()
    assert outcomes == ["rejected", "rejected", "rejected"]


def test_concurrent_refreshes_keep_both_revisions_and_lww_selects_pointer(
    service_database: ServiceDatabase,
) -> None:
    initial, scheduled_id = _initial(service_database)
    base = datetime.now(UTC)
    first_command = _refresh(service_database, initial, (), action_time=base)
    second_command = _refresh(
        service_database, initial, (scheduled_id,), action_time=base + timedelta(seconds=1)
    )
    first = asyncio.run(
        refresh_shopping_list(service_database.sessions, _context(service_database), first_command)
    )
    second = asyncio.run(
        refresh_shopping_list(service_database.sessions, _context(service_database), second_command)
    )
    with service_database.sync_engine.connect() as connection:
        pointer = connection.scalar(
            select(ShoppingList.current_generation_revision_id).where(
                ShoppingList.id == initial.shopping_list_id
            )
        )
        revision_ids = connection.scalars(
            select(ShoppingGenerationRevision.id).where(
                ShoppingGenerationRevision.shopping_list_id == initial.shopping_list_id
            )
        ).all()
        winner = connection.scalar(
            select(FieldClock.winning_mutation_id).where(
                FieldClock.entity_kind == "shopping_list",
                FieldClock.entity_id == initial.shopping_list_id,
                FieldClock.field_name == "current_generation_revision_id",
            )
        )
    assert first.outcome == second.outcome == "accepted"
    assert pointer == second.generation_revision_id
    assert {first.generation_revision_id, second.generation_revision_id} <= set(revision_ids)
    assert winner == second.mutation_id


def test_refresh_older_than_list_creation_keeps_creation_revision_as_lww_winner(
    service_database: ServiceDatabase,
) -> None:
    scheduled = asyncio.run(_scheduled(service_database))
    created_at = datetime.now(UTC)
    initial = asyncio.run(
        create_shopping_list(
            service_database.sessions,
            _context(service_database),
            replace(
                create_command(service_database, (scheduled.scheduled_recipe_id,)),
                client_wall_time=created_at,
            ),
        )
    )
    refresh = _refresh(
        service_database,
        initial,
        (),
        action_time=created_at - timedelta(microseconds=1),
    )
    result = asyncio.run(
        refresh_shopping_list(service_database.sessions, _context(service_database), refresh)
    )

    with service_database.sync_engine.connect() as connection:
        pointer = connection.scalar(
            select(ShoppingList.current_generation_revision_id).where(
                ShoppingList.id == initial.shopping_list_id
            )
        )
        clock = connection.execute(
            select(FieldClock.winning_client_wall_time, FieldClock.winning_mutation_id).where(
                FieldClock.entity_kind == "shopping_list",
                FieldClock.entity_id == initial.shopping_list_id,
                FieldClock.field_name == "current_generation_revision_id",
            )
        ).one()

    assert result.outcome == "partially_superseded"
    assert pointer == initial.generation_revision_id
    assert clock == (created_at, initial.mutation_id)


def test_refresh_pull_group_contains_complete_canonical_records(
    service_database: ServiceDatabase,
) -> None:
    initial, scheduled_id = _initial(service_database)
    asyncio.run(
        set_scheduled_ingredient_override(
            service_database.sessions,
            _context(service_database),
            override_command(
                service_database,
                scheduled_id,
                override_kind="add",
                target_line_key=None,
                ingredient_id=service_database.added_ingredient_id,
                ingredient_version_id=service_database.added_ingredient_version_id,
                quantity=Decimal("30"),
                include_in_portion_weight=True,
                position_key="z",
            ),
        )
    )
    refreshed = asyncio.run(
        refresh_shopping_list(
            service_database.sessions,
            _context(service_database),
            _refresh(service_database, initial, (scheduled_id,)),
        )
    )
    cursor_codec = SyncCursorCodec(encoded_hmac_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY")
    pulled = asyncio.run(
        SynchronizationQueryService(
            service_database.sessions,
            encoded_cursor_hmac_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
        ).pull(
            actor_user_id=service_database.actor_id,
            request=PullRequest(
                organization_id=service_database.organization_id,
                cursor=cursor_codec.encode(
                    SyncCursor(
                        organization_id=service_database.organization_id,
                        after_sequence=initial.last_change_sequence,
                    )
                ),
            ),
        )
    )
    group = next(
        group for group in pulled.transaction_groups if group.mutation_id == refreshed.mutation_id
    )
    kinds = [record.entity_kind for record in group.records]

    assert (group.first_sequence, group.last_sequence) == (
        refreshed.first_change_sequence,
        refreshed.last_change_sequence,
    )
    assert len(group.records) == group.last_sequence - group.first_sequence + 1
    assert kinds.count("shopping_generation_revision") == 1
    assert kinds.count("shopping_revision_source") == 1
    assert kinds.count("shopping_contribution_snapshot") == 2
    assert kinds.count("shopping_ingredient_row") == 2
    assert kinds.count("shopping_contribution") == 2
    assert kinds.count("shopping_list") == 1
    records = [record.payload["record"] for record in group.records]
    revision_record = next(
        record
        for record, kind in zip(records, kinds, strict=True)
        if kind == "shopping_generation_revision"
    )
    snapshot_records = [
        record
        for record, kind in zip(records, kinds, strict=True)
        if kind == "shopping_contribution_snapshot"
    ]
    row_records = [
        record
        for record, kind in zip(records, kinds, strict=True)
        if kind == "shopping_ingredient_row"
    ]
    contribution_records = [
        record
        for record, kind in zip(records, kinds, strict=True)
        if kind == "shopping_contribution"
    ]
    list_record = next(
        record for record, kind in zip(records, kinds, strict=True) if kind == "shopping_list"
    )
    assert isinstance(revision_record, dict)
    assert isinstance(list_record, dict)
    assert revision_record["immutable"] is True
    assert all(
        isinstance(record, dict) and record["immutable"] is True for record in snapshot_records
    )
    assert {record["id"] for record in contribution_records if isinstance(record, dict)} == {
        record["shopping_contribution_id"]
        for record in snapshot_records
        if isinstance(record, dict)
    }
    assert {record["id"] for record in row_records if isinstance(record, dict)} == {
        record["shopping_ingredient_row_id"]
        for record in contribution_records
        if isinstance(record, dict)
    }
    assert list_record["current_generation_revision_id"] == str(refreshed.generation_revision_id)
