"""Receipt-attachment identities and short-lived upload capabilities.

There is deliberately no public attachment finalization command here.  Until a
trusted storage adapter validates and persists bytes, clients must not be able
to claim that an attachment is ready merely by supplying metadata.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import _reserve_change_range
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.application.recipes import _authorize_and_lock_organization
from cookops.media_storage import LocalReceiptMediaStorage, StagedReceiptImage
from cookops.persistence.models import (
    Event,
    MediaUploadTicket,
    Mutation,
    OrganizationChange,
    Receipt,
    ReceiptAttachment,
)

COMMAND_SCHEMA_VERSION = 1
CREATE_COMMAND_KIND: Literal["receipt_attachment.create"] = "receipt_attachment.create"
ISSUE_TICKET_COMMAND_KIND: Literal["receipt_attachment.upload_ticket.issue"] = (
    "receipt_attachment.upload_ticket.issue"
)
MAXIMUM_IMAGE_BYTES = 2_000_000
UPLOAD_TICKET_LIFETIME = timedelta(minutes=15)
_MEDIA_TYPES = ("image/jpeg", "image/webp")


def _server_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CreateReceiptAttachmentCommand:
    mutation_id: UUID
    attachment_id: UUID
    organization_id: UUID
    receipt_id: UUID
    media_type: Literal["image/jpeg", "image/webp"]
    position_key: str
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class IssueReceiptAttachmentUploadTicketCommand:
    """Issue a replacement capability when a response was lost or it expired.

    Reissuing invalidates every still-live ticket for this attachment.  A raw
    secret is only ever returned by the live call that minted it, never from an
    idempotency replay.
    """

    mutation_id: UUID
    attachment_id: UUID
    organization_id: UUID
    receipt_id: UUID
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ReceiptAttachmentResult:
    mutation_id: UUID
    attachment_id: UUID
    organization_id: UUID
    receipt_id: UUID
    storage_state: Literal["pending"]
    media_type: Literal["image/jpeg", "image/webp"]
    position_key: str
    ticket_id: UUID | None
    ticket_secret: str | None
    ticket_expires_at: datetime | None
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class FinalizeReceiptAttachmentCommand:
    mutation_id: UUID
    attachment_id: UUID
    organization_id: UUID
    receipt_id: UUID
    ticket_secret: str
    client_wall_time: datetime
    replaces_attachment_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FinalizedReceiptAttachmentResult:
    attachment_id: UUID
    organization_id: UUID
    receipt_id: UUID
    storage_state: Literal["ready"]
    media_type: Literal["image/jpeg", "image/webp"]
    byte_size: int
    pixel_width: int
    pixel_height: int
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class SetReceiptAttachmentLifecycleCommand:
    mutation_id: UUID
    attachment_id: UUID
    organization_id: UUID
    receipt_id: UUID
    operation: Literal["retire", "restore"]
    client_wall_time: datetime


@dataclass(frozen=True, slots=True)
class ReceiptAttachmentLifecycleResult:
    attachment_id: UUID
    retired_at: datetime | None
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _Prepared:
    mutation_id: UUID
    command_kind: Literal["receipt_attachment.create", "receipt_attachment.upload_ticket.issue"]
    organization_id: UUID
    receipt_id: UUID
    attachment_id: UUID
    media_type: Literal["image/jpeg", "image/webp"] | None
    position_key: str | None
    client_wall_time: datetime
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


def _invalid(value: object) -> dict[str, str]:
    return {"invalid_type": type(value).__qualname__, "repr": repr(value)}


def _raw_uuid(value: object) -> str | dict[str, str] | None:
    return str(value) if isinstance(value, UUID) else _invalid(value)


def _raw_time(value: object) -> str | dict[str, str]:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _invalid(value)


def _raw_text(value: object) -> str | dict[str, str]:
    if not isinstance(value, str):
        return _invalid(value)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return _invalid(value)
    return value


def _kind(
    command: CreateReceiptAttachmentCommand | IssueReceiptAttachmentUploadTicketCommand,
) -> Literal["receipt_attachment.create", "receipt_attachment.upload_ticket.issue"]:
    return (
        CREATE_COMMAND_KIND
        if isinstance(command, CreateReceiptAttachmentCommand)
        else ISSUE_TICKET_COMMAND_KIND
    )


def _hash(
    command: CreateReceiptAttachmentCommand | IssueReceiptAttachmentUploadTicketCommand,
) -> bytes:
    request: dict[str, object] = {
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "command_kind": _kind(command),
        "mutation_id": _raw_uuid(command.mutation_id),
        "organization_id": _raw_uuid(command.organization_id),
        "receipt_id": _raw_uuid(command.receipt_id),
        "attachment_id": _raw_uuid(command.attachment_id),
        "client_wall_time": _raw_time(command.client_wall_time),
        "logical_operation_id": _raw_uuid(command.logical_operation_id),
    }
    if isinstance(command, CreateReceiptAttachmentCommand):
        request |= {
            "media_type": _raw_text(command.media_type),
            "position_key": _raw_text(command.position_key),
        }
    return hashlib.sha256(
        json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()


def _prepare(
    command: CreateReceiptAttachmentCommand | IssueReceiptAttachmentUploadTicketCommand,
) -> _Prepared:
    violations: list[FieldViolation] = []
    for name in ("mutation_id", "organization_id", "receipt_id", "attachment_id"):
        if not isinstance(getattr(command, name), UUID):
            violations.append(FieldViolation(name, "must_be_uuid"))
    has_time = (
        isinstance(command.client_wall_time, datetime)
        and command.client_wall_time.tzinfo is not None
        and command.client_wall_time.utcoffset() is not None
    )
    if not has_time:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    media_type: Literal["image/jpeg", "image/webp"] | None = None
    position_key: str | None = None
    if isinstance(command, CreateReceiptAttachmentCommand):
        media_type = command.media_type if command.media_type in _MEDIA_TYPES else None
        if media_type is None:
            violations.append(FieldViolation("media_type", "must_be_supported_image"))
        position_key = command.position_key if isinstance(command.position_key, str) else None
        if (
            position_key is None
            or not position_key
            or len(position_key) > 255
            or not position_key.isascii()
            or not position_key.isalnum()
        ):
            violations.append(FieldViolation("position_key", "must_be_ascii_alphanumeric_position"))
    return _Prepared(
        command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        _kind(command),
        command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0),
        command.receipt_id if isinstance(command.receipt_id, UUID) else UUID(int=0),
        command.attachment_id if isinstance(command.attachment_id, UUID) else UUID(int=0),
        media_type,
        position_key,
        command.client_wall_time.astimezone(UTC) if has_time else datetime(1970, 1, 1, tzinfo=UTC),
        command.logical_operation_id if isinstance(command.logical_operation_id, UUID) else None,
        tuple(violations),
    )


def _error_payload(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [
                {"path": item.path, "code": item.code} for item in error.field_violations
            ],
        }
    }


def _record(attachment: ReceiptAttachment) -> dict[str, object]:
    return {
        "id": str(attachment.id),
        "organization_id": str(attachment.organization_id),
        "receipt_id": str(attachment.receipt_id),
        "storage_state": attachment.storage_state,
        "media_type": attachment.media_type,
        "position_key": attachment.position_key,
        "byte_size": attachment.byte_size,
        "pixel_width": attachment.pixel_width,
        "pixel_height": attachment.pixel_height,
        "finalized_at": attachment.finalized_at.isoformat() if attachment.finalized_at else None,
        "created_at": attachment.created_at.isoformat(),
        "created_by_user_id": str(attachment.created_by_user_id),
        "retired_at": attachment.retired_at.isoformat() if attachment.retired_at else None,
        "retired_by_user_id": str(attachment.retired_by_user_id)
        if attachment.retired_by_user_id
        else None,
    }


def _result(
    prepared: _Prepared,
    attachment: ReceiptAttachment,
    first: int,
    last: int,
    *,
    ticket: MediaUploadTicket | None = None,
    secret: str | None = None,
    replayed: bool = False,
) -> ReceiptAttachmentResult:
    return ReceiptAttachmentResult(
        prepared.mutation_id,
        attachment.id,
        attachment.organization_id,
        attachment.receipt_id,
        cast(Literal["pending"], attachment.storage_state),
        cast(Literal["image/jpeg", "image/webp"], attachment.media_type),
        attachment.position_key,
        ticket.id if ticket else None,
        secret,
        ticket.expires_at if ticket else None,
        first,
        last,
        replayed,
    )


def _result_payload(result: ReceiptAttachmentResult) -> dict[str, object]:
    # The raw secret must never be retained: retry with a fresh issue command.
    return {
        "attachment": {
            "id": str(result.attachment_id),
            "organization_id": str(result.organization_id),
            "receipt_id": str(result.receipt_id),
            "storage_state": result.storage_state,
            "media_type": result.media_type,
            "position_key": result.position_key,
        },
        "ticket_id": str(result.ticket_id) if result.ticket_id else None,
        "ticket_expires_at": result.ticket_expires_at.isoformat()
        if result.ticket_expires_at
        else None,
    }


def _retained_result(prepared: _Prepared, mutation: Mutation) -> ReceiptAttachmentResult:
    payload, attachment = (
        mutation.outcome_payload or {},
        (mutation.outcome_payload or {}).get("attachment"),
    )
    if (
        not isinstance(attachment, dict)
        or mutation.first_change_sequence is None
        or mutation.last_change_sequence is None
    ):
        raise RuntimeError("Retained receipt attachment mutation has an invalid result payload")
    try:
        expires = payload.get("ticket_expires_at")
        return ReceiptAttachmentResult(
            prepared.mutation_id,
            UUID(cast(str, attachment["id"])),
            UUID(cast(str, attachment["organization_id"])),
            UUID(cast(str, attachment["receipt_id"])),
            cast(Literal["pending"], attachment["storage_state"]),
            cast(Literal["image/jpeg", "image/webp"], attachment["media_type"]),
            cast(str, attachment["position_key"]),
            UUID(cast(str, payload["ticket_id"]))
            if isinstance(payload.get("ticket_id"), str)
            else None,
            None,
            datetime.fromisoformat(expires) if isinstance(expires, str) else None,
            mutation.first_change_sequence,
            mutation.last_change_sequence,
            True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "Retained receipt attachment mutation has an invalid result payload"
        ) from error


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    error = (mutation.outcome_payload or {}).get("error")
    if not isinstance(error, dict) or error.get("code") not in (
        "validation_failed",
        "archived_event",
        "client_time_too_far_ahead",
    ):
        raise RuntimeError("Retained receipt attachment mutation has invalid rejection payload")
    raw = error.get("field_violations", [])
    if not isinstance(raw, list):
        raise RuntimeError("Retained receipt attachment mutation has invalid violations")
    violations = tuple(
        FieldViolation(cast(str, item["path"]), cast(str, item["code"]))
        for item in raw
        if isinstance(item, dict)
    )
    if len(violations) != len(raw):
        raise RuntimeError("Retained receipt attachment mutation has invalid violations")
    return ApplicationServiceError(
        cast(
            Literal["validation_failed", "archived_event", "client_time_too_far_ahead"],
            error["code"],
        ),
        field_violations=violations,
        retry_same_identity=False,
    )


def _mutation(
    prepared: _Prepared,
    context: ExecutionContext,
    role: Literal["member", "organization_admin", "system_admin"],
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=prepared.mutation_id,
        logical_operation_id=prepared.logical_operation_id,
        organization_id=prepared.organization_id,
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=prepared.client_wall_time,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=prepared.command_kind,
        target_identities=[
            {"entity_kind": "receipt", "entity_id": str(prepared.receipt_id)},
            {"entity_kind": "receipt_attachment", "entity_id": str(prepared.attachment_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


async def _load_receipt_and_event(
    session: AsyncSession, organization_id: UUID, receipt_id: UUID
) -> tuple[Receipt, Event] | None:
    row = (
        await session.execute(
            select(Receipt, Event)
            .join(Event, Event.id == Receipt.event_id)
            .where(
                Receipt.id == receipt_id,
                Receipt.organization_id == organization_id,
            )
            .with_for_update(of=(Receipt, Event))
        )
    ).one_or_none()
    return (row[0], row[1]) if row is not None else None


def _issue_ticket(
    context: ExecutionContext, attachment: ReceiptAttachment, now: datetime
) -> tuple[MediaUploadTicket, str]:
    secret = secrets.token_urlsafe(32)
    return MediaUploadTicket(
        id=uuid4(),
        receipt_attachment_id=attachment.id,
        user_id=context.actor_user_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        secret_hmac=hashlib.sha256(secret.encode("ascii")).digest(),
        media_type=attachment.media_type,
        maximum_byte_size=MAXIMUM_IMAGE_BYTES,
        created_at=now,
        expires_at=now + UPLOAD_TICKET_LIFETIME,
    ), secret


async def _apply(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateReceiptAttachmentCommand | IssueReceiptAttachmentUploadTicketCommand,
) -> ReceiptAttachmentResult:
    prepared, request_hash = _prepare(command), _hash(command)
    deferred: ApplicationServiceError | None = None
    result: ReceiptAttachmentResult | None = None
    attachment_for_change: ReceiptAttachment | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_and_lock_organization(session, context, prepared.organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", prepared.mutation_id)},
        )
        retained = await session.get(Mutation, prepared.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != prepared.command_kind
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "accepted":
                return _retained_result(prepared, retained)
            if retained.outcome == "rejected":
                deferred = _retained_error(retained)
            else:
                raise RuntimeError("Receipt attachment mutation retained an unsupported outcome")
        elif prepared.violations:
            deferred = ApplicationServiceError(
                "validation_failed", field_violations=prepared.violations, retry_same_identity=False
            )
        elif prepared.client_wall_time > _server_now() + timedelta(hours=24):
            deferred = ApplicationServiceError(
                "client_time_too_far_ahead", retry_same_identity=False
            )
        if deferred is None and retained is None:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("receipt_attachment", prepared.attachment_id)},
            )
            receipt_and_event = await _load_receipt_and_event(
                session, prepared.organization_id, prepared.receipt_id
            )
            if receipt_and_event is None or receipt_and_event[0].retired_at is not None:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation("receipt_id", "must_be_active_in_active_event"),
                    ),
                    retry_same_identity=False,
                )
            elif receipt_and_event[1].lifecycle != "active":
                deferred = ApplicationServiceError("archived_event", retry_same_identity=False)
            elif prepared.command_kind == CREATE_COMMAND_KIND:
                if (
                    await session.get(
                        ReceiptAttachment, prepared.attachment_id, with_for_update=True
                    )
                    is not None
                ):
                    deferred = ApplicationServiceError(
                        "validation_failed",
                        field_violations=(FieldViolation("attachment_id", "already_exists"),),
                        retry_same_identity=False,
                    )
                else:
                    now = _server_now()
                    attachment_for_change = ReceiptAttachment(
                        id=prepared.attachment_id,
                        organization_id=prepared.organization_id,
                        receipt_id=prepared.receipt_id,
                        storage_state="pending",
                        media_type=prepared.media_type,
                        position_key=prepared.position_key,
                        created_at=now,
                        created_by_user_id=context.actor_user_id,
                    )
                    session.add(attachment_for_change)
                    await session.flush()
                    ticket, secret = _issue_ticket(context, attachment_for_change, now)
                    session.add(ticket)
                    first, last = await _reserve_change_range(
                        session, prepared.organization_id, prepared.mutation_id, 1
                    )
                    result = _result(
                        prepared, attachment_for_change, first, last, ticket=ticket, secret=secret
                    )
            else:
                attachment_for_change = await session.scalar(
                    select(ReceiptAttachment)
                    .where(
                        ReceiptAttachment.id == prepared.attachment_id,
                        ReceiptAttachment.organization_id == prepared.organization_id,
                        ReceiptAttachment.receipt_id == prepared.receipt_id,
                    )
                    .with_for_update(of=ReceiptAttachment)
                )
                if (
                    attachment_for_change is None
                    or attachment_for_change.storage_state != "pending"
                    or attachment_for_change.retired_at is not None
                ):
                    deferred = ApplicationServiceError(
                        "validation_failed",
                        field_violations=(
                            FieldViolation("attachment_id", "must_be_pending_active_attachment"),
                        ),
                        retry_same_identity=False,
                    )
                else:
                    now = _server_now()
                    # The schema predates an explicit revoked timestamp.  Marking a live
                    # ticket used is the supported irreversible invalidation primitive.
                    active_tickets = (
                        await session.scalars(
                            select(MediaUploadTicket)
                            .where(
                                MediaUploadTicket.receipt_attachment_id == attachment_for_change.id,
                                MediaUploadTicket.used_at.is_(None),
                                MediaUploadTicket.expires_at > now,
                            )
                            .with_for_update(of=MediaUploadTicket)
                        )
                    ).all()
                    for old_ticket in active_tickets:
                        old_ticket.used_at = now
                    ticket, secret = _issue_ticket(context, attachment_for_change, now)
                    session.add(ticket)
                    first, last = await _reserve_change_range(
                        session, prepared.organization_id, prepared.mutation_id, 1
                    )
                    result = _result(
                        prepared, attachment_for_change, first, last, ticket=ticket, secret=secret
                    )
        if deferred is not None and retained is None:
            session.add(
                _mutation(
                    prepared, context, role, request_hash, "rejected", _error_payload(deferred)
                )
            )
        elif result is not None:
            if attachment_for_change is None:
                raise RuntimeError("Receipt attachment result has no change record")
            session.add(
                OrganizationChange(
                    organization_id=prepared.organization_id,
                    sequence=result.first_change_sequence,
                    mutation_id=prepared.mutation_id,
                    entity_id=result.attachment_id,
                    entity_kind="receipt_attachment",
                    operation="upsert",
                    payload={"record_schema_version": 1, "record": _record(attachment_for_change)},
                )
            )
            session.add(
                _mutation(
                    prepared,
                    context,
                    role,
                    request_hash,
                    "accepted",
                    _result_payload(result),
                    result.first_change_sequence,
                    result.last_change_sequence,
                )
            )
    if deferred is not None:
        raise deferred
    if result is None:
        raise RuntimeError("Receipt attachment mutation produced no outcome")
    return result


async def create_receipt_attachment(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateReceiptAttachmentCommand,
) -> ReceiptAttachmentResult:
    """Create a pending attachment and issue its initial one-time capability."""
    return await _apply(session_factory, context, command)


async def issue_receipt_attachment_upload_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: IssueReceiptAttachmentUploadTicketCommand,
) -> ReceiptAttachmentResult:
    """Issue a replacement upload capability without replaying an old secret."""
    return await _apply(session_factory, context, command)


async def finalize_receipt_attachment(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: FinalizeReceiptAttachmentCommand,
    staged: StagedReceiptImage,
    storage: LocalReceiptMediaStorage,
) -> FinalizedReceiptAttachmentResult:
    """Consume one ticket and publish only metadata measured from staged bytes."""
    if (
        not all(
            isinstance(value, UUID)
            for value in (
                command.mutation_id,
                command.attachment_id,
                command.organization_id,
                command.receipt_id,
            )
        )
        or not isinstance(command.ticket_secret, str)
        or not command.ticket_secret
        or not command.ticket_secret.isascii()
        or len(command.ticket_secret) > 128
    ):
        raise ApplicationServiceError(
            "validation_failed",
            retry_same_identity=False,
            field_violations=(FieldViolation("upload", "invalid"),),
        )
    if staged.media_type not in _MEDIA_TYPES or max(staged.width, staged.height) > 2000:
        raise ApplicationServiceError(
            "validation_failed",
            retry_same_identity=False,
            field_violations=(
                FieldViolation("image", "must_be_supported_and_at_most_2000_pixels"),
            ),
        )
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "attachment_id": str(command.attachment_id),
                "kind": "receipt_attachment.finalize",
                "organization_id": str(command.organization_id),
                "receipt_id": str(command.receipt_id),
                "replaces_attachment_id": str(command.replaces_attachment_id)
                if command.replaces_attachment_id
                else None,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).digest()
    now = _server_now()
    async with session_factory() as session, session.begin():
        role = await _authorize_and_lock_organization(session, context, command.organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != "receipt_attachment.finalize"
                or retained.request_hash != request_hash
                or retained.outcome != "accepted"
                or retained.first_change_sequence is None
                or retained.last_change_sequence is None
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            attachment = await session.get(ReceiptAttachment, command.attachment_id)
            if (
                attachment is None
                or attachment.storage_state != "ready"
                or attachment.content_hash != staged.content_hash
                or attachment.source_byte_size != staged.source_byte_size
                or attachment.source_content_hash != staged.source_content_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            return FinalizedReceiptAttachmentResult(
                attachment.id,
                attachment.organization_id,
                attachment.receipt_id,
                "ready",
                cast(Literal["image/jpeg", "image/webp"], attachment.media_type),
                cast(int, attachment.byte_size),
                cast(int, attachment.pixel_width),
                cast(int, attachment.pixel_height),
                retained.first_change_sequence,
                retained.last_change_sequence,
                True,
            )
        loaded = await _load_receipt_and_event(session, command.organization_id, command.receipt_id)
        attachment = await session.scalar(
            select(ReceiptAttachment)
            .where(
                ReceiptAttachment.id == command.attachment_id,
                ReceiptAttachment.organization_id == command.organization_id,
                ReceiptAttachment.receipt_id == command.receipt_id,
            )
            .with_for_update(of=ReceiptAttachment)
        )
        replaced = None
        if command.replaces_attachment_id is not None:
            replaced = await session.scalar(
                select(ReceiptAttachment)
                .where(
                    ReceiptAttachment.id == command.replaces_attachment_id,
                    ReceiptAttachment.organization_id == command.organization_id,
                    ReceiptAttachment.receipt_id == command.receipt_id,
                )
                .with_for_update(of=ReceiptAttachment)
            )
        ticket = await session.scalar(
            select(MediaUploadTicket)
            .where(
                MediaUploadTicket.receipt_attachment_id == command.attachment_id,
                MediaUploadTicket.secret_hmac
                == hashlib.sha256(command.ticket_secret.encode("ascii")).digest(),
            )
            .with_for_update(of=MediaUploadTicket)
        )
        if (
            loaded is None
            or loaded[0].retired_at is not None
            or loaded[1].lifecycle != "active"
            or attachment is None
            or attachment.storage_state != "pending"
            or attachment.retired_at is not None
            or ticket is None
            or ticket.user_id != context.actor_user_id
            or ticket.used_at is not None
            or ticket.expires_at <= now
            or ticket.media_type != staged.media_type
            or ticket.maximum_byte_size < staged.byte_size
            or (
                command.replaces_attachment_id is not None
                and (
                    replaced is None
                    or replaced.id == attachment.id
                    or replaced.storage_state != "ready"
                    or replaced.retired_at is not None
                )
            )
        ):
            raise ApplicationServiceError(
                "validation_failed",
                retry_same_identity=False,
                field_violations=(FieldViolation("upload", "not_permitted"),),
            )
        object_key, thumbnail_key = storage.promote(staged, attachment.id)
        attachment.storage_state = "ready"
        attachment.storage_object_key = object_key
        attachment.thumbnail_object_key = thumbnail_key
        attachment.byte_size = staged.byte_size
        attachment.pixel_width = staged.width
        attachment.pixel_height = staged.height
        attachment.content_hash = staged.content_hash
        attachment.source_byte_size = staged.source_byte_size
        attachment.source_content_hash = staged.source_content_hash
        attachment.finalized_at = now
        attachment.finalized_by_user_id = context.actor_user_id
        ticket.used_at = now
        if replaced is not None:
            replaced.retired_at = now
            replaced.retired_by_user_id = context.actor_user_id
        first, last = await _reserve_change_range(
            session, command.organization_id, command.mutation_id, 2 if replaced else 1
        )
        session.add(
            OrganizationChange(
                organization_id=command.organization_id,
                sequence=first,
                mutation_id=command.mutation_id,
                entity_id=attachment.id,
                entity_kind="receipt_attachment",
                operation="upsert",
                payload={"record_schema_version": 1, "record": _record(attachment)},
            )
        )
        if replaced is not None:
            session.add(
                OrganizationChange(
                    organization_id=command.organization_id,
                    sequence=last,
                    mutation_id=command.mutation_id,
                    entity_id=replaced.id,
                    entity_kind="receipt_attachment",
                    operation="upsert",
                    payload={"record_schema_version": 1, "record": _record(replaced)},
                )
            )
        session.add(
            Mutation(
                id=command.mutation_id,
                logical_operation_id=None,
                organization_id=command.organization_id,
                is_system_administration_scope=False,
                actor_user_id=context.actor_user_id,
                actor_role=role,
                client_installation_id=context.client_installation_id,
                oauth_client_id=context.oauth_client_id,
                oauth_grant_id=context.oauth_grant_id,
                client_wall_time=command.client_wall_time,
                command_schema_version=1,
                command_kind="receipt_attachment.finalize",
                target_identities=[
                    {"entity_kind": "receipt_attachment", "entity_id": str(attachment.id)}
                ] + (
                    [{"entity_kind": "receipt_attachment", "entity_id": str(replaced.id)}]
                    if replaced
                    else []
                ),
                request_hash=request_hash,
                outcome="accepted",
                outcome_payload={"attachment": _record(attachment)},
                first_change_sequence=first,
                last_change_sequence=last,
            )
        )
        return FinalizedReceiptAttachmentResult(
            attachment.id,
            attachment.organization_id,
            attachment.receipt_id,
            "ready",
            cast(Literal["image/jpeg", "image/webp"], attachment.media_type),
            staged.byte_size,
            staged.width,
            staged.height,
            first,
            last,
            False,
        )


async def set_receipt_attachment_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetReceiptAttachmentLifecycleCommand,
) -> ReceiptAttachmentLifecycleResult:
    """Retire or restore an attachment without ever rewriting finalized content."""
    if not all(
        isinstance(value, UUID)
        for value in (
            command.mutation_id,
            command.attachment_id,
            command.organization_id,
            command.receipt_id,
        )
    ) or command.operation not in ("retire", "restore"):
        raise ApplicationServiceError(
            "validation_failed",
            retry_same_identity=False,
            field_violations=(FieldViolation("attachment", "invalid"),),
        )
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "attachment_id": str(command.attachment_id),
                "kind": "receipt_attachment.lifecycle",
                "operation": command.operation,
                "organization_id": str(command.organization_id),
                "receipt_id": str(command.receipt_id),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).digest()
    async with session_factory() as session, session.begin():
        role = await _authorize_and_lock_organization(session, context, command.organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != "receipt_attachment.lifecycle"
                or retained.request_hash != request_hash
                or retained.outcome != "accepted"
                or retained.first_change_sequence is None
                or retained.last_change_sequence is None
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            attachment = await session.get(ReceiptAttachment, command.attachment_id)
            if attachment is None:
                raise RuntimeError("retained attachment lifecycle has no attachment")
            return ReceiptAttachmentLifecycleResult(
                attachment.id,
                attachment.retired_at,
                retained.first_change_sequence,
                retained.last_change_sequence,
                True,
            )
        loaded = await _load_receipt_and_event(session, command.organization_id, command.receipt_id)
        attachment = await session.scalar(
            select(ReceiptAttachment)
            .where(
                ReceiptAttachment.id == command.attachment_id,
                ReceiptAttachment.organization_id == command.organization_id,
                ReceiptAttachment.receipt_id == command.receipt_id,
            )
            .with_for_update(of=ReceiptAttachment)
        )
        if (
            loaded is None
            or loaded[0].retired_at is not None
            or loaded[1].lifecycle != "active"
            or attachment is None
            or (command.operation == "retire" and attachment.retired_at is not None)
            or (command.operation == "restore" and attachment.retired_at is None)
        ):
            raise ApplicationServiceError(
                "validation_failed",
                retry_same_identity=False,
                field_violations=(FieldViolation("attachment_id", "invalid_lifecycle_transition"),),
            )
        now = _server_now()
        if command.operation == "retire":
            attachment.retired_at = now
            attachment.retired_by_user_id = context.actor_user_id
        else:
            attachment.retired_at = None
            attachment.retired_by_user_id = None
        first, last = await _reserve_change_range(
            session, command.organization_id, command.mutation_id, 1
        )
        session.add(
            OrganizationChange(
                organization_id=command.organization_id,
                sequence=first,
                mutation_id=command.mutation_id,
                entity_id=attachment.id,
                entity_kind="receipt_attachment",
                operation="upsert",
                payload={"record_schema_version": 1, "record": _record(attachment)},
            )
        )
        session.add(
            Mutation(
                id=command.mutation_id,
                logical_operation_id=None,
                organization_id=command.organization_id,
                is_system_administration_scope=False,
                actor_user_id=context.actor_user_id,
                actor_role=role,
                client_installation_id=context.client_installation_id,
                oauth_client_id=context.oauth_client_id,
                oauth_grant_id=context.oauth_grant_id,
                client_wall_time=command.client_wall_time,
                command_schema_version=1,
                command_kind="receipt_attachment.lifecycle",
                target_identities=[
                    {"entity_kind": "receipt_attachment", "entity_id": str(attachment.id)}
                ],
                request_hash=request_hash,
                outcome="accepted",
                outcome_payload={"attachment": _record(attachment)},
                first_change_sequence=first,
                last_change_sequence=last,
            )
        )
        return ReceiptAttachmentLifecycleResult(
            attachment.id, attachment.retired_at, first, last, False
        )
