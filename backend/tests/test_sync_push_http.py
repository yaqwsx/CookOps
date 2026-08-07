import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from test_sync_pull_http import SyncDatabase, _settings, _sign_in
from test_sync_pull_http import sync_database as _sync_database_fixture

from cookops.main import create_app
from cookops.persistence.models import (
    ClientInstallation,
    EventDay,
    EventMealRole,
    Ingredient,
    IngredientVersion,
    OrganizationMembership,
    RecipeVersion,
    RecipeVersionIngredientLine,
    ScheduledRecipe,
    ShoppingIngredientRow,
    ShoppingList,
    UnitDefinition,
)


@pytest.fixture
def sync_database() -> Iterator[SyncDatabase]:
    """Reuse the pull fixture when this module is collected in the full suite.

    ``pytest_plugins`` in a test module is not a reliable cross-module fixture
    registration mechanism during full collection.
    """

    setup = cast(Callable[[], Iterator[SyncDatabase]], vars(_sync_database_fixture)["__wrapped__"])
    yield from setup()


def _installation(database: SyncDatabase) -> UUID:
    installation_id = uuid4()
    with database.engine.begin() as connection:
        actor_id = connection.scalar(
            select(OrganizationMembership.user_id).where(
                OrganizationMembership.organization_id == database.organization_id
            )
        )
        assert isinstance(actor_id, UUID)
        connection.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == database.organization_id,
                OrganizationMembership.user_id == actor_id,
            )
            .values(role="organization_admin")
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id, user_id=actor_id, installation_kind="browser"
            )
        )
    return installation_id


def _command(
    *, mutation_id: UUID, event_id: UUID, kind: str = "event.create", **payload: object
) -> dict[str, object]:
    if kind == "event.create":
        payload = {
            "event_id": str(event_id),
            "name": "Push event",
            "start_date": date(2026, 8, 10).isoformat(),
            "end_date": date(2026, 8, 10).isoformat(),
            "base_expected_attendance": 10,
            "budget_amount": "0",
            **payload,
        }
    elif kind == "event.update_base_attendance":
        payload = {"event_id": str(event_id), **payload}
    elif kind == "shopping_list.create":
        payload = {
            "shopping_list_id": str(uuid4()),
            "generation_revision_id": str(uuid4()),
            "event_id": str(event_id),
            "name": "Push shopping",
            "scheduled_recipe_ids": [],
            **payload,
        }
    return {
        "mutation_id": str(mutation_id),
        "command_kind": kind,
        "command_schema_version": 1,
        "client_wall_time": datetime.now(UTC).isoformat(),
        "payload": payload,
    }


def _body(
    database: SyncDatabase, installation_id: UUID, commands: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "organization_id": str(database.organization_id),
        "client_installation_id": str(installation_id),
        "request_sent_at": datetime.now(UTC).isoformat(),
        "sync_schema_version": 1,
        "commands": commands,
    }


def _recipe_command(
    *, mutation_id: UUID, scaling_unit_id: UUID, **payload: object
) -> dict[str, object]:
    recipe_payload = {
        "recipe_id": str(uuid4()),
        "recipe_version_id": str(uuid4()),
        "name": "  Push recipe  ",
        "scaling_unit_id": str(scaling_unit_id),
        "base_scaling_amount": "10",
        "ingredient_lines": [],
        **payload,
    }
    return _command(
        mutation_id=mutation_id,
        event_id=uuid4(),
        kind="recipe.create",
        **recipe_payload,
    )


def _ingredient_command(
    *, mutation_id: UUID, unit_id: UUID, **payload: object
) -> dict[str, object]:
    ingredient_payload = {
        "ingredient_id": str(uuid4()),
        "ingredient_version_id": str(uuid4()),
        "name": "  Push tomatoes  ",
        "canonical_unit_id": str(unit_id),
        "mass_per_canonical_quantity": "1",
        **payload,
    }
    return _command(
        mutation_id=mutation_id,
        event_id=uuid4(),
        kind="ingredient.create",
        **ingredient_payload,
    )


