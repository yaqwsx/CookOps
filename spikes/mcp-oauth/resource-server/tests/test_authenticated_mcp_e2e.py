import asyncio
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import urljoin, urlsplit

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.types import Receive, Scope, Send

CODE_VERIFIER = "cookops-authenticated-mcp-verifier-long-enough-for-pkce"
RESOURCE_SERVER_CLIENT_ID = "cookops-resource-server"
RESOURCE_SERVER_SECRET = "c" * 32
MCP_ROOT = Path(__file__).resolve().parents[2]


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(5)


def reserved_listener() -> socket.socket:
    return socket.create_server(("127.0.0.1", 0))


class OAuthFixture:
    def __init__(self, database_url: str, resource_port: int) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "COOKOPS_OAUTH_E2E_FIXTURE": "authenticated-mcp",
                "OAUTH_E2E_DATABASE_URL": database_url,
                "OAUTH_E2E_RESOURCE_PORT": str(resource_port),
            }
        )
        oauth_server = Path(__file__).parents[2] / "oauth-server"
        self._process = subprocess.Popen(
            [
                "node",
                "--import",
                "tsx",
                "test-fixtures/authenticated-mcp-fixture.ts",
            ],
            cwd=oauth_server,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    async def __aenter__(self) -> Self:
        assert self._process.stdout is not None
        try:
            line = await asyncio.wait_for(
                asyncio.to_thread(self._process.stdout.readline), timeout=15
            )
            self.configuration: dict[str, str | int] = json.loads(line)
        except (TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(await self._diagnostics()) from error
        return self

    async def _diagnostics(self) -> str:
        await self._stop(5)
        assert self._process.stderr is not None
        return self._process.stderr.read()

    async def _stop(self, timeout: float) -> None:
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            await asyncio.to_thread(self._process.wait, timeout)
        except subprocess.TimeoutExpired:
            self._process.kill()
            await asyncio.to_thread(self._process.wait, 5)

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._stop(10)
        if self._process.returncode != 0 and exception is None:
            assert self._process.stderr is not None
            raise RuntimeError(self._process.stderr.read())


async def assert_official_node_sdk_client(
    configuration: dict[str, str | int],
) -> None:
    resource = configuration["resource"]
    issuer = configuration["issuer"]
    subject = configuration["subject"]
    assert (
        isinstance(resource, str)
        and isinstance(issuer, str)
        and isinstance(subject, str)
    )
    oauth_server = Path(__file__).parents[2] / "oauth-server"
    environment = os.environ.copy()
    environment.update(
        {
            "MCP_E2E_ISSUER": issuer,
            "MCP_E2E_RESOURCE": resource,
            "MCP_E2E_SUBJECT": subject,
        }
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "node",
            "--import",
            "tsx",
            "test-fixtures/official-node-mcp-client.ts",
        ],
        cwd=oauth_server,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@asynccontextmanager
async def resource_server(
    configuration: dict[str, str | int], listener: socket.socket
) -> AsyncIterator[None]:
    private_port = configuration["privatePort"]
    assert isinstance(private_port, int)
    resource_url = configuration["resource"]
    issuer = configuration["issuer"]
    assert isinstance(resource_url, str) and isinstance(issuer, str)
    if os.environ.get("COOKOPS_BACKEND_MCP") == "1":
        resource_port = listener.getsockname()[1]
        listener.close()
        database_url = os.environ["OAUTH_E2E_DATABASE_URL"].replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
        environment = os.environ | {
            "COOKOPS_DATABASE_URL": database_url,
            "COOKOPS_ENVIRONMENT": "test",
            "COOKOPS_MCP_RESOURCE": resource_url,
            "COOKOPS_OAUTH_ISSUER": issuer,
            "COOKOPS_OAUTH_INTROSPECTION_URL": (
                f"http://127.0.0.1:{private_port}/oauth/introspect"
            ),
            "COOKOPS_OAUTH_RESOURCE_SERVER_SECRET": "c" * 32,
        }
        backend = await asyncio.to_thread(
            subprocess.Popen,
            [
                "uv",
                "run",
                "--project",
                str(MCP_ROOT / "../../backend"),
                "uvicorn",
                "backend_mcp_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(resource_port),
            ],
            cwd=MCP_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            async with asyncio.timeout(20):
                while True:
                    if backend.poll() is not None:
                        error = backend.stderr.read() if backend.stderr else ""
                        raise RuntimeError(f"backend MCP exited early: {error}")
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", resource_port), 0.2
                        ):
                            break
                    except OSError:
                        await asyncio.sleep(0.05)
            yield
        finally:
            stop_process(backend)
            if backend.returncode != 0 and backend.stderr is not None:
                print(f"Backend stderr:\n{backend.stderr.read()}", file=sys.stderr)
        return
    from cookops_mcp_oauth_spike import ResourceServerSettings, create_app

    settings = ResourceServerSettings(
        resource_url=resource_url,
        authorization_server_url=issuer,
        introspection_url=f"http://127.0.0.1:{private_port}/oauth/introspect",
        introspection_client_id=RESOURCE_SERVER_CLIENT_ID,
        introspection_client_secret=RESOURCE_SERVER_SECRET,
    )
    other_settings = ResourceServerSettings(
        resource_url=f"{resource_url.rsplit('/', 1)[0]}/other-mcp",
        authorization_server_url=issuer,
        introspection_url=f"http://127.0.0.1:{private_port}/oauth/introspect",
        introspection_client_id=RESOURCE_SERVER_CLIENT_ID,
        introspection_client_secret=RESOURCE_SERVER_SECRET,
    )
    primary_app = create_app(settings)
    other_app = create_app(other_settings)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        target = (
            other_app
            if scope["type"] == "http" and scope["path"].startswith("/other-mcp")
            else primary_app
        )
        await target(scope, receive, send)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
    )
    async with other_app.router.lifespan_context(other_app):
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)


