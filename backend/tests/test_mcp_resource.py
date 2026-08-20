import asyncio
import base64
from time import time
from uuid import uuid4

import httpx
import pytest

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
    ).routes


def test_settings_factory_is_disabled_only_when_all_mcp_values_are_absent() -> None:
    disabled = Settings(environment=Environment.TEST, human_auth_provider=HumanAuthProvider.DUMMY)
    assert create_mcp_protected_resource_from_settings(disabled) is None
    partial = disabled.model_construct(oauth_issuer="https://cookops.example/oauth")
    try:
        create_mcp_protected_resource_from_settings(partial)
    except RuntimeError as error:
        assert "incomplete" in str(error)
    else:
        raise AssertionError("partial MCP configuration must fail closed")