def _schedule_recipe_command(
    *,
    mutation_id: UUID,
    scheduled_recipe_id: UUID,
    event_id: UUID,
    event_day_id: UUID,
    event_meal_role_id: UUID,
    recipe_id: UUID,
    recipe_version_id: UUID,
    **payload: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "scheduled_recipe_id": str(scheduled_recipe_id),
        "event_day_id": str(event_day_id),
        "event_meal_role_id": str(event_meal_role_id),
        "recipe_id": str(recipe_id),
        "recipe_version_id": str(recipe_version_id),
        "consumption_percentage": "75.5",
        "position_key": "b",
        "note": "  Serve warm  ",
    }
    values.update(payload)
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="scheduled_recipe.schedule",
        **values,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _move_scheduled_recipe_command(
    *,
    mutation_id: UUID,
    scheduled_recipe_id: UUID,
    event_id: UUID,
    event_day_id: UUID,
    event_meal_role_id: UUID,
    position_key: str = "z",
) -> dict[str, object]:
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="scheduled_recipe.move",
        scheduled_recipe_id=str(scheduled_recipe_id),
        event_day_id=str(event_day_id),
        event_meal_role_id=str(event_meal_role_id),
        position_key=position_key,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _shopping_operation_command(
    *, mutation_id: UUID, kind: str, **payload: object
) -> dict[str, object]:
    values: dict[str, object] = {
        "shopping_list_id": str(uuid4()),
        "shopping_ingredient_row_id": str(uuid4()),
    }
    if kind == "shopping_list.set_available_supply":
        values["quantity"] = "1.25"
    elif kind == "shopping_list.set_manual_purchase_target":
        values["quantity"] = None
    elif kind == "shopping_list.set_row_fulfilment":
        values["fulfilled"] = True
    else:
        values = {
            "shopping_list_id": values["shopping_list_id"],
            "shopping_contribution_id": str(uuid4()),
            "fulfilled": True,
        }
    values.update(payload)
    return _command(mutation_id=mutation_id, event_id=uuid4(), kind=kind, **values)


def _receipt_command(
    *, mutation_id: UUID, event_id: UUID, kind: str = "receipt.create", **payload: object
) -> dict[str, object]:
    values: dict[str, object] = {
        "receipt_id": str(uuid4()),
        "title": "  Push receipt  ",
        "total_amount": "12.50",
        "receipt_date": "2026-08-10",
        "note": "  groceries  ",
    }
    values.update(payload)
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind=kind,
        **values,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _event_price_refresh_command(*, mutation_id: UUID, event_id: UUID) -> dict[str, object]:
    return {
        "mutation_id": str(mutation_id),
        "command_kind": "event.update_price_estimates",
        "command_schema_version": 1,
        "client_wall_time": datetime.now(UTC).isoformat(),
        "payload": {"event_id": str(event_id)},
    }


def test_push_queues_typed_event_price_refresh_and_rejects_extra_payload(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    event_id = uuid4()
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        created = client.post(
            "/api/v1/sync/push",
            json=_body(
                sync_database,
                installation_id,
                [_command(mutation_id=uuid4(), event_id=event_id)],
            ),
        )
        assert created.json()["outcomes"][0]["status"] == "accepted"
        refresh_command = _event_price_refresh_command(mutation_id=uuid4(), event_id=event_id)
        refreshed = client.post(
            "/api/v1/sync/push",
            json=_body(
                sync_database,
                installation_id,
                [refresh_command],
            ),
        )
        assert refreshed.json()["outcomes"][0]["status"] == "accepted"
        replayed = client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [refresh_command]),
        )
        assert replayed.json()["outcomes"][0]["replayed"] is True
        malformed = _event_price_refresh_command(mutation_id=uuid4(), event_id=event_id)
        cast(dict[str, object], malformed["payload"])["catalog_price_id"] = str(uuid4())
        rejected = client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [malformed]),
        )
    outcome = rejected.json()["outcomes"][0]
    assert outcome["status"] == "rejected"
    assert outcome["error"]["code"] == "validation_failed"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("shopping_list.set_available_supply", {"quantity": 1}),
        ("shopping_list.set_manual_purchase_target", {"quantity": {"bad": "input"}}),
        ("shopping_list.set_contribution_fulfilment", {"fulfilled": "true"}),
        ("shopping_list.set_row_fulfilment", {"fulfilled": 1}),
    ],
)
def test_push_strictly_rejects_malformed_typed_shopping_operation_payloads(
    sync_database: SyncDatabase, kind: str, payload: dict[str, object]
) -> None:
    installation_id = _installation(sync_database)
    command = _shopping_operation_command(mutation_id=uuid4(), kind=kind, **payload)
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        outcome = client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [command]),
        ).json()["outcomes"][0]
    assert outcome["command_kind"] == kind
    assert outcome["status"] == "rejected"
    assert outcome["error"]["code"] == "validation_failed"


