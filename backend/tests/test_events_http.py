import base64
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import PostgresDsn
from sqlalchemy import Engine, create_engine, insert

from alembic import command as alembic_command
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.main import create_app
from cookops.persistence.models import (
    Event,
    ExternalIdentity,
    Organization,
    OrganizationMembership,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()


@dataclass
class EventsHttpDatabase:
    configuration: Config
    engine: Engine
    organization_id: UUID
    other_organization_id: UUID
    event_ids: tuple[UUID, ...]


@pytest.fixture
def events_http_database() -> Iterator[EventsHttpDatabase]:
    database_url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url)
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    engine = create_engine(database_url)
    admin_id, member_id, other_id, creator_id = uuid4(), uuid4(), uuid4(), uuid4()
    organization_id, other_organization_id = uuid4(), uuid4()
    event_ids = (uuid4(), uuid4(), uuid4())
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(User),
            [
                {
                    "id": admin_id,
                    "display_name": "Event administrator",
                    "verified_email": "admin@example.test",
                    "normalized_email": "admin@example.test",
                },
                {
                    "id": member_id,
                    "display_name": "Event member",
                    "verified_email": "member@example.test",
                    "normalized_email": "member@example.test",
                },
                {
                    "id": other_id,
                    "display_name": "Other member",
                    "verified_email": "other@example.test",
                    "normalized_email": "other@example.test",
                },
                {
                    "id": creator_id,
                    "display_name": "Creator",
                    "verified_email": "creator@example.test",
                    "normalized_email": "creator@example.test",
                },
            ],
        )
        connection.execute(
            insert(ExternalIdentity),
            [
                {
                    "user_id": user_id,
                    "provider": "dummy",
                    "provider_subject": subject,
                    "verified_email": email,
                    "normalized_verified_email": email,
                }
                for user_id, subject, email in (
                    (admin_id, "event-admin", "admin@example.test"),
                    (member_id, "event-member", "member@example.test"),
                    (other_id, "event-other", "other@example.test"),
                )
            ],
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": organization_id,
                    "name": "Event organization",
                    "default_currency": "CZK",
                    "created_by_user_id": creator_id,
                },
                {
                    "id": other_organization_id,
                    "name": "Other organization",
                    "default_currency": "CZK",
                    "created_by_user_id": creator_id,
                },
            ],
        )
        connection.execute(
            insert(OrganizationMembership),
            [
                {
                    "organization_id": organization_id,
                    "user_id": admin_id,
                    "invited_email": "admin@example.test",
                    "role": "organization_admin",
                    "state": "active",
                    "invited_by_user_id": creator_id,
                    "claimed_at": now,
                },
                {
                    "organization_id": organization_id,
                    "user_id": member_id,
                    "invited_email": "member@example.test",
                    "role": "member",
                    "state": "active",
                    "invited_by_user_id": creator_id,
                    "claimed_at": now,
                },
                {
                    "organization_id": other_organization_id,
                    "user_id": other_id,
                    "invited_email": "other@example.test",
                    "role": "member",
                    "state": "active",
                    "invited_by_user_id": creator_id,
                    "claimed_at": now,
                },
            ],
        )
        connection.execute(
            insert(Event),
            [
                {
                    "id": event_id,
                    "organization_id": organization_id,
                    "name": f"Event {index}",
                    "start_date": date(2026, 7, index + 1),
                    "end_date": date(2026, 7, index + 1),
                    "base_expected_attendance": 10,
                    "budget_amount": Decimal("0"),
                    "currency": "CZK",
                    "created_by_user_id": admin_id,
                    "created_at": now + timedelta(seconds=index),
                }
                for index, event_id in enumerate(event_ids)
            ],
        )
    database = EventsHttpDatabase(
        configuration=configuration,
        engine=engine,
        organization_id=organization_id,
        other_organization_id=other_organization_id,
        event_ids=event_ids,
    )
    try:
        yield database
    finally:
        engine.dispose()
        alembic_command.downgrade(configuration, "base")


def _settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        human_auth_provider=HumanAuthProvider.DUMMY,
        database_url=PostgresDsn(os.environ["TEST_DATABASE_URL"]),
        browser_session_hmac_key=KEY,
    )


def _sign_in(client: TestClient, subject: str) -> None:
    response = client.post("/auth/dummy/session", json={"subject": subject})
    assert response.status_code == 204


def test_event_http_requires_authentication_and_hides_unreadable_organizations(
    events_http_database: EventsHttpDatabase,
) -> None:
    path = f"/api/v1/organizations/{events_http_database.organization_id}/events"
    with TestClient(create_app(_settings()), base_url="https://testserver") as anonymous:
        assert anonymous.get(path).status_code == 401

    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "event-other")
        response = client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": {"code": "not_found"}}


def test_event_http_lists_events_with_signed_bounded_keyset_pages(
    events_http_database: EventsHttpDatabase,
) -> None:
    path = f"/api/v1/organizations/{events_http_database.organization_id}/events"
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "event-member")
        first = client.get(path, params={"page_size": 2})
        assert first.status_code == 200
        assert [event["id"] for event in first.json()["events"]] == [
            str(events_http_database.event_ids[2]),
            str(events_http_database.event_ids[1]),
        ]
        cursor = first.json()["next_cursor"]
        assert isinstance(cursor, str)
        second = client.get(path, params={"cursor": cursor, "page_size": 2})
        assert second.status_code == 200
        assert [event["id"] for event in second.json()["events"]] == [
            str(events_http_database.event_ids[0])
        ]
        assert second.json()["next_cursor"] is None
        forged = client.get(path, params={"cursor": cursor + "x"})
        assert forged.status_code == 400
        assert forged.json() == {"detail": "invalid cursor"}


def test_event_http_exposes_a_read_only_typed_contract(
    events_http_database: EventsHttpDatabase,
) -> None:
    path = f"/api/v1/organizations/{events_http_database.organization_id}/events"
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "event-admin")
        schema = client.app.openapi()
        contract_path = path.replace(str(events_http_database.organization_id), "{organization_id}")
        assert set(schema["paths"][contract_path]) == {"get"}
