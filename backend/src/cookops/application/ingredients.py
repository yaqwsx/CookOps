"""Ingredient-catalog application services."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from iso4217 import Currency
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.events import _reserve_change_range
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
    _advisory_lock_key,
)
from cookops.persistence.models import (
    ClientInstallation,
    DietaryTag,
    Ingredient,
    IngredientPriceEstimate,
    IngredientVersion,
    IngredientVersionDietaryTag,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    StoreSection,
    SystemRoleAssignment,
    UnitDefinition,
    User,
)

COMMAND_KIND = "ingredient.create"
COMMAND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class InitialPrice:
    id: UUID
    amount: Decimal
    quantity: Decimal
    unit_id: UUID
    currency: str


@dataclass(frozen=True, slots=True)
class CreateIngredientCommand:
    mutation_id: UUID
    ingredient_id: UUID
    ingredient_version_id: UUID
    organization_id: UUID
    name: str
    canonical_unit_id: UUID
    mass_per_canonical_quantity: Decimal
    client_wall_time: datetime
    dietary_tag_ids: tuple[UUID, ...] = ()
    default_store_section_id: UUID | None = None
    initial_price: InitialPrice | None = None
    logical_operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CreateIngredientResult:
    mutation_id: UUID
    ingredient_id: UUID
    ingredient_version_id: UUID
    organization_id: UUID
    name: str
    normalized_name: str
    canonical_unit_id: UUID
    mass_per_canonical_quantity: Decimal
    dietary_tag_ids: tuple[UUID, ...]
    default_store_section_id: UUID | None
    initial_price_id: UUID | None
    first_change_sequence: int
    last_change_sequence: int
    replayed: bool
    outcome: Literal["accepted"] = "accepted"


@dataclass(frozen=True, slots=True)
class _PreparedCommand:
    mutation_id: UUID
    ingredient_id: UUID
    ingredient_version_id: UUID
    organization_id: UUID
    name: str
    normalized_name: str
    canonical_unit_id: UUID
    mass_per_canonical_quantity: Decimal
    client_wall_time: datetime
    dietary_tag_ids: tuple[UUID, ...]
    default_store_section_id: UUID | None
    initial_price: InitialPrice | None
    logical_operation_id: UUID | None
    violations: tuple[FieldViolation, ...]


def _canonical_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _decimal_text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _uuid(value: object, path: str, violations: list[FieldViolation]) -> UUID:
    if isinstance(value, UUID):
        return value
    violations.append(FieldViolation(path, "must_be_uuid"))
    return UUID(int=0)


def _prepare_command(command: CreateIngredientCommand) -> _PreparedCommand:
    violations: list[FieldViolation] = []
    name = _canonical_text(command.name) if isinstance(command.name, str) else ""
    normalized_name = name.lower()
    if (
        not isinstance(command.name, str)
        or not name
        or len(name) > 200
        or len(normalized_name) > 200
    ):
        violations.append(FieldViolation("name", "must_be_nonblank_and_at_most_200_characters"))
    mass = command.mass_per_canonical_quantity
    if not isinstance(mass, Decimal) or not mass.is_finite() or mass <= 0:
        violations.append(
            FieldViolation("mass_per_canonical_quantity", "must_be_positive_finite_decimal")
        )
        mass = Decimal(1)
    wall_time = command.client_wall_time
    valid_time = (
        isinstance(wall_time, datetime)
        and wall_time.tzinfo is not None
        and wall_time.utcoffset() is not None
    )
    if not valid_time:
        violations.append(FieldViolation("client_wall_time", "must_include_timezone"))
    tags: list[UUID] = []
    seen_tags: set[UUID] = set()
    if not isinstance(command.dietary_tag_ids, tuple):
        violations.append(FieldViolation("dietary_tag_ids", "must_be_tuple_of_unique_uuids"))
    else:
        for index, tag_id in enumerate(command.dietary_tag_ids):
            tag = _uuid(tag_id, f"dietary_tag_ids[{index}]", violations)
            if tag in seen_tags:
                violations.append(FieldViolation("dietary_tag_ids", "must_not_contain_duplicates"))
            else:
                seen_tags.add(tag)
                tags.append(tag)
    section_id: UUID | None = None
    if command.default_store_section_id is not None:
        section_id = _uuid(command.default_store_section_id, "default_store_section_id", violations)
    price = command.initial_price
    if price is not None:
        if not isinstance(price, InitialPrice):
            violations.append(FieldViolation("initial_price", "must_be_initial_price_or_null"))
            price = None
        else:
            price_id = _uuid(price.id, "initial_price.id", violations)
            unit_id = _uuid(price.unit_id, "initial_price.unit_id", violations)
            valid_amount = (
                isinstance(price.amount, Decimal) and price.amount.is_finite() and price.amount >= 0
            )
            if not valid_amount:
                violations.append(
                    FieldViolation("initial_price.amount", "must_be_nonnegative_finite_decimal")
                )
            valid_quantity = (
                isinstance(price.quantity, Decimal)
                and price.quantity.is_finite()
                and price.quantity > 0
            )
            if not valid_quantity:
                violations.append(
                    FieldViolation("initial_price.quantity", "must_be_positive_finite_decimal")
                )
            currency = price.currency.strip().upper() if isinstance(price.currency, str) else ""
            if currency not in Currency.__members__:
                violations.append(FieldViolation("initial_price.currency", "must_be_iso_4217_code"))
            # Keep the hashable representation typed even for rejected input. This
            # makes malformed offline payloads deterministic retained rejections,
            # rather than allowing an AttributeError before Mutation is persisted.
            price = InitialPrice(
                price_id,
                price.amount if valid_amount else Decimal(0),
                price.quantity if valid_quantity else Decimal(1),
                unit_id,
                currency,
            )
    mutation_id = _uuid(command.mutation_id, "mutation_id", violations)
    ingredient_id = _uuid(command.ingredient_id, "ingredient_id", violations)
    ingredient_version_id = _uuid(
        command.ingredient_version_id, "ingredient_version_id", violations
    )
    if ingredient_id == ingredient_version_id:
        violations.append(FieldViolation("ingredient_version_id", "must_differ_from_ingredient_id"))
    if price is not None and price.id in {ingredient_id, ingredient_version_id}:
        violations.append(FieldViolation("initial_price.id", "must_be_unique_within_command"))
    if command.logical_operation_id is not None and not isinstance(
        command.logical_operation_id, UUID
    ):
        violations.append(FieldViolation("logical_operation_id", "must_be_uuid_or_null"))
    return _PreparedCommand(
        mutation_id=mutation_id,
        ingredient_id=ingredient_id,
        ingredient_version_id=ingredient_version_id,
        organization_id=_uuid(command.organization_id, "organization_id", violations),
        name=name,
        normalized_name=normalized_name,
        canonical_unit_id=_uuid(command.canonical_unit_id, "canonical_unit_id", violations),
        mass_per_canonical_quantity=mass,
        client_wall_time=wall_time.astimezone(UTC)
        if valid_time
        else datetime(1970, 1, 1, tzinfo=UTC),
        dietary_tag_ids=tuple(tags),
        default_store_section_id=section_id,
        initial_price=price,
        logical_operation_id=command.logical_operation_id
        if isinstance(command.logical_operation_id, UUID)
        else None,
        violations=tuple(violations),
    )


def _request_hash(command: _PreparedCommand) -> bytes:
    price = command.initial_price
    values = {
        "command_kind": COMMAND_KIND,
        "command_schema_version": COMMAND_SCHEMA_VERSION,
        "mutation_id": str(command.mutation_id),
        "ingredient_id": str(command.ingredient_id),
        "ingredient_version_id": str(command.ingredient_version_id),
        "organization_id": str(command.organization_id),
        "name": command.name,
        "canonical_unit_id": str(command.canonical_unit_id),
        "mass_per_canonical_quantity": _decimal_text(command.mass_per_canonical_quantity),
        "dietary_tag_ids": [str(value) for value in command.dietary_tag_ids],
        "default_store_section_id": str(command.default_store_section_id)
        if command.default_store_section_id
        else None,
        "initial_price": None
        if price is None
        else {
            "id": str(price.id),
            "amount": _decimal_text(price.amount),
            "quantity": _decimal_text(price.quantity),
            "unit_id": str(price.unit_id),
            "currency": price.currency,
        },
        "client_wall_time": command.client_wall_time.isoformat().replace("+00:00", "Z"),
        "logical_operation_id": str(command.logical_operation_id)
        if command.logical_operation_id
        else None,
    }
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()


async def _authorize(
    session: AsyncSession, context: ExecutionContext, organization_id: UUID
) -> tuple[Literal["member", "organization_admin", "system_admin"], str]:
    kind = "agent" if context.oauth_client_id is not None else "browser"
    actor = await session.scalar(
        select(User.id)
        .join(
            ClientInstallation,
            (ClientInstallation.user_id == User.id)
            & (ClientInstallation.id == context.client_installation_id),
        )
        .where(
            User.id == context.actor_user_id,
            User.disabled_at.is_(None),
            ClientInstallation.disabled_at.is_(None),
            ClientInstallation.installation_kind == kind,
        )
        .with_for_update(of=(User, ClientInstallation))
    )
    organization = await session.scalar(
        select(Organization.default_currency)
        .where(Organization.id == organization_id, Organization.retired_at.is_(None))
        .with_for_update(of=Organization)
    )
    if actor is None or organization is None:
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    system = await session.scalar(
        select(SystemRoleAssignment.id)
        .where(
            SystemRoleAssignment.user_id == context.actor_user_id,
            SystemRoleAssignment.role == "system_admin",
            SystemRoleAssignment.revoked_at.is_(None),
        )
        .with_for_update(of=SystemRoleAssignment)
    )
    if system is not None:
        return "system_admin", organization
    role = await session.scalar(
        select(OrganizationMembership.role)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == context.actor_user_id,
            OrganizationMembership.state == "active",
            OrganizationMembership.role.in_(("member", "organization_admin")),
        )
        .with_for_update(of=OrganizationMembership)
    )
    if role is None:
        raise ApplicationServiceError("forbidden", retry_same_identity=True)
    return cast(Literal["member", "organization_admin"], role), organization


def _validation_error(violations: tuple[FieldViolation, ...]) -> ApplicationServiceError:
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


def _mutation(
    command: _PreparedCommand,
    context: ExecutionContext,
    role: str,
    request_hash: bytes,
    outcome: Literal["accepted", "rejected"],
    payload: dict[str, object],
    first: int | None = None,
    last: int | None = None,
) -> Mutation:
    return Mutation(
        id=command.mutation_id,
        logical_operation_id=command.logical_operation_id,
        organization_id=command.organization_id,
        is_system_administration_scope=False,
        actor_user_id=context.actor_user_id,
        actor_role=role,
        client_installation_id=context.client_installation_id,
        oauth_client_id=context.oauth_client_id,
        oauth_grant_id=context.oauth_grant_id,
        client_wall_time=command.client_wall_time,
        command_schema_version=COMMAND_SCHEMA_VERSION,
        command_kind=COMMAND_KIND,
        target_identities=[{"entity_kind": "ingredient", "entity_id": str(command.ingredient_id)}],
        request_hash=request_hash,
        outcome=outcome,
        outcome_payload=payload,
        first_change_sequence=first,
        last_change_sequence=last,
    )


def _result_payload(result: CreateIngredientResult) -> dict[str, object]:
    return {
        "ingredient": {
            "id": str(result.ingredient_id),
            "organization_id": str(result.organization_id),
            "version_id": str(result.ingredient_version_id),
            "name": result.name,
            "normalized_name": result.normalized_name,
            "canonical_unit_id": str(result.canonical_unit_id),
            "mass_per_canonical_quantity": _decimal_text(result.mass_per_canonical_quantity),
            "dietary_tag_ids": [str(value) for value in result.dietary_tag_ids],
            "default_store_section_id": str(result.default_store_section_id)
            if result.default_store_section_id
            else None,
            "initial_price_id": str(result.initial_price_id) if result.initial_price_id else None,
        }
    }


def _required_str(values: dict[object, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise TypeError
    return value


def _optional_uuid(values: dict[object, object], key: str) -> UUID | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError
    return UUID(value)


def _retained_result(mutation: Mutation) -> CreateIngredientResult:
    payload = mutation.outcome_payload
    if not isinstance(payload, dict):
        raise RuntimeError("Accepted ingredient mutation has an invalid outcome payload")
    item = payload.get("ingredient")
    if (
        not isinstance(item, dict)
        or mutation.first_change_sequence is None
        or mutation.last_change_sequence is None
    ):
        raise RuntimeError("Accepted ingredient mutation has an invalid outcome payload")
    try:
        tags = item["dietary_tag_ids"]
        if not isinstance(tags, list) or not all(isinstance(value, str) for value in tags):
            raise TypeError
        mass = Decimal(_required_str(item, "mass_per_canonical_quantity"))
        if not mass.is_finite() or mass <= 0:
            raise TypeError
        return CreateIngredientResult(
            mutation.id,
            UUID(_required_str(item, "id")),
            UUID(_required_str(item, "version_id")),
            UUID(_required_str(item, "organization_id")),
            _required_str(item, "name"),
            _required_str(item, "normalized_name"),
            UUID(_required_str(item, "canonical_unit_id")),
            mass,
            tuple(UUID(value) for value in tags),
            _optional_uuid(item, "default_store_section_id"),
            _optional_uuid(item, "initial_price_id"),
            mutation.first_change_sequence,
            mutation.last_change_sequence,
            True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Accepted ingredient mutation has an invalid outcome payload") from error


def _retained_error(mutation: Mutation) -> ApplicationServiceError:
    payload = mutation.outcome_payload
    if not isinstance(payload, dict):
        raise RuntimeError("Rejected ingredient mutation has an invalid outcome payload")
    error = payload.get("error")
    if (
        not isinstance(error, dict)
        or error.get("code") != "validation_failed"
        or not isinstance(error.get("field_violations"), list)
    ):
        raise RuntimeError("Rejected ingredient mutation has an invalid outcome payload")
    try:
        violations = tuple(
            FieldViolation(_required_str(item, "path"), _required_str(item, "code"))
            for item in error["field_violations"]
            if isinstance(item, dict)
        )
        if len(violations) != len(error["field_violations"]):
            raise TypeError
        return _validation_error(violations)
    except (KeyError, TypeError) as error_value:
        raise RuntimeError(
            "Rejected ingredient mutation has an invalid outcome payload"
        ) from error_value


async def _references_are_valid(
    session: AsyncSession, command: _PreparedCommand, currency: str
) -> tuple[FieldViolation, ...]:
    errors: list[FieldViolation] = []
    # Every mutable reference is locked exclusively until publication.  A concurrent
    # retirement therefore commits either before validation (and is rejected here)
    # or after this complete immutable publication; it cannot slip between them.
    unit = await session.get(UnitDefinition, command.canonical_unit_id, with_for_update=True)
    if (
        unit is None
        or unit.retired_at is not None
        or not unit.allows_ingredient_quantity
        or (unit.organization_id is not None and unit.organization_id != command.organization_id)
    ):
        errors.append(FieldViolation("canonical_unit_id", "not_available_in_organization"))
        return tuple(errors)
    if unit.dimension == "mass" and command.mass_per_canonical_quantity != unit.base_unit_factor:
        errors.append(
            FieldViolation("mass_per_canonical_quantity", "must_match_mass_canonical_unit")
        )
    if command.default_store_section_id is not None:
        section = await session.get(
            StoreSection, command.default_store_section_id, with_for_update=True
        )
        if (
            section is None
            or section.organization_id != command.organization_id
            or section.retired_at is not None
        ):
            errors.append(
                FieldViolation("default_store_section_id", "not_available_in_organization")
            )
    if command.dietary_tag_ids:
        found = set(
            (
                await session.execute(
                    select(DietaryTag.id)
                    .where(
                        DietaryTag.organization_id == command.organization_id,
                        DietaryTag.id.in_(command.dietary_tag_ids),
                        DietaryTag.retired_at.is_(None),
                    )
                    .with_for_update(of=DietaryTag)
                )
            ).scalars()
        )
        if found != set(command.dietary_tag_ids):
            errors.append(FieldViolation("dietary_tag_ids", "contain_unavailable_tag"))
    price = command.initial_price
    if price is not None:
        price_unit = await session.get(UnitDefinition, price.unit_id, with_for_update=True)
        if price.currency != currency:
            errors.append(
                FieldViolation("initial_price.currency", "must_match_organization_default_currency")
            )
        compatible = (
            price_unit is not None
            and price_unit.retired_at is None
            and price_unit.allows_ingredient_quantity
            and (
                price_unit.organization_id is None
                or price_unit.organization_id == command.organization_id
            )
            and price_unit.dimension == unit.dimension
            and (
                (unit.dimension in ("mass", "volume") and price_unit.base_unit_factor is not None)
                or (unit.dimension in ("count", "custom") and price.unit_id == unit.id)
            )
        )
        if not compatible:
            errors.append(
                FieldViolation("initial_price.unit_id", "must_be_compatible_available_unit")
            )
    return tuple(errors)


async def create_ingredient(
    session_factory: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    command: CreateIngredientCommand,
) -> CreateIngredientResult:
    """Publish a new ingredient root, initial immutable version, and optional price atomically."""
    prepared = _prepare_command(command)
    request_hash = _request_hash(prepared)
    result: CreateIngredientResult | None = None
    deferred: ApplicationServiceError | None = None
    async with session_factory() as session, session.begin():
        role, currency = await _authorize(session, context, prepared.organization_id)
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
                raise RuntimeError("Ingredient creation retained an unsupported outcome")
        elif prepared.violations:
            deferred = _validation_error(prepared.violations)
            session.add(
                _mutation(
                    prepared, context, role, request_hash, "rejected", _error_payload(deferred)
                )
            )
        else:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("ingredient", prepared.ingredient_id)},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key("ingredient-version", prepared.ingredient_version_id)},
            )
            if prepared.initial_price is not None:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": _advisory_lock_key("ingredient-price", prepared.initial_price.id)},
                )
            name_identity = UUID(
                bytes=hashlib.sha256(prepared.normalized_name.encode()).digest()[:16]
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        f"ingredient-name:{prepared.organization_id}", name_identity
                    )
                },
            )
            exists = await session.scalar(
                select(Ingredient.id).where(Ingredient.id == prepared.ingredient_id)
            )
            version_exists = await session.scalar(
                select(IngredientVersion.id).where(
                    IngredientVersion.id == prepared.ingredient_version_id
                )
            )
            price_exists = (
                await session.scalar(
                    select(IngredientPriceEstimate.id).where(
                        IngredientPriceEstimate.id == prepared.initial_price.id
                    )
                )
                if prepared.initial_price is not None
                else None
            )
            duplicate = await session.scalar(
                select(Ingredient.id)
                .join(IngredientVersion, Ingredient.current_version_id == IngredientVersion.id)
                .where(
                    Ingredient.organization_id == prepared.organization_id,
                    Ingredient.retired_at.is_(None),
                    IngredientVersion.normalized_name == prepared.normalized_name,
                )
            )
            errors = list(await _references_are_valid(session, prepared, currency))
            if exists is not None:
                errors.append(FieldViolation("ingredient_id", "already_exists"))
            if version_exists is not None:
                errors.append(FieldViolation("ingredient_version_id", "already_exists"))
            if price_exists is not None:
                errors.append(FieldViolation("initial_price.id", "already_exists"))
            if duplicate is not None:
                errors.append(FieldViolation("name", "already_exists"))
            if errors:
                deferred = _validation_error(tuple(errors))
                session.add(
                    _mutation(
                        prepared, context, role, request_hash, "rejected", _error_payload(deferred)
                    )
                )
            else:
                price = prepared.initial_price
                change_records: list[tuple[str, UUID, dict[str, object]]] = [
                    (
                        "ingredient",
                        prepared.ingredient_id,
                        {
                            "id": str(prepared.ingredient_id),
                            "organization_id": str(prepared.organization_id),
                            "current_version_id": str(prepared.ingredient_version_id),
                            "current_price_estimate_id": str(price.id) if price else None,
                            "retired_at": None,
                            "retired_by_user_id": None,
                            "lifecycle": "active",
                            "field_clocks": {"lifecycle": None},
                            "created_by_user_id": str(context.actor_user_id),
                        },
                    ),
                    (
                        "ingredient_version",
                        prepared.ingredient_version_id,
                        {
                            "id": str(prepared.ingredient_version_id),
                            "ingredient_id": str(prepared.ingredient_id),
                            "organization_id": str(prepared.organization_id),
                            "name": prepared.name,
                            "normalized_name": prepared.normalized_name,
                            "canonical_unit_id": str(prepared.canonical_unit_id),
                            "mass_per_canonical_quantity": _decimal_text(
                                prepared.mass_per_canonical_quantity
                            ),
                            "default_store_section_id": str(prepared.default_store_section_id)
                            if prepared.default_store_section_id
                            else None,
                            "dietary_tag_ids": [str(value) for value in prepared.dietary_tag_ids],
                            "published_by_user_id": str(context.actor_user_id),
                        },
                    ),
                ]
                if price:
                    change_records.append(
                        (
                            "ingredient_price_estimate",
                            price.id,
                            {
                                "id": str(price.id),
                                "ingredient_id": str(prepared.ingredient_id),
                                "organization_id": str(prepared.organization_id),
                                "state": "available",
                                "price_amount": _decimal_text(price.amount),
                                "priced_quantity": _decimal_text(price.quantity),
                                "priced_unit_id": str(price.unit_id),
                                "currency": price.currency,
                                "published_by_user_id": str(context.actor_user_id),
                            },
                        )
                    )
                first, last = await _reserve_change_range(
                    session, prepared.organization_id, prepared.mutation_id, len(change_records)
                )
                result = CreateIngredientResult(
                    prepared.mutation_id,
                    prepared.ingredient_id,
                    prepared.ingredient_version_id,
                    prepared.organization_id,
                    prepared.name,
                    prepared.normalized_name,
                    prepared.canonical_unit_id,
                    prepared.mass_per_canonical_quantity,
                    prepared.dietary_tag_ids,
                    prepared.default_store_section_id,
                    price.id if price else None,
                    first,
                    last,
                    False,
                )
                session.add(
                    Ingredient(
                        id=prepared.ingredient_id,
                        organization_id=prepared.organization_id,
                        current_version_id=prepared.ingredient_version_id,
                        current_price_estimate_id=price.id if price else None,
                        created_by_user_id=context.actor_user_id,
                    )
                )
                await session.flush()
                session.add_all(
                    IngredientVersionDietaryTag(
                        ingredient_version_id=prepared.ingredient_version_id,
                        dietary_tag_id=tag_id,
                        organization_id=prepared.organization_id,
                    )
                    for tag_id in prepared.dietary_tag_ids
                )
                # 0006's trigger deliberately permits tag rows only before their
                # immutable version exists, so the version and tag set publish as
                # one transaction but cannot be appended later.
                await session.flush()
                session.add(
                    IngredientVersion(
                        id=prepared.ingredient_version_id,
                        organization_id=prepared.organization_id,
                        ingredient_id=prepared.ingredient_id,
                        name=prepared.name,
                        normalized_name=prepared.normalized_name,
                        canonical_unit_id=prepared.canonical_unit_id,
                        mass_per_canonical_quantity=prepared.mass_per_canonical_quantity,
                        default_store_section_id=prepared.default_store_section_id,
                        published_by_user_id=context.actor_user_id,
                    )
                )
                await session.flush()
                if price:
                    session.add(
                        IngredientPriceEstimate(
                            id=price.id,
                            organization_id=prepared.organization_id,
                            ingredient_id=prepared.ingredient_id,
                            state="available",
                            price_amount=price.amount,
                            priced_quantity=price.quantity,
                            priced_unit_id=price.unit_id,
                            currency=price.currency,
                            published_by_user_id=context.actor_user_id,
                        )
                    )
                session.add_all(
                    OrganizationChange(
                        organization_id=prepared.organization_id,
                        sequence=first + index,
                        mutation_id=prepared.mutation_id,
                        entity_id=entity_id,
                        entity_kind=kind,
                        operation="upsert",
                        payload={"record_schema_version": 1, "record": record},
                    )
                    for index, (kind, entity_id, record) in enumerate(change_records)
                )
                session.add(
                    _mutation(
                        prepared,
                        context,
                        role,
                        request_hash,
                        "accepted",
                        _result_payload(result),
                        first,
                        last,
                    )
                )
    if deferred:
        raise deferred
    if result is None:
        raise RuntimeError("Ingredient creation produced no outcome")
    return result
