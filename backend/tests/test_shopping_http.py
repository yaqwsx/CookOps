import asyncio
import base64
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import PostgresDsn
from sqlalchemy import insert, select
from test_create_shopping_list_service import _scheduled
from test_schedule_recipe_service import ServiceDatabase

from cookops.application.organizations import ExecutionContext
from cookops.application.shopping_lists import CreateShoppingListCommand, create_shopping_list
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.main import create_app
from cookops.persistence.models import ExternalIdentity, ShoppingList

pytest_plugins = ("test_schedule_recipe_service",)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()


def _settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        human_auth_provider=HumanAuthProvider.DUMMY,
        database_url=PostgresDsn(os.environ["TEST_DATABASE_URL"]),
        browser_session_hmac_key=KEY,
    )


def _enable_dummy_sign_in(database: ServiceDatabase) -> None:
    with database.sync_engine.begin() as connection:
        connection.execute(
            insert(ExternalIdentity).values(
                user_id=database.actor_id,
                provider="dummy",
                provider_subject="shopping-member",
                verified_email="member@example.test",
                normalized_verified_email="member@example.test",
            )
        )


def _sign_in(client: TestClient) -> None:
    response = client.post("/auth/dummy/session", json={"subject": "shopping-member"})
    assert response.status_code == 204


def _materialize(database: ServiceDatabase, scheduled_recipe_id: UUID) -> UUID:
    result = asyncio.run(
        create_shopping_list(
            database.sessions,
            ExecutionContext(database.actor_id, database.installation_id),
            CreateShoppingListCommand(
                mutation_id=uuid4(),
                shopping_list_id=uuid4(),
                generation_revision_id=uuid4(),
                organization_id=database.organization_id,
                event_id=database.event_id,
                name="  First supermarket run  ",
                scheduled_recipe_ids=(scheduled_recipe_id,),
                client_wall_time=datetime.now(UTC),
            ),
        )
    )
    return result.shopping_list_id


def _path(database: ServiceDatabase) -> str:
    return (
        f"/api/v1/organizations/{database.organization_id}/events/{database.event_id}"
        "/shopping-lists"
    )


def test_materialized_shopping_list_summaries_require_authentication_and_are_readable(
    service_database: ServiceDatabase,
) -> None:
    scheduled = asyncio.run(_scheduled(service_database))
    shopping_list_id = _materialize(service_database, scheduled.scheduled_recipe_id)
    _enable_dummy_sign_in(service_database)
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        assert client.get(_path(service_database)).status_code == 401
        _sign_in(client)

        listed = client.get(_path(service_database))
        assert listed.status_code == 200
        summaries = listed.json()["shopping_lists"]
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary["id"] == str(shopping_list_id)
        assert summary["organization_id"] == str(service_database.organization_id)
        assert summary["event_id"] == str(service_database.event_id)
        assert summary["name"] == "First supermarket run"
        assert summary["current_generation_revision_id"] is not None
        assert summary["generated_at"] is not None
        assert summary["source_scheduled_recipe_count"] == 1
        assert summary["ingredient_row_count"] == 1
        assert summary["created_at"] is not None
        assert listed.json()["next_cursor"] is None

        detail = client.get(f"{_path(service_database)}/{shopping_list_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == str(shopping_list_id)
        assert detail.json()["ingredient_row_count"] == 1


def test_shopping_summary_http_hides_foreign_and_unknown_scopes_without_enumeration(
    service_database: ServiceDatabase,
) -> None:
    scheduled = asyncio.run(_scheduled(service_database))
    _materialize(service_database, scheduled.scheduled_recipe_id)
    _enable_dummy_sign_in(service_database)
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client)
        foreign_path = (
            f"/api/v1/organizations/{service_database.other_organization_id}/events/{uuid4()}"
            "/shopping-lists"
        )
        foreign_read = client.get(foreign_path)
        unknown_detail = client.get(f"{_path(service_database)}/{uuid4()}")

    assert foreign_read.status_code == unknown_detail.status_code == 404
    assert foreign_read.json() == {"detail": {"code": "not_found"}}
    assert unknown_detail.json() == {"detail": {"code": "not_found"}}


def test_shopping_summary_http_uses_a_scoped_opaque_cursor_and_bounded_pages(
    service_database: ServiceDatabase,
) -> None:
    scheduled = asyncio.run(_scheduled(service_database))
    first_id = _materialize(service_database, scheduled.scheduled_recipe_id)
    second_id = _materialize(service_database, scheduled.scheduled_recipe_id)
    _enable_dummy_sign_in(service_database)
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client)
        first = client.get(_path(service_database), params={"page_size": 1})
        assert first.status_code == 200
        first_payload = first.json()
        assert len(first_payload["shopping_lists"]) == 1
        cursor = first_payload["next_cursor"]
        assert isinstance(cursor, str)

        second = client.get(_path(service_database), params={"page_size": 1, "cursor": cursor})
        assert second.status_code == 200
        second_payload = second.json()
        assert len(second_payload["shopping_lists"]) == 1
        assert second_payload["next_cursor"] is None
        assert {
            first_payload["shopping_lists"][0]["id"],
            second_payload["shopping_lists"][0]["id"],
        } == {str(first_id), str(second_id)}

        forged = client.get(
            _path(service_database), params={"page_size": 1, "cursor": cursor + "x"}
        )
    assert forged.status_code == 400
    assert forged.json() == {"detail": "invalid cursor"}


def test_shopping_summary_http_does_not_add_a_second_browser_mutation_transport(
    service_database: ServiceDatabase,
) -> None:
    _enable_dummy_sign_in(service_database)
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client)
        response = client.post(_path(service_database), json={})
    assert response.status_code == 405
    with service_database.sync_engine.connect() as connection:
        assert connection.scalar(select(ShoppingList.id)) is None
