"""WebSocket availability hints remain an authenticated pull trigger only."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.testclient import WebSocketTestSession
from test_sync_pull_http import (
    SyncDatabase,
    _publish_changes,
    _settings,
    _sign_in,
)
from test_sync_pull_http import (
    sync_database as _sync_database_fixture,
)

from cookops.main import create_app


@pytest.fixture
def sync_database() -> Iterator[SyncDatabase]:
    # Keep this module independently runnable while sharing the full PostgreSQL
    # fixture with the pull transport tests.
    setup = cast(Callable[[], Iterator[SyncDatabase]], vars(_sync_database_fixture)["__wrapped__"])
    yield from setup()


@contextmanager
def _hints(client: TestClient) -> Iterator[WebSocketTestSession]:
    """TestClient does not copy Secure HTTPS cookies onto a ws:// test connection."""

    name = _settings().browser_session_cookie_name
    secret = client.cookies.get(name)
    assert secret is not None
    with client.websocket_connect(
        "/api/v1/sync/hints", headers={"cookie": f"{name}={secret}"}
    ) as socket:
        yield socket


def test_hints_require_an_authenticated_browser_session(sync_database: SyncDatabase) -> None:
    with (
        TestClient(create_app(_settings()), base_url="https://testserver") as client,
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/api/v1/sync/hints"),
    ):
        pass
    assert closed.value.code == 4401


def test_hints_do_not_enumerate_other_organizations(sync_database: SyncDatabase) -> None:
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        with _hints(client) as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "organization_ids": [str(sync_database.other_organization_id)],
                }
            )
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
    assert closed.value.code == 4403


def test_hints_send_only_an_opaque_change_availability_notice(
    sync_database: SyncDatabase,
) -> None:
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        with _hints(client) as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "organization_ids": [str(sync_database.organization_id)],
                }
            )
            subscription = socket.receive_json()
            assert subscription["type"] == "change_available"
            assert subscription["reason"] == "subscription"
            _publish_changes(sync_database, count=1)
            hint = socket.receive_json()
    assert hint["type"] == "change_available"
    assert hint["organization_id"] == str(sync_database.organization_id)
    assert hint["reason"] == "domain_change"
    assert hint["cursor"].startswith("v1.")
    assert set(hint) == {"type", "organization_id", "cursor", "reason"}


def test_hints_recheck_membership_before_each_notice(sync_database: SyncDatabase) -> None:
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        with _hints(client) as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "organization_ids": [str(sync_database.organization_id)],
                }
            )
            socket.receive_json()
            with sync_database.engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM organization_memberships "
                        "WHERE organization_id = :organization_id"
                    ),
                    {"organization_id": sync_database.organization_id},
                )
            assert socket.receive_json() == {
                "type": "access_changed",
                "organization_id": str(sync_database.organization_id),
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
    assert closed.value.code == 4403


def test_hints_close_when_the_live_browser_session_is_revoked(sync_database: SyncDatabase) -> None:
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        with _hints(client) as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "organization_ids": [str(sync_database.organization_id)],
                }
            )
            socket.receive_json()
            with sync_database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE browser_sessions "
                        "SET revoked_at = :now, revoked_by_user_id = user_id"
                    ),
                    {"now": datetime.now(UTC)},
                )
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
    assert closed.value.code == 4401