def test_push_accepts_supply_and_replays_then_pulls_the_authoritative_field_clock(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    event_id, list_id, revision_id = uuid4(), uuid4(), uuid4()
    ingredient_id, ingredient_version_id, row_id = uuid4(), uuid4(), uuid4()
    with sync_database.engine.begin() as connection:
        grams_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        actor_id = connection.scalar(
            select(OrganizationMembership.user_id).where(
                OrganizationMembership.organization_id == sync_database.organization_id
            )
        )
        assert isinstance(grams_id, UUID) and isinstance(actor_id, UUID)
        connection.execute(
            insert(Ingredient).values(
                id=ingredient_id,
                organization_id=sync_database.organization_id,
                current_version_id=ingredient_version_id,
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=ingredient_version_id,
                organization_id=sync_database.organization_id,
                ingredient_id=ingredient_id,
                name="Tomatoes",
                normalized_name="tomatoes",
                canonical_unit_id=grams_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            )
        )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        setup = client.post(
            "/api/v1/sync/push",
            json=_body(
                sync_database,
                installation_id,
                [
                    _command(mutation_id=uuid4(), event_id=event_id),
                    _command(
                        mutation_id=uuid4(),
                        event_id=event_id,
                        kind="shopping_list.create",
                        shopping_list_id=str(list_id),
                        generation_revision_id=str(revision_id),
                    ),
                ],
            ),
        )
        assert [outcome["status"] for outcome in setup.json()["outcomes"]] == [
            "accepted",
            "accepted",
        ]
        with sync_database.engine.begin() as connection:
            actor_id = connection.scalar(
                select(OrganizationMembership.user_id).where(
                    OrganizationMembership.organization_id == sync_database.organization_id
                )
            )
            grams_id = connection.scalar(
                select(UnitDefinition.id).where(
                    UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
                )
            )
            assert isinstance(actor_id, UUID) and isinstance(grams_id, UUID)
            connection.execute(
                insert(ShoppingIngredientRow).values(
                    id=row_id,
                    organization_id=sync_database.organization_id,
                    event_id=event_id,
                    shopping_list_id=list_id,
                    ingredient_id=ingredient_id,
                    ingredient_name="Tomatoes",
                    calculation_unit_id=grams_id,
                    created_by_user_id=actor_id,
                )
            )
        command = _shopping_operation_command(
            mutation_id=uuid4(),
            kind="shopping_list.set_available_supply",
            shopping_list_id=str(list_id),
            shopping_ingredient_row_id=str(row_id),
            quantity="7.5",
        )
        before = setup.json()["change_cursor"]
        body = _body(sync_database, installation_id, [command])
        accepted = client.post("/api/v1/sync/push", json=body).json()["outcomes"][0]
        assert accepted["status"] == "accepted" and not accepted["replayed"]
        assert client.post("/api/v1/sync/push", json=body).json()["outcomes"][0]["replayed"]
        pulled = client.post(
            "/api/v1/sync/pull",
            json={"organization_id": str(sync_database.organization_id), "cursor": before},
        ).json()
    record = pulled["transaction_groups"][-1]["records"][0]["payload"]["record"]
    assert record["available_supply_quantity"] == "7.5"
    assert (
        record["field_clocks"]["available_supply_quantity"]["winning_mutation_id"]
        == command["mutation_id"]
    )


def test_push_applies_ordered_commands_and_replays_a_retained_outcome(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    event_id, create_id, attendance_id = uuid4(), uuid4(), uuid4()
    commands = [
        _command(mutation_id=create_id, event_id=event_id),
        _command(
            mutation_id=attendance_id,
            event_id=event_id,
            kind="event.update_base_attendance",
            base_expected_attendance=24,
        ),
    ]
    body = _body(sync_database, installation_id, commands)
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post("/api/v1/sync/push", json=body)
        assert response.status_code == 200
        outcomes = response.json()["outcomes"]
        assert [(outcome["command_kind"], outcome["status"]) for outcome in outcomes] == [
            ("event.create", "accepted"),
            ("event.update_base_attendance", "accepted"),
        ]
        assert all(outcome["error"] is None for outcome in outcomes)
        assert all(outcome["first_change_sequence"] is not None for outcome in outcomes)
        replay = client.post("/api/v1/sync/push", json=body)
        assert replay.status_code == 200
        assert [outcome["replayed"] for outcome in replay.json()["outcomes"]] == [True, True]
        assert replay.json()["change_cursor"] == response.json()["change_cursor"]


def test_push_creates_a_shopping_list_through_the_typed_shared_command(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    event_id, shopping_list_id, generation_revision_id, mutation_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    commands = [
        _command(mutation_id=uuid4(), event_id=event_id),
        _command(
            mutation_id=mutation_id,
            event_id=event_id,
            kind="shopping_list.create",
            shopping_list_id=str(shopping_list_id),
            generation_revision_id=str(generation_revision_id),
            name="  Push shopping  ",
        ),
    ]
    body = _body(sync_database, installation_id, commands)
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post("/api/v1/sync/push", json=body)
        assert response.status_code == 200
        outcome = response.json()["outcomes"][1]
        assert outcome["command_kind"] == "shopping_list.create"
        assert outcome["status"] == "accepted"
        assert outcome["replayed"] is False
        assert outcome["first_change_sequence"] is not None
        assert client.post("/api/v1/sync/push", json=body).json()["outcomes"][1]["replayed"] is True
        changed = _body(
            sync_database,
            installation_id,
            [
                commands[0],
                _command(
                    mutation_id=mutation_id,
                    event_id=event_id,
                    kind="shopping_list.create",
                    shopping_list_id=str(shopping_list_id),
                    generation_revision_id=str(generation_revision_id),
                    name="Other",
                ),
            ],
        )
        mismatch = client.post("/api/v1/sync/push", json=changed).json()["outcomes"][1]
        assert mismatch["error"]["code"] == "idempotency_mismatch"
        invalid = _body(
            sync_database,
            installation_id,
            [
                _command(
                    mutation_id=uuid4(),
                    event_id=event_id,
                    kind="shopping_list.create",
                    unexpected="value",
                )
            ],
        )
        rejected = client.post("/api/v1/sync/push", json=invalid).json()["outcomes"][0]
        assert rejected["error"]["code"] == "validation_failed"
    with sync_database.engine.connect() as connection:
        assert connection.scalar(
            select(ShoppingList.name).where(ShoppingList.id == shopping_list_id)
        ) == ("Push shopping")


def test_push_creates_a_recipe_through_the_typed_shared_command(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    ingredient_id, ingredient_version_id = uuid4(), uuid4()
    with sync_database.engine.begin() as connection:
        scaling_unit_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "person"
            )
        )
        grams_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        actor_id = connection.scalar(
            select(OrganizationMembership.user_id).where(
                OrganizationMembership.organization_id == sync_database.organization_id
            )
        )
        assert isinstance(scaling_unit_id, UUID)
        assert isinstance(grams_id, UUID)
        assert isinstance(actor_id, UUID)
        connection.execute(
            insert(Ingredient).values(
                id=ingredient_id,
                organization_id=sync_database.organization_id,
                current_version_id=ingredient_version_id,
                created_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=ingredient_version_id,
                organization_id=sync_database.organization_id,
                ingredient_id=ingredient_id,
                name="Carrots",
                normalized_name="carrots",
                canonical_unit_id=grams_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            )
        )
    assert isinstance(scaling_unit_id, UUID)
    assert isinstance(grams_id, UUID)
    mutation_id = uuid4()
    recipe_id = uuid4()
    ingredient_line = {
        "id": str(uuid4()),
        "line_key": str(uuid4()),
        "ingredient_version_id": str(ingredient_version_id),
        "base_quantity": "750.5",
        "position_key": "a",
        "scaling_behavior": "fixed",
        "include_in_portion_weight": False,
        "preferred_display_unit_id": str(grams_id),
        "note": "diced",
    }
    command = _recipe_command(
        mutation_id=mutation_id,
        scaling_unit_id=scaling_unit_id,
        recipe_id=str(recipe_id),
        ingredient_lines=[ingredient_line],
    )
    body = _body(sync_database, installation_id, [command])
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post("/api/v1/sync/push", json=body)
        assert response.status_code == 200
        outcome = response.json()["outcomes"][0]
        assert outcome["command_kind"] == "recipe.create"
        assert outcome["status"] == "accepted"
        assert outcome["replayed"] is False
        assert outcome["first_change_sequence"] is not None
        assert client.post("/api/v1/sync/push", json=body).json()["outcomes"][0]["replayed"] is True
        changed = _body(
            sync_database,
            installation_id,
            [
                _recipe_command(
                    mutation_id=mutation_id, scaling_unit_id=scaling_unit_id, name="Other"
                )
            ],
        )
        assert (
            client.post("/api/v1/sync/push", json=changed).json()["outcomes"][0]["error"]["code"]
            == "idempotency_mismatch"
        )
        invalid = _body(
            sync_database,
            installation_id,
            [
                _recipe_command(
                    mutation_id=uuid4(),
                    scaling_unit_id=scaling_unit_id,
                    ingredient_lines=[
                        {
                            **ingredient_line,
                            "unexpected": "value",
                        }
                    ],
                )
            ],
        )
        assert (
            client.post("/api/v1/sync/push", json=invalid).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
    with sync_database.engine.connect() as connection:
        assert (
            connection.scalar(
                select(RecipeVersion.name).where(RecipeVersion.recipe_id == recipe_id)
            )
            == "Push recipe"
        )
        assert (
            connection.scalar(
                select(RecipeVersionIngredientLine.scaling_behavior).where(
                    RecipeVersionIngredientLine.recipe_id == recipe_id
                )
            )
            == "fixed"
        )


def test_push_schedules_a_recipe_through_the_typed_shared_command(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    event_id, recipe_id, recipe_version_id = uuid4(), uuid4(), uuid4()
    with sync_database.engine.connect() as connection:
        scaling_unit_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "person"
            )
        )
    assert isinstance(scaling_unit_id, UUID)
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        setup = client.post(
            "/api/v1/sync/push",
            json=_body(
                sync_database,
                installation_id,
                [
                    _command(mutation_id=uuid4(), event_id=event_id),
                    _recipe_command(
                        mutation_id=uuid4(),
                        scaling_unit_id=scaling_unit_id,
                        recipe_id=str(recipe_id),
                        recipe_version_id=str(recipe_version_id),
                    ),
                ],
            ),
        )
        assert [outcome["status"] for outcome in setup.json()["outcomes"]] == [
            "accepted",
            "accepted",
        ]
        with sync_database.engine.connect() as connection:
            event_day_id = connection.scalar(
                select(EventDay.id).where(EventDay.event_id == event_id)
            )
            actor_id = connection.scalar(
                select(OrganizationMembership.user_id).where(
                    OrganizationMembership.organization_id == sync_database.organization_id
                )
            )
        assert isinstance(event_day_id, UUID)
        assert isinstance(actor_id, UUID)
        event_meal_role_id = uuid4()
        with sync_database.engine.begin() as connection:
            connection.execute(
                insert(EventMealRole).values(
                    id=event_meal_role_id,
                    event_id=event_id,
                    built_in_translation_key="meal.dinner",
                    position_key="a",
                    created_by_user_id=actor_id,
                )
            )
        scheduled_recipe_id, mutation_id = uuid4(), uuid4()
        command = _schedule_recipe_command(
            mutation_id=mutation_id,
            scheduled_recipe_id=scheduled_recipe_id,
            event_id=event_id,
            event_day_id=event_day_id,
            event_meal_role_id=event_meal_role_id,
            recipe_id=recipe_id,
            recipe_version_id=recipe_version_id,
        )
        body = _body(sync_database, installation_id, [command])
        response = client.post("/api/v1/sync/push", json=body)
        assert response.status_code == 200
        outcome = response.json()["outcomes"][0]
        assert outcome["command_kind"] == "scheduled_recipe.schedule"
        assert outcome["status"] == "accepted"
        assert outcome["replayed"] is False
        assert outcome["first_change_sequence"] is not None
        assert client.post("/api/v1/sync/push", json=body).json()["outcomes"][0]["replayed"] is True
        move = _move_scheduled_recipe_command(
            mutation_id=uuid4(),
            scheduled_recipe_id=scheduled_recipe_id,
            event_id=event_id,
            event_day_id=event_day_id,
            event_meal_role_id=event_meal_role_id,
            position_key="z9",
        )
        move_body = _body(sync_database, installation_id, [move])
        move_response = client.post("/api/v1/sync/push", json=move_body)
        assert move_response.json()["outcomes"][0]["command_kind"] == "scheduled_recipe.move"
        assert move_response.json()["outcomes"][0]["status"] == "accepted"
        assert client.post("/api/v1/sync/push", json=move_body).json()["outcomes"][0]["replayed"]
        invalid_move = _move_scheduled_recipe_command(
            mutation_id=uuid4(),
            scheduled_recipe_id=scheduled_recipe_id,
            event_id=event_id,
            event_day_id=event_day_id,
            event_meal_role_id=event_meal_role_id,
            position_key="not-a-key",
        )
        assert (
            client.post(
                "/api/v1/sync/push",
                json=_body(sync_database, installation_id, [invalid_move]),
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
        cross_event_move = _move_scheduled_recipe_command(
            mutation_id=uuid4(),
            scheduled_recipe_id=scheduled_recipe_id,
            event_id=event_id,
            event_day_id=uuid4(),
            event_meal_role_id=event_meal_role_id,
        )
        cross_outcome = client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [cross_event_move]),
        ).json()["outcomes"][0]
        assert cross_outcome["error"]["code"] == "validation_failed"
        assert cross_outcome["error"]["field_violations"] == [
            {
                "path": "placement",
                "code": "must_reference_active_day_and_meal_role_in_event",
            }
        ]
        changed = _schedule_recipe_command(
            mutation_id=mutation_id,
            scheduled_recipe_id=scheduled_recipe_id,
            event_id=event_id,
            event_day_id=event_day_id,
            event_meal_role_id=event_meal_role_id,
            recipe_id=recipe_id,
            recipe_version_id=recipe_version_id,
            note="Other",
        )
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [changed])
            ).json()["outcomes"][0]["error"]["code"]
            == "idempotency_mismatch"
        )
        invalid = _schedule_recipe_command(
            mutation_id=uuid4(),
            scheduled_recipe_id=uuid4(),
            event_id=event_id,
            event_day_id=event_day_id,
            event_meal_role_id=event_meal_role_id,
            recipe_id=recipe_id,
            recipe_version_id=recipe_version_id,
            unexpected="value",
        )
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [invalid])
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
    with sync_database.engine.connect() as connection:
        assert connection.execute(
            select(
                ScheduledRecipe.consumption_percentage,
                ScheduledRecipe.position_key,
                ScheduledRecipe.note,
            ).where(ScheduledRecipe.id == scheduled_recipe_id)
        ).one() == (Decimal("75.5"), "z9", "  Serve warm  ")


