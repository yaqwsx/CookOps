"""Disposable HTTPS-ingress smoke for the authenticated MCP fixture."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).parent
PUBLIC_HOST = "mcp.localtest.me"
sys.path.insert(0, str(ROOT / "resource-server" / "tests"))
from test_authenticated_mcp_e2e import (
    CODE_VERIFIER,
    authorization_code,
    resource_server,
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return port


def require_loopback_dns() -> None:
    addresses = {item[4][0] for item in socket.getaddrinfo(PUBLIC_HOST, None)}
    if not addresses or not addresses <= {"127.0.0.1", "::1"}:
        raise RuntimeError(f"{PUBLIC_HOST} must resolve only to loopback; got {sorted(addresses)}")


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(5)


def apache_config(path: Path, cert: Path, key: Path, public_port: int,
                  fixture_port: int, resource_port: int) -> None:
    path.write_text(f"""ServerRoot \"{path.parent}\"
PidFile \"{path.parent}/httpd.pid\"
Listen {public_port}
LoadModule mpm_event_module /usr/lib/apache2/modules/mod_mpm_event.so
LoadModule ssl_module /usr/lib/apache2/modules/mod_ssl.so
LoadModule socache_shmcb_module /usr/lib/apache2/modules/mod_socache_shmcb.so
LoadModule headers_module /usr/lib/apache2/modules/mod_headers.so
LoadModule authz_core_module /usr/lib/apache2/modules/mod_authz_core.so
LoadModule proxy_module /usr/lib/apache2/modules/mod_proxy.so
LoadModule proxy_http_module /usr/lib/apache2/modules/mod_proxy_http.so
ErrorLog /dev/stderr
LogLevel warn
ServerName {PUBLIC_HOST}
<VirtualHost *:{public_port}>
SSLEngine on
SSLCertificateFile {cert}
SSLCertificateKeyFile {key}
ProxyPreserveHost On
RequestHeader set X-Forwarded-Proto https
RequestHeader set X-Forwarded-Host {PUBLIC_HOST}:{public_port}
ProxyPass /mcp http://127.0.0.1:{resource_port}/mcp
ProxyPassReverse /mcp http://127.0.0.1:{resource_port}/mcp
ProxyPass / http://127.0.0.1:{fixture_port}/
ProxyPassReverse / http://127.0.0.1:{fixture_port}/
<Location />
Require all granted
</Location>
</VirtualHost>
""")


async def main() -> None:
    public_port, fixture_port = free_port(), free_port()
    require_loopback_dns()
    public_origin = f"https://{PUBLIC_HOST}:{public_port}"
    resource_listener = socket.create_server(("127.0.0.1", 0))
    resource_port = int(resource_listener.getsockname()[1])
    database_url = os.environ["OAUTH_E2E_DATABASE_URL"]
    image = "cookops-mcp-oauth-node22-smoke"
    await asyncio.to_thread(
        subprocess.run,
        ["docker", "build", "-q", "-t", image, str(ROOT / "oauth-server")],
        check=True,
    )
    fixture_env = os.environ | {
        "COOKOPS_OAUTH_E2E_FIXTURE": "authenticated-mcp",
        "OAUTH_E2E_DATABASE_URL": database_url,
        "OAUTH_E2E_RESOURCE_PORT": str(resource_port),
        "OAUTH_E2E_PUBLIC_PORT": str(fixture_port),
        "OAUTH_E2E_PUBLIC_ORIGIN": public_origin,
    }
    fixture = await asyncio.to_thread(
        subprocess.Popen,
        ["docker", "run", "--rm", "--network", "host",
         "-e", "COOKOPS_OAUTH_E2E_FIXTURE=authenticated-mcp",
         "-e", "OAUTH_E2E_DATABASE_URL",
         "-e", f"OAUTH_E2E_RESOURCE_PORT={resource_port}",
         "-e", f"OAUTH_E2E_PUBLIC_PORT={fixture_port}",
         "-e", f"OAUTH_E2E_PUBLIC_ORIGIN={public_origin}", image,
         "node", "--import", "tsx", "test-fixtures/authenticated-mcp-fixture.ts"],
        env=fixture_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    proxy = None
    failed = False
    try:
        assert fixture.stdout is not None
        try:
            line = await asyncio.wait_for(
                asyncio.to_thread(fixture.stdout.readline), 15
            )
        except TimeoutError as error:
            stop_process(fixture)
            stderr = fixture.stderr.read() if fixture.stderr else ""
            raise RuntimeError(f"OAuth fixture startup timed out: {stderr}") from error
        if not line:
            stderr = fixture.stderr.read() if fixture.stderr else ""
            raise RuntimeError(f"OAuth fixture exited before configuration: {stderr}")
        configuration = json.loads(line)
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cert, key = temp_path / "cert.pem", temp_path / "key.pem"
            await asyncio.to_thread(subprocess.run, [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "1",
                "-subj", f"/CN={PUBLIC_HOST}", "-addext", f"subjectAltName=DNS:{PUBLIC_HOST}",
            ], check=True, capture_output=True)
            config = temp_path / "httpd.conf"
            apache_config(config, cert, key, public_port, fixture_port, resource_port)
            proxy = await asyncio.to_thread(subprocess.Popen,
                ["apache2", "-DFOREGROUND", "-f", str(config)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            await asyncio.sleep(0.2)
            if proxy.poll() is not None:
                raise RuntimeError(f"Apache exited early: {proxy.stderr.read()}")
            async with resource_server(configuration, resource_listener):
                issuer = configuration.get("issuer")
                resource = configuration.get("resource")
                subject = configuration.get("subject")
                assert isinstance(issuer, str) and isinstance(resource, str)
                assert isinstance(subject, str)
                async with httpx.AsyncClient(verify=str(cert), follow_redirects=False) as client:
                    protected = await client.get(
                        f"{issuer.rsplit('/oauth', 1)[0]}/.well-known/oauth-protected-resource/mcp"
                    )
                    assert protected.status_code == 200, (
                        f"protected-resource discovery failed: {protected.status_code} "
                        f"{protected.text}"
                    )
                    discovery = await client.get(
                        f"{issuer.rsplit('/oauth', 1)[0]}/.well-known/oauth-authorization-server/oauth"
                    )
                    assert discovery.status_code == 200
                    assert discovery.json()["issuer"] == issuer
                    assert (await client.post(resource, json={})).status_code == 401
                    code = await authorization_code(client, issuer, resource)
                    token_response = await client.post(f"{issuer}/token", data={
                        "client_id": "cookops-spike-client", "code": code,
                        "code_verifier": CODE_VERIFIER,
                        "grant_type": "authorization_code",
                        "redirect_uri": f"{public_origin}/callback",
                        "resource": resource,
                    })
                    assert token_response.status_code == 200
                    token = token_response.json()["access_token"]
                async with httpx.AsyncClient(
                    verify=str(cert), follow_redirects=False
                ) as negative_client:
                    assert (
                        await negative_client.post(
                            resource,
                            headers={"authorization": "Bearer malformed"},
                            json={},
                        )
                    ).status_code == 401
                    wrong_audience = await negative_client.post(
                        f"{public_origin}/other-mcp/mcp",
                        headers={"authorization": f"Bearer {token}"},
                        json={},
                    )
                    assert wrong_audience.status_code == 401
                    wrong_host = await negative_client.post(
                        resource,
                        headers={
                            "authorization": f"Bearer {token}",
                            "host": "evil.example",
                        },
                        json={},
                    )
                    assert wrong_host.status_code == 421
                    wrong_origin = await negative_client.post(
                        resource,
                        headers={
                            "authorization": f"Bearer {token}",
                            "origin": "https://evil.example",
                        },
                        json={},
                    )
                    assert wrong_origin.status_code == 403
                async with (
                    httpx.AsyncClient(
                        headers={"authorization": f"Bearer {token}"}, verify=str(cert)
                    ) as mcp_http,
                    streamable_http_client(
                        resource, http_client=mcp_http, terminate_on_close=False
                    ) as streams,
                    ClientSession(streams[0], streams[1]) as session,
                ):
                    await session.initialize()
                    if os.environ.get("COOKOPS_BACKEND_MCP") == "1":
                        organization_id = os.environ["MCP_E2E_ORGANIZATION_ID"]
                        event_id = os.environ["MCP_E2E_EVENT_ID"]
                        result = await session.call_tool(
                            "get_event_summary",
                            {
                                "organization_id": organization_id,
                                "event_id": event_id,
                            },
                        )
                        structured = getattr(result, "structuredContent", None)
                        if structured is None:
                            structured = result.structured_content
                        assert structured["id"] == event_id
                        assert structured["organization_id"] == organization_id
                        denied = await session.call_tool(
                            "get_event_summary",
                            {
                                "organization_id": os.environ[
                                    "MCP_E2E_FOREIGN_ORGANIZATION_ID"
                                ],
                                "event_id": os.environ["MCP_E2E_FOREIGN_EVENT_ID"],
                            },
                        )
                        denied_error = getattr(
                            denied, "isError", getattr(denied, "is_error", None)
                        )
                        assert denied_error is True
                    else:
                        result = await session.call_tool("authenticated_identity")
                is_error = getattr(result, "isError", getattr(result, "is_error", None))
                assert is_error is False
                if os.environ.get("COOKOPS_BACKEND_MCP") != "1":
                    structured = getattr(result, "structuredContent", None)
                    if structured is None:
                        structured = result.structured_content
                    assert structured == {
                        "client_id": "cookops-spike-client",
                        "resource": resource,
                        "subject": subject,
                    }
                async with httpx.AsyncClient(
                    verify=str(cert), follow_redirects=False
                ) as post_client:
                    await asyncio.sleep(6)
                    expired = await post_client.post(
                        resource,
                        headers={"authorization": f"Bearer {token}"},
                        json={},
                    )
                    assert expired.status_code == 401
                    revoked = await post_client.post(
                        f"{issuer}/revoke",
                        data={
                            "client_id": "cookops-spike-client",
                            "token": token,
                            "token_type_hint": "access_token",
                        },
                    )
                    assert revoked.status_code == 200
                    after_revoke = await post_client.post(
                        resource,
                        headers={"authorization": f"Bearer {token}"},
                        json={},
                    )
                    assert after_revoke.status_code == 401
        print("live HTTPS proxy smoke: PASS")
    except BaseException:
        failed = True
        raise
    finally:
        resource_listener.close()
        if proxy is not None:
            proxy.terminate()
            try:
                proxy.wait(5)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait(5)
            if failed and proxy.stderr is not None:
                print(f"Apache stderr:\n{proxy.stderr.read()}", file=sys.stderr)
        stop_process(fixture)
        if failed and fixture.stderr is not None:
            print(f"Fixture stderr:\n{fixture.stderr.read()}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
