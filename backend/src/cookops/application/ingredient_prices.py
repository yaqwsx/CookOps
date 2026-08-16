"""Publish immutable catalog ingredient prices."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from iso4217 import Currency
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import _reserve_change_range
from cookops.application.ingredient_lifecycle import _record
from cookops.application.ingredients import (
    _authorize,
    _decimal_text,
    _error_payload,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import (
    FieldClock,
    Ingredient,
    IngredientPriceEstimate,
    IngredientVersion,
    Mutation,
    OrganizationChange,
    UnitDefinition,
)

COMMAND_KIND = "ingredient.publish_price_estimate"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PublishIngredientPriceEstimateCommand:
    mutation_id: UUID
    ingredient_id: UUID
    ingredient_price_estimate_id: UUID
    organization_id: UUID
    amount: Decimal
    priced_quantity: Decimal
    unit_id: UUID
    currency: str
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PublishIngredientPriceEstimateResult:
    mutation_id: UUID
    ingredient_id: UUID
    ingredient_price_estimate_id: UUID
    organization_id: UUID
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted", "partially_superseded"] = "accepted"


def _hash(command: PublishIngredientPriceEstimateCommand) -> bytes:
    def encode(value: object) -> object:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        if isinstance(value, Decimal):
            return _decimal_text(value) if value.is_finite() else {"invalid": str(value)}
        if value is None or isinstance(value, str):
            return value
        return {"invalid": type(value).__name__}

    value = {key: encode(getattr(command, key)) for key in command.__dataclass_fields__}
    value.update(command_kind=COMMAND_KIND, command_schema_version=COMMAND_SCHEMA_VERSION)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _retained_price_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict) or error.get("code") not in {
        "validation_failed",
        "stale_precondition",
    }:
        raise RuntimeError("Rejected price mutation has an invalid outcome payload")
    raw = error.get("field_violations", [])
    if not isinstance(raw, list):
        raise RuntimeError("Rejected price mutation has an invalid outcome payload")
    violations = tuple(
        FieldViolation(item["path"], item["code"])
        for item in raw
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("code"), str)
    )
    if len(violations) != len(raw):
        raise RuntimeError("Rejected price mutation has an invalid outcome payload")
    return ApplicationServiceError(
        error["code"], field_violations=violations, retry_same_identity=False
    )


async def publish_ingredient_price_estimate(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: PublishIngredientPriceEstimateCommand,
) -> PublishIngredientPriceEstimateResult:
    violations: list[FieldViolation] = []
    for name, value in (
        ("mutation_id", command.mutation_id),
        ("ingredient_id", command.ingredient_id),
        ("ingredient_price_estimate_id", command.ingredient_price_estimate_id),
        ("organization_id", command.organization_id),
        ("unit_id", command.unit_id),
    ):
        if not isinstance(value, UUID):
            violations.append(FieldViolation(name, "must_be_uuid"))
    for name, decimal_value, positive in (
        ("amount", command.amount, False),
        ("priced_quantity", command.priced_quantity, True),
    ):
        bounded = (
            isinstance(decimal_value, Decimal)
            and decimal_value.is_finite()
            and abs(decimal_value.adjusted()) <= 100
        )
        if (
            not bounded
            or (decimal_value < 0 if not positive else decimal_value <= 0)
        ):
            violations.append(FieldViolation(name, "must_be_positive_finite_decimal"))
    currency = command.currency.strip().upper() if isinstance(command.currency, str) else ""
    if currency not in Currency.__members__:
        violations.append(FieldViolation("currency", "must_be_iso_4217_code"))
    when = command.client_wall_time
    if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
        when = datetime(1970, 1, 1, tzinfo=UTC)
    else:
        when = when.astimezone(UTC)
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    request_hash = _hash(command)
    result = None
    deferred: ApplicationServiceError | None = None
    async with session_factory() as session, session.begin():
        role, default_currency = await _authorize(session, context, command.organization_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", command.mutation_id)},
        )
        retained = await session.get(Mutation, command.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome in {"accepted", "partially_superseded"}:
                return PublishIngredientPriceEstimateResult(
                    command.mutation_id,
                    command.ingredient_id,
                    command.ingredient_price_estimate_id,
                    command.organization_id,
                    retained.first_change_sequence or 0,
                    retained.last_change_sequence or 0,
                    True,
                    retained.outcome,
                )
            raise _retained_price_error(retained)
        if violations:
            deferred = ApplicationServiceError(
                "validation_failed", field_violations=tuple(violations), retry_same_identity=False
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("ingredient", command.ingredient_id)},
            )
            ingredient = await session.scalar(
                select(Ingredient)
                .where(
                    Ingredient.id == command.ingredient_id,
                    Ingredient.organization_id == command.organization_id,
                )
                .with_for_update(of=Ingredient)
            )
            version = await session.scalar(
                select(IngredientVersion).where(
                    IngredientVersion.id == ingredient.current_version_id
                    if ingredient
                    else text("false")
                )
            )
            unit = await session.scalar(
                select(UnitDefinition)
                .where(UnitDefinition.id == command.unit_id)
                .with_for_update(of=UnitDefinition)
            )
            exists = await session.scalar(
                select(IngredientPriceEstimate.id).where(
                    IngredientPriceEstimate.id == command.ingredient_price_estimate_id
                )
            )
            if ingredient is None or ingredient.retired_at is not None or version is None:
                deferred = ApplicationServiceError("stale_precondition", retry_same_identity=False)
            elif exists is not None:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation("ingredient_price_estimate_id", "already_exists"),
                    ),
                    retry_same_identity=False,
                )
            elif currency != default_currency:
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(
                        FieldViolation("currency", "must_match_organization_default"),
                    ),
                    retry_same_identity=False,
                )
            elif (
                unit is None
                or unit.retired_at is not None
                or not unit.allows_ingredient_quantity
                or (
                    unit.organization_id is not None
                    and unit.organization_id != command.organization_id
                )
                or unit.dimension
                != (
                    await session.scalar(
                        select(UnitDefinition.dimension).where(
                            UnitDefinition.id == version.canonical_unit_id
                        )
                    )
                )
                or (unit.dimension in ("count", "custom") and unit.id != version.canonical_unit_id)
            ):
                deferred = ApplicationServiceError(
                    "validation_failed",
                    field_violations=(FieldViolation("unit_id", "must_be_compatible_active_unit"),),
                    retry_same_identity=False,
                )
            if deferred is None:
                assert ingredient is not None and unit is not None
                clock = await session.scalar(
                    select(FieldClock)
                    .where(
                        FieldClock.organization_id == command.organization_id,
                        FieldClock.entity_kind == "ingredient",
                        FieldClock.entity_id == ingredient.id,
                        FieldClock.field_name == "current_price_estimate_id",
                    )
                    .with_for_update(of=FieldClock)
                )
                wins = clock is None or (when, command.mutation_id) > (
                    clock.winning_client_wall_time,
                    clock.winning_mutation_id,
                )
                estimate = IngredientPriceEstimate(
                    id=command.ingredient_price_estimate_id,
                    organization_id=command.organization_id,
                    ingredient_id=ingredient.id,
                    based_on_estimate_id=ingredient.current_price_estimate_id,
                    state="available",
                    price_amount=command.amount,
                    priced_quantity=command.priced_quantity,
                    priced_unit_id=command.unit_id,
                    currency=currency,
                    published_by_user_id=context.actor_user_id,
                )
                session.add(estimate)
                await session.flush()
                if wins:
                    ingredient.current_price_estimate_id = estimate.id
                clocks = (
                    await session.scalars(
                        select(FieldClock).where(
                            FieldClock.organization_id == command.organization_id,
                            FieldClock.entity_kind == "ingredient",
                            FieldClock.entity_id == ingredient.id,
                        )
                    )
                ).all()
                record = _record(ingredient, list(clocks))
                field_clocks = record["field_clocks"]
                assert isinstance(field_clocks, dict)
                if wins:
                    field_clocks["current_price_estimate_id"] = {
                        "winning_client_wall_time": when.isoformat(),
                        "winning_mutation_id": str(command.mutation_id),
                    }
                elif clock:
                    field_clocks["current_price_estimate_id"] = {
                        "winning_client_wall_time": clock.winning_client_wall_time.isoformat(),
                        "winning_mutation_id": str(clock.winning_mutation_id),
                    }
                price_record = {
                    "id": str(estimate.id),
                    "organization_id": str(estimate.organization_id),
                    "ingredient_id": str(estimate.ingredient_id),
                    "based_on_estimate_id": str(estimate.based_on_estimate_id)
                    if estimate.based_on_estimate_id
                    else None,
                    "state": "available",
                    "price_amount": _decimal_text(command.amount),
                    "priced_quantity": _decimal_text(command.priced_quantity),
                    "priced_unit_id": str(command.unit_id),
                    "currency": currency,
                    "published_by_user_id": str(context.actor_user_id),
                    "immutable": True,
                }
                records = [
                    ("ingredient_price_estimate", estimate.id, price_record),
                    ("ingredient", ingredient.id, record),
                ]
                first, last = await _reserve_change_range(
                    session, command.organization_id, command.mutation_id, 2
                )
                session.add_all(
                    OrganizationChange(
                        organization_id=command.organization_id,
                        sequence=first + i,
                        mutation_id=command.mutation_id,
                        entity_id=entity_id,
                        entity_kind=kind,
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": payload},
                    )
                    for i, (kind, entity_id, payload) in enumerate(records)
                )
                if wins:
                    if clock is None:
                        session.add(
                            FieldClock(
                                organization_id=command.organization_id,
                                entity_kind="ingredient",
                                entity_id=ingredient.id,
                                field_name="current_price_estimate_id",
                                winning_client_wall_time=when,
                                winning_mutation_id=command.mutation_id,
                            )
                        )
                    else:
                        clock.winning_client_wall_time, clock.winning_mutation_id = (
                            when,
                            command.mutation_id,
                        )
                session.add(
                    Mutation(
                        id=command.mutation_id,
                        logical_operation_id=command.logical_operation_id,
                        organization_id=command.organization_id,
                        is_system_administration_scope=False,
                        actor_user_id=context.actor_user_id,
                        actor_role=role,
                        client_installation_id=context.client_installation_id,
                        oauth_client_id=context.oauth_client_id,
                        oauth_grant_id=context.oauth_grant_id,
                        client_wall_time=when,
                        command_schema_version=1,
                        command_kind=COMMAND_KIND,
                        target_identities=[
                            {"entity_kind": "ingredient", "entity_id": str(ingredient.id)}
                        ],
                        request_hash=request_hash,
                        outcome="accepted" if wins else "partially_superseded",
                        outcome_payload={"ingredient_price_estimate": price_record},
                        first_change_sequence=first,
                        last_change_sequence=last,
                    )
                )
                result = PublishIngredientPriceEstimateResult(
                    command.mutation_id,
                    ingredient.id,
                    estimate.id,
                    command.organization_id,
                    first,
                    last,
                    False,
                    "accepted" if wins else "partially_superseded",
                )
        if deferred is not None:
            session.add(
                Mutation(
                    id=command.mutation_id,
                    logical_operation_id=command.logical_operation_id,
                    organization_id=command.organization_id,
                    is_system_administration_scope=False,
                    actor_user_id=context.actor_user_id,
                    actor_role=role,
                    client_installation_id=context.client_installation_id,
                    oauth_client_id=context.oauth_client_id,
                    oauth_grant_id=context.oauth_grant_id,
                    client_wall_time=when,
                    command_schema_version=1,
                    command_kind=COMMAND_KIND,
                    target_identities=[
                        {"entity_kind": "ingredient", "entity_id": str(command.ingredient_id)}
                    ],
                    request_hash=request_hash,
                    outcome="rejected",
                    outcome_payload=_error_payload(deferred),
                )
            )
    if deferred is not None:
        raise deferred
    assert result is not None
    return result