async def authorization_code(
    client: httpx.AsyncClient, issuer: str, resource: str
) -> str:
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = "authenticated-mcp-state"
    parsed_resource = urlsplit(resource)
    current = f"{issuer}/authorize"
    parameters: dict[str, str] | None = {
        "client_id": "cookops-spike-client",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": f"{parsed_resource.scheme}://{parsed_resource.netloc}/callback",
        "resource": resource,
        "response_type": "code",
        "scope": "cookops:mcp",
        "state": state,
    }
    for _redirect in range(10):
        response = await client.get(current, params=parameters)
        parameters = None
        assert response.status_code in (302, 303), response.text
        location = response.headers["location"]
        redirect = urljoin(current, location)
        parsed = urlsplit(redirect)
        if parsed.path == "/callback":
            query = httpx.QueryParams(parsed.query)
            assert query["state"] == state
            assert query["iss"] == issuer
            return query["code"]
        current = redirect
    raise AssertionError("OAuth interaction exceeded redirect limit")


async def scenario(database_url: str) -> None:
    with reserved_listener() as listener:
        resource_port = listener.getsockname()[1]
        async with OAuthFixture(database_url, resource_port) as fixture:
            configuration = fixture.configuration
            issuer = configuration["issuer"]
            resource = configuration["resource"]
            subject = configuration["subject"]
            assert isinstance(issuer, str)
            assert isinstance(resource, str)
            assert isinstance(subject, str)
            parsed_issuer = urlsplit(issuer)
            public_origin = f"{parsed_issuer.scheme}://{parsed_issuer.netloc}"
            async with resource_server(configuration, listener):
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    metadata = await client.get(
                        f"{public_origin}/.well-known/oauth-protected-resource/mcp"
                    )
                    assert metadata.status_code == 200
                    assert metadata.json() == {
                        "authorization_servers": [issuer],
                        "bearer_methods_supported": ["header"],
                        "resource": resource,
                        "scopes_supported": ["cookops:mcp"],
                    }

                    discovery = await client.get(
                        f"{public_origin}/.well-known/oauth-authorization-server/oauth"
                    )
                    assert discovery.status_code == 200
                    discovery_document = discovery.json()
                    assert {
                        "authorization_endpoint": discovery_document[
                            "authorization_endpoint"
                        ],
                        "introspection_endpoint": discovery_document[
                            "introspection_endpoint"
                        ],
                        "issuer": discovery_document["issuer"],
                        "token_endpoint": discovery_document["token_endpoint"],
                    } == {
                        "authorization_endpoint": f"{issuer}/authorize",
                        "introspection_endpoint": f"{issuer}/introspect",
                        "issuer": issuer,
                        "token_endpoint": f"{issuer}/token",
                    }

                    unauthorized = await client.post(resource, json={})
                    assert unauthorized.status_code == 401
                    assert (
                        f'resource_metadata="{public_origin}/.well-known/'
                        'oauth-protected-resource/mcp"'
                        in unauthorized.headers["www-authenticate"]
                    )
                    rejected = await client.post(
                        resource,
                        headers={"authorization": "Bearer not-an-access-token"},
                        json={},
                    )
                    assert rejected.status_code == 401
                    code = await authorization_code(client, issuer, resource)
                    token_response = await client.post(
                        f"{issuer}/token",
                        data={
                            "client_id": "cookops-spike-client",
                            "code": code,
                            "code_verifier": CODE_VERIFIER,
                            "grant_type": "authorization_code",
                            "redirect_uri": f"{public_origin}/callback",
                            "resource": resource,
                        },
                    )
                    assert token_response.status_code == 200
                    tokens = token_response.json()
                    access_token = tokens["access_token"]
                    refresh_token = tokens["refresh_token"]
                    assert "." not in access_token
                    wrong_host = await client.post(
                        resource,
                        headers={
                            "authorization": f"Bearer {access_token}",
                            "host": "evil.example",
                        },
                        json={},
                    )
                    assert wrong_host.status_code in (400, 421)
                    wrong_origin = await client.post(
                        resource,
                        headers={
                            "authorization": f"Bearer {access_token}",
                            "origin": "https://evil.example",
                        },
                        json={},
                    )
                    assert wrong_origin.status_code == 403

                async with (
                    httpx.AsyncClient(
                        headers={"authorization": f"Bearer {access_token}"}
                    ) as mcp_http,
                    streamable_http_client(resource, http_client=mcp_http) as (
                        read_stream,
                        write_stream,
                    ),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    await session.initialize()
                    result = await session.call_tool("authenticated_identity")

                assert result.is_error is False
                assert result.structured_content == {
                    "client_id": "cookops-spike-client",
                    "resource": resource,
                    "subject": subject,
                }

                await asyncio.sleep(5.2)
                async with httpx.AsyncClient() as expiry_client:
                    expired = await expiry_client.post(
                        resource,
                        headers={"authorization": f"Bearer {access_token}"},
                        json={},
                    )
                assert expired.status_code == 401

                async with httpx.AsyncClient() as refresh_client:
                    rotated_response = await refresh_client.post(
                        f"{issuer}/token",
                        data={
                            "client_id": "cookops-spike-client",
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                            "resource": resource,
                        },
                    )
                    assert rotated_response.status_code == 200
                    rotated_tokens = rotated_response.json()
                    rotated_access_token = rotated_tokens["access_token"]
                    assert rotated_access_token and rotated_tokens["refresh_token"]
                    replayed_response = await refresh_client.post(
                        f"{issuer}/token",
                        data={
                            "client_id": "cookops-spike-client",
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                            "resource": resource,
                        },
                    )
                    assert replayed_response.status_code == 400
                    assert replayed_response.json()["error"] == "invalid_grant"
                    rejected_rotated_access = await refresh_client.post(
                        resource,
                        headers={"authorization": f"Bearer {rotated_access_token}"},
                        json={},
                    )
                    assert rejected_rotated_access.status_code == 401
                    fresh_code = await authorization_code(
                        refresh_client, issuer, resource
                    )
                    fresh_token_response = await refresh_client.post(
                        f"{issuer}/token",
                        data={
                            "client_id": "cookops-spike-client",
                            "code": fresh_code,
                            "code_verifier": CODE_VERIFIER,
                            "grant_type": "authorization_code",
                            "redirect_uri": f"{public_origin}/callback",
                            "resource": resource,
                        },
                    )
                    assert fresh_token_response.status_code == 200
                    fresh_access_token = fresh_token_response.json()["access_token"]
                    assert fresh_access_token and "." not in fresh_access_token
                    fresh_valid = await refresh_client.post(
                        resource,
                        headers={"authorization": f"Bearer {fresh_access_token}"},
                        json={},
                    )
                    assert fresh_valid.status_code == 200
                    mixed_up = await refresh_client.post(
                        f"{public_origin}/other-mcp",
                        headers={"authorization": f"Bearer {fresh_access_token}"},
                        json={},
                    )
                    assert mixed_up.status_code == 401
                    revoked = await refresh_client.post(
                        f"{issuer}/revoke",
                        data={
                            "client_id": "cookops-spike-client",
                            "token": fresh_access_token,
                            "token_type_hint": "access_token",
                        },
                    )
                    assert revoked.status_code == 200
                    rejected_after_revoke = await refresh_client.post(
                        resource,
                        headers={"authorization": f"Bearer {fresh_access_token}"},
                        json={},
                    )
                    assert rejected_after_revoke.status_code == 401

                await assert_official_node_sdk_client(configuration)


def test_real_opaque_token_authenticates_an_mcp_request() -> None:
    database_url = os.environ.get("OAUTH_E2E_DATABASE_URL")
    if database_url is None:
        pytest.skip("OAUTH_E2E_DATABASE_URL is not configured")
    asyncio.run(scenario(database_url))
