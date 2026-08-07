"""Read the retained organization change feed for browser synchronization.

This intentionally implements only the server-to-client half of the protocol.
Commands continue to use their existing application services; a generic push
dispatcher is not useful until those commands have one common typed envelope.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.browser_sessions import decode_browser_session_hmac_key
from cookops.application.events import (
    CreateEventCommand,
    CreateEventResult,
    UpdateEventBaseAttendanceCommand,
    UpdateEventBaseAttendanceResult,
    create_event,
    update_event_base_attendance,
)
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.application.shopping_lists import (
    CreateShoppingListCommand,
    CreateShoppingListResult,
    create_shopping_list,
)
from cookops.persistence.models import (
    ClientInstallation,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationChangeHead,
    OrganizationChangeTransaction,
    OrganizationMembership,
    SystemRoleAssignment,
    User,
)

SYNC_SCHEMA_VERSION: Literal[1] = 1
MAX_TRANSACTION_GROUPS_PER_PULL = 100
MAX_COMMANDS_PER_PUSH = 100

Clock = Callable[[], datetime]


class SyncQueryDenied(PermissionError):
    """The caller is not currently allowed to read this organization."""


class InvalidSyncCursor(ValueError):
    """The supplied opaque cursor is malformed, forged, or not a safe boundary."""


class SyncPushDenied(PermissionError):
    """The caller or browser installation is not currently allowed to push."""


@dataclass(frozen=True, slots=True)
class SyncCursor:
    organization_id: UUID
    after_sequence: int


@dataclass(frozen=True, slots=True)
class SyncRecord:
    organization_id: UUID
    sequence: int
    entity_id: UUID
    entity_kind: str
    operation: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class SyncTransactionGroup:
    mutation_id: UUID
    first_sequence: int
    last_sequence: int
    records: tuple[SyncRecord, ...]


@dataclass(frozen=True, slots=True)
class PullRequest:
    organization_id: UUID
    cursor: str | None
    transaction_group_limit: int = MAX_TRANSACTION_GROUPS_PER_PULL

    def __post_init__(self) -> None:
        if not 1 <= self.transaction_group_limit <= MAX_TRANSACTION_GROUPS_PER_PULL:
            raise ValueError(
                f"transaction_group_limit must be between 1 and {MAX_TRANSACTION_GROUPS_PER_PULL}"
            )


@dataclass(frozen=True, slots=True)
class PullResult:
    status: Literal["ok", "bootstrap_required"]
    sync_schema_version: Literal[1]
    server_time: datetime
    next_cursor: str | None
    transaction_groups: tuple[SyncTransactionGroup, ...]
    oldest_available_at: datetime | None


@dataclass(frozen=True, slots=True)
class UnsupportedSyncCommand:
    mutation_id: UUID
    command_kind: str
    request_hash: bytes
    client_wall_time: datetime
    rejection_code: str = "unsupported_command_kind"


SyncCommand = (
    CreateEventCommand
    | UpdateEventBaseAttendanceCommand
    | CreateShoppingListCommand
    | UnsupportedSyncCommand
)


def _command_kind(
    command: CreateEventCommand | UpdateEventBaseAttendanceCommand | CreateShoppingListCommand,
) -> str:
    if isinstance(command, CreateEventCommand):
        return "event.create"
    if isinstance(command, UpdateEventBaseAttendanceCommand):
        return "event.update_base_attendance"
    return "shopping_list.create"


@dataclass(frozen=True, slots=True)
class PushRequest:
    organization_id: UUID
    client_installation_id: UUID
    request_sent_at: datetime
    commands: tuple[SyncCommand, ...]

    def __post_init__(self) -> None:
        if len(self.commands) > MAX_COMMANDS_PER_PUSH:
            raise ValueError(f"commands must contain at most {MAX_COMMANDS_PER_PUSH} items")


@dataclass(frozen=True, slots=True)
class PushCommandOutcome:
    mutation_id: UUID
    command_kind: str
    status: Literal["accepted", "partially_superseded", "rejected"]
    replayed: bool
    first_change_sequence: int | None
    last_change_sequence: int | None
    error_code: str | None
    field_violations: tuple[tuple[str, str], ...]
    retry_same_identity: bool


@dataclass(frozen=True, slots=True)
class PushResult:
    sync_schema_version: Literal[1]
    server_time: datetime
    clock_skew_seconds: int | None
    change_cursor: str
    outcomes: tuple[PushCommandOutcome, ...]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_now(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock result must include a timezone")
    return now.astimezone(UTC)


class SyncCursorCodec:
    """Bind an opaque cursor to its organization and protocol schema.

    Cursors are not credentials, but authenticating them prevents a client from
    accidentally or deliberately advancing through the middle of a transaction
    group and losing its own canonical data.
    """

    def __init__(self, *, encoded_hmac_key: str) -> None:
        self._key = decode_browser_session_hmac_key(encoded_hmac_key)

    def encode(self, cursor: SyncCursor) -> str:
        if cursor.after_sequence < 0:
            raise ValueError("after_sequence must be nonnegative")
        payload = json.dumps(
            {
                "after_sequence": cursor.after_sequence,
                "organization_id": str(cursor.organization_id),
                "sync_schema_version": SYNC_SCHEMA_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(
            self._key,
            b"cookops.sync.cursor.v1:" + encoded_payload,
            hashlib.sha256,
        ).digest()
        return (
            "v1."
            + encoded_payload.decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )

    def decode(self, token: str) -> SyncCursor:
        if not isinstance(token, str) or len(token) > 512:
            raise InvalidSyncCursor("invalid cursor")
        version, separator, remainder = token.partition(".")
        encoded_payload, separator_two, encoded_signature = remainder.partition(".")
        if (
            version != "v1"
            or not separator
            or not separator_two
            or not encoded_payload
            or not encoded_signature
        ):
            raise InvalidSyncCursor("invalid cursor")
        try:
            payload_bytes = self._decode_base64url(encoded_payload)
            signature = self._decode_base64url(encoded_signature)
            decoded = json.loads(payload_bytes)
            organization_id = UUID(decoded["organization_id"])
            after_sequence = decoded["after_sequence"]
            schema_version = decoded["sync_schema_version"]
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            UnicodeEncodeError,
            binascii.Error,
            json.JSONDecodeError,
        ) as error:
            raise InvalidSyncCursor("invalid cursor") from error
        expected_signature = hmac.new(
            self._key,
            b"cookops.sync.cursor.v1:" + encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if (
            not hmac.compare_digest(signature, expected_signature)
            or type(after_sequence) is not int
            or after_sequence < 0
            or schema_version != SYNC_SCHEMA_VERSION
        ):
            raise InvalidSyncCursor("invalid cursor")
        return SyncCursor(organization_id=organization_id, after_sequence=after_sequence)

    @staticmethod
    def _decode_base64url(value: str) -> bytes:
        if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise InvalidSyncCursor("invalid cursor")
        try:
            decoded = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        except (binascii.Error, ValueError) as error:
            raise InvalidSyncCursor("invalid cursor") from error
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(value, canonical):
            raise InvalidSyncCursor("invalid cursor")
        return decoded


class SynchronizationCommandService:
    """Apply the small, typed browser-command subset in transport order."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        encoded_cursor_hmac_key: str,
        clock: Clock = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._cursor_codec = SyncCursorCodec(encoded_hmac_key=encoded_cursor_hmac_key)
        self._clock = clock

    async def push(self, *, actor_user_id: UUID, request: PushRequest) -> PushResult:
        now = _normalize_now(self._clock())
        request_sent_at = _normalize_now(request.request_sent_at)
        actor_role = await self._authorize_push(
            actor_user_id=actor_user_id,
            organization_id=request.organization_id,
            client_installation_id=request.client_installation_id,
        )
        context = ExecutionContext(
            actor_user_id=actor_user_id,
            client_installation_id=request.client_installation_id,
        )
        outcomes: list[PushCommandOutcome] = []
        for command in request.commands:
            outcomes.append(
                await self._dispatch(
                    context=context,
                    actor_role=actor_role,
                    organization_id=request.organization_id,
                    command=command,
                )
            )
        async with self._session_factory() as session, session.begin():
            head = await session.scalar(
                select(OrganizationChangeHead.next_sequence).where(
                    OrganizationChangeHead.organization_id == request.organization_id
                )
            )
        return PushResult(
            sync_schema_version=SYNC_SCHEMA_VERSION,
            server_time=now,
            clock_skew_seconds=(
                int((request_sent_at - now).total_seconds())
                if abs(request_sent_at - now) > timedelta(minutes=5)
                else None
            ),
            change_cursor=self._cursor_codec.encode(
                SyncCursor(
                    organization_id=request.organization_id,
                    after_sequence=(head - 1 if head is not None else 0),
                )
            ),
            outcomes=tuple(outcomes),
        )

    async def _authorize_push(
        self, *, actor_user_id: UUID, organization_id: UUID, client_installation_id: UUID
    ) -> Literal["member", "organization_admin", "system_admin"]:
        async with self._session_factory() as session, session.begin():
            await SynchronizationQueryService._authorize_read(
                session, actor_user_id, organization_id
            )
            installation = await session.scalar(
                select(ClientInstallation.id)
                .where(
                    ClientInstallation.id == client_installation_id,
                    ClientInstallation.user_id == actor_user_id,
                    ClientInstallation.installation_kind == "browser",
                    ClientInstallation.disabled_at.is_(None),
                )
                .with_for_update(of=ClientInstallation)
            )
            if installation is None:
                await session.execute(
                    insert(ClientInstallation)
                    .values(
                        id=client_installation_id,
                        user_id=actor_user_id,
                        installation_kind="browser",
                    )
                    .on_conflict_do_nothing(index_elements=("id",))
                )
                installation = await session.scalar(
                    select(ClientInstallation.id)
                    .where(
                        ClientInstallation.id == client_installation_id,
                        ClientInstallation.user_id == actor_user_id,
                        ClientInstallation.installation_kind == "browser",
                        ClientInstallation.disabled_at.is_(None),
                    )
                    .with_for_update(of=ClientInstallation)
                )
                if installation is None:
                    raise SyncPushDenied("browser installation access denied")
            system_admin = await session.scalar(
                select(SystemRoleAssignment.id).where(
                    SystemRoleAssignment.user_id == actor_user_id,
                    SystemRoleAssignment.role == "system_admin",
                    SystemRoleAssignment.revoked_at.is_(None),
                )
            )
            if system_admin is not None:
                return "system_admin"
            role = await session.scalar(
                select(OrganizationMembership.role).where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id == actor_user_id,
                    OrganizationMembership.state == "active",
                    OrganizationMembership.role.in_(("member", "organization_admin")),
                )
            )
            if role in ("member", "organization_admin"):
                return cast(Literal["member", "organization_admin"], role)
            raise SyncPushDenied("organization access denied")

    async def _dispatch(
        self,
        *,
        context: ExecutionContext,
        actor_role: Literal["member", "organization_admin", "system_admin"],
        organization_id: UUID,
        command: SyncCommand,
    ) -> PushCommandOutcome:
        if isinstance(command, UnsupportedSyncCommand):
            return await self._retain_adapter_rejection(
                context=context,
                actor_role=actor_role,
                organization_id=organization_id,
                command=command,
            )
        if command.organization_id != organization_id:
            return PushCommandOutcome(
                mutation_id=command.mutation_id,
                command_kind=_command_kind(command),
                status="rejected",
                replayed=False,
                first_change_sequence=None,
                last_change_sequence=None,
                error_code="organization_mismatch",
                field_violations=(),
                retry_same_identity=False,
            )
        try:
            result: CreateEventResult | UpdateEventBaseAttendanceResult | CreateShoppingListResult
            if isinstance(command, CreateEventCommand):
                result = await create_event(self._session_factory, context, command)
            elif isinstance(command, UpdateEventBaseAttendanceCommand):
                result = await update_event_base_attendance(self._session_factory, context, command)
            else:
                result = await create_shopping_list(self._session_factory, context, command)
        except ApplicationServiceError as error:
            return PushCommandOutcome(
                mutation_id=command.mutation_id,
                command_kind=_command_kind(command),
                status="rejected",
                replayed=False,
                first_change_sequence=None,
                last_change_sequence=None,
                error_code=error.code,
                field_violations=tuple(
                    (violation.path, violation.code) for violation in error.field_violations
                ),
                retry_same_identity=error.retry_same_identity,
            )
        return self._result_outcome(result)

    async def _retain_adapter_rejection(
        self,
        *,
        context: ExecutionContext,
        actor_role: Literal["member", "organization_admin", "system_admin"],
        organization_id: UUID,
        command: UnsupportedSyncCommand,
    ) -> PushCommandOutcome:
        async with self._session_factory() as session, session.begin():
            retained = await session.get(Mutation, command.mutation_id, with_for_update=True)
            if retained is not None:
                if (
                    retained.organization_id != organization_id
                    or retained.actor_user_id != context.actor_user_id
                    or retained.command_kind != command.command_kind
                    or retained.command_schema_version != SYNC_SCHEMA_VERSION
                    or retained.request_hash != command.request_hash
                    or retained.outcome != "rejected"
                ):
                    return PushCommandOutcome(
                        mutation_id=command.mutation_id,
                        command_kind=command.command_kind,
                        status="rejected",
                        replayed=False,
                        first_change_sequence=None,
                        last_change_sequence=None,
                        error_code="idempotency_mismatch",
                        field_violations=(),
                        retry_same_identity=False,
                    )
                return PushCommandOutcome(
                    mutation_id=command.mutation_id,
                    command_kind=command.command_kind,
                    status="rejected",
                    replayed=True,
                    first_change_sequence=None,
                    last_change_sequence=None,
                    error_code=command.rejection_code,
                    field_violations=(),
                    retry_same_identity=False,
                )
            session.add(
                Mutation(
                    id=command.mutation_id,
                    organization_id=organization_id,
                    is_system_administration_scope=False,
                    actor_user_id=context.actor_user_id,
                    actor_role=actor_role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=None,
                    oauth_grant_id=None,
                    client_wall_time=command.client_wall_time,
                    command_schema_version=SYNC_SCHEMA_VERSION,
                    command_kind=command.command_kind,
                    target_identities=[
                        {"entity_kind": "sync_command", "entity_id": str(command.mutation_id)}
                    ],
                    request_hash=command.request_hash,
                    outcome="rejected",
                    outcome_payload={
                        "error": {
                            "code": command.rejection_code,
                            "field_violations": [],
                            "retry_same_identity": False,
                        }
                    },
                    first_change_sequence=None,
                    last_change_sequence=None,
                )
            )
        return PushCommandOutcome(
            mutation_id=command.mutation_id,
            command_kind=command.command_kind,
            status="rejected",
            replayed=False,
            first_change_sequence=None,
            last_change_sequence=None,
            error_code=command.rejection_code,
            field_violations=(),
            retry_same_identity=False,
        )

    @staticmethod
    def _result_outcome(
        result: CreateEventResult | UpdateEventBaseAttendanceResult | CreateShoppingListResult,
    ) -> PushCommandOutcome:
        return PushCommandOutcome(
            mutation_id=result.mutation_id,
            command_kind=(
                "event.create"
                if isinstance(result, CreateEventResult)
                else "event.update_base_attendance"
                if isinstance(result, UpdateEventBaseAttendanceResult)
                else "shopping_list.create"
            ),
            status=result.outcome,
            replayed=result.replayed,
            first_change_sequence=result.first_change_sequence,
            last_change_sequence=result.last_change_sequence,
            error_code=None,
            field_violations=(),
            retry_same_identity=True,
        )


