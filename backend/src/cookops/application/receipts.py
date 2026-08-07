"""Offline-safe receipt metadata commands.

Receipt attachments intentionally have a separate lifecycle.  This module only
owns the small, synchronizable metadata aggregate and never accepts image bytes
or storage keys.
"""

import hashlib
import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

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
from cookops.persistence.models import Event, FieldClock, Mutation, OrganizationChange, Receipt

COMMAND_SCHEMA_VERSION = 1
CREATE_COMMAND_KIND = "receipt.create"
UPDATE_COMMAND_KIND = "receipt.update"
LIFECYCLE_COMMAND_KIND = "receipt.lifecycle"
MAX_SERIALIZED_NOTE_BYTES = 131_072
_METADATA_FIELDS = ("title", "total_amount", "receipt_date", "note")
_LIFECYCLE_FIELD = "lifecycle"


@dataclass(frozen=True, slots=True)
class CreateReceiptCommand:
    mutation_id: UUID
    receipt_id: UUID
    organization_id: UUID
    event_id: UUID
    title: str
    total_amount: Decimal
    client_wall_time: datetime
    receipt_date: date | None = None
    note: str | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class UpdateReceiptCommand:
    """Replace receipt metadata; each scalar independently follows LWW."""

    mutation_id: UUID
    receipt_id: UUID
    organization_id: UUID
    event_id: UUID
    title: str
    total_amount: Decimal
    client_wall_time: datetime
    receipt_date: date | None = None
    note: str | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SetReceiptLifecycleCommand:
    mutation_id: UUID
    receipt_id: UUID
    organization_id: UUID
    event_id: UUID
    operation: Literal["retire", "restore"]
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ReceiptResult:
    mutation_id: UUID
    receipt_id: UUID
    organization_id: UUID
    event_id: UUID
    title: str
    total_amount: Decimal
    currency: str
    receipt_date: date | None
    note: str | None
    retired_at: datetime | None
    retired_by_user_id: UUID | None
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


@dataclass(frozen=True, slots=True)
class _Prepared:
    mutation_id: UUID
    receipt_id: UUID
    organization_id: UUID
    event_id: UUID
    command_kind: str
    client_wall_time: datetime
    title: str | None
    total_amount: Decimal | None
    receipt_date: date | None
    note: str | None
    operation: Literal["retire", "restore"] | None
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


