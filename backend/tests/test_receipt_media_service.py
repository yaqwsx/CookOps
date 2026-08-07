import asyncio
import hashlib
import io
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import insert, select, update
from test_create_event_service import ServiceDatabase, context, event_command
from test_create_event_service import service_database as create_event_service_database

from cookops.application.events import create_event
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.application.receipt_media import (
    CreateReceiptAttachmentCommand,
    FinalizeReceiptAttachmentCommand,
    IssueReceiptAttachmentUploadTicketCommand,
    SetReceiptAttachmentLifecycleCommand,
    create_receipt_attachment,
    finalize_receipt_attachment,
    issue_receipt_attachment_upload_ticket,
    set_receipt_attachment_lifecycle,
)
from cookops.application.receipts import CreateReceiptCommand, create_receipt
from cookops.media_storage import LocalReceiptMediaStorage
from cookops.persistence.models import (
    ClientInstallation,
    Event,
    EventArchiveSnapshot,
    MediaUploadTicket,
    Mutation,
    OrganizationChange,
    ReceiptAttachment,
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


def _receipt(database: ServiceDatabase, event_id: UUID) -> UUID:
    receipt_id = uuid4()
    asyncio.run(
        create_receipt(
            database.sessions,
            context(database),
            CreateReceiptCommand(
                uuid4(),
                receipt_id,
                database.organization_id,
                event_id,
                "Corner shop",
                Decimal("42"),
                datetime.now(UTC),
            ),
        )
    )
    return receipt_id


def _create(
    database: ServiceDatabase, receipt_id: UUID, **changes: object
) -> CreateReceiptAttachmentCommand:
    values: dict[str, object] = {
        "mutation_id": uuid4(),
        "attachment_id": uuid4(),
        "organization_id": database.organization_id,
        "receipt_id": receipt_id,
        "media_type": "image/jpeg",
        "position_key": "a",
        "client_wall_time": datetime.now(UTC),
    }
    values.update(changes)
    return CreateReceiptAttachmentCommand(**values)  # type: ignore[arg-type]


def _issue(
    database: ServiceDatabase, created: CreateReceiptAttachmentCommand, **changes: object
) -> IssueReceiptAttachmentUploadTicketCommand:
    values: dict[str, object] = {
        "mutation_id": uuid4(),
        "attachment_id": created.attachment_id,
        "organization_id": database.organization_id,
        "receipt_id": created.receipt_id,
        "client_wall_time": datetime.now(UTC),
    }
    values.update(changes)
    return IssueReceiptAttachmentUploadTicketCommand(**values)  # type: ignore[arg-type]


def _jpeg(color: str = "black") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 2), color=color).save(output, format="JPEG")
    return output.getvalue()


def test_member_creates_pending_attachment_and_reissues_without_secret_replay(
    service_database: ServiceDatabase,
) -> None:
    created_command = _create(
        service_database, _receipt(service_database, _event(service_database))
    )
    created = asyncio.run(
        create_receipt_attachment(
            service_database.sessions, context(service_database), created_command
        )
    )
    assert created.storage_state == "pending"
    assert created.ticket_id is not None and created.ticket_secret is not None
    assert created.ticket_expires_at is not None
    replayed_create = asyncio.run(
        create_receipt_attachment(
            service_database.sessions, context(service_database), created_command
        )
    )
    assert replayed_create.replayed and replayed_create.ticket_secret is None

    issue_command = _issue(service_database, created_command)
    replacement = asyncio.run(
        issue_receipt_attachment_upload_ticket(
            service_database.sessions, context(service_database), issue_command
        )
    )
    assert replacement.storage_state == "pending"
    assert replacement.ticket_id is not None and replacement.ticket_secret is not None
    assert replacement.ticket_id != created.ticket_id
    replayed_issue = asyncio.run(
        issue_receipt_attachment_upload_ticket(
            service_database.sessions, context(service_database), issue_command
        )
    )
    assert replayed_issue.replayed and replayed_issue.ticket_secret is None
    with service_database.sync_engine.connect() as connection:
        attachment = connection.execute(
            select(ReceiptAttachment.storage_state).where(
                ReceiptAttachment.id == created_command.attachment_id
            )
        ).scalar_one()
        assert attachment == "pending"
        tickets = connection.execute(
            select(MediaUploadTicket.id, MediaUploadTicket.secret_hmac, MediaUploadTicket.used_at)
            .where(MediaUploadTicket.receipt_attachment_id == created_command.attachment_id)
            .order_by(MediaUploadTicket.created_at)
        ).all()
        assert len(tickets) == 2
        assert tickets[0].id == created.ticket_id and tickets[0].used_at is not None
        assert tickets[0].secret_hmac != created.ticket_secret.encode()
        assert tickets[1].id == replacement.ticket_id and tickets[1].used_at is None
        assert (
            connection.scalar(
                select(Mutation.outcome).where(Mutation.id == issue_command.mutation_id)
            )
            == "accepted"
        )
        assert (
            connection.scalar(
                select(OrganizationChange.entity_kind).where(
                    OrganizationChange.mutation_id == issue_command.mutation_id
                )
            )
            == "receipt_attachment"
        )


