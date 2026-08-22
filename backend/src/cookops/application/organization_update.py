"""Scoped, synchronizable organization metadata updates."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from iso4217 import Currency
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import (
    _authorize_member_and_lock_organization,
    _reserve_change_range,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import FieldClock, Mutation, Organization, OrganizationChange

COMMAND_KIND = "organization.update"


@dataclass(frozen=True, slots=True)
class OrganizationUpdateCommand:
    mutation_id: UUID
    organization_id: UUID
    name: str
    description: str | None
    default_currency: str
    client_wall_time: datetime


@dataclass(frozen=True, slots=True)
class OrganizationUpdateResult:
    mutation_id: UUID
    organization_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


def _error(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
    )


def _wins(candidate: datetime, mutation_id: UUID, current: FieldClock | None) -> bool:
    return current is None or (candidate, mutation_id) > (
        current.winning_client_wall_time,
        current.winning_mutation_id,
    )


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload or {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        raise RuntimeError("Rejected organization update has invalid outcome payload")
    code = error.get("code")
    if code == "client_time_too_far_ahead":
        return ApplicationServiceError(code, retry_same_identity=False)
    violations = error.get("field_violations", [])
    if not isinstance(violations, list):
        raise RuntimeError("Rejected organization update has invalid violations")
    parsed = tuple(
        FieldViolation(item["path"], item["code"])
        for item in violations
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("code"), str)
    )
    return _error(parsed)


def _prepared(
    command: OrganizationUpdateCommand,
) -> tuple[tuple[FieldViolation, ...], dict[str, object]]:
    errors: list[FieldViolation] = []
    values: dict[str, object] = {}
    if not isinstance(command.mutation_id, UUID):
        errors.append(FieldViolation("mutation_id", "must_be_uuid"))
    if not isinstance(command.organization_id, UUID):
        errors.append(FieldViolation("organization_id", "must_be_uuid"))
    if (
        not isinstance(command.client_wall_time, datetime)
        or command.client_wall_time.tzinfo is None
        or command.client_wall_time.utcoffset() is None
    ):
        errors.append(FieldViolation("client_wall_time", "must_include_timezone"))
    if not isinstance(command.name, str):
        errors.append(FieldViolation("name", "must_be_string"))
    else:
        name = unicodedata.normalize("NFC", command.name).strip()
        if (
            not name
            or len(name) > 200
            or "\0" in name
            or any(0xD800 <= ord(ch) <= 0xDFFF for ch in name)
        ):
            errors.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
        else:
            values["name"] = name
    if command.description is not None:
        if (
            not isinstance(command.description, str)
            or "\0" in command.description
            or any(0xD800 <= ord(ch) <= 0xDFFF for ch in command.description)
            or len(command.description) > 10000
        ):
            errors.append(FieldViolation("description", "must_be_valid_text"))
        else:
            values["description"] = (
                unicodedata.normalize("NFC", command.description)
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
    else:
        values["description"] = None
    currency = command.default_currency.upper() if isinstance(command.default_currency, str) else ""
    if currency not in Currency.__members__:
        errors.append(FieldViolation("default_currency", "must_be_iso_currency"))
    else:
        values["default_currency"] = currency
    return tuple(errors), values


async def update_organization(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: OrganizationUpdateCommand,
) -> OrganizationUpdateResult:
    violations, values = _prepared(command)
    if violations and any(
        item.path in {"mutation_id", "organization_id", "client_wall_time"}
        for item in violations
    ):
        raise _error(violations)
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "command_kind": COMMAND_KIND,
                "organization_id": str(command.organization_id),
                **values,
                "client_wall_time": command.client_wall_time.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).digest()
    async with session_factory() as session, session.begin():
        role = await _authorize_member_and_lock_organization(
            session, context, command.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id, with_for_update=True)
        if retained:
            if (
                retained.request_hash != request_hash
                or retained.command_kind != COMMAND_KIND
                or retained.actor_user_id != context.actor_user_id
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "rejected":
                raise _retained_error(retained)
            return OrganizationUpdateResult(
                command.mutation_id,
                command.organization_id,
                retained.first_change_sequence or 0,
                retained.last_change_sequence or 0,
                True,
                cast(Literal["accepted", "partially_superseded"], retained.outcome),
            )
        if violations:
            session.add(
                Mutation(
                    id=command.mutation_id,
                    organization_id=command.organization_id,
                    is_system_administration_scope=False,
                    actor_user_id=context.actor_user_id,
                    actor_role=role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=context.oauth_client_id,
                    oauth_grant_id=context.oauth_grant_id,
                    client_wall_time=command.client_wall_time.astimezone(UTC),
                    command_schema_version=1,
                    command_kind=COMMAND_KIND,
                    target_identities=[
                        {"entity_kind": "organization", "entity_id": str(command.organization_id)}
                    ],
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload={
                        "error": {
                            "code": "validation_failed",
                            "field_violations": [
                                {"path": item.path, "code": item.code}
                                for item in violations
                            ],
                        }
                    },
                )
            )
            raise _error(violations)
        if command.client_wall_time.astimezone(UTC) > datetime.now(UTC) + timedelta(hours=24):
            error = ApplicationServiceError("client_time_too_far_ahead", retry_same_identity=False)
            session.add(
                Mutation(
                    id=command.mutation_id,
                    organization_id=command.organization_id,
                    is_system_administration_scope=False,
                    actor_user_id=context.actor_user_id,
                    actor_role=role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=context.oauth_client_id,
                    oauth_grant_id=context.oauth_grant_id,
                    client_wall_time=command.client_wall_time.astimezone(UTC),
                    command_schema_version=1,
                    command_kind=COMMAND_KIND,
                    target_identities=[
                        {"entity_kind": "organization", "entity_id": str(command.organization_id)}
                    ],
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload={"error": {"code": error.code}},
                )
            )
            raise error
        organization = await session.scalar(
            select(Organization).where(Organization.id == command.organization_id).with_for_update()
        )
        if organization is None or organization.retired_at is not None:
            raise ApplicationServiceError("forbidden", retry_same_identity=True)
        first, last = await _reserve_change_range(
            session, command.organization_id, command.mutation_id, 1
        )
        applied = 0
        for field in ("name", "description", "default_currency"):
            existing = await session.scalar(
                select(FieldClock)
                .where(
                    FieldClock.organization_id == command.organization_id,
                    FieldClock.entity_kind == "organization",
                    FieldClock.entity_id == command.organization_id,
                    FieldClock.field_name == field,
                )
                .with_for_update()
            )
            if _wins(command.client_wall_time, command.mutation_id, existing):
                setattr(organization, field, values[field])
                applied += 1
            if existing is None and _wins(command.client_wall_time, command.mutation_id, existing):
                session.add(
                    FieldClock(
                        organization_id=command.organization_id,
                        entity_kind="organization",
                        entity_id=command.organization_id,
                        field_name=field,
                        winning_client_wall_time=command.client_wall_time,
                        winning_mutation_id=command.mutation_id,
                    )
                )
            elif existing is not None and _wins(
                command.client_wall_time, command.mutation_id, existing
            ):
                existing.winning_client_wall_time, existing.winning_mutation_id = (
                    command.client_wall_time,
                    command.mutation_id,
                )
        clocks = (
            await session.scalars(
                select(FieldClock).where(
                    FieldClock.organization_id == command.organization_id,
                    FieldClock.entity_kind == "organization",
                    FieldClock.entity_id == command.organization_id,
                )
            )
        ).all()
        record = {
            "id": str(organization.id),
            "organization_id": str(organization.id),
            "name": organization.name,
            "description": organization.description,
            "default_currency": organization.default_currency,
            "field_clocks": {
                clock.field_name: {
                    "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                    "winning_mutation_id": str(clock.winning_mutation_id),
                }
                for clock in clocks
            },
        }
        session.add(
            OrganizationChange(
                organization_id=command.organization_id,
                sequence=first,
                mutation_id=command.mutation_id,
                entity_id=command.organization_id,
                entity_kind="organization",
                operation="upsert",
                payload={"record_schema_version": 1, "record": record},
            )
        )
        session.add(
            Mutation(
                id=command.mutation_id,
                organization_id=command.organization_id,
                is_system_administration_scope=False,
                actor_user_id=context.actor_user_id,
                actor_role=role,
                client_installation_id=context.client_installation_id,
                oauth_client_id=context.oauth_client_id,
                oauth_grant_id=context.oauth_grant_id,
                client_wall_time=command.client_wall_time.astimezone(UTC),
                command_schema_version=1,
                command_kind=COMMAND_KIND,
                target_identities=[
                    {"entity_kind": "organization", "entity_id": str(command.organization_id)}
                ],
                request_hash=request_hash,
                outcome="accepted" if applied == 3 else "partially_superseded",
                first_change_sequence=first,
                last_change_sequence=last,
                outcome_payload={"organization": record},
            )
        )
        return OrganizationUpdateResult(
            command.mutation_id, command.organization_id, first, last, False,
            "accepted" if applied == 3 else "partially_superseded",
        )
