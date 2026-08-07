"""Private RFC 7662 verification for the deliberately unmounted MCP resource."""

from __future__ import annotations

from time import time
from typing import Any, cast
from uuid import UUID

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from cookops.config import Settings


class McpIntrospectionVerifier(TokenVerifier):
    """Fail closed; no positive cache means revocation is visible on the next call."""

    def __init__(
        self,
        *,
        issuer: str,
        resource: str,
        introspection_url: str,
        resource_server_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not all((issuer, resource, introspection_url, resource_server_secret)):
            raise ValueError("MCP token verifier requires complete OAuth configuration")
        url = httpx.URL(introspection_url)
        if url.scheme != "http" or not url.host or url.params or url.query or url.fragment:
            raise ValueError("MCP introspection must use a private HTTP URL without extras")
        self._issuer = issuer.rstrip("/")
        self._resource = resource.rstrip("/")
        self._url = str(url)
        self._secret = resource_server_secret
        self._transport = transport

    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not token or len(token) > 4096:
            return None
        try:
            async with httpx.AsyncClient(timeout=5, transport=self._transport) as client:
                response = await client.post(
                    self._url,
                    data={"token": token},
                    auth=("cookops-resource-server", self._secret),
                )
            payload = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("active") is not True:
            return None
        scope = payload.get("scope")
        client_id = payload.get("client_id")
        subject = payload.get("sub")
        expires_at = payload.get("exp")
        audience = payload.get("aud")
        if (
            payload.get("iss") != self._issuer
            or not isinstance(scope, str)
            or "cookops:mcp" not in scope.split()
            or not isinstance(client_id, str)
            or not client_id
            or not isinstance(subject, str)
            or not _uuid(subject)
            or not isinstance(expires_at, int)
            or expires_at <= time()
            or not _audience_contains(audience, self._resource)
        ):
            return None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scope.split(),
            expires_at=expires_at,
            resource=self._resource,
            subject=subject,
            claims={"iss": self._issuer},
        )


def _uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _audience_contains(value: Any, resource: str) -> bool:
    return value == resource or (isinstance(value, list) and resource in value)


def create_mcp_protected_resource(verifier: TokenVerifier, *, issuer: str, resource: str) -> object:
    """Build, but do not mount, the Streamable HTTP ASGI protected resource."""
    return FastMCP(
        "CookOps",
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=cast(Any, issuer),
            resource_server_url=cast(Any, resource),
            required_scopes=["cookops:mcp"],
        ),
        stateless_http=True,
        streamable_http_path="/mcp",
    ).streamable_http_app()


def create_mcp_protected_resource_from_settings(settings: Settings) -> object | None:
    """Return no app only when MCP is entirely disabled; callers must mount deliberately."""
    values = (
        settings.oauth_issuer,
        settings.mcp_resource,
        settings.oauth_introspection_url,
        settings.oauth_resource_server_secret,
    )
    if all(value is None for value in values):
        return None
    if any(not isinstance(value, str) or not value for value in values):
        raise RuntimeError("MCP OAuth verification settings are incomplete")
    issuer, resource, introspection_url, secret = cast(tuple[str, str, str, str], values)
    return create_mcp_protected_resource(
        McpIntrospectionVerifier(
            issuer=issuer,
            resource=resource,
            introspection_url=introspection_url,
            resource_server_secret=secret,
        ),
        issuer=issuer,
        resource=resource,
    )
