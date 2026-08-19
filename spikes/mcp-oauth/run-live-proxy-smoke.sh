#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${OAUTH_E2E_DATABASE_URL:?set OAUTH_E2E_DATABASE_URL to the disposable fixture database}"
command -v docker >/dev/null || { echo 'docker is required for the pinned Node22 fixture' >&2; exit 78; }
export PYTHONPATH="$PWD/resource-server/src${PYTHONPATH:+:$PYTHONPATH}"
exec uv run --project resource-server python live-proxy-smoke.py
