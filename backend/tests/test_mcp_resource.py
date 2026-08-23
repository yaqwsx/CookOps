import asyncio
import base64
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime
from decimal import Decimal
from time import time
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken, TokenVerifier

from cookops.application.events import EventSummary
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.mcp_resource import (
    McpIntrospectionVerifier,
    create_mcp_protected_resource,
    create_mcp_protected_resource_from_settings,
)


def verifier(payload: dict[str, object]) -> McpIntrospectionVerifier:
    return McpIntrospectionVerifier(
        issuer="https://cookops.example/oauth",
        resource="https://cookops.example/mcp",
        introspection_url="http://oauth-server:3000/oauth/introspect",
        resource_server_secret="secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
    )


def test_private_introspection_accepts_only_complete_mcp_access_token() -> None:
    subject = str(uuid4())
    token = asyncio.run(
        verifier(
            {
                "active": True,
                "iss": "https://cookops.example/oauth",
                "aud": ["https://cookops.example/mcp"],
                "scope": "cookops:mcp",
                "client_id": "client",
                "sub": subject,
                "exp": int(time()) + 60,
            }
        ).verify_token("opaque")
    )
    assert token is not None and token.subject == subject


def test_private_introspection_posts_form_token_with_basic_credentials() -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "active": False,
            },
        )

    verifier_instance = McpIntrospectionVerifier(
        issuer="https://cookops.example/oauth",
        resource="https://cookops.example/mcp",
        introspection_url="http://oauth-server:3000/oauth/introspect",
        resource_server_secret="secret",
        transport=httpx.MockTransport(transport),
    )
    assert asyncio.run(verifier_instance.verify_token("opaque")) is None

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://oauth-server:3000/oauth/introspect"
    assert request.content == b"token=opaque"
    assert request.headers["authorization"] == "Basic " + base64.b64encode(
        b"cookops-resource-server:secret"
    ).decode()


def test_private_introspection_does_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://public.example/introspect"})

    verifier_instance = McpIntrospectionVerifier(
        issuer="https://cookops.example/oauth",
        resource="https://cookops.example/mcp",
        introspection_url="http://oauth-server:3000/oauth/introspect",
        resource_server_secret="secret",
        transport=httpx.MockTransport(transport),
    )
    assert asyncio.run(verifier_instance.verify_token("opaque")) is None
    assert len(requests) == 1
    assert str(requests[0].url) == "http://oauth-server:3000/oauth/introspect"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503), httpx.Response(200, content=b"not-json")],
    ids=["non-200", "malformed-body"],
)
def test_private_introspection_fails_closed_for_invalid_transport_response(
    response: httpx.Response,
) -> None:
    verifier_instance = McpIntrospectionVerifier(
        issuer="https://cookops.example/oauth",
        resource="https://cookops.example/mcp",
        introspection_url="http://oauth-server:3000/oauth/introspect",
        resource_server_secret="secret",
        transport=httpx.MockTransport(lambda _request: response),
    )
    assert asyncio.run(verifier_instance.verify_token("opaque")) is None


def test_private_introspection_rejects_wrong_resource_and_never_mounts_routes() -> None:
    token = asyncio.run(
        verifier(
            {
                "active": True,
                "iss": "https://cookops.example/oauth",
                "aud": "https://other.example/mcp",
                "scope": "cookops:mcp",
                "client_id": "client",
                "sub": str(uuid4()),
                "exp": int(time()) + 60,
            }
        ).verify_token("opaque")
    )
    assert token is None
    assert create_mcp_protected_resource(
        verifier({"active": False}),
        issuer="https://cookops.example/oauth",
        resource="https://cookops.example/mcp",
        session_factory=cast(Any, object()),
    ).routes


def test_settings_factory_is_disabled_only_when_all_mcp_values_are_absent() -> None:
    disabled = Settings(environment=Environment.TEST, human_auth_provider=HumanAuthProvider.DUMMY)
    assert create_mcp_protected_resource_from_settings(disabled, cast(Any, object())) is None
    partial = disabled.model_construct(oauth_issuer="https://cookops.example/oauth")
    try:
        create_mcp_protected_resource_from_settings(partial, cast(Any, object()))
    except RuntimeError as error:
        assert "incomplete" in str(error)
    else:
        raise AssertionError("partial MCP configuration must fail closed")


class StaticTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        return AccessToken(
            token=token,
            client_id="test-client",
            scopes=["cookops:mcp"],
            expires_at=int(time()) + 60,
            resource="https://cookops.example/mcp",
            subject=str(ACTOR_ID),
        )


ACTOR_ID = uuid4()
ORGANIZATION_ID = uuid4()
EVENT_ID = uuid4()


def test_mcp_client_lists_and_calls_event_summary_with_verified_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = EventSummary(
        id=EVENT_ID,
        organization_id=ORGANIZATION_ID,
        name="Harvest",
        start_date=date(2026, 8, 20),
        end_date=date(2026, 8, 21),
        base_expected_attendance=12,
        budget_amount=Decimal("10.50"),
        currency="CZK",
        lifecycle="active",
        archived_at=None,
        current_archive_snapshot_id=None,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )

    async def authorized_summary(*args: Any, **kwargs: Any) -> EventSummary:
        assert kwargs["actor_user_id"] == ACTOR_ID
        assert kwargs["organization_id"] == ORGANIZATION_ID
        assert kwargs["event_id"] == EVENT_ID
        return expected

    monkeypatch.setattr("cookops.mcp_resource.get_event_summary", authorized_summary)
    app = create_mcp_protected_resource(
        StaticTokenVerifier(),
        issuer="https://cookops.example/oauth",
        resource="https://cookops.example/mcp",
        session_factory=cast(Any, object()),
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=transport,
                    base_url="https://cookops.example",
                    headers={"Authorization": "Bearer verified-token"},
                )
            )
            streams = await stack.enter_async_context(
                streamable_http_client("https://cookops.example/mcp", http_client=http)
            )
            client = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await client.initialize()
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == ["get_event_summary"]
            result = await client.call_tool(
                "get_event_summary",
                {
                    "organization_id": str(ORGANIZATION_ID),
                    "event_id": str(EVENT_ID),
                },
            )
            assert result.isError is False
            assert result.structuredContent == {
                "id": str(EVENT_ID),
                "organization_id": str(ORGANIZATION_ID),
                "name": "Harvest",
                "start_date": "2026-08-20",
                "end_date": "2026-08-21",
                "attendance": 12,
                "budget": "10.50",
                "currency": "CZK",
                "lifecycle": "active",
                "archived_at": None,
                "archive_snapshot_id": None,
            }
            malformed = await client.call_tool(
                "get_event_summary",
                {"organization_id": "bad", "event_id": str(EVENT_ID)},
            )
            assert malformed.isError is True
            assert "invalid identifier" in str(malformed.content)

    asyncio.run(exercise())
