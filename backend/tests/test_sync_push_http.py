import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from test_sync_pull_http import SyncDatabase, _settings, _sign_in

from cookops.main import create_app
from cookops.persistence.models import ClientInstallation, OrganizationMembership, ShoppingList

pytest_plugins = ("test_sync_pull_http",)


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


@pytest.mark.parametrize(
    ("request_sent_at", "status_code", "has_warning"),
    [
        (datetime.now(UTC).isoformat(), 200, False),
        ((datetime.now(UTC) + timedelta(minutes=6)).isoformat(), 200, True),
        ("2026-08-10T12:00:00", 422, False),
    ],
)
def test_push_clock_and_timestamp_boundaries_are_safe(
    sync_database: SyncDatabase, request_sent_at: str, status_code: int, has_warning: bool
) -> None:
    installation_id = _installation(sync_database)
    body = _body(sync_database, installation_id, [])
    body["request_sent_at"] = request_sent_at
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post("/api/v1/sync/push", json=body)
    assert response.status_code == status_code
    if status_code == 200:
        assert (response.json()["clock_skew_warning"] is not None) is has_warning
