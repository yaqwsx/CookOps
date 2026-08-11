import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from test_sync_pull_http import SyncDatabase, _settings, _sign_in
from test_sync_pull_http import sync_database as _sync_database_fixture

from cookops.main import create_app
from cookops.persistence.models import (
    AdHocShoppingItem,
    ClientInstallation,
    Event,
    EventArchiveSnapshot,
    EventDay,
    EventMealRole,
    FieldClock,
    Ingredient,
    IngredientVersion,
    Mutation,
    OrganizationChange,
    OrganizationMembership,
    Recipe,
    RecipeVersion,
    RecipeVersionIngredientLine,
    ScheduledRecipe,
    ShoppingIngredientRow,
    ShoppingList,
    StoreSection,
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
    elif kind == "event.lifecycle":
        payload = {"event_id": str(event_id), "operation": "archive", **payload}
    elif kind == "event.update_base_attendance":
        payload = {"event_id": str(event_id), **payload}
    elif kind == "event.metadata":
        payload = {
            "event_id": str(event_id),
            "name": "Updated push event",
            "location": "Prague",
            "budget_amount": "25.50",
            "general_note": "Bring cups",
            **payload,
        }
    elif kind == "shopping_list.create":
        payload = {
            "shopping_list_id": str(uuid4()),
            "generation_revision_id": str(uuid4()),
            "event_id": str(event_id),
            "name": "Push shopping",
            "scheduled_recipe_ids": [],
            **payload,
        }
    elif kind == "shopping_list.refresh":
        payload = {
            "generation_revision_id": str(uuid4()),
            "shopping_list_id": str(uuid4()),
            "parent_generation_revision_id": str(uuid4()),
            "scheduled_recipe_ids": [],
            **payload,
        }
    elif kind == "shopping_list.rename":
        payload = {
            "shopping_list_id": str(uuid4()),
            "name": "Renamed shopping",
            **payload,
        }
    elif kind == "shopping_list.create_ad_hoc_item":
        payload = {
            "shopping_list_id": str(uuid4()),
            "ad_hoc_shopping_item_id": str(uuid4()),
            "name": "Ad-hoc item",
            "target_amount": "1",
            "unit_id": str(uuid4()),
            "store_section_id": str(uuid4()),
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


def _recipe_lifecycle_command(
    *, mutation_id: UUID, recipe_id: UUID, operation: str = "retire", **payload: object
) -> dict[str, object]:
    return _command(
        mutation_id=mutation_id,
        event_id=uuid4(),
        kind="recipe.lifecycle",
        recipe_id=str(recipe_id),
        operation=operation,
        **payload,
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


def _ingredient_lifecycle_command(
    *, mutation_id: UUID, ingredient_id: UUID, operation: str = "retire", **payload: object
) -> dict[str, object]:
    return _command(
        mutation_id=mutation_id,
        event_id=uuid4(),
        kind="ingredient.lifecycle",
        ingredient_id=str(ingredient_id),
        operation=operation,
        **payload,
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


def _scheduled_recipe_attendance_command(
    *, mutation_id: UUID, event_id: UUID, scheduled_recipe_id: UUID, **payload: object
) -> dict[str, object]:
    values: dict[str, object] = {
        "scheduled_recipe_id": str(scheduled_recipe_id),
        "operation": "set_manual",
        "diner_count": 17,
    }
    values.update(payload)
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="scheduled_recipe.attendance",
        **values,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _scheduled_recipe_lifecycle_command(
    *, mutation_id: UUID, event_id: UUID, scheduled_recipe_id: UUID, operation: str = "retire"
) -> dict[str, object]:
    command = _command(
        mutation_id=mutation_id, event_id=event_id, kind="scheduled_recipe.lifecycle",
        scheduled_recipe_id=str(scheduled_recipe_id), operation=operation,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _event_day_visibility_command(
    *, mutation_id: UUID, event_id: UUID, event_day_id: UUID, is_visible: object
) -> dict[str, object]:
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="event_day.visibility",
        event_day_id=str(event_day_id),
        is_visible=is_visible,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _event_day_note_command(
    *, mutation_id: UUID, event_id: UUID, event_day_id: UUID, note: object
) -> dict[str, object]:
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="event_day.note",
        event_day_id=str(event_day_id),
        note=note,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _event_meal_role_command(
    *, mutation_id: UUID, event_id: UUID, event_meal_role_id: UUID, custom_name: object
) -> dict[str, object]:
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="event_meal_role.create",
        event_meal_role_id=str(event_meal_role_id),
        custom_name=custom_name,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _event_meal_role_name_command(
    *, mutation_id: UUID, event_id: UUID, event_meal_role_id: UUID, custom_name: object
) -> dict[str, object]:
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="event_meal_role.name",
        event_meal_role_id=str(event_meal_role_id),
        custom_name=custom_name,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _event_day_create_command(
    *, mutation_id: UUID, event_id: UUID, event_day_id: UUID, calendar_date: object
) -> dict[str, object]:
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="event_day.create",
        event_day_id=str(event_day_id),
        calendar_date=calendar_date,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _scheduled_recipe_context_command(
    *, mutation_id: UUID, event_id: UUID, scheduled_recipe_id: UUID, **payload: object
) -> dict[str, object]:
    values: dict[str, object] = {
        "scheduled_recipe_id": str(scheduled_recipe_id),
        "consumption_percentage": "75",
        "operation": "set_manual",
        "selected_scale_amount": "3",
    }
    values.update(payload)
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="scheduled_recipe.context",
        **values,
    )
    cast(dict[str, object], command["payload"])["event_id"] = str(event_id)
    return command


def _scheduled_ingredient_override_command(
    *,
    mutation_id: UUID,
    event_id: UUID,
    scheduled_recipe_id: UUID,
    line_key: UUID,
    **payload: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "override_id": str(uuid4()),
        "scheduled_recipe_id": str(scheduled_recipe_id),
        "operation": "set",
        "override_kind": "replace",
        "target_line_key": str(line_key),
        "quantity": "2.5",
    }
    values.update(payload)
    if values["override_kind"] == "add":
        values.pop("target_line_key", None)
    command = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="scheduled_recipe.ingredient_override",
        **values,
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


def test_push_replicates_catalog_configuration_and_replays_identity(
    sync_database: SyncDatabase, tmp_path: Path
) -> None:
    installation_id = _installation(sync_database)
    mutation_id, tag_id = uuid4(), uuid4()
    command = {
        "mutation_id": str(mutation_id),
        "command_kind": "catalog_configuration.mutate",
        "command_schema_version": 1,
        "client_wall_time": datetime.now(UTC).isoformat(),
        "payload": {
            "entity_id": str(tag_id),
            "entity_kind": "recipe_tag",
            "operation": "create",
            "name": "  Soup  ",
            "color": "#123456",
        },
    }
    settings = _settings().model_copy(update={"receipt_media_root": tmp_path / "media"})
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        body = _body(sync_database, installation_id, [command])
        accepted = client.post("/api/v1/sync/push", json=body).json()["outcomes"][0]
        assert accepted["status"] == "accepted"
        assert client.post("/api/v1/sync/push", json=body).json()["outcomes"][0]["replayed"]
        bootstrap = client.post(
            "/api/v1/sync/bootstrap",
            json={"organization_id": str(sync_database.organization_id)},
        ).json()
        record = next(
            item["payload"]["record"]
            for item in bootstrap["records"]
            if item["entity_kind"] == "recipe_tag" and item["entity_id"] == str(tag_id)
        )
        assert set(record["field_clocks"]) == {"name", "color", "lifecycle"}
    with sync_database.engine.connect() as connection:
        assert set(
            connection.scalars(
                select(FieldClock.field_name).where(
                    FieldClock.organization_id == sync_database.organization_id,
                    FieldClock.entity_kind == "recipe_tag",
                    FieldClock.entity_id == tag_id,
                )
            )
        ) == {"name", "color", "lifecycle"}


def test_catalog_configuration_future_time_is_retained(
    sync_database: SyncDatabase, tmp_path: Path
) -> None:
    installation_id = _installation(sync_database)
    command = {
        "mutation_id": str(uuid4()),
        "command_kind": "catalog_configuration.mutate",
        "command_schema_version": 1,
        "client_wall_time": (datetime.now(UTC) + timedelta(hours=25)).isoformat(),
        "payload": {
            "entity_id": str(uuid4()),
            "entity_kind": "recipe_tag",
            "operation": "create",
            "name": "Future",
            "color": "#123456",
        },
    }
    settings = _settings().model_copy(update={"receipt_media_root": tmp_path / "media"})
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        body = _body(sync_database, installation_id, [command])
        first = client.post("/api/v1/sync/push", json=body).json()["outcomes"][0]
        second = client.post("/api/v1/sync/push", json=body).json()["outcomes"][0]
    assert first["error"]["code"] == second["error"]["code"] == "client_time_too_far_ahead"
    with sync_database.engine.connect() as connection:
        assert (
            connection.scalar(
                select(Mutation.outcome).where(Mutation.id == UUID(command["mutation_id"]))
            )
            == "rejected"
        )


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


def test_push_creates_an_ad_hoc_item_with_scoped_dependencies_and_replays(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    event_id, list_id, revision_id, item_id, mutation_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    section_id = uuid4()
    with sync_database.engine.begin() as connection:
        actor_id = connection.scalar(
            select(OrganizationMembership.user_id).where(
                OrganizationMembership.organization_id == sync_database.organization_id
            )
        )
        unit_id = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        assert isinstance(actor_id, UUID) and isinstance(unit_id, UUID)
        connection.execute(
            insert(StoreSection).values(
                id=section_id,
                organization_id=sync_database.organization_id,
                name="Produce",
                normalized_name="produce",
                position_key="a",
                created_by_user_id=actor_id,
            )
        )
    setup = [
        _command(mutation_id=uuid4(), event_id=event_id),
        _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.create",
            shopping_list_id=str(list_id),
            generation_revision_id=str(revision_id),
        ),
    ]
    item = _command(
        mutation_id=mutation_id,
        event_id=event_id,
        kind="shopping_list.create_ad_hoc_item",
        shopping_list_id=str(list_id),
        ad_hoc_shopping_item_id=str(item_id),
        name="  Lemons ",
        target_amount="3.5",
        unit_id=str(unit_id),
        store_section_id=str(section_id),
        note="fresh",
    )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        assert [
            outcome["status"]
            for outcome in client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, setup)
            ).json()["outcomes"]
        ] == ["accepted", "accepted"]
        accepted = client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [item])
        ).json()["outcomes"][0]
        assert accepted["command_kind"] == "shopping_list.create_ad_hoc_item"
        assert accepted["status"] == "accepted" and not accepted["replayed"]
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [item])
            ).json()["outcomes"][0]["replayed"]
            is True
        )
        update = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.update_ad_hoc_item",
            shopping_list_id=str(list_id),
            ad_hoc_shopping_item_id=str(item_id),
            name="Limes",
            target_amount="4",
            unit_id=str(unit_id),
            store_section_id=str(section_id),
            note=None,
        )
        updated = client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [update])
        ).json()["outcomes"][0]
        assert updated["command_kind"] == "shopping_list.update_ad_hoc_item"
        assert updated["status"] == "accepted" and not updated["replayed"]
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [update])
        ).json()["outcomes"][0]["replayed"] is True
        malformed_update = {**update, "mutation_id": str(uuid4()), "payload": {**update["payload"], "target_amount": 4}}
        malformed_outcome = client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [malformed_update])
        ).json()["outcomes"][0]
        assert malformed_outcome["status"] == "rejected"
        fulfilment = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.set_ad_hoc_item_fulfilment",
            shopping_list_id=str(list_id),
            ad_hoc_shopping_item_id=str(item_id),
            fulfilled=True,
        )
        outcome = client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [fulfilment])
        ).json()["outcomes"][0]
        assert outcome["status"] == "accepted" and not outcome["replayed"]
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [fulfilment])
        ).json()["outcomes"][0]["replayed"] is True
        retire = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.ad_hoc_item_lifecycle",
            shopping_list_id=str(list_id),
            ad_hoc_shopping_item_id=str(item_id),
            operation="retire",
        )
        retired = client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [retire])
        ).json()["outcomes"][0]
        assert retired["status"] == "accepted"
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [retire])
        ).json()["outcomes"][0]["replayed"] is True
        retired_fulfilment = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.set_ad_hoc_item_fulfilment",
            shopping_list_id=str(list_id),
            ad_hoc_shopping_item_id=str(item_id),
            fulfilled=False,
        )
        assert client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [retired_fulfilment]),
        ).json()["outcomes"][0]["status"] == "rejected"
        restore = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.ad_hoc_item_lifecycle",
            shopping_list_id=str(list_id),
            ad_hoc_shopping_item_id=str(item_id),
            operation="restore",
        )
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [restore])
        ).json()["outcomes"][0]["status"] == "accepted"
        missing_fulfilment = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.set_ad_hoc_item_fulfilment",
            shopping_list_id=str(list_id),
            ad_hoc_shopping_item_id=str(uuid4()),
            fulfilled=True,
        )
        first_rejection = client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [missing_fulfilment]),
        ).json()["outcomes"][0]
        replayed_rejection = client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [missing_fulfilment]),
        ).json()["outcomes"][0]
        assert first_rejection["error"]["code"] == "validation_failed"
        assert replayed_rejection["error"] == first_rejection["error"]
        bootstrap = client.post(
            "/api/v1/sync/bootstrap",
            json={"organization_id": str(sync_database.organization_id)},
        ).json()
        record = next(
            item["payload"]["record"]
            for item in bootstrap["records"]
            if item["entity_kind"] == "ad_hoc_shopping_item"
            and item["entity_id"] == str(item_id)
        )
        assert record["field_clocks"]["fulfilment_credit"]["winning_mutation_id"] == fulfilment[
            "mutation_id"
        ]
        wrong_scope = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.create_ad_hoc_item",
            shopping_list_id=str(list_id),
            ad_hoc_shopping_item_id=str(uuid4()),
            unit_id=str(unit_id),
            store_section_id=str(uuid4()),
        )
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [wrong_scope])
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
    with sync_database.engine.connect() as connection:
        retired_record = connection.scalar(
            select(OrganizationChange.payload).where(
                OrganizationChange.mutation_id == UUID(retire["mutation_id"])
            )
        )
        assert isinstance(retired_record, dict)
        assert isinstance(retired_record["record"].get("retired_at"), str)
        assert connection.scalar(
            select(Mutation.outcome).where(Mutation.id == UUID(missing_fulfilment["mutation_id"]))
        ) == "rejected"
        assert connection.execute(
            select(
                AdHocShoppingItem.name,
                AdHocShoppingItem.target_amount,
                AdHocShoppingItem.fulfilment_credit,
            ).where(
                AdHocShoppingItem.id == item_id
            )
        ).one() == ("Limes", Decimal("4"), Decimal("4"))


