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
from cookops.persistence.models import (
    ClientInstallation,
    ExternalIdentity,
    Mutation,
    Organization,
    SystemRoleAssignment,
)


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


def _lifecycle_body(operation: str, *, mutation_id: str | None = None) -> dict[str, object]:
    return {
        "operation": operation,
        "mutation_id": mutation_id or str(uuid4()),
        "client_installation_id": str(uuid4()),
        "client_wall_time": datetime.now(UTC).isoformat(),
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


def test_system_admin_can_list_retire_and_restore_organizations(
    dummy_auth_database: DummyAuthDatabase,  # noqa: F811
) -> None:
    retired_id = uuid4()
    with dummy_auth_database.sync_engine.begin() as connection:
        connection.execute(
            insert(ExternalIdentity).values(
                user_id=dummy_auth_database.actor_id,
                provider="dummy",
                provider_subject="dummy-system-admin-lifecycle",
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
                claimed_at=datetime.now(UTC),
            )
        )
        connection.execute(
            insert(Organization).values(
                id=retired_id,
                name="Retired kitchen",
                default_currency="CZK",
                created_by_user_id=dummy_auth_database.actor_id,
                retired_at=datetime(2026, 8, 1, tzinfo=UTC),
                retired_by_user_id=dummy_auth_database.actor_id,
            )
        )

    with TestClient(create_app(settings()), base_url="https://testserver") as client:
        assert client.get("/api/v1/system/organizations").status_code == 401
        assert client.post(
            f"/api/v1/system/organizations/{retired_id}/lifecycle",
            json=_lifecycle_body("retire"),
        ).status_code == 401
        assert client.post(
            "/auth/dummy/session", json={"subject": "dummy-system-admin-lifecycle"}
        ).status_code == 204

        listed = client.get("/api/v1/system/organizations")
        assert listed.status_code == 200
        assert {item["id"] for item in listed.json()} == {
            str(dummy_auth_database.organization_id),
            str(retired_id),
        }
        listed_retired = next(item for item in listed.json() if item["id"] == str(retired_id))
        assert listed_retired["retired_at"].startswith("2026-08-01T00:00:00")
        assert listed_retired["retired_by_user_id"] == str(dummy_auth_database.actor_id)
        retire_body = _lifecycle_body("retire")
        retired = client.post(
            f"/api/v1/system/organizations/{dummy_auth_database.organization_id}/lifecycle",
            json=retire_body,
        )
        assert retired.status_code == 200
        assert retired.json()["retired_by_user_id"] == str(dummy_auth_database.actor_id)
        assert retired.json()["retired_at"] is not None
        with dummy_auth_database.sync_engine.connect() as connection:
            row = connection.execute(
                select(Organization.retired_at, Organization.retired_by_user_id).where(
                    Organization.id == dummy_auth_database.organization_id
                )
            ).one()
        assert row.retired_at is not None
        assert row.retired_by_user_id == dummy_auth_database.actor_id
        assert str(dummy_auth_database.organization_id) not in {
            item["id"] for item in client.get("/api/v1/organizations").json()["organizations"]
        }
        replay = client.post(
            f"/api/v1/system/organizations/{dummy_auth_database.organization_id}/lifecycle",
            json=retire_body,
        )
        assert replay.status_code == 200
        assert replay.json() == retired.json()
        with dummy_auth_database.sync_engine.connect() as connection:
            mutation = connection.execute(
                select(
                    Mutation.outcome,
                    Mutation.organization_id,
                    Mutation.is_system_administration_scope,
                )
                .where(Mutation.id == retire_body["mutation_id"])
            ).one()
        assert mutation.outcome == "accepted"
        assert mutation.organization_id is None
        assert mutation.is_system_administration_scope is True

        restored = client.post(
            f"/api/v1/system/organizations/{dummy_auth_database.organization_id}/lifecycle",
            json=_lifecycle_body("restore"),
        )
        assert restored.status_code == 200
        assert restored.json()["retired_at"] is None
        assert restored.json()["retired_by_user_id"] is None

        unknown = client.post(
            f"/api/v1/system/organizations/{uuid4()}/lifecycle",
            json=_lifecycle_body("retire"),
        )
        assert unknown.status_code == 422

        assert client.post("/auth/session/logout").status_code == 204
        assert client.get("/api/v1/system/organizations").status_code == 401
        assert client.post(
            f"/api/v1/system/organizations/{dummy_auth_database.organization_id}/lifecycle",
            json=_lifecycle_body("retire"),
        ).status_code == 401
        assert (
            client.post("/auth/dummy/session", json={"subject": "dummy-alice"}).status_code
            == 204
        )
        assert client.get("/api/v1/system/organizations").status_code == 403
        assert client.post(
            f"/api/v1/system/organizations/{dummy_auth_database.organization_id}/lifecycle",
            json=_lifecycle_body("retire"),
        ).status_code == 403
