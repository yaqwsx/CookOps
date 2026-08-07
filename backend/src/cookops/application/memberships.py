"""Online-only organization member invitations and removals."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import _authorize_and_lock_organization, _reserve_change_range
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import (
    ClientInstallation,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    SystemRoleAssignment,
    User,
)

INVITE_COMMAND_KIND = "organization_member.invite"
REMOVE_COMMAND_KIND = "organization_member.remove"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class InviteMemberCommand:
    mutation_id: UUID
    organization_id: UUID
    invited_email: str
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RemoveMemberCommand:
    mutation_id: UUID
    organization_id: UUID
    membership_id: UUID
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class MembershipSummary:
    id: UUID
    invited_email: str
    role: Literal["member", "organization_admin"]
    state: Literal["invited", "active"]


@dataclass(frozen=True, slots=True)
class MembershipMutationResult:
    mutation_id: UUID
    organization_id: UUID
    membership_id: UUID
    state: Literal["invited", "removed"]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


def _normalized_email(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().lower()


def _validate_command(command: InviteMemberCommand | RemoveMemberCommand) -> None:
    if (
        not isinstance(command.mutation_id, UUID)
        or not isinstance(command.organization_id, UUID)
        or command.client_wall_time.tzinfo is None
        or command.client_wall_time.utcoffset() is None
    ):
        raise ApplicationServiceError(
            "validation_failed",
            field_violations=(FieldViolation("command", "invalid"),),
            retry_same_identity=False,
        )
    if isinstance(command, InviteMemberCommand):
        email = _normalized_email(command.invited_email)
        if not email or len(email) > 320:
            raise ApplicationServiceError(
                "validation_failed",
                field_violations=(FieldViolation("invited_email", "invalid"),),
                retry_same_identity=False,
            )


def _request_hash(command: InviteMemberCommand | RemoveMemberCommand) -> bytes:
    values: dict[str, object] = {
        "client_wall_time": command.client_wall_time.astimezone(UTC).isoformat(),
        "command_kind": (
            INVITE_COMMAND_KIND if isinstance(command, InviteMemberCommand) else REMOVE_COMMAND_KIND
        ),
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "logical_operation_id": str(command.logical_operation_id)
        if command.logical_operation_id
        else None,
        "organization_id": str(command.organization_id),
    }
    if isinstance(command, InviteMemberCommand):
        values["invited_email"] = _normalized_email(command.invited_email)
    else:
        values["membership_id"] = str(command.membership_id)
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()


async def _ensure_browser_installation(session: AsyncSession, context: ExecutionContext) -> None:
    """Register this browser installation once, as the sync transport does."""

    if context.oauth_client_id is not None:
        return
    await session.execute(
        insert(ClientInstallation)
        .values(
            id=context.client_installation_id,
            user_id=context.actor_user_id,
            installation_kind="browser",
        )
        .on_conflict_do_nothing(index_elements=("id",))
    )


def _organization_record(organization: Organization) -> dict[str, object]:
    """A safe feed marker: membership identities are not organization replica data."""

    return {
        "id": str(organization.id),
        "name": organization.name,
        "description": organization.description,
        "default_currency": organization.default_currency,
        "created_at": organization.created_at.isoformat(),
        "created_by_user_id": str(organization.created_by_user_id),
        "retired_at": organization.retired_at.isoformat() if organization.retired_at else None,
        "retired_by_user_id": (
            str(organization.retired_by_user_id) if organization.retired_by_user_id else None
        ),
    }


def _result_from_mutation(mutation: Mutation) -> MembershipMutationResult:
    payload = mutation.outcome_payload or {}
    try:
        membership_id = UUID(cast(str, payload["membership_id"]))
        state = cast(Literal["invited", "removed"], payload["state"])
        if state not in ("invited", "removed"):
            raise ValueError
        if mutation.organization_id is None:
            raise ValueError
        return MembershipMutationResult(
            mutation.id,
            mutation.organization_id,
            membership_id,
            state,
            cast(int, mutation.first_change_sequence),
            cast(int, mutation.last_change_sequence),
            True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Membership mutation has an invalid accepted outcome") from error


async def list_members(
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: UUID,
    organization_id: UUID,
) -> tuple[MembershipSummary, ...]:
    async with session_factory() as session, session.begin():
        actor = await session.scalar(
            select(User.id)
            .where(User.id == actor_user_id, User.disabled_at.is_(None))
            .with_for_update(of=User)
        )
        system_admin = await session.scalar(
            select(SystemRoleAssignment.id)
            .where(
                SystemRoleAssignment.user_id == actor_user_id,
                SystemRoleAssignment.role == "system_admin",
                SystemRoleAssignment.revoked_at.is_(None),
            )
            .with_for_update(of=SystemRoleAssignment)
        )
        organization = await session.scalar(
            select(Organization.id)
            .where(Organization.id == organization_id, Organization.retired_at.is_(None))
            .with_for_update(of=Organization)
        )
        organization_admin = await session.scalar(
            select(OrganizationMembership.id)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == actor_user_id,
                OrganizationMembership.state == "active",
                OrganizationMembership.role == "organization_admin",
            )
            .with_for_update(of=OrganizationMembership)
        )
        if (
            actor is None
            or organization is None
            or (system_admin is None and organization_admin is None)
        ):
            raise ApplicationServiceError("forbidden", retry_same_identity=True)
        rows = (
            await session.execute(
                select(OrganizationMembership)
                .where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.state.in_(("invited", "active")),
                )
                .order_by(OrganizationMembership.invited_email, OrganizationMembership.id)
            )
        ).scalars()
        return tuple(
            MembershipSummary(
                item.id,
                item.invited_email,
                cast(Literal["member", "organization_admin"], item.role),
                cast(Literal["invited", "active"], item.state),
            )
            for item in rows
        )


async def invite_member(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: InviteMemberCommand,
) -> MembershipMutationResult:
    _validate_command(command)
    normalized_email = _normalized_email(command.invited_email)
    request_hash = _request_hash(command)
    async with session_factory() as session, session.begin():
        await _ensure_browser_installation(session, context)
        actor_role, _ = await _authorize_and_lock_organization(
            session, context, command.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": f"{command.organization_id}:{normalized_email}"},
        )
        retained = await session.get(Mutation, command.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != INVITE_COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "accepted":
                return _result_from_mutation(retained)
            raise RuntimeError("Membership invitation retained an invalid outcome")
        if command.client_wall_time.astimezone(UTC) > datetime.now(UTC) + timedelta(hours=24):
            raise ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        existing = await session.scalar(
            select(OrganizationMembership.id)
            .where(
                OrganizationMembership.organization_id == command.organization_id,
                OrganizationMembership.invited_email == normalized_email,
                OrganizationMembership.state.in_(("invited", "active")),
            )
            .with_for_update(of=OrganizationMembership)
        )
        if existing is not None:
            raise ApplicationServiceError("validation_failed", retry_same_identity=False)
        organization = await session.scalar(
            select(Organization)
            .where(Organization.id == command.organization_id)
            .with_for_update(of=Organization)
        )
        if organization is None:
            raise ApplicationServiceError("forbidden", retry_same_identity=True)
        membership = OrganizationMembership(
            id=uuid4(),
            organization_id=command.organization_id,
            invited_email=normalized_email,
            role="member",
            state="invited",
            invited_by_user_id=context.actor_user_id,
        )
        first, last = await _reserve_change_range(
            session, command.organization_id, command.mutation_id, 1
        )
        result = MembershipMutationResult(
            command.mutation_id,
            command.organization_id,
            membership.id,
            "invited",
            first,
            last,
            False,
        )
        session.add_all(
            (
                membership,
                OrganizationChange(
                    organization_id=command.organization_id,
                    sequence=first,
                    mutation_id=command.mutation_id,
                    entity_id=organization.id,
                    entity_kind="organization",
                    operation="upsert",
                    payload={
                        "record_schema_version": 1,
                        "record": _organization_record(organization),
                    },
                ),
                Mutation(
                    id=command.mutation_id,
                    logical_operation_id=command.logical_operation_id,
                    organization_id=command.organization_id,
                    is_system_administration_scope=False,
                    actor_user_id=context.actor_user_id,
                    actor_role=actor_role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=context.oauth_client_id,
                    oauth_grant_id=context.oauth_grant_id,
                    client_wall_time=command.client_wall_time.astimezone(UTC),
                    command_schema_version=COMMAND_SCHEMA_VERSION,
                    command_kind=INVITE_COMMAND_KIND,
                    target_identities=[
                        {"entity_kind": "organization_membership", "entity_id": str(membership.id)}
                    ],
                    request_hash=request_hash,
                    outcome="accepted",
                    outcome_payload={"membership_id": str(membership.id), "state": "invited"},
                    first_change_sequence=first,
                    last_change_sequence=last,
                ),
            )
        )
        return result


async def remove_member(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: RemoveMemberCommand,
) -> MembershipMutationResult:
    _validate_command(command)
    request_hash = _request_hash(command)
    async with session_factory() as session, session.begin():
        await _ensure_browser_installation(session, context)
        actor_role, _ = await _authorize_and_lock_organization(
            session, context, command.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != REMOVE_COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "accepted":
                return _result_from_mutation(retained)
            raise RuntimeError("Member removal retained an invalid outcome")
        if command.client_wall_time.astimezone(UTC) > datetime.now(UTC) + timedelta(hours=24):
            raise ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
        membership = await session.scalar(
            select(OrganizationMembership)
            .where(
                OrganizationMembership.id == command.membership_id,
                OrganizationMembership.organization_id == command.organization_id,
                OrganizationMembership.state == "active",
                OrganizationMembership.role == "member",
            )
            .with_for_update(of=OrganizationMembership)
        )
        if membership is None:
            raise ApplicationServiceError("forbidden", retry_same_identity=True)
        organization = await session.scalar(
            select(Organization)
            .where(Organization.id == command.organization_id)
            .with_for_update(of=Organization)
        )
        if organization is None:
            raise ApplicationServiceError("forbidden", retry_same_identity=True)
        membership.state = "removed"
        membership.removed_at = datetime.now(UTC)
        membership.removed_by_user_id = context.actor_user_id
        first, last = await _reserve_change_range(
            session, command.organization_id, command.mutation_id, 1
        )
        result = MembershipMutationResult(
            command.mutation_id,
            command.organization_id,
            membership.id,
            "removed",
            first,
            last,
            False,
        )
        session.add_all(
            (
                OrganizationChange(
                    organization_id=command.organization_id,
                    sequence=first,
                    mutation_id=command.mutation_id,
                    entity_id=organization.id,
                    entity_kind="organization",
                    operation="upsert",
                    payload={
                        "record_schema_version": 1,
                        "record": _organization_record(organization),
                    },
                ),
                Mutation(
                    id=command.mutation_id,
                    logical_operation_id=command.logical_operation_id,
                    organization_id=command.organization_id,
                    is_system_administration_scope=False,
                    actor_user_id=context.actor_user_id,
                    actor_role=actor_role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=context.oauth_client_id,
                    oauth_grant_id=context.oauth_grant_id,
                    client_wall_time=command.client_wall_time.astimezone(UTC),
                    command_schema_version=COMMAND_SCHEMA_VERSION,
                    command_kind=REMOVE_COMMAND_KIND,
                    target_identities=[
                        {"entity_kind": "organization_membership", "entity_id": str(membership.id)}
                    ],
                    request_hash=request_hash,
                    outcome="accepted",
                    outcome_payload={"membership_id": str(membership.id), "state": "removed"},
                    first_change_sequence=first,
                    last_change_sequence=last,
                ),
            )
        )
        return result
