import asyncio
import hashlib
import os
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update
from test_create_event_service import ServiceDatabase, context, event_command
from test_create_event_service import service_database as create_event_service_database

from cookops.application.events import create_event
from cookops.application.organizations import ApplicationServiceError
from cookops.application.receipts import (
    CreateReceiptCommand,
    SetReceiptLifecycleCommand,
    UpdateReceiptCommand,
    create_receipt,
    set_receipt_lifecycle,
    update_receipt,
)
from cookops.persistence.models import (
    Event,
    EventArchiveSnapshot,
    FieldClock,
    Mutation,
    OrganizationChange,
    Receipt,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


@pytest.fixture
def service_database() -> Iterator[ServiceDatabase]:
    fixture = cast(
        Callable[[], Iterator[ServiceDatabase]], vars(create_event_service_database)["__wrapped__"]
    )
    yield from fixture()


def _event(database: ServiceDatabase) -> UUID:
    return asyncio.run(
        create_event(database.sessions, context(database), event_command(database))
    ).event_id


def _create(database: ServiceDatabase, event_id: UUID, **changes: object) -> CreateReceiptCommand:
    values: dict[str, object] = {
        "mutation_id": uuid4(),
        "receipt_id": uuid4(),
        "organization_id": database.organization_id,
        "event_id": event_id,
        "title": "  Corner shop  ",
        "total_amount": Decimal("123.45"),
        "receipt_date": date(2026, 7, 1),
        "note": "Vegetables\r\nfor lunch",
        "client_wall_time": datetime.now(UTC),
    }
    values.update(changes)
    return CreateReceiptCommand(**values)  # type: ignore[arg-type]


def _update(
    database: ServiceDatabase, created: CreateReceiptCommand, **changes: object
) -> UpdateReceiptCommand:
    values: dict[str, object] = {
        "mutation_id": uuid4(),
        "receipt_id": created.receipt_id,
        "organization_id": database.organization_id,
        "event_id": created.event_id,
        "title": "Market",
        "total_amount": Decimal("200"),
        "receipt_date": None,
        "note": "Fresh items",
        "client_wall_time": datetime.now(UTC),
    }
    values.update(changes)
    return UpdateReceiptCommand(**values)  # type: ignore[arg-type]


def test_member_creates_updates_and_replays_receipt_with_change_feed(
    service_database: ServiceDatabase,
) -> None:
    event_id = _event(service_database)
    created_command = _create(service_database, event_id)
    created = asyncio.run(
        create_receipt(service_database.sessions, context(service_database), created_command)
    )
    assert created.title == "Corner shop"
    assert created.currency == "EUR"
    assert created.note == "Vegetables\nfor lunch"
    assert created.replayed is False
    assert asyncio.run(
        create_receipt(service_database.sessions, context(service_database), created_command)
    ).replayed

    command = _update(service_database, created_command)
    result = asyncio.run(
        update_receipt(service_database.sessions, context(service_database), command)
    )
    assert result.outcome == "accepted"
    with service_database.sync_engine.connect() as connection:
        row = connection.execute(
            select(Receipt.title, Receipt.total_amount, Receipt.receipt_date, Receipt.note).where(
                Receipt.id == created.receipt_id
            )
        ).one()
        assert row == ("Market", Decimal("200"), None, "Fresh items")
        change_result = connection.execute(
            select(OrganizationChange.payload).where(
                OrganizationChange.mutation_id == command.mutation_id
            )
        )
        change = change_result.scalar_one()
        assert cast(dict[str, object], change["record"])["currency"] == "EUR"
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == command.mutation_id))
            == "accepted"
        )
        assert (
            connection.scalar(
                select(FieldClock.field_name).where(
                    FieldClock.entity_id == created.receipt_id, FieldClock.field_name == "title"
                )
            )
            == "title"
        )


