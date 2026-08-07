"""RFC 9728 discovery and opaque-token verification for the OAuth spike."""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import (
    build_resource_metadata_url,
)
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

MCP_SCOPE = "cookops:mcp"
RESOURCE_SERVER_CLIENT_ID = "cookops-resource-server"
MAX_INTROSPECTION_RESPONSE_BYTES = 64 * 1024
INTROSPECTION_TIMEOUT_SECONDS = 5.0
PRIVATE_HTTP_INTROSPECTION_HOSTS = frozenset(
    {"127.0.0.1", "::1", "localhost", "oauth-server"}
)


def _validated_endpoint(
    name: str, value: str, *, allow_private_http: bool = False
) -> tuple[str, str, int]:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical URL")

    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain a query or fragment")
    if parsed.path in ("", "/") or parsed.path.endswith("/"):
        raise ValueError(f"{name} must have a non-root path without a trailing slash")

    loopback = parsed.hostname in ("127.0.0.1", "::1", "localhost")
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{name} must use HTTP or HTTPS")
    if parsed.scheme == "http":
        if (
            allow_private_http
            and parsed.hostname not in PRIVATE_HTTP_INTROSPECTION_HOSTS
        ):
            raise ValueError(
                f"{name} may use HTTP only for the private OAuth service or loopback"
            )
        if not allow_private_http and not loopback:
            raise ValueError(f"{name} must use HTTPS outside loopback development")

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{name} has an invalid port") from error
    default_port = 443 if parsed.scheme == "https" else 80
    if port == default_port:
        raise ValueError(f"{name} must omit its default port")
    if "%" in parsed.path or str(httpx.URL(value)) != value:
        raise ValueError(f"{name} must use its canonical URL spelling")
    return parsed.scheme, parsed.hostname, port or default_port


@dataclass(frozen=True)
class ResourceServerSettings:
    """Exact public OAuth identity and private introspection boundary."""

    resource_url: str
    authorization_server_url: str
    introspection_url: str
    introspection_client_secret: str = field(repr=False)
    introspection_client_id: str = RESOURCE_SERVER_CLIENT_ID

    def __post_init__(self) -> None:
        resource_origin = _validated_endpoint("resource_url", self.resource_url)
        authorization_origin = _validated_endpoint(
            "authorization_server_url", self.authorization_server_url
        )
        introspection_origin = _validated_endpoint(
            "introspection_url", self.introspection_url, allow_private_http=True
        )
        if resource_origin != authorization_origin:
            raise ValueError(
                "resource_url and authorization_server_url must share the public origin"
            )
        if self.resource_url == self.authorization_server_url:
            raise ValueError(
                "resource_url and authorization_server_url must identify different endpoints"
            )
        if introspection_origin == authorization_origin:
            raise ValueError(
                "introspection_url must use a separate private service origin"
            )
        if not self.introspection_client_id.strip():
            raise ValueError("introspection_client_id must not be blank")
        if len(self.introspection_client_secret) < 32:
            raise ValueError(
                "introspection_client_secret must contain at least 32 characters"
            )

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environment is None else environment

        def required(name: str) -> str:
            value = source.get(name)
            if value is None:
                raise ValueError(f"{name} is required")
            return value

        return cls(
            resource_url=required("MCP_RESOURCE"),
            authorization_server_url=required("OAUTH_ISSUER"),
            introspection_url=required("OAUTH_INTROSPECTION_URL"),
            introspection_client_id=source.get(
                "OAUTH_RESOURCE_SERVER_CLIENT_ID", RESOURCE_SERVER_CLIENT_ID
            ),
            introspection_client_secret=required("OAUTH_RESOURCE_SERVER_SECRET"),
        )

    @property
    def resource_metadata_url(self) -> str:
        return str(build_resource_metadata_url(self.resource_url))


class IntrospectionTokenVerifier(TokenVerifier):
    """Validate opaque tokens against the exact configured issuer and resource."""

    def __init__(
        self,
        settings: ResourceServerSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._http_client = httpx.AsyncClient(transport=transport, trust_env=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http_client.aclose()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            async with asyncio.timeout(INTROSPECTION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "POST",
                    self._settings.introspection_url,
                    auth=(
                        self._settings.introspection_client_id,
                        self._settings.introspection_client_secret,
                    ),
                    data={"token": token},
                    timeout=INTROSPECTION_TIMEOUT_SECONDS,
                    follow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").partition(
                        ";"
                    )[0]
                    if content_type.strip().lower() != "application/json":
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_INTROSPECTION_RESPONSE_BYTES:
                            return None
                        body.extend(chunk)
                    result = json.loads(body)
        except (httpx.HTTPError, RecursionError, TimeoutError, ValueError):
            return None

        now = int(time.time())
        if not isinstance(result, dict) or result.get("active") is not True:
            return None
        if result.get("iss") != self._settings.authorization_server_url:
            return None
        if result.get("aud") != self._settings.resource_url:
            return None

        client_id = result.get("client_id")
        subject = result.get("sub")
        expires_at = result.get("exp")
        scope = result.get("scope")
        if (
            not isinstance(client_id, str)
            or not client_id
            or client_id != client_id.strip()
        ):
            return None
        if not isinstance(subject, str):
            return None
        try:
            if str(UUID(subject)) != subject:
                return None
        except ValueError:
            return None
        if (
            not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at <= now
        ):
            return None
        if not isinstance(scope, str):
            return None
        scopes = scope.split()
        if scopes != [MCP_SCOPE]:
            return None

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=self._settings.resource_url,
            subject=subject,
            claims={"iss": self._settings.authorization_server_url},
        )


def create_app(settings: ResourceServerSettings) -> Starlette:
    """Create an authenticated Streamable HTTP MCP protected resource."""

    verifier = IntrospectionTokenVerifier(settings)
    resource_url = urlsplit(settings.resource_url)
    public_origin = f"{resource_url.scheme}://{resource_url.netloc}"

    @asynccontextmanager
    async def lifespan(_server: MCPServer[Any]) -> AsyncIterator[dict[str, object]]:
        try:
            yield {}
        finally:
            await verifier.aclose()

    server = MCPServer(
        name="cookops",
        title="CookOps MCP",
        version="0.0.0-spike",
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=settings.authorization_server_url,
            resource_server_url=settings.resource_url,
            required_scopes=[MCP_SCOPE],
        ),
        lifespan=lifespan,
    )

    @server.tool(name="authenticated_identity", structured_output=True)
    def authenticated_identity() -> dict[str, str]:
        """Return the OAuth identity established for this MCP request."""

        access_token = get_access_token()
        if access_token is None or access_token.subject is None:
            raise RuntimeError("authenticated MCP request has no subject")
        return {
            "client_id": access_token.client_id,
            "resource": access_token.resource or "",
            "subject": access_token.subject,
        }

    return server.streamable_http_app(
        streamable_http_path=resource_url.path,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[resource_url.netloc],
            allowed_origins=[public_origin],
        ),
    )
