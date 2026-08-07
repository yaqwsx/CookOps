import asyncio
from time import time
from uuid import uuid4

import httpx

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