def test_expired_ticket_lost_response_recovers_with_new_secret(
    service_database: ServiceDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _create(service_database, _receipt(service_database, _event(service_database)))
    issued = asyncio.run(
        create_receipt_attachment(service_database.sessions, context(service_database), command)
    )
    assert issued.ticket_id is not None and issued.ticket_secret is not None
    assert issued.ticket_expires_at is not None
    monkeypatch.setattr(
        "cookops.application.receipt_media._server_now",
        lambda: issued.ticket_expires_at + timedelta(seconds=1),
    )
    replacement = asyncio.run(
        issue_receipt_attachment_upload_ticket(
            service_database.sessions, context(service_database), _issue(service_database, command)
        )
    )
    assert replacement.ticket_secret is not None
    assert replacement.ticket_secret != issued.ticket_secret
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(
                select(ReceiptAttachment.storage_state).where(
                    ReceiptAttachment.id == command.attachment_id
                )
            )
            == "pending"
        )


def test_finalization_only_accepts_server_measured_staged_bytes_and_replays(
    service_database: ServiceDatabase, tmp_path: Path
) -> None:
    receipt_id = _receipt(service_database, _event(service_database))
    created_command = _create(service_database, receipt_id)
    created = asyncio.run(
        create_receipt_attachment(
            service_database.sessions, context(service_database), created_command
        )
    )
    assert created.ticket_secret is not None
    storage = LocalReceiptMediaStorage(tmp_path)
    reissued = asyncio.run(
        issue_receipt_attachment_upload_ticket(
            service_database.sessions,
            context(service_database),
            _issue(service_database, created_command),
        )
    )
    assert reissued.ticket_secret is not None
    finalize = FinalizeReceiptAttachmentCommand(
        uuid4(),
        created_command.attachment_id,
        service_database.organization_id,
        receipt_id,
        reissued.ticket_secret,
        datetime.now(UTC),
    )
    staged = storage.stage(storage.new_stage_path(), [_jpeg()], 2_000_000)
    finalized = asyncio.run(
        finalize_receipt_attachment(
            service_database.sessions,
            context(service_database),
            finalize,
            staged,
            storage,
        )
    )
    assert (finalized.storage_state, finalized.media_type, finalized.pixel_width) == (
        "ready",
        "image/jpeg",
        3,
    )
    replay_stage = storage.stage(storage.new_stage_path(), [_jpeg()], 2_000_000)
    replayed = asyncio.run(
        finalize_receipt_attachment(
            service_database.sessions,
            context(service_database),
            finalize,
            replay_stage,
            storage,
        )
    )
    replay_stage.path.unlink()
    assert replayed.replayed
    changed_stage = storage.stage(storage.new_stage_path(), [_jpeg("white")], 2_000_000)
    with pytest.raises(ApplicationServiceError, match="idempotency_mismatch"):
        asyncio.run(
            finalize_receipt_attachment(
                service_database.sessions,
                context(service_database),
                finalize,
                changed_stage,
                storage,
            )
        )
    changed_stage.path.unlink()
    with service_database.sync_engine.connect() as connection:
        record = connection.scalar(
            select(OrganizationChange.payload).where(
                OrganizationChange.mutation_id == finalize.mutation_id
            )
        )
        assert record is not None
        attachment_record = cast(dict[str, object], record["record"])
        assert attachment_record["storage_state"] == "ready"
        assert "storage_object_key" not in attachment_record
        source_hash, source_size = connection.execute(
            select(ReceiptAttachment.source_content_hash, ReceiptAttachment.source_byte_size).where(
                ReceiptAttachment.id == created_command.attachment_id
            )
        ).one()
        assert (source_hash, source_size) == (hashlib.sha256(_jpeg()).digest(), len(_jpeg()))


def test_replacement_retires_previous_attachment_and_lifecycle_can_restore(
    service_database: ServiceDatabase, tmp_path: Path
) -> None:
    receipt_id = _receipt(service_database, _event(service_database))
    storage = LocalReceiptMediaStorage(tmp_path)
    first_command = _create(service_database, receipt_id)
    first = asyncio.run(
        create_receipt_attachment(
            service_database.sessions, context(service_database), first_command
        )
    )
    assert first.ticket_secret is not None
    asyncio.run(
        finalize_receipt_attachment(
            service_database.sessions,
            context(service_database),
            FinalizeReceiptAttachmentCommand(
                uuid4(),
                first_command.attachment_id,
                service_database.organization_id,
                receipt_id,
                first.ticket_secret,
                datetime.now(UTC),
            ),
            storage.stage(storage.new_stage_path(), [_jpeg()], 2_000_000),
            storage,
        )
    )
    second_command = _create(service_database, receipt_id, position_key="b")
    second = asyncio.run(
        create_receipt_attachment(
            service_database.sessions, context(service_database), second_command
        )
    )
    assert second.ticket_secret is not None
    finalized = asyncio.run(
        finalize_receipt_attachment(
            service_database.sessions,
            context(service_database),
            FinalizeReceiptAttachmentCommand(
                uuid4(),
                second_command.attachment_id,
                service_database.organization_id,
                receipt_id,
                second.ticket_secret,
                datetime.now(UTC),
                first_command.attachment_id,
            ),
            storage.stage(storage.new_stage_path(), [_jpeg()], 2_000_000),
            storage,
        )
    )
    assert finalized.last_change_sequence == finalized.first_change_sequence + 1
    restored = asyncio.run(
        set_receipt_attachment_lifecycle(
            service_database.sessions,
            context(service_database),
            SetReceiptAttachmentLifecycleCommand(
                uuid4(),
                first_command.attachment_id,
                service_database.organization_id,
                receipt_id,
                "restore",
                datetime.now(UTC),
            ),
        )
    )
    assert restored.retired_at is None
    with service_database.sync_engine.connect() as connection:
        states: dict[UUID, datetime | None] = {
            row[0]: row[1]
            for row in connection.execute(
                select(ReceiptAttachment.id, ReceiptAttachment.retired_at).where(
                    ReceiptAttachment.id.in_(
                        [first_command.attachment_id, second_command.attachment_id]
                    )
                )
            ).all()
        }
        assert states[first_command.attachment_id] is None
        assert states[second_command.attachment_id] is None


