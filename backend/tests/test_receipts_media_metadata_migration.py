import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from cookops.persistence.models import (
    Event,
    MediaUploadTicket,
    Organization,
    Receipt,
    ReceiptAttachment,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

RECEIPT_TABLES = {"receipts", "receipt_attachments", "media_upload_tickets"}


@dataclass
class MigrationDatabase:
    configuration: Config
    engine: Engine


@pytest.fixture
def migration_database() -> Iterator[MigrationDatabase]:
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    command.downgrade(configuration, "base")
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    try:
        yield MigrationDatabase(configuration, engine)
    finally:
        engine.dispose()
        command.downgrade(configuration, "base")


def _seed(engine: Engine) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    actor_id, organization_id, other_organization_id = uuid4(), uuid4(), uuid4()
    event_id, euro_event_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Receipt tester",
                verified_email="receipt@example.test",
                normalized_email="receipt@example.test",
            )
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": organization_id,
                    "name": "Receipt organization",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": other_organization_id,
                    "name": "Other organization",
                    "created_by_user_id": actor_id,
                },
            ],
        )
        connection.execute(
            insert(Event),
            [
                {
                    "id": event_id,
                    "organization_id": organization_id,
                    "name": "CZK event",
                    "start_date": date(2026, 8, 1),
                    "end_date": date(2026, 8, 1),
                    "base_expected_attendance": 0,
                    "budget_amount": Decimal("0"),
                    "currency": "CZK",
                    "created_by_user_id": actor_id,
                },
                {
                    "id": euro_event_id,
                    "organization_id": organization_id,
                    "name": "EUR event",
                    "start_date": date(2026, 8, 2),
                    "end_date": date(2026, 8, 2),
                    "base_expected_attendance": 0,
                    "budget_amount": Decimal("0"),
                    "currency": "EUR",
                    "created_by_user_id": actor_id,
                },
            ],
        )
    return actor_id, organization_id, other_organization_id, event_id, euro_event_id


def _receipt(
    *, actor_id: UUID, organization_id: UUID, event_id: UUID, currency: str = "CZK"
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "organization_id": organization_id,
        "event_id": event_id,
        "title": "Corner shop",
        "total_amount": Decimal("123.45"),
        "currency": currency,
        "receipt_date": date(2026, 8, 1),
        "note": "Vegetables",
        "created_at": now,
        "created_by_user_id": actor_id,
        "last_modified_at": now,
        "last_modified_by_user_id": actor_id,
    }


def _pending_attachment(
    *, actor_id: UUID, organization_id: UUID, receipt_id: UUID
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "organization_id": organization_id,
        "receipt_id": receipt_id,
        "storage_state": "pending",
        "media_type": "image/jpeg",
        "position_key": "a",
        "created_by_user_id": actor_id,
    }


def test_receipt_media_schema_parity_and_downgrade(
    migration_database: MigrationDatabase,
) -> None:
    configuration, engine = migration_database.configuration, migration_database.engine
    command.upgrade(configuration, "head")
    assert set(inspect(engine).get_table_names()) >= RECEIPT_TABLES
    command.check(configuration)
    command.downgrade(configuration, "0013_shopping_list_foundation")
    assert RECEIPT_TABLES.isdisjoint(inspect(engine).get_table_names())


def test_receipts_stay_with_the_event_organization_currency_and_lifecycle(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    actor_id, organization_id, other_organization_id, event_id, euro_event_id = _seed(
        migration_database.engine
    )
    receipt = _receipt(actor_id=actor_id, organization_id=organization_id, event_id=event_id)
    with migration_database.engine.begin() as connection:
        connection.execute(insert(Receipt).values(**receipt))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(Receipt).values(
                    **_receipt(
                        actor_id=actor_id,
                        organization_id=organization_id,
                        event_id=euro_event_id,
                    )
                )
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(Receipt).values(
                    **_receipt(
                        actor_id=actor_id,
                        organization_id=other_organization_id,
                        event_id=event_id,
                    )
                )
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(Receipt).values(
                    **(
                        _receipt(
                            actor_id=actor_id,
                            organization_id=organization_id,
                            event_id=event_id,
                        )
                        | {"title": "  "}
                    )
                )
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(Receipt).values(
                    **(
                        _receipt(
                            actor_id=actor_id,
                            organization_id=organization_id,
                            event_id=event_id,
                        )
                        | {"total_amount": Decimal("-0.01")}
                    )
                )
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                update(Receipt)
                .where(Receipt.id == receipt["id"])
                .values(retired_at=datetime.now(UTC))
            )
        connection.execute(
            update(Receipt)
            .where(Receipt.id == receipt["id"])
            .values(retired_at=datetime.now(UTC), retired_by_user_id=actor_id)
        )