def test_push_refreshes_shopping_list_through_the_typed_shared_command(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    event_id, shopping_list_id, parent_revision_id = uuid4(), uuid4(), uuid4()
    refresh_mutation_id, refresh_revision_id = uuid4(), uuid4()
    setup = [
        _command(mutation_id=uuid4(), event_id=event_id),
        _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.create",
            shopping_list_id=str(shopping_list_id),
            generation_revision_id=str(parent_revision_id),
        ),
    ]
    refresh = _command(
        mutation_id=refresh_mutation_id,
        event_id=event_id,
        kind="shopping_list.refresh",
        shopping_list_id=str(shopping_list_id),
        parent_generation_revision_id=str(parent_revision_id),
        generation_revision_id=str(refresh_revision_id),
    )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        assert [item["status"] for item in client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, setup)
        ).json()["outcomes"]] == ["accepted", "accepted"]
        accepted = client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [refresh]),
        ).json()["outcomes"][0]
        assert accepted["command_kind"] == "shopping_list.refresh"
        assert accepted["status"] == "accepted" and not accepted["replayed"]
        assert client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [refresh]),
        ).json()["outcomes"][0]["replayed"] is True
        stale = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.refresh",
            shopping_list_id=str(shopping_list_id),
            parent_generation_revision_id=str(uuid4()),
        )
        assert client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [stale]),
        ).json()["outcomes"][0]["error"]["code"] == "stale_precondition"
        malformed = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.refresh",
            shopping_list_id=str(shopping_list_id),
            parent_generation_revision_id=str(refresh_revision_id),
            unexpected="value",
        )
        assert client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [malformed]),
        ).json()["outcomes"][0]["error"]["code"] == "validation_failed"
        duplicate_sources = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.refresh",
            shopping_list_id=str(shopping_list_id),
            parent_generation_revision_id=str(refresh_revision_id),
            scheduled_recipe_ids=[str(uuid4())] * 2,
        )
        assert client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [duplicate_sources]),
        ).json()["outcomes"][0]["error"]["code"] == "validation_failed"