def _canonical_note(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _canonical_decimal(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _is_utf8_encodable(value: str) -> bool:
    try:
        value.encode()
    except UnicodeEncodeError:
        return False
    return True


def _invalid(value: object) -> dict[str, str]:
    return {"invalid_type": type(value).__qualname__, "repr": repr(value)}


def _raw_text(value: object, canonicalize: Callable[[str], str]) -> str | dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return _invalid(value)
    canonical = canonicalize(value)
    return canonical if _is_utf8_encodable(canonical) else _invalid(value)


def _raw_uuid(value: object) -> str | dict[str, str] | None:
    if value is None:
        return None
    return str(value) if isinstance(value, UUID) else _invalid(value)


def _raw_time(value: object) -> str | dict[str, str]:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _invalid(value)


def _raw_date(value: object) -> str | dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return _invalid(value)


def _raw_decimal(value: object) -> str | dict[str, str]:
    return (
        _canonical_decimal(value)
        if isinstance(value, Decimal) and value.is_finite()
        else _invalid(value)
    )


def _hash(
    command: CreateReceiptCommand | UpdateReceiptCommand | SetReceiptLifecycleCommand,
) -> bytes:
    kind = (
        CREATE_COMMAND_KIND
        if isinstance(command, CreateReceiptCommand)
        else UPDATE_COMMAND_KIND
        if isinstance(command, UpdateReceiptCommand)
        else LIFECYCLE_COMMAND_KIND
    )
    request: dict[str, object] = {
        "command_kind": kind,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "mutation_id": _raw_uuid(command.mutation_id),
        "receipt_id": _raw_uuid(command.receipt_id),
        "organization_id": _raw_uuid(command.organization_id),
        "event_id": _raw_uuid(command.event_id),
        "client_wall_time": _raw_time(command.client_wall_time),
        "logical_operation_id": _raw_uuid(command.logical_operation_id),
    }
    if isinstance(command, (CreateReceiptCommand, UpdateReceiptCommand)):
        request |= {
            "title": _raw_text(
                command.title,
                lambda text_value: unicodedata.normalize("NFC", text_value).strip(),
            ),
            "total_amount": _raw_decimal(command.total_amount),
            "receipt_date": _raw_date(command.receipt_date),
            "note": _raw_text(command.note, _canonical_note),
        }
    else:
        request["operation"] = (
            command.operation if isinstance(command.operation, str) else _invalid(command.operation)
        )
    return hashlib.sha256(
        json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()


def _prepare(
    command: CreateReceiptCommand | UpdateReceiptCommand | SetReceiptLifecycleCommand,
) -> _Prepared:
    kind = (
        CREATE_COMMAND_KIND
        if isinstance(command, CreateReceiptCommand)
        else UPDATE_COMMAND_KIND
        if isinstance(command, UpdateReceiptCommand)
        else LIFECYCLE_COMMAND_KIND
    )
    violations: list[FieldViolation] = []
    for path, value in (
        ("mutation_id", command.mutation_id),
        ("receipt_id", command.receipt_id),
        ("organization_id", command.organization_id),
        ("event_id", command.event_id),
    ):
        if not isinstance(value, UUID):
            violations.append(FieldViolation(path, "must_be_uuid"))
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
    title: str | None = None
    amount: Decimal | None = None
    receipt_date: date | None = None
    note: str | None = None
    operation: Literal["retire", "restore"] | None = None
    if isinstance(command, (CreateReceiptCommand, UpdateReceiptCommand)):
        title = (
            unicodedata.normalize("NFC", command.title).strip()
            if isinstance(command.title, str)
            else ""
        )
        if not isinstance(command.title, str) or not title or len(title) > 200:
            violations.append(
                FieldViolation("title", "must_be_nonblank_and_at_most_200_characters")
            )
        elif not _is_utf8_encodable(title):
            violations.append(FieldViolation("title", "must_be_valid_unicode_text"))
        elif "\x00" in title:
            violations.append(FieldViolation("title", "must_not_contain_nul"))
        amount = (
            command.total_amount
            if isinstance(command.total_amount, Decimal) and command.total_amount.is_finite()
            else None
        )
        if amount is None or amount < 0:
            violations.append(FieldViolation("total_amount", "must_be_nonnegative_finite_decimal"))
        if command.receipt_date is not None:
            if isinstance(command.receipt_date, date) and not isinstance(
                command.receipt_date, datetime
            ):
                receipt_date = command.receipt_date
            else:
                violations.append(FieldViolation("receipt_date", "must_be_calendar_date_or_null"))
        if command.note is not None and not isinstance(command.note, str):
            violations.append(FieldViolation("note", "must_be_string_or_null"))
        if isinstance(command.note, str):
            note = _canonical_note(command.note) or None
            if note is not None and not _is_utf8_encodable(note):
                violations.append(FieldViolation("note", "must_be_valid_unicode_text"))
            elif note is not None and "\x00" in note:
                violations.append(FieldViolation("note", "must_not_contain_nul"))
            elif (
                note is not None
                and len(json.dumps(note, ensure_ascii=False).encode()) > MAX_SERIALIZED_NOTE_BYTES
            ):
                violations.append(FieldViolation("note", "must_fit_change_record"))
    else:
        if command.operation not in ("retire", "restore"):
            violations.append(FieldViolation("operation", "must_be_retire_or_restore"))
        else:
            operation = command.operation
    return _Prepared(
        mutation_id=command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        receipt_id=command.receipt_id if isinstance(command.receipt_id, UUID) else UUID(int=0),
        organization_id=command.organization_id
        if isinstance(command.organization_id, UUID)
        else UUID(int=0),
        event_id=command.event_id if isinstance(command.event_id, UUID) else UUID(int=0),
        command_kind=kind,
        client_wall_time=command.client_wall_time.astimezone(UTC)
        if has_time
        else datetime(1970, 1, 1, tzinfo=UTC),
        title=title,
        total_amount=amount,
        receipt_date=receipt_date,
        note=note,
        operation=operation,
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        violations=tuple(violations),
    )


def _validation(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
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


def _record(receipt: Receipt) -> dict[str, object]:
    return {
        "id": str(receipt.id),
        "organization_id": str(receipt.organization_id),
        "event_id": str(receipt.event_id),
        "title": receipt.title,
        "total_amount": _canonical_decimal(receipt.total_amount),
        "currency": receipt.currency,
        "receipt_date": receipt.receipt_date.isoformat() if receipt.receipt_date else None,
        "note": receipt.note,
        "created_at": receipt.created_at.isoformat(),
        "created_by_user_id": str(receipt.created_by_user_id),
        "last_modified_at": receipt.last_modified_at.isoformat(),
        "last_modified_by_user_id": str(receipt.last_modified_by_user_id),
        "retired_at": receipt.retired_at.isoformat() if receipt.retired_at else None,
        "retired_by_user_id": str(receipt.retired_by_user_id)
        if receipt.retired_by_user_id
        else None,
    }


def _result(
    prepared: _Prepared,
    receipt: Receipt,
    first: int,
    last: int,
    replayed: bool,
    outcome: Literal["accepted", "partially_superseded"],
) -> ReceiptResult:
    return ReceiptResult(
        prepared.mutation_id,
        receipt.id,
        receipt.organization_id,
        receipt.event_id,
        receipt.title,
        receipt.total_amount,
        receipt.currency,
        receipt.receipt_date,
        receipt.note,
        receipt.retired_at,
        receipt.retired_by_user_id,
        first,
        last,
        replayed,
        outcome,
    )


def _result_payload(result: ReceiptResult) -> dict[str, object]:
    return {
        "receipt": {
            "id": str(result.receipt_id),
            "organization_id": str(result.organization_id),
            "event_id": str(result.event_id),
            "title": result.title,
            "total_amount": _canonical_decimal(result.total_amount),
            "currency": result.currency,
            "receipt_date": result.receipt_date.isoformat() if result.receipt_date else None,
            "note": result.note,
            "retired_at": result.retired_at.isoformat() if result.retired_at else None,
            "retired_by_user_id": str(result.retired_by_user_id)
            if result.retired_by_user_id
            else None,
        },
        "outcome": result.outcome,
    }


def _retained_result(prepared: _Prepared, mutation: Mutation) -> ReceiptResult:
    payload = mutation.outcome_payload or {}
    receipt_payload = payload.get("receipt")
    if (
        not isinstance(receipt_payload, dict)
        or mutation.first_change_sequence is None
        or mutation.last_change_sequence is None
    ):
        raise RuntimeError("Retained receipt mutation has an invalid result payload")
    try:
        date_text = receipt_payload.get("receipt_date")
        retired_text = receipt_payload.get("retired_at")
        retired_by = receipt_payload.get("retired_by_user_id")
        outcome = payload.get("outcome")
        if outcome not in ("accepted", "partially_superseded"):
            raise TypeError
        return ReceiptResult(
            prepared.mutation_id,
            UUID(cast(str, receipt_payload["id"])),
            UUID(cast(str, receipt_payload["organization_id"])),
            UUID(cast(str, receipt_payload["event_id"])),
            cast(str, receipt_payload["title"]),
            Decimal(cast(str, receipt_payload["total_amount"])),
            cast(str, receipt_payload["currency"]),
            date.fromisoformat(date_text) if isinstance(date_text, str) else None,
            cast(str | None, receipt_payload["note"]),
            datetime.fromisoformat(retired_text) if isinstance(retired_text, str) else None,
            UUID(retired_by) if isinstance(retired_by, str) else None,
            mutation.first_change_sequence,
            mutation.last_change_sequence,
            True,
            outcome,
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise RuntimeError("Retained receipt mutation has an invalid result payload") from error


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    error = (mutation.outcome_payload or {}).get("error")
    if not isinstance(error, dict) or error.get("code") not in (
        "validation_failed",
        "archived_event",
        "client_time_too_far_ahead",
    ):
        raise RuntimeError("Retained receipt mutation has an invalid rejection payload")
    raw_violations = error.get("field_violations", [])
    if not isinstance(raw_violations, list):
        raise RuntimeError("Retained receipt mutation has invalid violations")
    try:
        violations = tuple(
            FieldViolation(cast(str, item["path"]), cast(str, item["code"]))
            for item in raw_violations
            if isinstance(item, dict)
        )
    except (KeyError, TypeError) as value_error:
        raise RuntimeError("Retained receipt mutation has invalid violations") from value_error
    if len(violations) != len(raw_violations):
        raise RuntimeError("Retained receipt mutation has invalid violations")
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
    outcome: Literal["accepted", "partially_superseded", "rejected"],
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
            {"entity_kind": "event", "entity_id": str(prepared.event_id)},
            {"entity_kind": "receipt", "entity_id": str(prepared.receipt_id)},
        ],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


def _clock_wins(clock: FieldClock | None, prepared: _Prepared) -> bool:
    return clock is None or (prepared.client_wall_time, prepared.mutation_id) > (
        clock.winning_client_wall_time,
        clock.winning_mutation_id,
    )


async def _apply(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateReceiptCommand | UpdateReceiptCommand | SetReceiptLifecycleCommand,
) -> ReceiptResult:
    prepared, request_hash = _prepare(command), _hash(command)
    deferred: ApplicationServiceError | None = None
    result: ReceiptResult | None = None
    receipt_for_change: Receipt | None = None
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
            if retained.outcome in ("accepted", "partially_superseded"):
                return _retained_result(prepared, retained)
            if retained.outcome == "rejected":
                deferred = _retained_error(retained)
            else:
                raise RuntimeError("Receipt mutation retained an unsupported outcome")
        elif prepared.violations:
            deferred = _validation(prepared.violations)
        elif prepared.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            deferred = ApplicationServiceError(
                "client_time_too_far_ahead", retry_same_identity=False
            )
        if deferred is None and retained is None:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("receipt", prepared.receipt_id)},
            )
            event = await session.scalar(
                select(Event)
                .where(
                    Event.id == prepared.event_id, Event.organization_id == prepared.organization_id
                )
                .with_for_update(of=Event)
            )
            if event is None:
                deferred = _validation((FieldViolation("event_id", "must_belong_to_organization"),))
            elif event.lifecycle != "active":
                deferred = ApplicationServiceError("archived_event", retry_same_identity=False)
            elif prepared.command_kind == CREATE_COMMAND_KIND:
                existing = await session.get(Receipt, prepared.receipt_id, with_for_update=True)
                if existing is not None:
                    deferred = _validation((FieldViolation("receipt_id", "already_exists"),))
                else:
                    assert prepared.title is not None and prepared.total_amount is not None
                    now = datetime.now(UTC)
                    created_receipt = Receipt(
                        id=prepared.receipt_id,
                        organization_id=prepared.organization_id,
                        event_id=prepared.event_id,
                        title=prepared.title,
                        total_amount=prepared.total_amount,
                        currency=event.currency,
                        receipt_date=prepared.receipt_date,
                        note=prepared.note,
                        created_at=now,
                        created_by_user_id=context.actor_user_id,
                        last_modified_at=now,
                        last_modified_by_user_id=context.actor_user_id,
                    )
                    session.add(created_receipt)
                    for field in (*_METADATA_FIELDS, _LIFECYCLE_FIELD):
                        session.add(
                            FieldClock(
                                organization_id=prepared.organization_id,
                                entity_kind="receipt",
                                entity_id=created_receipt.id,
                                field_name=field,
                                winning_client_wall_time=prepared.client_wall_time,
                                winning_mutation_id=prepared.mutation_id,
                            )
                        )
                    first, last = await _reserve_change_range(
                        session, prepared.organization_id, prepared.mutation_id, 1
                    )
                    receipt_for_change = created_receipt
                    result = _result(prepared, created_receipt, first, last, False, "accepted")
            else:
                receipt = await session.scalar(
                    select(Receipt)
                    .where(
                        Receipt.id == prepared.receipt_id,
                        Receipt.organization_id == prepared.organization_id,
                        Receipt.event_id == prepared.event_id,
                    )
                    .with_for_update(of=Receipt)
                )
                if receipt is None:
                    deferred = _validation((FieldViolation("receipt_id", "must_belong_to_event"),))
                else:
                    names = (
                        _METADATA_FIELDS
                        if prepared.command_kind == UPDATE_COMMAND_KIND
                        else (_LIFECYCLE_FIELD,)
                    )
                    clocks = {
                        clock.field_name: clock
                        for clock in (
                            await session.execute(
                                select(FieldClock)
                                .where(
                                    FieldClock.organization_id == prepared.organization_id,
                                    FieldClock.entity_kind == "receipt",
                                    FieldClock.entity_id == receipt.id,
                                    FieldClock.field_name.in_(names),
                                )
                                .with_for_update(of=FieldClock)
                            )
                        ).scalars()
                    }
                    winners = tuple(
                        name for name in names if _clock_wins(clocks.get(name), prepared)
                    )
                    if prepared.command_kind == UPDATE_COMMAND_KIND:
                        # A tombstone never implicitly restores a record; its metadata may
                        # still converge so the user's later restore recovers offline work.
                        for name in winners:
                            setattr(receipt, name, getattr(prepared, name))
                    else:
                        assert prepared.operation is not None
                        if _LIFECYCLE_FIELD in winners:
                            now = datetime.now(UTC)
                            if prepared.operation == "retire":
                                receipt.retired_at, receipt.retired_by_user_id = (
                                    now,
                                    context.actor_user_id,
                                )
                            else:
                                receipt.retired_at, receipt.retired_by_user_id = None, None
                    if winners:
                        now = datetime.now(UTC)
                        receipt.last_modified_at, receipt.last_modified_by_user_id = (
                            now,
                            context.actor_user_id,
                        )
                        for name in winners:
                            clock = clocks.get(name)
                            if clock is None:
                                session.add(
                                    FieldClock(
                                        organization_id=prepared.organization_id,
                                        entity_kind="receipt",
                                        entity_id=receipt.id,
                                        field_name=name,
                                        winning_client_wall_time=prepared.client_wall_time,
                                        winning_mutation_id=prepared.mutation_id,
                                    )
                                )
                            else:
                                clock.winning_client_wall_time, clock.winning_mutation_id = (
                                    prepared.client_wall_time,
                                    prepared.mutation_id,
                                )
                    outcome: Literal["accepted", "partially_superseded"] = (
                        "accepted" if len(winners) == len(names) else "partially_superseded"
                    )
                    first, last = await _reserve_change_range(
                        session, prepared.organization_id, prepared.mutation_id, 1
                    )
                    result = _result(prepared, receipt, first, last, False, outcome)
                    receipt_for_change = receipt
        if deferred is not None and retained is None:
            session.add(
                _mutation(
                    prepared, context, role, request_hash, "rejected", _error_payload(deferred)
                )
            )
        elif result is not None:
            if receipt_for_change is None:
                raise RuntimeError("Receipt result has no change record")
            session.add(
                OrganizationChange(
                    organization_id=prepared.organization_id,
                    sequence=result.first_change_sequence,
                    mutation_id=prepared.mutation_id,
                    entity_id=result.receipt_id,
                    entity_kind="receipt",
                    operation="upsert",
                    payload={"record_schema_version": 1, "record": _record(receipt_for_change)},
                )
            )
            session.add(
                _mutation(
                    prepared,
                    context,
                    role,
                    request_hash,
                    result.outcome,
                    _result_payload(result),
                    result.first_change_sequence,
                    result.last_change_sequence,
                )
            )
    if deferred is not None:
        raise deferred
    if result is None:
        raise RuntimeError("Receipt mutation produced no outcome")
    return result


async def create_receipt(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateReceiptCommand,
) -> ReceiptResult:
    """Create receipt metadata offline; attachment upload is deliberately separate."""
    return await _apply(session_factory, context, command)


async def update_receipt(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: UpdateReceiptCommand,
) -> ReceiptResult:
    return await _apply(session_factory, context, command)


async def set_receipt_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetReceiptLifecycleCommand,
) -> ReceiptResult:
    return await _apply(session_factory, context, command)


async def retire_receipt(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetReceiptLifecycleCommand,
) -> ReceiptResult:
    if command.operation != "retire":
        raise ValueError("retire_receipt requires operation='retire'")
    return await _apply(session_factory, context, command)


async def restore_receipt(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: SetReceiptLifecycleCommand,
) -> ReceiptResult:
    if command.operation != "restore":
        raise ValueError("restore_receipt requires operation='restore'")
    return await _apply(session_factory, context, command)