def test_attachment_metadata_is_tenant_bound_and_ready_content_is_immutable(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    actor_id, organization_id, other_organization_id, event_id, _ = _seed(migration_database.engine)
    receipt = _receipt(actor_id=actor_id, organization_id=organization_id, event_id=event_id)
    attachment = _pending_attachment(
        actor_id=actor_id,
        organization_id=organization_id,
        receipt_id=cast(UUID, receipt["id"]),
    )
    with migration_database.engine.begin() as connection:
        connection.execute(insert(Receipt).values(**receipt))
        connection.execute(insert(ReceiptAttachment).values(**attachment))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert(ReceiptAttachment).values(
                    **_pending_attachment(
                        actor_id=actor_id,
                        organization_id=other_organization_id,
                        receipt_id=cast(UUID, receipt["id"]),
                    )
                )
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                update(ReceiptAttachment)
                .where(ReceiptAttachment.id == attachment["id"])
                .values(storage_state="ready")
            )
        finalized_at = datetime.now(UTC)
        connection.execute(
            update(ReceiptAttachment)
            .where(ReceiptAttachment.id == attachment["id"])
            .values(
                storage_state="ready",
                storage_object_key="receipts/object.jpg",
                thumbnail_object_key="receipts/thumb.webp",
                byte_size=1024,
                pixel_width=100,
                pixel_height=200,
                content_hash=b"h" * 32,
                finalized_at=finalized_at,
                finalized_by_user_id=actor_id,
            )
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(ReceiptAttachment)
                .where(ReceiptAttachment.id == attachment["id"])
                .values(storage_object_key="receipts/replaced.jpg")
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(ReceiptAttachment)
                .where(ReceiptAttachment.id == attachment["id"])
                .values(media_type="image/webp")
            )
        connection.execute(
            update(ReceiptAttachment)
            .where(ReceiptAttachment.id == attachment["id"])
            .values(retired_at=datetime.now(UTC), retired_by_user_id=actor_id)
        )


def test_upload_ticket_is_bound_to_media_and_can_only_be_consumed_once(
    migration_database: MigrationDatabase,
) -> None:
    command.upgrade(migration_database.configuration, "head")
    actor_id, organization_id, _, event_id, _ = _seed(migration_database.engine)
    receipt = _receipt(actor_id=actor_id, organization_id=organization_id, event_id=event_id)
    attachment = _pending_attachment(
        actor_id=actor_id,
        organization_id=organization_id,
        receipt_id=cast(UUID, receipt["id"]),
    )
    now = datetime.now(UTC)
    ticket = {
        "id": uuid4(),
        "receipt_attachment_id": attachment["id"],
        "user_id": actor_id,
        "secret_hmac": b"s" * 32,
        "media_type": "image/jpeg",
        "maximum_byte_size": 2_000_000,
        "created_at": now,
        "expires_at": now + timedelta(minutes=10),
    }
    with migration_database.engine.begin() as connection:
        connection.execute(insert(Receipt).values(**receipt))
        connection.execute(insert(ReceiptAttachment).values(**attachment))
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                insert(MediaUploadTicket).values(**(ticket | {"media_type": "image/webp"}))
            )
            connection.exec_driver_sql(
                "SET CONSTRAINTS tr_media_upload_ticket_verify_attachment_media_type IMMEDIATE"
            )
        connection.execute(insert(MediaUploadTicket).values(**ticket))
        connection.execute(
            update(MediaUploadTicket)
            .where(MediaUploadTicket.id == ticket["id"])
            .values(used_at=now)
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(MediaUploadTicket)
                .where(MediaUploadTicket.id == ticket["id"])
                .values(used_at=now + timedelta(seconds=1))
            )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                update(MediaUploadTicket)
                .where(MediaUploadTicket.id == ticket["id"])
                .values(maximum_byte_size=1)
            )
