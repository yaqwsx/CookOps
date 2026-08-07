import base64
import hashlib
import hmac
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import PostgresDsn
from sqlalchemy import Engine, create_engine, insert, text

from alembic import command as alembic_command
from cookops.application.synchronization import SyncCursor, SyncCursorCodec
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.main import create_app
from cookops.persistence.models import (
    ClientInstallation,
    ExternalIdentity,
    Mutation,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()


@dataclass
class SyncDatabase:
    configuration: Config
    engine: Engine
    organization_id: UUID
    other_organization_id: UUID


@pytest.fixture
def sync_database() -> Iterator[SyncDatabase]:
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    alembic_command.downgrade(configuration, "base")
    alembic_command.upgrade(configuration, "head")
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    actor_id, other_actor_id, creator_id = uuid4(), uuid4(), uuid4()
    organization_id, other_organization_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(User),
            [
                {
                    "id": actor_id,
                    "display_name": "Sync member",
                    "verified_email": "member@example.test",
                    "normalized_email": "member@example.test",
                },
                {
                    "id": other_actor_id,
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
                    "user_id": actor_id,
                    "provider": "dummy",
                    "provider_subject": "dummy-member",
                    "verified_email": "member@example.test",
                    "normalized_verified_email": "member@example.test",
                },
                {
                    "user_id": other_actor_id,
                    "provider": "dummy",
                    "provider_subject": "dummy-other",
                    "verified_email": "other@example.test",
                    "normalized_verified_email": "other@example.test",
                },
            ],
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": organization_id,
                    "name": "Sync organization",
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
                    "user_id": actor_id,
                    "invited_email": "member@example.test",
                    "role": "member",
                    "state": "active",
                    "invited_by_user_id": creator_id,
                    "claimed_at": now,
                },
                {
                    "organization_id": other_organization_id,
                    "user_id": other_actor_id,
                    "invited_email": "other@example.test",
                    "role": "member",
                    "state": "active",
                    "invited_by_user_id": creator_id,
                    "claimed_at": now,
                },
            ],
        )
    database = SyncDatabase(
        configuration=configuration,
        engine=engine,
        organization_id=organization_id,
        other_organization_id=other_organization_id,
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


def _cursor(organization_id: UUID, after_sequence: int) -> str:
    return SyncCursorCodec(encoded_hmac_key=KEY).encode(
        SyncCursor(organization_id=organization_id, after_sequence=after_sequence)
    )


def _noncanonical_base64url_component(component: str) -> str:
    """Change unused Base64URL tail bits without changing decoded bytes."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    assert len(component) % 4 in (2, 3)
    return component[:-1] + alphabet[alphabet.index(component[-1]) ^ 1]


def _cursor_with_payload_component(payload: str) -> str:
    signature = hmac.new(
        b"0123456789abcdef0123456789abcdef",
        b"cookops.sync.cursor.v1:" + payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"v1.{payload}.{encoded_signature}"


def _publish_changes(
    database: SyncDatabase,
    *,
    count: int,
    published_at: datetime | None = None,
) -> tuple[int, int]:
    actor_id, installation_id, mutation_id = uuid4(), uuid4(), uuid4()
    with database.engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor_id,
                display_name="Change actor",
                verified_email=f"{actor_id}@example.test",
                normalized_email=f"{actor_id}@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation_id,
                user_id=actor_id,
                installation_kind="browser",
            )
        )
        first_sequence, last_sequence = connection.execute(
            text(
                "SELECT first_change_sequence, last_change_sequence "
                "FROM reserve_organization_change_transaction("
                ":organization_id, :mutation_id, :count)"
            ),
            {
                "organization_id": database.organization_id,
                "mutation_id": mutation_id,
                "count": count,
            },
        ).one()
        connection.execute(
            insert(Mutation).values(
                id=mutation_id,
                organization_id=database.organization_id,
                is_system_administration_scope=False,
                actor_user_id=actor_id,
                actor_role="member",
                client_installation_id=installation_id,
                client_wall_time=datetime.now(UTC),
                command_schema_version=1,
                command_kind="sync.test",
                target_identities=[{"entity_kind": "event", "entity_id": str(uuid4())}],
                request_hash=b"s" * 32,
                outcome="accepted",
                outcome_payload={"outcome": "accepted"},
                first_change_sequence=first_sequence,
                last_change_sequence=last_sequence,
            )
        )
        connection.execute(
            insert(OrganizationChange),
            [
                {
                    "organization_id": database.organization_id,
                    "sequence": sequence,
                    "mutation_id": mutation_id,
                    "entity_id": uuid4(),
                    "entity_kind": "event",
                    "operation": "upsert",
                    "payload": {
                        "record_schema_version": 1,
                        "record": {"name": f"Event {sequence}"},
                    },
                    **({"published_at": published_at} if published_at is not None else {}),
                }
                for sequence in range(first_sequence, last_sequence + 1)
            ],
        )
    return int(first_sequence), int(last_sequence)


def _sign_in(client: TestClient, subject: str) -> None:
    response = client.post("/auth/dummy/session", json={"subject": subject})
    assert response.status_code == 204


def test_pull_requires_current_cookie_and_current_organization_membership(
    sync_database: SyncDatabase,
) -> None:
    with TestClient(create_app(_settings()), base_url="https://testserver") as anonymous:
        assert (
            anonymous.post(
                "/api/v1/sync/pull", json={"organization_id": str(sync_database.organization_id)}
            ).status_code
            == 401
        )

    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-other")
        response = client.post(
            "/api/v1/sync/pull", json={"organization_id": str(sync_database.organization_id)}
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "organization access denied"}


def test_pull_pages_complete_transaction_groups_and_uses_signed_organization_cursor(
    sync_database: SyncDatabase,
) -> None:
    first_start, first_end = _publish_changes(sync_database, count=2)
    second_start, second_end = _publish_changes(sync_database, count=1)
    assert (first_start, first_end, second_start, second_end) == (1, 2, 3, 3)

    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        initial = client.post(
            "/api/v1/sync/pull", json={"organization_id": str(sync_database.organization_id)}
        )
        assert initial.status_code == 200
        assert initial.json()["status"] == "bootstrap_required"

        first_page = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor(sync_database.organization_id, 0),
                "transaction_group_limit": 1,
            },
        )
        assert first_page.status_code == 200
        first_payload = first_page.json()
        assert first_payload["status"] == "ok"
        assert [
            (group["first_sequence"], group["last_sequence"])
            for group in first_payload["transaction_groups"]
        ] == [(1, 2)]
        assert [
            record["sequence"] for record in first_payload["transaction_groups"][0]["records"]
        ] == [
            1,
            2,
        ]
        assert {
            record["organization_id"]
            for record in first_payload["transaction_groups"][0]["records"]
        } == {str(sync_database.organization_id)}

        second_page = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": first_payload["next_cursor"],
            },
        )
        assert second_page.status_code == 200
        assert [
            (group["first_sequence"], group["last_sequence"])
            for group in second_page.json()["transaction_groups"]
        ] == [(3, 3)]

        forged = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor(sync_database.organization_id, 1),
            },
        )
        assert forged.status_code == 400
        assert forged.json() == {"detail": "invalid cursor"}

        forged_signature = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor(sync_database.organization_id, 0) + "x",
            },
        )
        assert forged_signature.status_code == 400
        assert forged_signature.json() == {"detail": "invalid cursor"}

        version, payload, signature = _cursor(sync_database.organization_id, 0).split(".")
        noncanonical_signature = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": f"{version}.{payload}.{_noncanonical_base64url_component(signature)}",
            },
        )
        assert noncanonical_signature.status_code == 400
        assert noncanonical_signature.json() == {"detail": "invalid cursor"}

        noncanonical_payload = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor_with_payload_component(
                    _noncanonical_base64url_component(payload)
                ),
            },
        )
        assert noncanonical_payload.status_code == 400
        assert noncanonical_payload.json() == {"detail": "invalid cursor"}

        invalid_utf8_payload = "_w"
        invalid_utf8 = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor_with_payload_component(invalid_utf8_payload),
            },
        )
        assert invalid_utf8.status_code == 400
        assert invalid_utf8.json() == {"detail": "invalid cursor"}

        malformed = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": "v1.***.not-base64",
            },
        )
        assert malformed.status_code == 400
        assert malformed.json() == {"detail": "invalid cursor"}

        wrong_organization = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor(sync_database.other_organization_id, 0),
            },
        )
        assert wrong_organization.status_code == 400


def test_pull_keeps_old_published_records_until_a_commit_time_safe_cleanup_exists(
    sync_database: SyncDatabase,
) -> None:
    _publish_changes(
        sync_database,
        count=1,
        published_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor(sync_database.organization_id, 0),
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert [group["first_sequence"] for group in response.json()["transaction_groups"]] == [1]


def test_current_cursor_returns_empty_ok_even_when_the_organization_has_no_changes(
    sync_database: SyncDatabase,
) -> None:
    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor(sync_database.organization_id, 0),
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["transaction_groups"] == []
    assert response.json()["next_cursor"] == _cursor(sync_database.organization_id, 0)


@pytest.mark.parametrize("defect", ["missing_record", "wrong_mutation"])
def test_pull_requires_bootstrap_for_a_controlled_physical_history_defect(
    sync_database: SyncDatabase, defect: str
) -> None:
    if defect == "missing_record":
        _, second_sequence = _publish_changes(sync_database, count=3)
        second_sequence -= 1
    else:
        _publish_changes(sync_database, count=1)
        _, second_sequence = _publish_changes(sync_database, count=1)
    with sync_database.engine.begin() as connection:
        mutation_ids = (
            connection.execute(
                text(
                    "SELECT mutation_id FROM organization_change_transactions "
                    "WHERE organization_id = :organization_id "
                    "ORDER BY first_change_sequence"
                ),
                {"organization_id": sync_database.organization_id},
            )
            .scalars()
            .all()
        )
        connection.execute(text("ALTER TABLE organization_changes DISABLE TRIGGER ALL"))
        try:
            if defect == "missing_record":
                connection.execute(
                    text(
                        "DELETE FROM organization_changes "
                        "WHERE organization_id = :organization_id AND sequence = :sequence"
                    ),
                    {
                        "organization_id": sync_database.organization_id,
                        "sequence": second_sequence,
                    },
                )
            else:
                connection.execute(
                    text(
                        "UPDATE organization_changes SET mutation_id = :mutation_id "
                        "WHERE organization_id = :organization_id AND sequence = :sequence"
                    ),
                    {
                        "mutation_id": mutation_ids[0],
                        "organization_id": sync_database.organization_id,
                        "sequence": second_sequence,
                    },
                )
        finally:
            connection.execute(text("ALTER TABLE organization_changes ENABLE TRIGGER ALL"))

    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor(sync_database.organization_id, 0),
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "bootstrap_required"
    assert response.json()["next_cursor"] is None


def test_pull_requires_bootstrap_when_the_change_head_is_missing(
    sync_database: SyncDatabase,
) -> None:
    _publish_changes(sync_database, count=1)
    with sync_database.engine.begin() as connection:
        connection.execute(text("ALTER TABLE organization_change_heads DISABLE TRIGGER ALL"))
        try:
            connection.execute(
                text(
                    "DELETE FROM organization_change_heads WHERE organization_id = :organization_id"
                ),
                {"organization_id": sync_database.organization_id},
            )
        finally:
            connection.execute(text("ALTER TABLE organization_change_heads ENABLE TRIGGER ALL"))

    with TestClient(create_app(_settings()), base_url="https://testserver") as client:
        _sign_in(client, "dummy-member")
        response = client.post(
            "/api/v1/sync/pull",
            json={
                "organization_id": str(sync_database.organization_id),
                "cursor": _cursor(sync_database.organization_id, 0),
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "bootstrap_required"
