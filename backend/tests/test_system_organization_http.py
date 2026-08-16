from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from test_dummy_auth_http import (  # noqa: F401,F811
    DummyAuthDatabase,
    dummy_auth_database,
    settings,
)

from cookops.main import create_app
from cookops.persistence.models import ClientInstallation, ExternalIdentity, SystemRoleAssignment


def _body() -> dict[str, object]:
    return {
        "mutation_id": str(uuid4()),
        "organization_id": str(uuid4()),
        "client_installation_id": str(uuid4()),
        "client_wall_time": datetime.now(UTC).isoformat(),
        "name": "Second kitchen",
        "description": "A test organization",
        "default_currency": "CZK",
    }


def test_system_admin_can_create_and_non_admin_cannot(
    dummy_auth_database: DummyAuthDatabase,  # noqa: F811
) -> None:
    now = datetime.now(UTC)
    with dummy_auth_database.sync_engine.begin() as connection:
        connection.execute(
            insert(ExternalIdentity).values(
                user_id=dummy_auth_database.actor_id,
                provider="dummy",
                provider_subject="dummy-system-admin",
                verified_email="actor@example.test",
                normalized_verified_email="actor@example.test",
            )
        )
        connection.execute(
            insert(SystemRoleAssignment).values(
                id=uuid4(),
                user_id=dummy_auth_database.actor_id,
                invited_email="actor@example.test",
                role="system_admin",
                granted_by_user_id=dummy_auth_database.actor_id,
                claimed_at=now,
            )
        )

    with TestClient(create_app(settings()), base_url="https://testserver") as client:
        assert client.get("/api/v1/system/organizations/access").status_code == 401
        assert client.post("/api/v1/system/organizations", json=_body()).status_code == 401

        signed_in = client.post(
            "/auth/dummy/session", json={"subject": "dummy-system-admin"}
        )
        assert signed_in.status_code == 204
        body = _body()
        created = client.post("/api/v1/system/organizations", json=body)
        assert created.status_code == 201
        assert created.json()["name"] == "Second kitchen"
        assert any(
            value["id"] == body["organization_id"]
            for value in client.get("/api/v1/organizations").json()["organizations"]
        )

        with dummy_auth_database.sync_engine.begin() as connection:
            connection.execute(
                update(SystemRoleAssignment)
                .where(SystemRoleAssignment.user_id == dummy_auth_database.actor_id)
                .values(invited_email="previous@example.test")
            )
        assert client.get("/api/v1/system/organizations/access").status_code == 204
        mismatched_body = _body()
        assert client.post("/api/v1/system/organizations", json=mismatched_body).status_code == 201

        malformed_body = {**_body(), "default_currency": "wat"}
        malformed = client.post("/api/v1/system/organizations", json=malformed_body)
        assert malformed.status_code == 422
        with dummy_auth_database.sync_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(ClientInstallation.id).where(
                        ClientInstallation.id == malformed_body["client_installation_id"]
                    )
                )
                is None
            )

        client.post("/auth/session/logout")
        assert (
            client.post("/auth/dummy/session", json={"subject": "dummy-alice"}).status_code
            == 204
        )
        assert client.get("/api/v1/system/organizations/access").status_code == 403
        denied_body = _body()
        assert client.post("/api/v1/system/organizations", json=denied_body).status_code == 403
        with dummy_auth_database.sync_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(ClientInstallation.id).where(
                        ClientInstallation.id == denied_body["client_installation_id"]
                    )
                )
                is None
            )
