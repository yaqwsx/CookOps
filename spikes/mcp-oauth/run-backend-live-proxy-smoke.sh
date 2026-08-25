#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo 'docker is required for the pinned Node22 fixture' >&2; exit 78; }
command -v uv >/dev/null || { echo 'uv is required for the backend project' >&2; exit 78; }
database_container="cookops-mcp-postgres-$$"
database_image='postgres@sha256:32ca0af8e77bfb8c6610c488e4691f83f972a3e9e64d3b02facf3ab111ad5500'
cleanup() {
    docker rm --force "$database_container" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker run --detach --name "$database_container" --publish 127.0.0.1::5432 \
    --env POSTGRES_DB=cookops --env POSTGRES_PASSWORD=cookops --env POSTGRES_USER=cookops \
    "$database_image" >/dev/null
database_port=""
for _attempt in $(seq 1 30); do
    database_port=$(docker port "$database_container" 5432/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')
    if [ -n "$database_port" ] && docker exec "$database_container" pg_isready -U cookops -d cookops >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
[ -n "$database_port" ] || { echo 'disposable PostgreSQL did not become reachable' >&2; exit 1; }
export OAUTH_E2E_DATABASE_URL="postgresql://cookops:cookops@127.0.0.1:$database_port/cookops"
export COOKOPS_DATABASE_URL="postgresql+psycopg://cookops:cookops@127.0.0.1:$database_port/cookops"
export MCP_E2E_ORGANIZATION_ID=018f7cca-4a90-7fa0-b7e4-77f6c42d5731
export MCP_E2E_EVENT_ID=018f7ccb-4a90-7fa0-b7e4-77f6c42d5731
export MCP_E2E_FOREIGN_ORGANIZATION_ID=018f7ccc-4a90-7fa0-b7e4-77f6c42d5731
export MCP_E2E_FOREIGN_EVENT_ID=018f7ccd-4a90-7fa0-b7e4-77f6c42d5731
export COOKOPS_OAUTH_E2E_ACCESS_TOKEN_TTL_SECONDS=5
(cd ../../backend && uv run --locked alembic upgrade head && uv run --locked python ../spikes/mcp-oauth/seed-backend-mcp.py)
export COOKOPS_BACKEND_MCP=1
export PYTHONPATH="$PWD/resource-server/src:$PWD/../../backend/src:$PWD${PYTHONPATH:+:$PYTHONPATH}"
uv run --project ../../backend python live-proxy-smoke.py
