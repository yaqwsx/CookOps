"""Event-local catalog price refresh commands."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import case, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import (
    _authorize_member_and_lock_organization,
    _event_change_record,
    _event_field_clocks,
    _reserve_change_range,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import (
    Event,
    EventIngredientPrice,
    EventIngredientPriceSnapshot,
    Ingredient,
    IngredientPriceEstimate,
    IngredientVersion,
    Mutation,
    OrganizationChange,
    RecipeVersion,
    RecipeVersionIngredientLine,
    ScheduledIngredientOverride,
    ScheduledRecipe,
)

COMMAND_KIND = "event.update_price_estimates"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class UpdateEventPriceEstimatesCommand:
    mutation_id: UUID
    organization_id: UUID
    event_id: UUID
    client_wall_time: datetime
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class UpdateEventPriceEstimatesResult:
    mutation_id: UUID
    organization_id: UUID
    event_id: UUID
    price_snapshot_ids: tuple[UUID, ...]
    unavailable_ingredient_ids: tuple[UUID, ...]
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


@dataclass(frozen=True, slots=True)
class _Prepared:
    mutation_id: UUID
    organization_id: UUID
    event_id: UUID
    client_wall_time: datetime
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


@dataclass(frozen=True, slots=True)
class _InitialPriceCapture:
    """One first-use capture whose stream was locked for this transaction."""

    ingredient_id: UUID
    price: EventIngredientPrice
    estimate: IngredientPriceEstimate | None
    available: bool


def _decimal_record_value(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _snapshot_record(snapshot: EventIngredientPriceSnapshot) -> dict[str, object]:
    """Return the complete immutable sync record, rather than a pointer surrogate."""

    return {
        "id": str(snapshot.id),
        "organization_id": str(snapshot.organization_id),
        "event_id": str(snapshot.event_id),
        "ingredient_id": str(snapshot.ingredient_id),
        "event_ingredient_price_id": str(snapshot.event_ingredient_price_id),
        "previous_snapshot_id": (
            str(snapshot.previous_snapshot_id) if snapshot.previous_snapshot_id else None
        ),
        "source_ingredient_price_estimate_id": (
            str(snapshot.source_ingredient_price_estimate_id)
            if snapshot.source_ingredient_price_estimate_id
            else None
        ),
        "state": snapshot.state,
        "price_amount": _decimal_record_value(snapshot.price_amount),
        "priced_quantity": _decimal_record_value(snapshot.priced_quantity),
        "priced_unit_id": str(snapshot.priced_unit_id) if snapshot.priced_unit_id else None,
        "currency": snapshot.currency,
        "captured_by_user_id": str(snapshot.captured_by_user_id),
        "effective_client_action_time": snapshot.effective_client_action_time.isoformat(),
        "server_received_at": snapshot.server_received_at.isoformat(),
        "originating_mutation_id": str(snapshot.originating_mutation_id),
    }


def _price_pointer_record(price: EventIngredientPrice) -> dict[str, object]:
    assert price.current_snapshot_id is not None
    return {
        "id": str(price.id),
        "organization_id": str(price.organization_id),
        "event_id": str(price.event_id),
        "ingredient_id": str(price.ingredient_id),
        "current_snapshot_id": str(price.current_snapshot_id),
        "created_at": price.created_at.isoformat(),
        "created_by_user_id": str(price.created_by_user_id),
    }


async def _prepare_initial_event_price_captures(
    session: AsyncSession,
    *,
    organization_id: UUID,
    event: Event,
    ingredient_ids: set[UUID],
    actor_user_id: UUID,
) -> tuple[_InitialPriceCapture, ...]:
    """Lock and prepare only streams not yet known to an event.

    The presence of a stream is the durable first-use marker.  Consequently a
    removed ingredient which later becomes relevant again keeps its original
    capture instead of silently adopting a new catalog price.
    """

    if not ingredient_ids:
        return ()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _advisory_lock_key("event_price_capture", event.id)},
    )
    existing = (
        await session.scalars(
            select(EventIngredientPrice)
            .where(
                EventIngredientPrice.organization_id == organization_id,
                EventIngredientPrice.event_id == event.id,
                EventIngredientPrice.ingredient_id.in_(ingredient_ids),
            )
            .with_for_update(of=EventIngredientPrice)
        )
    ).all()
    known = {price.ingredient_id for price in existing}
    captures: list[_InitialPriceCapture] = []
    for ingredient_id in sorted(ingredient_ids - known, key=str):
        price = EventIngredientPrice(
            id=uuid4(),
            organization_id=organization_id,
            event_id=event.id,
            ingredient_id=ingredient_id,
            created_by_user_id=actor_user_id,
        )
        ingredient = await session.scalar(
            select(Ingredient).where(
                Ingredient.id == ingredient_id,
                Ingredient.organization_id == organization_id,
            )
        )
        estimate = None
        if ingredient is not None and ingredient.current_price_estimate_id is not None:
            estimate = await session.scalar(
                select(IngredientPriceEstimate).where(
                    IngredientPriceEstimate.id == ingredient.current_price_estimate_id,
                    IngredientPriceEstimate.ingredient_id == ingredient_id,
                )
            )
        available = (
            estimate is not None
            and estimate.state == "available"
            and estimate.currency == event.currency
        )
        session.add(price)
        captures.append(_InitialPriceCapture(ingredient_id, price, estimate, available))
    return tuple(captures)


async def _emit_event_price_snapshots(
    session: AsyncSession,
    *,
    captures: tuple[_InitialPriceCapture, ...],
    organization_id: UUID,
    event: Event,
    actor_user_id: UUID,
    client_wall_time: datetime,
    originating_mutation: Mutation,
    first_change_sequence: int,
) -> tuple[UUID, ...]:
    """Append immutable snapshots and paired pointer records in one transaction."""

    snapshots: list[EventIngredientPriceSnapshot] = []
    for capture in captures:
        estimate = capture.estimate
        available_estimate = cast(IngredientPriceEstimate, estimate) if capture.available else None
        snapshot = EventIngredientPriceSnapshot(
            id=uuid4(),
            organization_id=organization_id,
            event_id=event.id,
            ingredient_id=capture.ingredient_id,
            event_ingredient_price_id=capture.price.id,
            previous_snapshot_id=capture.price.current_snapshot_id,
            source_ingredient_price_estimate_id=estimate.id if estimate is not None else None,
            state="available" if capture.available else "unavailable",
            price_amount=available_estimate.price_amount if available_estimate else None,
            priced_quantity=available_estimate.priced_quantity if available_estimate else None,
            priced_unit_id=available_estimate.priced_unit_id if available_estimate else None,
            currency=available_estimate.currency if available_estimate else None,
            captured_by_user_id=actor_user_id,
            effective_client_action_time=client_wall_time,
            server_received_at=originating_mutation.server_received_at,
            originating_mutation_id=originating_mutation.id,
        )
        snapshots.append(snapshot)
        session.add(snapshot)
    await session.flush()
    for index, (capture, snapshot) in enumerate(zip(captures, snapshots, strict=True)):
        capture.price.current_snapshot_id = snapshot.id
        sequence = first_change_sequence + index * 2
        session.add_all(
            (
                OrganizationChange(
                    organization_id=organization_id,
                    sequence=sequence,
                    mutation_id=originating_mutation.id,
                    entity_id=snapshot.id,
                    entity_kind="event_ingredient_price_snapshot",
                    operation="upsert",
                    payload={"record_schema_version": 1, "record": _snapshot_record(snapshot)},
                ),
                OrganizationChange(
                    organization_id=organization_id,
                    sequence=sequence + 1,
                    mutation_id=originating_mutation.id,
                    entity_id=capture.price.id,
                    entity_kind="event_ingredient_price",
                    operation="upsert",
                    payload={
                        "record_schema_version": 1,
                        "record": _price_pointer_record(capture.price),
                    },
                ),
            )
        )
    return tuple(snapshot.id for snapshot in snapshots)


def _invalid(value: object) -> dict[str, str]:
    return {"invalid_type": type(value).__qualname__, "repr": repr(value)}


def _raw_uuid(value: object) -> str | dict[str, str] | None:
    return (
        str(value) if isinstance(value, UUID) else (_invalid(value) if value is not None else None)
    )


def _raw_time(value: object) -> str | dict[str, str]:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return _invalid(value)


def _request_hash(command: UpdateEventPriceEstimatesCommand) -> bytes:
    value = {
        "command_kind": COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "mutation_id": _raw_uuid(command.mutation_id),
        "organization_id": _raw_uuid(command.organization_id),
        "event_id": _raw_uuid(command.event_id),
        "client_wall_time": _raw_time(command.client_wall_time),
        "logical_operation_id": _raw_uuid(command.logical_operation_id),
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()


def _prepare(command: UpdateEventPriceEstimatesCommand) -> _Prepared:
    violations: list[FieldViolation] = []
    for path, value in (
        ("mutation_id", command.mutation_id),
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
    return _Prepared(
        command.mutation_id if isinstance(command.mutation_id, UUID) else UUID(int=0),
        command.organization_id if isinstance(command.organization_id, UUID) else UUID(int=0),
        command.event_id if isinstance(command.event_id, UUID) else UUID(int=0),
        command.client_wall_time.astimezone(UTC) if has_time else datetime(1970, 1, 1, tzinfo=UTC),
        command.logical_operation_id if isinstance(command.logical_operation_id, UUID) else None,
        tuple(violations),
    )


def _error(error: ApplicationServiceError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "field_violations": [{"path": v.path, "code": v.code} for v in error.field_violations],
        }
    }


def _validation(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
    return ApplicationServiceError(
        "validation_failed", field_violations=violations, retry_same_identity=False
    )


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    """Reconstruct only well-formed deterministic rejection outcomes."""

    error = mutation.outcome_payload.get("error") if mutation.outcome_payload else None
    if not isinstance(error, dict):
        raise RuntimeError("Retained price refresh rejection has an invalid payload")
    try:
        code = error.get("code")
        if code not in ("validation_failed", "archived_event", "client_time_too_far_ahead"):
            raise TypeError
        raw_violations = error.get("field_violations")
        if raw_violations is None and code != "validation_failed":
            raw_violations = []
        if not isinstance(raw_violations, list):
            raise TypeError
        violations = tuple(
            FieldViolation(str(item["path"]), str(item["code"]))
            for item in raw_violations
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("code"), str)
        )
        if len(violations) != len(raw_violations):
            raise TypeError
    except TypeError as error_value:
        raise RuntimeError(
            "Retained price refresh rejection has an invalid payload"
        ) from error_value
    return ApplicationServiceError(
        cast(Literal["validation_failed", "archived_event", "client_time_too_far_ahead"], code),
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
        command_kind=COMMAND_KIND,
        target_identities=[{"entity_kind": "event", "entity_id": str(prepared.event_id)}],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


def _result_payload(result: UpdateEventPriceEstimatesResult) -> dict[str, object]:
    return {
        "event_price_refresh": {
            "event_id": str(result.event_id),
            "organization_id": str(result.organization_id),
            "price_snapshot_ids": [str(value) for value in result.price_snapshot_ids],
            "unavailable_ingredient_ids": [
                str(value) for value in result.unavailable_ingredient_ids
            ],
        }
    }


def _retained_result(mutation: Mutation) -> UpdateEventPriceEstimatesResult:
    payload = (
        mutation.outcome_payload.get("event_price_refresh") if mutation.outcome_payload else None
    )
    if (
        not isinstance(payload, dict)
        or mutation.first_change_sequence is None
        or mutation.last_change_sequence is None
    ):
        raise RuntimeError("Retained price refresh has an invalid payload")
    try:
        snapshot_ids = tuple(UUID(str(value)) for value in payload["price_snapshot_ids"])
        unavailable = tuple(UUID(str(value)) for value in payload["unavailable_ingredient_ids"])
        return UpdateEventPriceEstimatesResult(
            mutation.id,
            UUID(str(payload["organization_id"])),
            UUID(str(payload["event_id"])),
            snapshot_ids,
            unavailable,
            mutation.first_change_sequence,
            mutation.last_change_sequence,
            True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Retained price refresh has an invalid payload") from error


async def _resolved_ingredient_ids(session: AsyncSession, prepared: _Prepared) -> set[UUID]:
    replacement = ScheduledIngredientOverride.__table__.alias("replacement")
    ordinary_quantity = case(
        (replacement.c.id.is_not(None), replacement.c.quantity),
        (
            RecipeVersionIngredientLine.scaling_behavior == "fixed",
            RecipeVersionIngredientLine.base_quantity,
        ),
        else_=RecipeVersionIngredientLine.base_quantity
        * ScheduledRecipe.selected_scale_amount
        / RecipeVersion.base_scaling_amount,
    )
    ordinary = await session.scalars(
        select(IngredientVersion.ingredient_id)
        .select_from(ScheduledRecipe)
        .join(RecipeVersion, RecipeVersion.id == ScheduledRecipe.recipe_version_id)
        .join(
            RecipeVersionIngredientLine,
            RecipeVersionIngredientLine.recipe_version_id == ScheduledRecipe.recipe_version_id,
        )
        .join(
            IngredientVersion,
            IngredientVersion.id == RecipeVersionIngredientLine.ingredient_version_id,
        )
        .outerjoin(
            replacement,
            (replacement.c.scheduled_recipe_id == ScheduledRecipe.id)
            & (replacement.c.override_kind == "replace")
            & (replacement.c.target_line_key == RecipeVersionIngredientLine.line_key)
            & replacement.c.retired_at.is_(None),
        )
        .where(
            ScheduledRecipe.event_id == prepared.event_id,
            ScheduledRecipe.organization_id == prepared.organization_id,
            ScheduledRecipe.retired_at.is_(None),
            ordinary_quantity > 0,
        )
    )
    added = await session.scalars(
        select(ScheduledIngredientOverride.ingredient_id)
        .join(
            ScheduledRecipe, ScheduledRecipe.id == ScheduledIngredientOverride.scheduled_recipe_id
        )
        .where(
            ScheduledIngredientOverride.event_id == prepared.event_id,
            ScheduledIngredientOverride.organization_id == prepared.organization_id,
            ScheduledIngredientOverride.override_kind == "add",
            ScheduledIngredientOverride.retired_at.is_(None),
            ScheduledIngredientOverride.quantity > 0,
            ScheduledRecipe.retired_at.is_(None),
        )
    )
    return set(ordinary).union(added)


async def update_event_price_estimates(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: UpdateEventPriceEstimatesCommand,
) -> UpdateEventPriceEstimatesResult:
    """Atomically capture server-current catalog prices for every relevant event ingredient."""
    prepared, request_hash = _prepare(command), _request_hash(command)
    deferred: ApplicationServiceError | None = None
    result: UpdateEventPriceEstimatesResult | None = None
    async with session_factory() as session, session.begin():
        role = await _authorize_member_and_lock_organization(
            session, context, prepared.organization_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key("mutation", prepared.mutation_id)},
        )
        retained = await session.get(Mutation, prepared.mutation_id)
        if retained is not None:
            if (
                retained.actor_user_id != context.actor_user_id
                or retained.command_kind != COMMAND_KIND
                or retained.command_schema_version != COMMAND_SCHEMA_VERSION
                or retained.request_hash != request_hash
            ):
                raise ApplicationServiceError("idempotency_mismatch", retry_same_identity=False)
            if retained.outcome == "accepted":
                return _retained_result(retained)
            if retained.outcome == "rejected":
                deferred = _retained_error(retained)
            else:
                raise RuntimeError("Retained price refresh has an unsupported outcome")
        elif prepared.violations:
            deferred = _validation(prepared.violations)
        elif prepared.client_wall_time > datetime.now(UTC) + timedelta(hours=24):
            deferred = ApplicationServiceError(
                "client_time_too_far_ahead", retry_same_identity=False
            )
        if deferred is not None:
            if retained is None:
                session.add(
                    _mutation(prepared, context, role, request_hash, "rejected", _error(deferred))
                )
        elif retained is None:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("event_price_refresh", prepared.event_id)},
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
            if deferred is not None:
                session.add(
                    _mutation(prepared, context, role, request_hash, "rejected", _error(deferred))
                )
            else:
                assert event is not None
                existing = (
                    await session.scalars(
                        select(EventIngredientPrice)
                        .where(
                            EventIngredientPrice.event_id == event.id,
                            EventIngredientPrice.organization_id == prepared.organization_id,
                        )
                        .with_for_update(of=EventIngredientPrice)
                    )
                ).all()
                by_ingredient = {value.ingredient_id: value for value in existing}
                target_ids = set(by_ingredient).union(
                    await _resolved_ingredient_ids(session, prepared)
                )
                for ingredient_id in sorted(target_ids, key=str):
                    if ingredient_id not in by_ingredient:
                        price = EventIngredientPrice(
                            id=uuid4(),
                            organization_id=prepared.organization_id,
                            event_id=event.id,
                            ingredient_id=ingredient_id,
                            created_by_user_id=context.actor_user_id,
                        )
                        session.add(price)
                        by_ingredient[ingredient_id] = price
                # A refresh publishes both sides of every price revision: the
                # complete immutable snapshot and the mutable stream pointer.
                # They are one transaction group, so a replica cannot render a
                # pointer to a snapshot it has not received.
                change_count = max(1, len(target_ids) * 2)
                first, last = await _reserve_change_range(
                    session, prepared.organization_id, prepared.mutation_id, change_count
                )
                mutation = _mutation(
                    prepared, context, role, request_hash, "accepted", {}, first, last
                )
                session.add(mutation)
                await session.flush()
                snapshot_ids: list[UUID] = []
                unavailable: list[UUID] = []
                pointer_updates: list[tuple[EventIngredientPrice, UUID]] = []
                for ingredient_id in sorted(target_ids, key=str):
                    price = by_ingredient[ingredient_id]
                    ingredient = await session.scalar(
                        select(Ingredient).where(
                            Ingredient.id == ingredient_id,
                            Ingredient.organization_id == prepared.organization_id,
                        )
                    )
                    estimate = None
                    if ingredient is not None and ingredient.current_price_estimate_id is not None:
                        estimate = await session.scalar(
                            select(IngredientPriceEstimate).where(
                                IngredientPriceEstimate.id == ingredient.current_price_estimate_id,
                                IngredientPriceEstimate.ingredient_id == ingredient_id,
                            )
                        )
                    available = (
                        estimate is not None
                        and estimate.state == "available"
                        and estimate.currency == event.currency
                    )
                    snapshot_id = uuid4()
                    snapshot_ids.append(snapshot_id)
                    if not available:
                        unavailable.append(ingredient_id)
                    available_estimate = (
                        cast(IngredientPriceEstimate, estimate) if available else None
                    )
                    session.add(
                        EventIngredientPriceSnapshot(
                            id=snapshot_id,
                            organization_id=prepared.organization_id,
                            event_id=event.id,
                            ingredient_id=ingredient_id,
                            event_ingredient_price_id=price.id,
                            previous_snapshot_id=price.current_snapshot_id,
                            source_ingredient_price_estimate_id=estimate.id
                            if estimate is not None
                            else None,
                            state="available" if available else "unavailable",
                            price_amount=available_estimate.price_amount
                            if available_estimate
                            else None,
                            priced_quantity=available_estimate.priced_quantity
                            if available_estimate
                            else None,
                            priced_unit_id=available_estimate.priced_unit_id
                            if available_estimate
                            else None,
                            currency=available_estimate.currency if available_estimate else None,
                            captured_by_user_id=context.actor_user_id,
                            effective_client_action_time=prepared.client_wall_time,
                            server_received_at=mutation.server_received_at,
                            originating_mutation_id=prepared.mutation_id,
                        )
                    )
                    pointer_updates.append((price, snapshot_id))
                # The database trigger intentionally accepts an append only while the
                # stream still points at its predecessor. Flush immutable snapshots
                # before moving all mutable pointers together.
                await session.flush()
                for price, snapshot_id in pointer_updates:
                    price.current_snapshot_id = snapshot_id
                result = UpdateEventPriceEstimatesResult(
                    prepared.mutation_id,
                    prepared.organization_id,
                    event.id,
                    tuple(snapshot_ids),
                    tuple(unavailable),
                    first,
                    last,
                    False,
                )
                mutation.outcome_payload = _result_payload(result)
                if target_ids:
                    snapshots_by_ingredient = {
                        snapshot.ingredient_id: snapshot
                        for snapshot in await session.scalars(
                            select(EventIngredientPriceSnapshot).where(
                                EventIngredientPriceSnapshot.id.in_(snapshot_ids)
                            )
                        )
                    }
                    for index, ingredient_id in enumerate(sorted(target_ids, key=str)):
                        price = by_ingredient[ingredient_id]
                        snapshot = snapshots_by_ingredient[ingredient_id]
                        session.add_all(
                            (
                                OrganizationChange(
                                    organization_id=prepared.organization_id,
                                    sequence=first + index * 2,
                                    mutation_id=prepared.mutation_id,
                                    entity_id=snapshot.id,
                                    entity_kind="event_ingredient_price_snapshot",
                                    operation="upsert",
                                    payload={
                                        "record_schema_version": 1,
                                        "record": _snapshot_record(snapshot),
                                    },
                                ),
                                OrganizationChange(
                                    organization_id=prepared.organization_id,
                                    sequence=first + index * 2 + 1,
                                    mutation_id=prepared.mutation_id,
                                    entity_id=price.id,
                                    entity_kind="event_ingredient_price",
                                    operation="upsert",
                                    payload={
                                        "record_schema_version": 1,
                                        "record": _price_pointer_record(price),
                                    },
                                ),
                            )
                        )
                else:
                    event_clocks = await _event_field_clocks(
                        session, prepared.organization_id, event.id
                    )
                    event_kind, event_entity_id, event_record = _event_change_record(
                        event, field_clocks=event_clocks
                    )
                    session.add(
                        OrganizationChange(
                            organization_id=prepared.organization_id,
                            sequence=first,
                            mutation_id=prepared.mutation_id,
                            entity_id=event_entity_id,
                            entity_kind=event_kind,
                            operation="upsert",
                            payload={
                                "record_schema_version": 1,
                                "record": event_record,
                            },
                        )
                    )
    if deferred is not None:
        raise deferred
    if result is None:
        raise RuntimeError("Updating event price estimates produced no outcome")
    return result
