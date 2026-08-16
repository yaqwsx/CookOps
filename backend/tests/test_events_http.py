import base64
import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import PostgresDsn
from sqlalchemy import Engine, create_engine, insert, update

from alembic import command as alembic_command
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.main import create_app
from cookops.persistence.models import (
    Event,
    EventArchiveSnapshot,
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
    archive_snapshot_id: UUID
    archive_created_by_user_id: UUID


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
    archive_snapshot_id = uuid4()
    archive_payload = {"schema_version": 1, "event": {"id": str(event_ids[0])}}
    archive_hash = hashlib.sha256(
        json.dumps(archive_payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
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
        connection.execute(
            insert(EventArchiveSnapshot).values(
                id=archive_snapshot_id,
                event_id=event_ids[0],
                archive_schema_version=1,
                payload=archive_payload,
                content_hash=archive_hash,
                attachment_manifest=[],
                created_by_user_id=admin_id,
            )
        )
        connection.execute(
            update(Event)
            .where(Event.id == event_ids[0])
            .values(
                lifecycle="archived",
                current_archive_snapshot_id=archive_snapshot_id,
                archived_at=now + timedelta(seconds=10),
                archived_by_user_id=admin_id,
            )
        )
    database = EventsHttpDatabase(
        configuration=configuration,
        engine=engine,
        organization_id=organization_id,
        other_organization_id=other_organization_id,
        event_ids=event_ids,
        archive_snapshot_id=archive_snapshot_id,
        archive_created_by_user_id=admin_id,
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
        schema = cast(FastAPI, client.app).openapi()
        contract_path = path.replace(str(events_http_database.organization_id), "{organization_id}")
        assert set(schema["paths"][contract_path]) == {"get"}


def test_archived_event_http_reads_only_the_current_verified_snapshot(
    events_http_database: EventsHttpDatabase,
) -> None:
    database = events_http_database
    path = (
        f"/api/v1/organizations/{database.organization_id}/events/{database.event_ids[0]}"
        f"/archive/{database.archive_snapshot_id}"
    )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "event-member")
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "archive_schema_version": 1,
            "content_hash": hashlib.sha256(
                json.dumps(
                    {"schema_version": 1, "event": {"id": str(database.event_ids[0])}},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "payload": {"schema_version": 1, "event": {"id": str(database.event_ids[0])}},
        }

        for forged_path in (
            path.replace(str(database.archive_snapshot_id), str(uuid4())),
            path.replace(str(database.event_ids[0]), str(database.event_ids[1])),
            path.replace(str(database.organization_id), str(database.other_organization_id)),
        ):
            assert client.get(forged_path).status_code == 404


def test_archived_event_http_fails_closed_for_tampered_or_unsupported_snapshot(
    events_http_database: EventsHttpDatabase,
) -> None:
    database = events_http_database
    payload = {"schema_version": 1, "event": {"id": str(database.event_ids[0])}}
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
    tampered_snapshot_id, unsupported_snapshot_id = uuid4(), uuid4()
    with events_http_database.engine.begin() as connection:
        connection.execute(
            insert(EventArchiveSnapshot),
            [
                {
                    "id": tampered_snapshot_id,
                    "event_id": database.event_ids[0],
                    "archive_schema_version": 1,
                    "payload": payload,
                    "content_hash": b"0" * 32,
                    "attachment_manifest": [],
                    "created_by_user_id": database.archive_created_by_user_id,
                },
                {
                    "id": unsupported_snapshot_id,
                    "event_id": database.event_ids[0],
                    "archive_schema_version": 99,
                    "payload": payload,
                    "content_hash": payload_hash,
                    "attachment_manifest": [],
                    "created_by_user_id": database.archive_created_by_user_id,
                },
            ],
        )
        connection.execute(
            update(Event)
            .where(Event.id == database.event_ids[0])
            .values(current_archive_snapshot_id=tampered_snapshot_id)
        )
    tampered_path = (
        f"/api/v1/organizations/{database.organization_id}/events/{database.event_ids[0]}"
        f"/archive/{tampered_snapshot_id}"
    )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "event-member")
        assert client.get(tampered_path).status_code == 404
    with events_http_database.engine.begin() as connection:
        connection.execute(
            update(Event)
            .where(Event.id == database.event_ids[0])
            .values(current_archive_snapshot_id=unsupported_snapshot_id)
        )
    unsupported_path = (
        f"/api/v1/organizations/{database.organization_id}/events/{database.event_ids[0]}"
        f"/archive/{unsupported_snapshot_id}"
    )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "event-member")
        assert client.get(unsupported_path).status_code == 404
        stale_path = (
            f"/api/v1/organizations/{database.organization_id}/events/{database.event_ids[0]}"
            f"/archive/{tampered_snapshot_id}"
        )
        assert client.get(stale_path).status_code == 404
