import asyncio
import base64
import time
from urllib.parse import parse_qs

import httpx
import pytest

from cookops_mcp_oauth_spike import (
    IntrospectionTokenVerifier,
    ResourceServerSettings,
    create_app,
    resource_server,
)

RESOURCE = "https://cookops.example/mcp"
ISSUER = "https://cookops.example/oauth"
INTROSPECTION_URL = "http://oauth-server:3000/oauth/introspect"
SECRET = "resource-server-secret-at-least-32-bytes"
SUBJECT = "018f5e3b-a1ad-7b48-a7f8-2a36116b8293"
MISSING = object()


def settings(**changes: str) -> ResourceServerSettings:
    values = {
        "resource_url": RESOURCE,
        "authorization_server_url": ISSUER,
        "introspection_url": INTROSPECTION_URL,
        "introspection_client_secret": SECRET,
    }
    values.update(changes)
    return ResourceServerSettings(**values)


def valid_introspection_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "active": True,
        "aud": RESOURCE,
        "client_id": "agent-client",
        "exp": int(time.time()) + 900,
        "iss": ISSUER,
        "scope": "cookops:mcp",
        "sub": SUBJECT,
    }
    payload.update(changes)
    return payload


def test_publishes_rfc_9728_metadata_at_resource_path() -> None:
    async def scenario() -> None:
        configuration = settings()
        assert (
            configuration.resource_metadata_url
            == "https://cookops.example/.well-known/oauth-protected-resource/mcp"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(configuration)),
            base_url="https://cookops.example",
        ) as client:
            response = await client.get("/.well-known/oauth-protected-resource/mcp")
            wrong_path = await client.get("/mcp/.well-known/oauth-protected-resource")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "public, max-age=3600"
        assert response.json() == {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "scopes_supported": ["cookops:mcp"],
            "bearer_methods_supported": ["header"],
            "resource_name": "CookOps MCP",
        }
        assert wrong_path.status_code == 404

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"resource_url": "http://cookops.example/mcp"}, "HTTPS"),
        ({"resource_url": "https://cookops.example/mcp/"}, "trailing slash"),
        ({"resource_url": "https://cookops.example/mcp?tenant=1"}, "query"),
        ({"authorization_server_url": "https://auth.example/oauth"}, "public origin"),
        ({"authorization_server_url": RESOURCE}, "different endpoints"),
        ({"resource_url": "https://COOKOPS.example/mcp"}, "canonical URL"),
        ({"resource_url": "https://cookops.example/a/../mcp"}, "canonical URL"),
        ({"introspection_url": "file:///oauth/introspect"}, "absolute URL"),
        (
            {"introspection_url": "http://evil.example/oauth/introspect"},
            "private OAuth service",
        ),
        (
            {"introspection_url": "https://cookops.example/oauth/introspect"},
            "private service origin",
        ),
        ({"introspection_client_secret": "too-short"}, "at least 32"),
    ],
)
def test_rejects_ambiguous_oauth_boundary(
    changes: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        settings(**changes)


def test_loads_separate_public_and_private_endpoints_from_environment() -> None:
    configuration = ResourceServerSettings.from_environment(
        {
            "MCP_RESOURCE": RESOURCE,
            "OAUTH_ISSUER": ISSUER,
            "OAUTH_INTROSPECTION_URL": INTROSPECTION_URL,
            "OAUTH_RESOURCE_SERVER_SECRET": SECRET,
        }
    )

    assert configuration.authorization_server_url == ISSUER
    assert configuration.introspection_url == INTROSPECTION_URL
    assert SECRET not in repr(configuration)


def test_introspection_verifier_enforces_exact_issuer_and_resource() -> None:
    expected_authorization = (
        "Basic "
        + base64.b64encode(f"cookops-resource-server:{SECRET}".encode()).decode()
    )

    async def verify(audience: str, issuer: str):
        def introspection(request: httpx.Request) -> httpx.Response:
            assert request.url == INTROSPECTION_URL
            assert request.headers["authorization"] == expected_authorization
            assert parse_qs(request.content.decode()) == {"token": ["opaque-token"]}
            return httpx.Response(
                200,
                json=valid_introspection_payload(aud=audience, iss=issuer),
            )

        async with IntrospectionTokenVerifier(
            settings(), transport=httpx.MockTransport(introspection)
        ) as verifier:
            return await verifier.verify_token("opaque-token")

    accepted = asyncio.run(verify(RESOURCE, ISSUER))
    assert accepted is not None
    assert accepted.resource == RESOURCE
    assert accepted.claims == {"iss": ISSUER}
    assert asyncio.run(verify("https://cookops.example/other", ISSUER)) is None
    assert asyncio.run(verify(RESOURCE, "https://cookops.example/other-issuer")) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active", False),
        ("client_id", MISSING),
        ("client_id", ""),
        ("client_id", " agent-client "),
        ("sub", MISSING),
        ("sub", "not-a-user"),
        ("sub", SUBJECT.upper()),
        ("exp", MISSING),
        ("exp", True),
        ("exp", int(time.time()) - 1),
        ("scope", MISSING),
        ("scope", ""),
        ("scope", "other"),
        ("scope", "cookops:mcp cookops:mcp"),
    ],
)
def test_introspection_verifier_rejects_invalid_claims(
    field: str, value: object
) -> None:
    payload = valid_introspection_payload()
    if value is MISSING:
        del payload[field]
    else:
        payload[field] = value

    async def scenario() -> None:
        async with IntrospectionTokenVerifier(
            settings(),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=payload)
            ),
        ) as verifier:
            assert await verifier.verify_token("opaque-token") is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "content", "content_type"),
    [
        (503, b"{}", "application/json"),
        (200, b"{}", "text/plain"),
        (200, b"not-json", "application/json"),
        (200, b"[]", "application/json"),
        (200, b"[" * 10_000 + b"0" + b"]" * 10_000, "application/json"),
        (200, b"x" * (64 * 1024 + 1), "application/json"),
    ],
)
def test_introspection_verifier_rejects_invalid_responses(
    status: int, content: bytes, content_type: str
) -> None:
    async def scenario() -> None:
        async with IntrospectionTokenVerifier(
            settings(),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    status,
                    content=content,
                    headers={"content-type": content_type},
                ),
            ),
        ) as verifier:
            assert await verifier.verify_token("opaque-token") is None

    asyncio.run(scenario())


def test_introspection_verifier_enforces_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_server, "INTROSPECTION_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        async def slow_response(_request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.1)
            return httpx.Response(200, json=valid_introspection_payload())

        async with IntrospectionTokenVerifier(
            settings(), transport=httpx.MockTransport(slow_response)
        ) as verifier:
            assert await verifier.verify_token("opaque-token") is None

    asyncio.run(scenario())


def test_introspection_verifier_fails_closed_on_network_error() -> None:
    async def scenario() -> None:
        def unavailable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unavailable", request=request)

        async with IntrospectionTokenVerifier(
            settings(), transport=httpx.MockTransport(unavailable)
        ) as verifier:
            assert await verifier.verify_token("opaque-token") is None

    asyncio.run(scenario())