def test_push_renames_shopping_list_through_typed_command_and_rejects_invalid_scope(
    sync_database: SyncDatabase,
) -> None:
    installation_id = _installation(sync_database)
    event_id, shopping_list_id = uuid4(), uuid4()
    create = [
        _command(mutation_id=uuid4(), event_id=event_id),
        _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.create",
            shopping_list_id=str(shopping_list_id),
        ),
    ]
    rename = _command(
        mutation_id=uuid4(),
        event_id=event_id,
        kind="shopping_list.rename",
        shopping_list_id=str(shopping_list_id),
        name="  Café  ",
    )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        assert [item["status"] for item in client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, create)
        ).json()["outcomes"]] == ["accepted", "accepted"]
        accepted = client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [rename])
        ).json()["outcomes"][0]
        assert accepted["command_kind"] == "shopping_list.rename"
        assert accepted["status"] == "accepted" and not accepted["replayed"]
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [rename])
        ).json()["outcomes"][0]["replayed"] is True
        malformed = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.rename",
            shopping_list_id=str(shopping_list_id),
            name="x" * 201,
        )
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [malformed])
        ).json()["outcomes"][0]["error"]["code"] == "validation_failed"
        bootstrap = client.post(
            "/api/v1/sync/bootstrap",
            json={"organization_id": str(sync_database.organization_id)},
        ).json()
        record = next(
            item["payload"]["record"]
            for item in bootstrap["records"]
            if item["entity_kind"] == "shopping_list"
            and item["entity_id"] == str(shopping_list_id)
        )
        assert record["name"] == "Café"
        assert set(record["field_clocks"]) == {"name", "current_generation_revision_id"}
        archive = _command(mutation_id=uuid4(), event_id=event_id, kind="event.lifecycle")
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [archive])
        ).json()["outcomes"][0]["status"] == "accepted"
        archived = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="shopping_list.rename",
            shopping_list_id=str(shopping_list_id),
        )
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [archived])
        ).json()["outcomes"][0]["error"]["code"] == "archived_event"


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
        lifecycle = _body(
            sync_database,
            installation_id,
            [_recipe_lifecycle_command(mutation_id=uuid4(), recipe_id=recipe_id)],
        )
        retired = client.post("/api/v1/sync/push", json=lifecycle).json()["outcomes"][0]
        assert retired["command_kind"] == "recipe.lifecycle"
        assert retired["status"] == "accepted"
        restored = _body(
            sync_database,
            installation_id,
            [
                _recipe_lifecycle_command(
                    mutation_id=uuid4(), recipe_id=recipe_id, operation="restore"
                )
            ],
        )
        assert client.post("/api/v1/sync/push", json=restored).json()["outcomes"][0][
            "status"
        ] == "accepted"
        assert client.post("/api/v1/sync/push", json=restored).json()["outcomes"][0][
            "replayed"
        ] is True
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
        assert connection.scalar(select(Recipe.retired_at).where(Recipe.id == recipe_id)) is None
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
    ingredient_id, ingredient_version_id, line_key = uuid4(), uuid4(), uuid4()
    added_ingredient_id, added_ingredient_version_id = uuid4(), uuid4()
    foreign_ingredient_id, foreign_ingredient_version_id = uuid4(), uuid4()
    with sync_database.engine.begin() as connection:
        other_actor_id = connection.scalar(
            select(OrganizationMembership.user_id).where(
                OrganizationMembership.organization_id == sync_database.other_organization_id
            )
        )
        assert isinstance(other_actor_id, UUID)
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
                name="Override ingredient",
                normalized_name="override ingredient",
                canonical_unit_id=grams_id,
                mass_per_canonical_quantity=Decimal("1"),
                published_by_user_id=actor_id,
            )
        )
        connection.execute(
            insert(Ingredient),
            [
                {
                    "id": added_ingredient_id,
                    "organization_id": sync_database.organization_id,
                    "current_version_id": added_ingredient_version_id,
                    "created_by_user_id": actor_id,
                },
                {
                    "id": foreign_ingredient_id,
                    "organization_id": sync_database.other_organization_id,
                    "current_version_id": foreign_ingredient_version_id,
                    "created_by_user_id": other_actor_id,
                },
            ],
        )
        connection.execute(
            insert(IngredientVersion),
            [
                {
                    "id": added_ingredient_version_id,
                    "organization_id": sync_database.organization_id,
                    "ingredient_id": added_ingredient_id,
                    "name": "Added ingredient",
                    "normalized_name": "added ingredient",
                    "canonical_unit_id": grams_id,
                    "mass_per_canonical_quantity": Decimal("1"),
                    "published_by_user_id": actor_id,
                },
                {
                    "id": foreign_ingredient_version_id,
                    "organization_id": sync_database.other_organization_id,
                    "ingredient_id": foreign_ingredient_id,
                    "name": "Foreign ingredient",
                    "normalized_name": "foreign ingredient",
                    "canonical_unit_id": grams_id,
                    "mass_per_canonical_quantity": Decimal("1"),
                    "published_by_user_id": other_actor_id,
                },
            ],
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
                    _recipe_command(
                        mutation_id=uuid4(),
                        scaling_unit_id=scaling_unit_id,
                        recipe_id=str(recipe_id),
                        recipe_version_id=str(recipe_version_id),
                        ingredient_lines=[
                            {
                                "id": str(uuid4()),
                                "line_key": str(line_key),
                                "ingredient_version_id": str(ingredient_version_id),
                                "base_quantity": "1",
                                "position_key": "a",
                                "scaling_behavior": "fixed",
                                "include_in_portion_weight": True,
                            }
                        ],
                    ),
                ],
            ),
        )
        assert [outcome["status"] for outcome in setup.json()["outcomes"]] == [
            "accepted",
            "accepted",
        ]
        metadata = _command(mutation_id=uuid4(), event_id=event_id, kind="event.metadata")
        metadata_body = _body(sync_database, installation_id, [metadata])
        metadata_outcome = client.post(
            "/api/v1/sync/push", json=metadata_body
        ).json()["outcomes"][0]
        assert metadata_outcome["status"] == "accepted"
        assert metadata_outcome["command_kind"] == "event.metadata"
        assert client.post("/api/v1/sync/push", json=metadata_body).json()["outcomes"][0][
            "replayed"
        ]
        malformed_metadata = _command(
            mutation_id=uuid4(), event_id=event_id, kind="event.metadata", budget_amount=25.5
        )
        assert (
            client.post(
                "/api/v1/sync/push",
                json=_body(sync_database, installation_id, [malformed_metadata]),
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
        with sync_database.engine.connect() as connection:
            assert connection.execute(
                select(Event.name, Event.location, Event.budget_amount, Event.general_note).where(
                    Event.id == event_id
                )
            ).one() == ("Updated push event", "Prague", Decimal("25.50"), "Bring cups")
        with sync_database.engine.connect() as connection:
            event_day_id = connection.scalar(
                select(EventDay.id).where(EventDay.event_id == event_id)
            )
            assert isinstance(event_day_id, UUID)
        visibility = _event_day_visibility_command(
            mutation_id=uuid4(),
            event_id=event_id,
            event_day_id=event_day_id,
            is_visible=False,
        )
        visibility_body = _body(sync_database, installation_id, [visibility])
        visibility_outcome = client.post(
            "/api/v1/sync/push", json=visibility_body
        ).json()["outcomes"][0]
        assert visibility_outcome["status"] == "accepted"
        assert visibility_outcome["command_kind"] == "event_day.visibility"
        assert client.post("/api/v1/sync/push", json=visibility_body).json()["outcomes"][0][
            "replayed"
        ]
        note = _event_day_note_command(
            mutation_id=uuid4(),
            event_id=event_id,
            event_day_id=event_day_id,
            note="Friday menu",
        )
        note_body = _body(sync_database, installation_id, [note])
        note_outcome = client.post("/api/v1/sync/push", json=note_body).json()["outcomes"][0]
        assert note_outcome["status"] == "accepted"
        assert note_outcome["command_kind"] == "event_day.note"
        assert client.post("/api/v1/sync/push", json=note_body).json()["outcomes"][0]["replayed"]
        malformed_note = _event_day_note_command(
            mutation_id=uuid4(), event_id=event_id, event_day_id=event_day_id, note=42
        )
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [malformed_note])
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
        nul_note = _event_day_note_command(
            mutation_id=uuid4(), event_id=event_id, event_day_id=event_day_id, note="bad\0note"
        )
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [nul_note])
        ).json()["outcomes"][0]["status"] == "rejected"
        meal_role = _event_meal_role_command(
            mutation_id=uuid4(),
            event_id=event_id,
            event_meal_role_id=uuid4(),
            custom_name="Late supper",
        )
        meal_role_body = _body(sync_database, installation_id, [meal_role])
        meal_role_outcome = client.post(
            "/api/v1/sync/push", json=meal_role_body
        ).json()["outcomes"][0]
        assert meal_role_outcome["status"] == "accepted"
        assert meal_role_outcome["command_kind"] == "event_meal_role.create"
        assert client.post("/api/v1/sync/push", json=meal_role_body).json()["outcomes"][0][
            "replayed"
        ]
        role_name = _event_meal_role_name_command(
            mutation_id=uuid4(),
            event_id=event_id,
            event_meal_role_id=UUID(meal_role["payload"]["event_meal_role_id"]),
            custom_name="Late dinner",
        )
        role_name_body = _body(sync_database, installation_id, [role_name])
        role_name_outcome = client.post(
            "/api/v1/sync/push", json=role_name_body
        ).json()["outcomes"][0]
        assert role_name_outcome["status"] == "accepted"
        assert role_name_outcome["command_kind"] == "event_meal_role.name"
        assert client.post("/api/v1/sync/push", json=role_name_body).json()["outcomes"][0][
            "replayed"
        ]
        role_position = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="event_meal_role.position",
            event_meal_role_id=meal_role["payload"]["event_meal_role_id"],
            position_key="z9",
        )
        cast(dict[str, object], role_position["payload"])["event_id"] = str(event_id)
        role_position_body = _body(sync_database, installation_id, [role_position])
        assert client.post("/api/v1/sync/push", json=role_position_body).json()["outcomes"][0][
            "status"
        ] == "accepted"
        assert client.post("/api/v1/sync/push", json=role_position_body).json()["outcomes"][0][
            "replayed"
        ]
        role_lifecycle = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="event_meal_role.lifecycle",
            event_meal_role_id=meal_role["payload"]["event_meal_role_id"],
            operation="retire",
        )
        cast(dict[str, object], role_lifecycle["payload"])["event_id"] = str(event_id)
        role_lifecycle_body = _body(sync_database, installation_id, [role_lifecycle])
        assert client.post(
            "/api/v1/sync/push", json=role_lifecycle_body
        ).json()["outcomes"][0]["status"] == "accepted"
        assert client.post(
            "/api/v1/sync/push", json=role_lifecycle_body).json()["outcomes"][0][
            "replayed"
        ]
        malformed_position = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="event_meal_role.position",
            event_meal_role_id=meal_role["payload"]["event_meal_role_id"],
            position_key="!",
        )
        cast(dict[str, object], malformed_position["payload"])["event_id"] = str(event_id)
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [malformed_position])
        ).json()["outcomes"][0]["error"]["code"] == "validation_failed"
        bootstrap = client.post(
            "/api/v1/sync/bootstrap",
            json={"organization_id": str(sync_database.organization_id)},
        ).json()
        role_record = next(
            item["payload"]["record"]
            for item in bootstrap["records"]
            if item["entity_kind"] == "event_meal_role"
            and item["entity_id"] == meal_role["payload"]["event_meal_role_id"]
        )
        assert role_record["position_key"] == "z9"
        assert role_record["custom_name"] == "Late dinner"
        assert role_record["field_clocks"]["custom_name"]["winning_mutation_id"] == role_name[
            "mutation_id"
        ]
        assert role_record["field_clocks"]["position_key"]["winning_mutation_id"] == role_position[
            "mutation_id"
        ]
        assert role_record["retired_at"] is not None
        assert role_record["field_clocks"]["lifecycle"]["winning_mutation_id"] == role_lifecycle[
            "mutation_id"
        ]
        malformed_role = _event_meal_role_command(
            mutation_id=uuid4(), event_id=event_id, event_meal_role_id=uuid4(), custom_name=""
        )
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [malformed_role])
        ).json()["outcomes"][0]["error"]["code"] == "validation_failed"
        malformed_role_name = _event_meal_role_name_command(
            mutation_id=uuid4(),
            event_id=event_id,
            event_meal_role_id=UUID(meal_role["payload"]["event_meal_role_id"]),
            custom_name="bad\0name",
        )
        assert client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [malformed_role_name]),
        ).json()["outcomes"][0]["error"]["code"] == "validation_failed"
        malformed_visibility = _event_day_visibility_command(
            mutation_id=uuid4(),
            event_id=event_id,
            event_day_id=event_day_id,
            is_visible="false",
        )
        assert client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [malformed_visibility]),
        ).json()["outcomes"][0]["error"]["code"] == "validation_failed"
        created_day = _event_day_create_command(
            mutation_id=uuid4(),
            event_id=event_id,
            event_day_id=uuid4(),
            calendar_date="2026-08-11",
        )
        created_day_body = _body(sync_database, installation_id, [created_day])
        assert (
            client.post("/api/v1/sync/push", json=created_day_body).json()["outcomes"][0]["status"]
            == "accepted"
        )
        assert client.post("/api/v1/sync/push", json=created_day_body).json()["outcomes"][0][
            "replayed"
        ]
        day_lifecycle = _command(
            mutation_id=uuid4(),
            event_id=event_id,
            kind="event_day.lifecycle",
            event_day_id=str(created_day["payload"]["event_day_id"]),
            operation="retire",
        )
        cast(dict[str, object], day_lifecycle["payload"])["event_id"] = str(event_id)
        lifecycle_body = _body(sync_database, installation_id, [day_lifecycle])
        assert client.post("/api/v1/sync/push", json=lifecycle_body).json()["outcomes"][0][
            "status"
        ] == "accepted"
        assert client.post("/api/v1/sync/push", json=lifecycle_body).json()["outcomes"][0][
            "replayed"
        ]
        invalid_lifecycle = {
            **day_lifecycle,
            "mutation_id": str(uuid4()),
            "payload": {**day_lifecycle["payload"], "operation": "hide"},
        }
        assert client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [invalid_lifecycle])
        ).json()["outcomes"][0]["error"]["code"] == "validation_failed"
        malformed_day = _event_day_create_command(
            mutation_id=uuid4(),
            event_id=event_id,
            event_day_id=uuid4(),
            calendar_date="not-a-date",
        )
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [malformed_day])
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
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
        attendance = _scheduled_recipe_attendance_command(
            mutation_id=uuid4(), event_id=event_id, scheduled_recipe_id=scheduled_recipe_id
        )
        attendance_response = client.post(
            "/api/v1/sync/push", json=_body(sync_database, installation_id, [attendance])
        )
        assert attendance_response.json()["outcomes"][0]["status"] == "accepted"
        context = _scheduled_recipe_context_command(
            mutation_id=uuid4(), event_id=event_id, scheduled_recipe_id=scheduled_recipe_id
        )
        context_body = _body(sync_database, installation_id, [context])
        context_response = client.post("/api/v1/sync/push", json=context_body)
        assert context_response.json()["outcomes"][0]["status"] == "accepted"
        assert context_response.json()["outcomes"][0]["command_kind"] == "scheduled_recipe.context"
        assert client.post("/api/v1/sync/push", json=context_body).json()["outcomes"][0]["replayed"]
        malformed_context = _scheduled_recipe_context_command(
            mutation_id=uuid4(),
            event_id=event_id,
            scheduled_recipe_id=scheduled_recipe_id,
            consumption_percentage=75,
        )
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [malformed_context])
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
        for malformed_diners in ("17", True):
            malformed_attendance = _scheduled_recipe_attendance_command(
                mutation_id=uuid4(),
                event_id=event_id,
                scheduled_recipe_id=scheduled_recipe_id,
                diner_count=malformed_diners,
            )
            assert (
                client.post(
                    "/api/v1/sync/push",
                    json=_body(sync_database, installation_id, [malformed_attendance]),
                ).json()["outcomes"][0]["error"]["code"]
                == "validation_failed"
            )
        override = _scheduled_ingredient_override_command(
            mutation_id=uuid4(),
            event_id=event_id,
            scheduled_recipe_id=scheduled_recipe_id,
            line_key=line_key,
        )
        override_body = _body(sync_database, installation_id, [override])
        override_outcome = client.post("/api/v1/sync/push", json=override_body).json()["outcomes"][
            0
        ]
        assert override_outcome["status"] == "accepted"
        assert override_outcome["command_kind"] == "scheduled_recipe.ingredient_override"
        assert client.post("/api/v1/sync/push", json=override_body).json()["outcomes"][0][
            "replayed"
        ]
        added_override = _scheduled_ingredient_override_command(
            mutation_id=uuid4(),
            event_id=event_id,
            scheduled_recipe_id=scheduled_recipe_id,
            line_key=line_key,
            override_kind="add",
            ingredient_id=str(added_ingredient_id),
            ingredient_version_id=str(added_ingredient_version_id),
            include_in_portion_weight=True,
            position_key="z",
        )
        added_body = _body(sync_database, installation_id, [added_override])
        added_outcome = client.post("/api/v1/sync/push", json=added_body).json()["outcomes"][0]
        assert added_outcome["status"] == "accepted"
        assert client.post("/api/v1/sync/push", json=added_body).json()["outcomes"][0][
            "replayed"
        ]
        malformed_added = _scheduled_ingredient_override_command(
            mutation_id=uuid4(),
            event_id=event_id,
            scheduled_recipe_id=scheduled_recipe_id,
            line_key=line_key,
            override_kind="add",
            ingredient_id=str(added_ingredient_id),
            ingredient_version_id=str(added_ingredient_version_id),
            include_in_portion_weight="true",
            position_key="z",
        )
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [malformed_added])
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
        mixed_added = _scheduled_ingredient_override_command(
            mutation_id=uuid4(),
            event_id=event_id,
            scheduled_recipe_id=scheduled_recipe_id,
            line_key=line_key,
            override_kind="add",
            ingredient_id=str(added_ingredient_id),
            ingredient_version_id=str(added_ingredient_version_id),
            include_in_portion_weight=True,
            position_key="z",
        )
        cast(dict[str, object], mixed_added["payload"])["target_line_key"] = str(line_key)
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [mixed_added])
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
        cross_organization_add = _scheduled_ingredient_override_command(
            mutation_id=uuid4(),
            event_id=event_id,
            scheduled_recipe_id=scheduled_recipe_id,
            line_key=line_key,
            override_kind="add",
            ingredient_id=str(foreign_ingredient_id),
            ingredient_version_id=str(foreign_ingredient_version_id),
            include_in_portion_weight=True,
            position_key="z",
        )
        assert (
            client.post(
                "/api/v1/sync/push",
                json=_body(sync_database, installation_id, [cross_organization_add]),
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
        malformed_override = _scheduled_ingredient_override_command(
            mutation_id=uuid4(),
            event_id=event_id,
            scheduled_recipe_id=scheduled_recipe_id,
            line_key=line_key,
            quantity=2.5,
        )
        assert (
            client.post(
                "/api/v1/sync/push",
                json=_body(sync_database, installation_id, [malformed_override]),
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
        clear_override = _scheduled_ingredient_override_command(
            mutation_id=uuid4(),
            event_id=event_id,
            scheduled_recipe_id=scheduled_recipe_id,
            line_key=line_key,
            operation="clear",
        )
        assert (
            client.post(
                "/api/v1/sync/push",
                json=_body(sync_database, installation_id, [clear_override]),
            ).json()["outcomes"][0]["error"]["code"]
            == "validation_failed"
        )
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
        lifecycle = _scheduled_recipe_lifecycle_command(
            mutation_id=uuid4(), event_id=event_id, scheduled_recipe_id=scheduled_recipe_id
        )
        lifecycle_body = _body(sync_database, installation_id, [lifecycle])
        lifecycle_outcome = client.post(
            "/api/v1/sync/push", json=lifecycle_body
        ).json()["outcomes"][0]
        assert lifecycle_outcome["command_kind"] == "scheduled_recipe.lifecycle"
        assert lifecycle_outcome["status"] == "accepted"
        assert client.post("/api/v1/sync/push", json=lifecycle_body).json()["outcomes"][0][
            "replayed"
        ]
        malformed_lifecycle = _scheduled_recipe_lifecycle_command(
            mutation_id=uuid4(), event_id=event_id, scheduled_recipe_id=scheduled_recipe_id
        )
        cast(dict[str, object], malformed_lifecycle["payload"])["operation"] = "delete"
        assert client.post(
            "/api/v1/sync/push",
            json=_body(sync_database, installation_id, [malformed_lifecycle]),
        ).json()["outcomes"][0]["error"]["code"] == "validation_failed"
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
        snapshot_id = uuid4()
        with sync_database.engine.begin() as connection:
            connection.execute(
                insert(EventArchiveSnapshot).values(
                    id=snapshot_id,
                    event_id=event_id,
                    archive_schema_version=1,
                    payload={"event": {}},
                    content_hash=b"s" * 32,
                    attachment_manifest=[],
                    created_by_user_id=actor_id,
                )
            )
            connection.execute(
                update(Event)
                .where(Event.id == event_id)
                .values(
                    lifecycle="archived",
                    current_archive_snapshot_id=snapshot_id,
                    archived_at=datetime.now(UTC),
                    archived_by_user_id=actor_id,
                )
            )
        archived_add = _scheduled_ingredient_override_command(
            mutation_id=uuid4(),
            event_id=event_id,
            scheduled_recipe_id=scheduled_recipe_id,
            line_key=line_key,
            override_kind="add",
            ingredient_id=str(added_ingredient_id),
            ingredient_version_id=str(added_ingredient_version_id),
            include_in_portion_weight=True,
            position_key="z",
        )
        assert (
            client.post(
                "/api/v1/sync/push", json=_body(sync_database, installation_id, [archived_add])
            ).json()["outcomes"][0]["error"]["code"]
            == "archived_event"
        )
    with sync_database.engine.connect() as connection:
        assert connection.execute(
            select(
                ScheduledRecipe.consumption_percentage,
                ScheduledRecipe.position_key,
                ScheduledRecipe.note,
            ).where(ScheduledRecipe.id == scheduled_recipe_id)
        ).one() == (Decimal("75"), "z9", "  Serve warm  ")


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
        ingredient_id = UUID(
            cast(dict[str, object], body["commands"][0])["payload"]["ingredient_id"]
        )
        retire = _body(
            sync_database,
            installation_id,
            [_ingredient_lifecycle_command(mutation_id=uuid4(), ingredient_id=ingredient_id)],
        )
        retired = client.post("/api/v1/sync/push", json=retire).json()["outcomes"][0]
        assert (retired["command_kind"], retired["status"]) == (
            "ingredient.lifecycle",
            "accepted",
        )
        restore = _body(
            sync_database,
            installation_id,
            [
                _ingredient_lifecycle_command(
                    mutation_id=uuid4(), ingredient_id=ingredient_id, operation="restore"
                )
            ],
        )
        restored = client.post("/api/v1/sync/push", json=restore).json()["outcomes"][0]
        assert (restored["command_kind"], restored["status"]) == (
            "ingredient.lifecycle",
            "accepted",
        )
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
