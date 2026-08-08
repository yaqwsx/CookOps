from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cookops.application.oauth_interactions import OAuthInteractionDetails
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.http_oauth_interactions import (
    OAuthInteractionHttpServices,
    create_oauth_interaction_router,
)

KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"
DETAILS_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWU"
UID = "N9E_oxk7dD9t7rR10dj-3"


def _settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        human_auth_provider=HumanAuthProvider.DUMMY,
        oauth_interaction_details_api_credential_base64url=DETAILS_KEY,
        oauth_interaction_approval_api_credential_base64url=KEY,
        oauth_interaction_origin="https://testserver",
    )


def _client(approvals: AsyncMock) -> TestClient:
    app = FastAPI()
    app.state.oauth_interactions = OAuthInteractionHttpServices(approvals)
    app.include_router(create_oauth_interaction_router(_settings()))
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("cookops_session", "opaque")
    return client


def test_consent_page_displays_only_private_validated_details() -> None:
    approvals = AsyncMock()
    approvals.details.return_value = OAuthInteractionDetails(
        interaction_uid=UID,
        client_name="Trusted agent",
        resource="https://cookops.example/mcp",
        scopes=("cookops:mcp",),
    )

    response = _client(approvals).get(f"/auth/mcp-interactions/{UID}")

    assert response.status_code == 200
    assert "Trusted agent" in response.text
    assert "https://cookops.example/mcp" in response.text
    assert "cookops:mcp" in response.text
    assert f"https://testserver/oauth/interaction/{UID}/complete" in response.text
    assert response.headers["cache-control"] == "no-store"
    approvals.details.assert_awaited_once_with(browser_session_secret="opaque", interaction_uid=UID)


def test_consent_post_requires_origin_session_and_records_decision() -> None:
    approvals = AsyncMock()
    approvals.submit.return_value = True
    client = _client(approvals)

    assert (
        client.post(f"/auth/mcp-interactions/{UID}", json={"decision": "approve"}).status_code
        == 403
    )
    assert (
        client.post(
            f"/auth/mcp-interactions/{UID}",
            json={"decision": "approve"},
            headers={"origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    response = client.post(
        f"/auth/mcp-interactions/{UID}",
        json={"decision": "deny"},
        headers={"origin": "https://testserver"},
    )

    assert response.status_code == 204
    approvals.submit.assert_awaited_once_with(
        browser_session_secret="opaque", interaction_uid=UID, decision="deny"
    )