def test_receipt_metadata_is_field_lww_and_tombstone_does_not_drop_late_offline_work(
    service_database: ServiceDatabase,
) -> None:
    event_id = _event(service_database)
    create = _create(
        service_database, event_id, client_wall_time=datetime.now(UTC) - timedelta(minutes=5)
    )
    asyncio.run(create_receipt(service_database.sessions, context(service_database), create))
    newer_time = datetime.now(UTC)
    newer = _update(
        service_database,
        create,
        title="New",
        total_amount=Decimal("5"),
        note="New note",
        client_wall_time=newer_time,
    )
    assert (
        asyncio.run(
            update_receipt(service_database.sessions, context(service_database), newer)
        ).outcome
        == "accepted"
    )
    older = _update(
        service_database,
        create,
        title="Old",
        total_amount=Decimal("1"),
        note="Old note",
        client_wall_time=newer_time - timedelta(seconds=1),
    )
    assert (
        asyncio.run(
            update_receipt(service_database.sessions, context(service_database), older)
        ).outcome
        == "partially_superseded"
    )
    retire = SetReceiptLifecycleCommand(
        uuid4(),
        create.receipt_id,
        service_database.organization_id,
        event_id,
        "retire",
        newer_time + timedelta(seconds=1),
    )
    assert (
        asyncio.run(
            set_receipt_lifecycle(service_database.sessions, context(service_database), retire)
        ).retired_at
        is not None
    )
    later_update = _update(
        service_database,
        create,
        title="Recovered",
        total_amount=Decimal("9"),
        client_wall_time=newer_time + timedelta(seconds=2),
    )
    asyncio.run(update_receipt(service_database.sessions, context(service_database), later_update))
    restore = SetReceiptLifecycleCommand(
        uuid4(),
        create.receipt_id,
        service_database.organization_id,
        event_id,
        "restore",
        newer_time + timedelta(seconds=3),
    )
    restored = asyncio.run(
        set_receipt_lifecycle(service_database.sessions, context(service_database), restore)
    )
    assert restored.retired_at is None and restored.title == "Recovered"


@pytest.mark.parametrize(
    "amount", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), cast(Decimal, True)]
)
def test_invalid_receipts_are_retained_rejections(
    service_database: ServiceDatabase, amount: Decimal
) -> None:
    command = _create(service_database, _event(service_database), total_amount=amount)
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(create_receipt(service_database.sessions, context(service_database), command))
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(create_receipt(service_database.sessions, context(service_database), command))
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == command.mutation_id))
            == "rejected"
        )


def test_archived_event_and_future_clock_reject_receipt_mutations(
    service_database: ServiceDatabase,
) -> None:
    event_id = _event(service_database)
    snapshot_id = uuid4()
    now = datetime.now(UTC)
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=snapshot_id,
                event_id=event_id,
                archive_schema_version=1,
                payload={},
                attachment_manifest=[],
                content_hash=hashlib.sha256(b"receipt-test").digest(),
                created_by_user_id=service_database.actor_id,
            )
        )
        connection.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=snapshot_id,
                archived_at=now,
                archived_by_user_id=service_database.actor_id,
            )
        )
    with pytest.raises(ApplicationServiceError, match="archived_event"):
        asyncio.run(
            create_receipt(
                service_database.sessions,
                context(service_database),
                _create(service_database, event_id),
            )
        )
    future = _create(
        service_database,
        _event(service_database),
        client_wall_time=datetime.now(UTC) + timedelta(hours=25),
    )
    with pytest.raises(ApplicationServiceError, match="client_time_too_far_ahead"):
        asyncio.run(create_receipt(service_database.sessions, context(service_database), future))


@pytest.mark.parametrize("title", ["", " \t", "x" * 201, "bad\x00title", "\ud800", cast(str, 3)])
def test_receipt_text_validation_fuzzes_common_invalid_shapes(
    service_database: ServiceDatabase, title: str
) -> None:
    command = _create(service_database, _event(service_database), title=title)
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(create_receipt(service_database.sessions, context(service_database), command))