class SynchronizationQueryService:
    """Authorize and page canonical organization changes without splitting commands."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        encoded_cursor_hmac_key: str,
        clock: Clock = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._cursor_codec = SyncCursorCodec(encoded_hmac_key=encoded_cursor_hmac_key)
        self._clock = clock

    async def pull(self, *, actor_user_id: UUID, request: PullRequest) -> PullResult:
        now = _normalize_now(self._clock())
        cursor = self._decode_cursor(request)
        async with self._session_factory() as session, session.begin():
            await self._authorize_read(session, actor_user_id, request.organization_id)
            if cursor is None:
                return await self._bootstrap_required(session, request.organization_id, now)

            head, physical_head = (
                await session.execute(
                    select(
                        select(OrganizationChangeHead.next_sequence)
                        .where(OrganizationChangeHead.organization_id == request.organization_id)
                        .scalar_subquery(),
                        select(func.max(OrganizationChange.sequence))
                        .where(OrganizationChange.organization_id == request.organization_id)
                        .scalar_subquery(),
                    )
                )
            ).one()
            if head is None:
                if physical_head is not None:
                    return await self._bootstrap_required(session, request.organization_id, now)
                current_sequence = 0
            else:
                current_sequence = head - 1
                if physical_head != current_sequence:
                    return await self._bootstrap_required(session, request.organization_id, now)
            if cursor.after_sequence > current_sequence:
                return await self._bootstrap_required(session, request.organization_id, now)
            if not await self._is_transaction_boundary(
                session, request.organization_id, cursor.after_sequence
            ):
                raise InvalidSyncCursor("invalid cursor")

            oldest_available_at, oldest_available_sequence = await self._oldest_available(
                session, request.organization_id
            )
            # A cursor at the head is always safe. In particular, an organization
            # with no retained records must not force an unnecessary bootstrap.
            if cursor.after_sequence == current_sequence:
                return PullResult(
                    status="ok",
                    sync_schema_version=SYNC_SCHEMA_VERSION,
                    server_time=now,
                    next_cursor=request.cursor,
                    transaction_groups=(),
                    oldest_available_at=oldest_available_at,
                )
            # The feed currently has no physical cleanup job. Do not treat
            # ``published_at`` as a retention boundary: PostgreSQL's
            # CURRENT_TIMESTAMP is the beginning of the publishing transaction,
            # not its commit time. Until a cleanup job has a trustworthy
            # commit-time marker, every stored change remains available. A real
            # physical gap still requires a bootstrap.
            if (
                oldest_available_sequence is not None
                and cursor.after_sequence < oldest_available_sequence - 1
            ):
                return PullResult(
                    status="bootstrap_required",
                    sync_schema_version=SYNC_SCHEMA_VERSION,
                    server_time=now,
                    next_cursor=None,
                    transaction_groups=(),
                    oldest_available_at=oldest_available_at,
                )

            transactions = (
                (
                    await session.execute(
                        select(OrganizationChangeTransaction)
                        .where(
                            OrganizationChangeTransaction.organization_id
                            == request.organization_id,
                            OrganizationChangeTransaction.last_change_sequence
                            > cursor.after_sequence,
                        )
                        .order_by(OrganizationChangeTransaction.first_change_sequence)
                        .limit(request.transaction_group_limit)
                    )
                )
                .scalars()
                .all()
            )
            # Feed rows are append-only in normal operation, but a partial
            # restore or manual repair must never let a replica advance past a
            # physical hole. Validate only this bounded page: a later gap is
            # checked before its page can advance a cursor.
            if not await self._page_is_contiguous(
                session,
                organization_id=request.organization_id,
                after_sequence=cursor.after_sequence,
                transactions=transactions,
            ):
                return await self._bootstrap_required(session, request.organization_id, now)
            groups = await self._load_groups(session, request.organization_id, transactions)
            next_sequence = groups[-1].last_sequence if groups else cursor.after_sequence
            return PullResult(
                status="ok",
                sync_schema_version=SYNC_SCHEMA_VERSION,
                server_time=now,
                next_cursor=self._cursor_codec.encode(
                    SyncCursor(
                        organization_id=request.organization_id,
                        after_sequence=next_sequence,
                    )
                ),
                transaction_groups=groups,
                oldest_available_at=oldest_available_at,
            )

    def _decode_cursor(self, request: PullRequest) -> SyncCursor | None:
        if request.cursor is None:
            return None
        cursor = self._cursor_codec.decode(request.cursor)
        if cursor.organization_id != request.organization_id:
            raise InvalidSyncCursor("invalid cursor")
        return cursor

    @staticmethod
    async def _authorize_read(
        session: AsyncSession, actor_user_id: UUID, organization_id: UUID
    ) -> None:
        actor = await session.scalar(
            select(User.id)
            .where(User.id == actor_user_id, User.disabled_at.is_(None))
            .with_for_update(of=User)
        )
        organization = await session.scalar(
            select(Organization.id)
            .where(Organization.id == organization_id, Organization.retired_at.is_(None))
            .with_for_update(of=Organization)
        )
        if actor is None or organization is None:
            raise SyncQueryDenied("organization access denied")
        system_admin = await session.scalar(
            select(SystemRoleAssignment.id)
            .where(
                SystemRoleAssignment.user_id == actor_user_id,
                SystemRoleAssignment.role == "system_admin",
                SystemRoleAssignment.revoked_at.is_(None),
            )
            .with_for_update(of=SystemRoleAssignment)
        )
        if system_admin is not None:
            return
        membership = await session.scalar(
            select(OrganizationMembership.id)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == actor_user_id,
                OrganizationMembership.state == "active",
                OrganizationMembership.role.in_(("member", "organization_admin")),
            )
            .with_for_update(of=OrganizationMembership)
        )
        if membership is None:
            raise SyncQueryDenied("organization access denied")

    async def _bootstrap_required(
        self, session: AsyncSession, organization_id: UUID, now: datetime
    ) -> PullResult:
        oldest_available_at, _ = await self._oldest_available(session, organization_id)
        return PullResult(
            status="bootstrap_required",
            sync_schema_version=SYNC_SCHEMA_VERSION,
            server_time=now,
            next_cursor=None,
            transaction_groups=(),
            oldest_available_at=oldest_available_at,
        )

    @staticmethod
    async def _is_transaction_boundary(
        session: AsyncSession, organization_id: UUID, after_sequence: int
    ) -> bool:
        if after_sequence == 0:
            return True
        return (
            await session.scalar(
                select(OrganizationChangeTransaction.mutation_id).where(
                    OrganizationChangeTransaction.organization_id == organization_id,
                    OrganizationChangeTransaction.last_change_sequence == after_sequence,
                )
            )
            is not None
        )

    @staticmethod
    async def _page_is_contiguous(
        session: AsyncSession,
        *,
        organization_id: UUID,
        after_sequence: int,
        transactions: Sequence[OrganizationChangeTransaction],
    ) -> bool:
        """Verify complete, ordered command groups before a page advances a cursor."""
        if not transactions:
            return False
        expected_sequence = after_sequence + 1
        for transaction in transactions:
            first_sequence = transaction.first_change_sequence
            last_sequence = transaction.last_change_sequence
            if first_sequence != expected_sequence or last_sequence < first_sequence:
                return False
            expected_sequence = last_sequence + 1

        first_sequence = transactions[0].first_change_sequence
        last_sequence = transactions[-1].last_change_sequence
        changes = (
            (
                await session.execute(
                    select(OrganizationChange.sequence, OrganizationChange.mutation_id)
                    .where(
                        OrganizationChange.organization_id == organization_id,
                        OrganizationChange.sequence >= first_sequence,
                        OrganizationChange.sequence <= last_sequence,
                    )
                    .order_by(OrganizationChange.sequence)
                )
            )
            .tuples()
            .all()
        )
        transaction_index = 0
        expected_sequence = after_sequence + 1
        for sequence, mutation_id in changes:
            if sequence != expected_sequence or transaction_index >= len(transactions):
                return False
            transaction = transactions[transaction_index]
            if mutation_id != transaction.mutation_id:
                return False
            if sequence == transaction.last_change_sequence:
                transaction_index += 1
            expected_sequence += 1
        return expected_sequence == last_sequence + 1 and transaction_index == len(transactions)

    @staticmethod
    async def _oldest_available(
        session: AsyncSession, organization_id: UUID
    ) -> tuple[datetime | None, int | None]:
        oldest_available = (
            await session.execute(
                select(
                    func.min(OrganizationChange.published_at),
                    func.min(OrganizationChange.sequence),
                ).where(OrganizationChange.organization_id == organization_id)
            )
        ).one()
        return oldest_available[0], oldest_available[1]

    @staticmethod
    async def _load_groups(
        session: AsyncSession,
        organization_id: UUID,
        transactions: Sequence[OrganizationChangeTransaction],
    ) -> tuple[SyncTransactionGroup, ...]:
        if not transactions:
            return ()
        first_sequence = transactions[0].first_change_sequence
        last_sequence = transactions[-1].last_change_sequence
        changes = (
            (
                await session.execute(
                    select(OrganizationChange)
                    .where(
                        OrganizationChange.organization_id == organization_id,
                        OrganizationChange.sequence >= first_sequence,
                        OrganizationChange.sequence <= last_sequence,
                    )
                    .order_by(OrganizationChange.sequence)
                )
            )
            .scalars()
            .all()
        )
        by_mutation: dict[UUID, list[SyncRecord]] = {}
        for change in changes:
            by_mutation.setdefault(change.mutation_id, []).append(
                SyncRecord(
                    organization_id=organization_id,
                    sequence=change.sequence,
                    entity_id=change.entity_id,
                    entity_kind=change.entity_kind,
                    operation=change.operation,
                    payload=change.payload,
                )
            )
        return tuple(
            SyncTransactionGroup(
                mutation_id=transaction.mutation_id,
                first_sequence=transaction.first_change_sequence,
                last_sequence=transaction.last_change_sequence,
                records=tuple(by_mutation.get(transaction.mutation_id, ())),
            )
            for transaction in transactions
        )
