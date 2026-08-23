"""Focused browser transport checks for the guarded recipe-copy command."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from cookops.application.organizations import ApplicationServiceError
from cookops.application.recipe_copy import CopyRecipeToOrganizationResult
from cookops.config import Settings
from cookops.http_recipe_copy import RecipeCopyHttpServices
from cookops.main import create_app


def _app(monkeypatch: Any, handler: Any, *, user_id: UUID | None = None) -> Any:
    actor = user_id or uuid4()
    settings = Settings(
        browser_session_cookie_name="cookops_session", browser_origin="https://testserver"
    )
    app = create_app(settings, readiness_probe=lambda: _ready())

    async def authenticate(secret: str) -> Any:
        return SimpleNamespace(user_id=actor) if secret == "session" else None

    app.state.recipe_copy = RecipeCopyHttpServices(
        browser_sessions=cast(Any, SimpleNamespace(authenticate=authenticate)),
        session_factory=cast(Any, object()),
    )
    monkeypatch.setattr("cookops.http_recipe_copy.copy_recipe_to_organization", handler)
    return app


async def _ready() -> bool:
    return True


def _payload(*, mutation_id: UUID | None = None) -> dict[str, object]:
    return {
        "source_organization_id": str(uuid4()),
        "source_recipe_id": str(uuid4()),
        "source_current_recipe_version_id": str(uuid4()),
        "destination_recipe_id": str(uuid4()),
        "destination_recipe_version_id": str(uuid4()),
        "ingredient_version_mappings": {str(uuid4()): str(uuid4())},
        "recipe_tag_mappings": {str(uuid4()): str(uuid4())},
        "scaling_unit_mappings": {str(uuid4()): str(uuid4())},
        "preferred_display_unit_mappings": {str(uuid4()): str(uuid4())},
        "client_installation_id": str(uuid4()),
        "mutation_id": str(mutation_id or uuid4()),
        "client_wall_time": datetime.now(UTC).isoformat(),
    }


def _result(command: Any, replayed: bool) -> CopyRecipeToOrganizationResult:
    return CopyRecipeToOrganizationResult(
        command.mutation_id,
        command.source_organization_id,
        command.destination_organization_id,
        command.source_recipe_id,
        command.destination_recipe_id,
        command.source_current_recipe_version_id,
        command.destination_recipe_version_id,
        7,
        14,
        replayed,
    )


def test_recipe_copy_http_accepts_and_replays(monkeypatch: Any) -> None:
    calls = []

    async def handler(_: Any, __: Any, command: Any) -> CopyRecipeToOrganizationResult:
        calls.append(command)
        return _result(command, len(calls) > 1)

    payload = _payload()
    app = _app(monkeypatch, handler)
    route = f"/api/v1/organizations/{uuid4()}/recipe-copy"
    request_options = {
        "cookies": {"cookops_session": "session"},
        "headers": {"origin": "https://testserver"},
    }
    with TestClient(app) as client:
        first = client.post(route, json=payload, **request_options)
        second = client.post(route, json=payload, **request_options)
    assert first.status_code == second.status_code == 200
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert len(calls) == 2
    assert calls[0].ingredient_version_mappings


@pytest.mark.parametrize("origin", [None, "https://attacker.example"])
def test_recipe_copy_http_origin_is_fail_closed(monkeypatch: Any, origin: str | None) -> None:
    calls = 0

    async def handler(_: Any, __: Any, command: Any) -> CopyRecipeToOrganizationResult:
        nonlocal calls
        calls += 1
        return _result(command, False)

    payload = _payload()
    app = _app(monkeypatch, handler)
    headers = {} if origin is None else {"origin": origin}
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/organizations/{uuid4()}/recipe-copy",
            json=payload,
            cookies={"cookops_session": "session"},
            headers=headers,
        )
    assert response.status_code == 403
    assert calls == 0


def test_recipe_copy_http_auth_and_payload_validation(monkeypatch: Any) -> None:
    calls = 0

    async def handler(_: Any, __: Any, command: Any) -> CopyRecipeToOrganizationResult:
        nonlocal calls
        calls += 1
        return _result(command, False)

    app = _app(monkeypatch, handler)
    route = f"/api/v1/organizations/{uuid4()}/recipe-copy"
    payload = _payload()
    with TestClient(app) as client:
        unauthenticated = client.post(route, json=payload, headers={"origin": "https://testserver"})
        options = {
            "cookies": {"cookops_session": "session"},
            "headers": {"origin": "https://testserver"},
        }
        extra = client.post(route, json={**payload, "extra": True}, **options)
        malformed = client.post(route, json={**payload, "mutation_id": 42}, **options)
        zero = client.post(route, json={**payload, "mutation_id": str(UUID(int=0))}, **options)
    assert unauthenticated.status_code == 401
    assert extra.status_code == malformed.status_code == zero.status_code == 422
    assert calls == 0


@pytest.mark.parametrize(
    "code,status",
    [("forbidden", 404), ("stale_precondition", 409), ("validation_failed", 422)],
)
def test_recipe_copy_http_maps_application_errors(monkeypatch: Any, code: str, status: int) -> None:
    async def handler(_: Any, __: Any, command: Any) -> CopyRecipeToOrganizationResult:
        raise ApplicationServiceError(cast(Any, code), retry_same_identity=False)

    app = _app(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/organizations/{uuid4()}/recipe-copy",
            json=_payload(),
            cookies={"cookops_session": "session"},
            headers={"origin": "https://testserver"},
        )
    assert response.status_code == status
    if code == "forbidden":
        assert response.json() == {"detail": {"code": "not_found"}}