def test_ticket_is_bound_to_issuing_user_and_oauth_context(
    service_database: ServiceDatabase,
) -> None:
    receipt_id = _receipt(service_database, _event(service_database))
    owner_installation_id, other_installation_id = uuid4(), uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(ClientInstallation),
            [
                {
                    "id": owner_installation_id,
                    "user_id": service_database.actor_id,
                    "installation_kind": "agent",
                },
                {
                    "id": other_installation_id,
                    "user_id": service_database.actor_id,
                    "installation_kind": "agent",
                },
            ],
        )
    owner_context = ExecutionContext(
        service_database.actor_id, owner_installation_id, "agent-client", "agent-grant"
    )
    command = _create(service_database, receipt_id)
    issued = asyncio.run(
        create_receipt_attachment(service_database.sessions, owner_context, command)
    )
    assert issued.ticket_id is not None
    other_context = ExecutionContext(
        service_database.actor_id, other_installation_id, "other-client", "other-grant"
    )
    replacement = asyncio.run(
        issue_receipt_attachment_upload_ticket(
            service_database.sessions, other_context, _issue(service_database, command)
        )
    )
    assert replacement.ticket_id is not None
    with service_database.sync_engine.connect() as connection:
        original, later = connection.execute(
            select(
                MediaUploadTicket.user_id,
                MediaUploadTicket.oauth_client_id,
                MediaUploadTicket.oauth_grant_id,
                MediaUploadTicket.used_at,
            )
            .where(MediaUploadTicket.receipt_attachment_id == command.attachment_id)
            .order_by(MediaUploadTicket.created_at)
        ).all()
        assert (
            original[0],
            original[1],
            original[2],
            original[3] is not None,
        ) == (service_database.actor_id, "agent-client", "agent-grant", True)
        assert later == (service_database.actor_id, "other-client", "other-grant", None)


def test_archived_event_rejects_create_and_ticket_issue(service_database: ServiceDatabase) -> None:
    event_id = _event(service_database)
    receipt_id = _receipt(service_database, event_id)
    created_command = _create(service_database, receipt_id)
    asyncio.run(
        create_receipt_attachment(
            service_database.sessions, context(service_database), created_command
        )
    )
    now, snapshot_id = datetime.now(UTC), uuid4()
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=snapshot_id,
                event_id=event_id,
                archive_schema_version=1,
                payload={},
                attachment_manifest=[],
                content_hash=hashlib.sha256(b"archive").digest(),
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
            create_receipt_attachment(
                service_database.sessions,
                context(service_database),
                _create(service_database, receipt_id),
            )
        )
    with pytest.raises(ApplicationServiceError, match="archived_event"):
        asyncio.run(
            issue_receipt_attachment_upload_ticket(
                service_database.sessions,
                context(service_database),
                _issue(service_database, created_command),
            )
        )


@pytest.mark.parametrize(
    "media_type,position_key",
    [
        ("image/png", "a"),
        ("image/jpeg", ""),
        ("image/jpeg", "not allowed"),
        ("image/jpeg", "a" * 256),
        (cast(str, True), "a"),
    ],
)
def test_create_validation_fuzz_is_retained_without_ticket(
    service_database: ServiceDatabase, media_type: str, position_key: str
) -> None:
    command = _create(
        service_database,
        _receipt(service_database, _event(service_database)),
        media_type=media_type,
        position_key=position_key,
    )
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(
            create_receipt_attachment(service_database.sessions, context(service_database), command)
        )
    with service_database.sync_engine.connect() as connection:
        assert (
            connection.scalar(select(Mutation.outcome).where(Mutation.id == command.mutation_id))
            == "rejected"
        )
        assert (
            connection.scalar(
                select(MediaUploadTicket.id).where(
                    MediaUploadTicket.receipt_attachment_id == command.attachment_id
                )
            )
            is None
        )
