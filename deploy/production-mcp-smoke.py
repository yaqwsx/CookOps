"""Real production-compose OAuth bearer smoke through the public Apache edge."""

import asyncio
import base64
import hashlib
import os
import re
import sys
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from cookops.development_seed import (  # type: ignore[import-untyped]
    DEVELOPMENT_EVENT_ID,
    PRIMARY_ORGANIZATION_ID,
)

_VERIFIER = "cookops-production-compose-mcp-verifier-long-enough-for-pkce"
_TOKEN = re.compile(r"csrfToken:'([0-9a-f]{64})'")


async def _approve(client: httpx.AsyncClient, origin: str, location: str) -> str:
    page = await client.get(urljoin(origin, location))
    assert page.status_code == 200, page.text
    match = _TOKEN.search(page.text)
    assert match is not None, "consent page did not expose its CSRF token"
    interaction = urlsplit(str(page.url)).path.rsplit("/", 1)[-1]
    decision = await client.post(
        f"{origin}/auth/mcp-interactions/{interaction}",
        headers={"origin": origin},
        json={"decision": "approve", "csrfToken": match.group(1)},
    )
    assert decision.status_code == 204, decision.text
    complete = await client.get(
        f"{origin}/oauth/interaction/{interaction}/complete",
        follow_redirects=False,
    )
    assert complete.status_code in (302, 303), complete.text
    return urljoin(origin, complete.headers["location"])


async def main() -> None:
    origin = os.environ["COOKOPS_MCP_SMOKE_ORIGIN"]
    certificate = os.environ["COOKOPS_MCP_SMOKE_CERT"]
    resource = f"{origin}/mcp"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(_VERIFIER.encode()).digest()).rstrip(
        b"="
    ).decode()
    async with httpx.AsyncClient(verify=certificate, follow_redirects=False) as client:
        protected_metadata = await client.get(
            f"{origin}/.well-known/oauth-protected-resource/mcp"
        )
        assert protected_metadata.status_code == 200, protected_metadata.text
        assert protected_metadata.json()["resource"] == resource
        authorization_servers = protected_metadata.json()["authorization_servers"]
        assert authorization_servers == [f"{origin}/oauth"]
        authorization_metadata = await client.get(
            f"{origin}/.well-known/oauth-authorization-server/oauth"
        )
        assert authorization_metadata.status_code == 200, authorization_metadata.text
        assert authorization_metadata.json()["issuer"] == f"{origin}/oauth"
        registration = await client.post(
            f"{origin}/oauth/register",
            json={
                "application_type": "web",
                "client_name": "CookOps production MCP smoke",
                "grant_types": ["authorization_code"],
                "id_token_signed_response_alg": "ES256",
                "redirect_uris": [f"{origin}/callback"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]
        session = await client.post(
            f"{origin}/auth/dummy/session", json={"subject": "dummy-member"}
        )
        assert session.status_code == 204, session.text
        query = urlencode(
            {
                "client_id": client_id,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": f"{origin}/callback",
                "resource": resource,
                "response_type": "code",
                "scope": "cookops:mcp",
                "state": "production-compose-mcp-smoke",
            }
        )
        location = f"{origin}/oauth/authorize?{query}"
        code = None
        for _ in range(8):
            response = await client.get(location)
            assert response.status_code in (302, 303), response.text
            location = urljoin(origin, response.headers["location"])
            parsed = urlsplit(location)
            if parsed.path == "/callback":
                callback = parse_qs(parsed.query)
                assert callback["state"] == ["production-compose-mcp-smoke"]
                code = callback["code"][0]
                break
            assert parsed.path.startswith("/auth/mcp-interactions/")
            location = await _approve(client, origin, location)
        assert code is not None, "OAuth consent did not return an authorization code"
        token_response = await client.post(
            f"{origin}/oauth/token",
            data={
                "client_id": client_id,
                "code": code,
                "code_verifier": _VERIFIER,
                "grant_type": "authorization_code",
                "redirect_uri": f"{origin}/callback",
                "resource": resource,
            },
        )
        assert token_response.status_code == 200, token_response.text
        token = token_response.json()["access_token"]

    async with (
        httpx.AsyncClient(
            verify=certificate,
            headers={"authorization": f"Bearer {token}"},
        ) as mcp_http,
        streamable_http_client(resource, http_client=mcp_http) as streams,
        ClientSession(streams[0], streams[1]) as mcp_client,
    ):
        await mcp_client.initialize()
        result = await mcp_client.call_tool(
            "get_event_summary",
            {
                "organization_id": str(PRIMARY_ORGANIZATION_ID),
                "event_id": str(DEVELOPMENT_EVENT_ID),
            },
        )
        assert result.isError is False, result.content
        structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = getattr(result, "structured_content", None)
        assert isinstance(structured, dict)
        assert structured["id"] == str(DEVELOPMENT_EVENT_ID)
        assert structured["organization_id"] == str(PRIMARY_ORGANIZATION_ID)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (AssertionError, KeyError, httpx.HTTPError) as error:
        print(f"production Compose MCP smoke failed: {error}", file=sys.stderr)
        raise
