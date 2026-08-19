"""Focused disposable ingress check; requires the fixture database."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    "OAUTH_E2E_DATABASE_URL" not in os.environ,
    reason="OAUTH_E2E_DATABASE_URL is not configured",
)
def test_live_https_proxy_smoke() -> None:
    result = subprocess.run(
        [str(Path(__file__).with_name("run-live-proxy-smoke.sh"))],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_smoke_keeps_tls_verification_and_ip_san() -> None:
    source = Path(__file__).with_name("live-proxy-smoke.py").read_text()
    assert "subjectAltName=DNS:{PUBLIC_HOST}" in source
    assert 'httpx.AsyncClient(verify=str(cert)' in source
    assert 'verify=str(cert)' in source
    assert "ProxyPass /mcp" in source
    assert "Require all granted" in source
    assert "ServerName {PUBLIC_HOST}" in source
    assert "authz_core_module" in source


def test_launcher_exports_resource_server_import_path() -> None:
    launcher = Path(__file__).with_name("run-live-proxy-smoke.sh").read_text()
    source = Path(__file__).with_name("live-proxy-smoke.py").read_text()
    assert 'PYTHONPATH="$PWD/resource-server/src' in launcher
    assert "docker" in launcher
    for name in ("COOKOPS_OAUTH_E2E_FIXTURE", "OAUTH_E2E_DATABASE_URL", "OAUTH_E2E_RESOURCE_PORT", "OAUTH_E2E_PUBLIC_PORT", "OAUTH_E2E_PUBLIC_ORIGIN"):
        assert name in source
    assert 'f"OAUTH_E2E_DATABASE_URL={database_url}"' not in source
    assert '"-e", "OAUTH_E2E_DATABASE_URL"' in source