def test_push_rejects_unknown_commands_and_untrusted_batch_shapes(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    unknown_command = _command(mutation_id=uuid4(), event_id=uuid4(), kind="future.command")
    unknown_body = _body(sync_database, installation_id, [unknown_command])
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        unknown = client.post("/api/v1/sync/push", json=unknown_body)
        assert unknown.status_code == 200
        assert unknown.json()["outcomes"] == [
            {
                "mutation_id": unknown.json()["outcomes"][0]["mutation_id"],
                "command_kind": "future.command",
                "status": "rejected",
                "replayed": False,
                "first_change_sequence": None,
                "last_change_sequence": None,
                "error": {
                    "code": "unsupported_command_kind",
                    "field_violations": [],
                    "retry_same_identity": False,
                },
            }
        ]
        assert client.post("/api/v1/sync/push", json=unknown_body).json()["outcomes"][0]["replayed"]
        too_many = client.post(
            "/api/v1/sync/push",
            json=_body(
                sync_database,
                installation_id,
                [_command(mutation_id=uuid4(), event_id=uuid4()) for _ in range(101)],
            ),
        )
        assert too_many.status_code == 422
        oversized = _body(
            sync_database,
            installation_id,
            [_command(mutation_id=uuid4(), event_id=uuid4(), name="x" * (1024 * 1024))],
        )
        encoded = json.dumps(oversized).encode()
        assert len(encoded) > 1024 * 1024
        assert client.post("/api/v1/sync/push", content=encoded).status_code == 413


def test_push_registers_an_authenticated_browser_installation_once(
    sync_database: SyncDatabase,
) -> None:
    installation_id = uuid4()
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post("/api/v1/sync/push", json=_body(sync_database, installation_id, []))
    assert response.status_code == 200
    with sync_database.engine.connect() as connection:
        assert (
            connection.scalar(
                select(ClientInstallation.id).where(ClientInstallation.id == installation_id)
            )
            == installation_id
        )


def test_push_creates_an_ingredient_through_the_typed_sync_adapter(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    with sync_database.engine.connect() as connection:
        unit_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
    assert isinstance(unit_id, UUID)
    mutation_id = uuid4()
    body = _body(
        sync_database,
        installation_id,
        [_ingredient_command(mutation_id=mutation_id, unit_id=unit_id)],
    )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post("/api/v1/sync/push", json=body)
        assert response.status_code == 200
        outcome = response.json()["outcomes"][0]
        assert outcome["command_kind"] == "ingredient.create"
        assert outcome["status"] == "accepted"
        assert client.post("/api/v1/sync/push", json=body).json()["outcomes"][0]["replayed"]
        invalid = _body(
            sync_database,
            installation_id,
            [
                _ingredient_command(
                    mutation_id=uuid4(), unit_id=unit_id, mass_per_canonical_quantity=1
                )
            ],
        )
        rejected = client.post("/api/v1/sync/push", json=invalid).json()["outcomes"][0]
        assert rejected["status"] == "rejected"
        assert rejected["error"]["code"] == "validation_failed"


def test_push_applies_idempotent_receipt_metadata_commands(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    event_id, receipt_id = uuid4(), uuid4()
    create = _receipt_command(mutation_id=uuid4(), event_id=event_id, receipt_id=str(receipt_id))
    update = _receipt_command(
        mutation_id=uuid4(),
        event_id=event_id,
        kind="receipt.update",
        receipt_id=str(receipt_id),
        title="Bakery",
        total_amount="0",
        receipt_date=None,
        note=None,
    )
    retire = _command(
        mutation_id=uuid4(),
        event_id=event_id,
        kind="receipt.lifecycle",
        receipt_id=str(receipt_id),
        operation="retire",
    )
    cast(dict[str, object], retire["payload"])["event_id"] = str(event_id)
    restore = _command(
        mutation_id=uuid4(),
        event_id=event_id,
        kind="receipt.lifecycle",
        receipt_id=str(receipt_id),
        operation="restore",
    )
    cast(dict[str, object], restore["payload"])["event_id"] = str(event_id)
    commands = [_command(mutation_id=uuid4(), event_id=event_id), create, update, retire, restore]
    body = _body(sync_database, installation_id, commands)
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post("/api/v1/sync/push", json=body)
        assert response.status_code == 200
        outcomes = response.json()["outcomes"]
        assert [(item["command_kind"], item["status"]) for item in outcomes[1:]] == [
            ("receipt.create", "accepted"),
            ("receipt.update", "accepted"),
            ("receipt.lifecycle", "accepted"),
            ("receipt.lifecycle", "accepted"),
        ]
        assert all(
            client.post("/api/v1/sync/push", json=body).json()["outcomes"][index]["replayed"]
            for index in range(1, 5)
        )
        invalid = _body(
            sync_database,
            installation_id,
            [_receipt_command(mutation_id=uuid4(), event_id=event_id, total_amount=12.5)],
        )
        assert (
            client.post("/api/v1/sync/push", json=invalid).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )


@pytest.mark.parametrize(
    ("offset_minutes", "status_code", "has_warning"),
    [
        (0, 200, False),
        (6, 200, True),
        (None, 422, False),
    ],
)
def test_push_clock_and_timestamp_boundaries_are_safe(
    sync_database: SyncDatabase, offset_minutes: int | None, status_code: int, has_warning: bool
) -> None:
    installation_id = _installation(sync_database)
    body = _body(sync_database, installation_id, [])
    body["request_sent_at"] = (
        (datetime.now(UTC) + timedelta(minutes=offset_minutes)).isoformat()
        if offset_minutes is not None
        else "2026-08-10T12:00:00"
    )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post("/api/v1/sync/push", json=body)
    assert response.status_code == status_code
    if status_code == 200:
        assert (response.json()["clock_skew_warning"] is not None) is has_warning
