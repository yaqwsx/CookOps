#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
docker build --quiet --tag cookops-api-image-test "$root/backend" >/dev/null
docker build --quiet --tag cookops-web-image-test "$root/frontend" >/dev/null
docker build --quiet --tag cookops-oauth-image-test "$root/oauth-server" >/dev/null
docker run --rm --entrypoint=nginx cookops-web-image-test -t

assert_runtime_config() (
    provider=$1
    expected=$2
    client_id=${3-}
    container_id=$(docker run --detach --publish 127.0.0.1::8080 \
        --env "COOKOPS_HUMAN_AUTH_PROVIDER=$provider" \
        --env "COOKOPS_GOOGLE_CLIENT_ID=$client_id" \
        cookops-web-image-test)
    cleanup() {
        docker rm --force "$container_id" >/dev/null 2>&1 || true
    }
    trap cleanup EXIT HUP INT TERM
    port=$(docker port "$container_id" 8080/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')
    test -n "$port"
    runtime_config=
    for attempt in 1 2 3 4 5; do
        runtime_config=$(curl --silent "http://127.0.0.1:$port/runtime-config.js" || true)
        [ "$runtime_config" = "$expected" ] && break
        sleep 1
    done
    test "$runtime_config" = "$expected"
)

assert_runtime_config dummy \
    'window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };'
assert_runtime_config google \
    'window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "google", googleClientId: "example.apps.googleusercontent.com" } };' \
    example.apps.googleusercontent.com

docker run --rm --entrypoint=node cookops-oauth-image-test --version | grep -q '^v22\.'
docker run --rm --entrypoint=pg_dump cookops-api-image-test --version | grep -Eq '^pg_dump \(PostgreSQL\) 18\.'
docker run --rm --entrypoint=pg_restore cookops-api-image-test --version | grep -Eq '^pg_restore \(PostgreSQL\) 18\.'

docker compose --env-file "$root/deploy/.env.example" -f "$root/deploy/compose.yaml" config >/dev/null
for service in backup restore; do
    docker compose --profile operations --env-file "$root/deploy/.env.example" -f "$root/deploy/compose.yaml" \
        config --services | grep -qx "$service"
    test "$(docker compose --profile operations --env-file "$root/deploy/.env.example" -f "$root/deploy/compose.yaml" \
        config --format json | python -c "import json, sys; print(json.load(sys.stdin)['services']['$service'].get('ports', []))")" = '[]'
done
docker compose --profile operations --env-file "$root/deploy/.env.example" -f "$root/deploy/compose.yaml" \
    config --format json | python -c 'import json, sys; s=json.load(sys.stdin)["services"]; assert all(s[n].get("user") == "0:0" for n in ("backup", "restore")); assert all(all(k in s[n]["environment"] for k in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")) for n in ("backup", "restore")); assert "/operator-scripts" in str(s["backup"]["volumes"]); assert "/operator-scripts" in str(s["restore"]["volumes"]); assert "/var/lib/cookops/receipts" in str(s["backup"]["volumes"]); assert "/var/lib/cookops/backups" in str(s["backup"]["volumes"]); assert "/var/lib/cookops/backups" in str(s["restore"]["volumes"]); assert "backup_archive.py" in str(s["backup"]["command"]); assert "restore_archive.py" in str(s["restore"]["command"]); assert "verify_media_references.py" in str(s["restore"]["command"]); assert "--database-name" in str(s["backup"]["command"]); assert "--database-name" in str(s["restore"]["command"]); assert "--clean-database" in str(s["restore"]["command"]); assert "--allow-nonempty" not in str(s["restore"]["command"]); restore_command=s["restore"]["command"]; assert restore_command[:2] == ["/bin/sh", "-c"]; body=restore_command[2]; assert body.startswith("python /operator-scripts/restore_archive.py"); assert body.index("restore_archive.py") < body.index("alembic upgrade head") < body.index("verify_media_references.py"); assert "PGPASSWORD=\"$${COOKOPS_API_DB_PASSWORD}\"" in body; assert "COOKOPS_DATABASE_URL=\"postgresql+psycopg://cookops_api@postgres:5432/$${PGDATABASE}\"" in body; assert "COOKOPS_API_DB_PASSWORD" in s["restore"]["environment"]; assert "COOKOPS_API_DB_PASSWORD" not in body.replace("$${COOKOPS_API_DB_PASSWORD}", "")'
"$root/deploy/test-postgres-roles.sh"
"$root/deploy/test-backup-restore.sh"

prohibited_proxy_route() {
    awk '
        $1 ~ /^ProxyPass(Match|Reverse)?$/ {
            route = $2
            sub(/^\^/, "", route)
            sub(/\(\?:\/\|\$\)$/, "", route)
            if (route ~ /^\/mcp(\/|$)/ || route ~ /^\/auth\/mcp-grants(\/|$)/) {
                prohibited = 1
                exit
            }
        }
        END { exit prohibited ? 0 : 1 }
    ' "$@"
}
for proxy_rule in \
    'ProxyPass /mcp http://127.0.0.1:8000/' \
    'ProxyPassMatch ^/mcp http://127.0.0.1:8000/' \
    'ProxyPassMatch ^/mcp(?:/|$) http://127.0.0.1:8000/' \
    'ProxyPassMatch ^/auth/mcp-grants(?:/|$) http://127.0.0.1:8000/'; do
    if ! printf '%s\n' "$proxy_rule" | prohibited_proxy_route; then
        echo "OAuth/MCP proxy guard failed to recognize $proxy_rule" >&2
        exit 1
    fi
done
if printf '%s\n' 'ProxyPassMatch ^/mcpack(?:/|$) http://127.0.0.1:8000/' | prohibited_proxy_route; then
    echo 'OAuth/MCP proxy guard incorrectly rejected a near-miss route' >&2
    exit 1
fi
if printf '%s\n' 'ProxyPass /api/ http://oauth-server:3000/health/' | prohibited_proxy_route; then
    echo 'OAuth/MCP proxy guard incorrectly inspected an upstream target' >&2
    exit 1
fi
if ! awk '$1 == "ProxyPass" && $2 == "/mcp" && $3 == "http://127.0.0.1:8000/mcp" { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/cookops.conf.example"; then
    echo 'MCP must proxy exactly to the authenticated API resource' >&2
    exit 1
fi
if ! awk '$1 == "ProxyPass" && $2 == "/.well-known/oauth-protected-resource/mcp" && $3 == "http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp" { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/cookops.conf.example"; then
    echo 'MCP protected-resource metadata must proxy to the API' >&2
    exit 1
fi
if ! awk '$1 == "ProxyPass" && $2 == "/oauth/" && $3 == "http://127.0.0.1:3000/oauth/" { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/cookops.conf.example"; then
    echo 'OAuth must proxy only its public source path to loopback oauth-server' >&2
    exit 1
fi
if ! awk '$1 == "ProxyPass" && $2 == "/oauth/private" && $3 == "!" { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/cookops.conf.example"; then
    echo 'OAuth private bridge must be excluded before the public OAuth proxy' >&2
    exit 1
fi
for apache_config in cookops.conf.example oauth-consent-smoke.conf; do
    if ! awk '$1 == "ProxyPass" && $2 == "/mcp" && $3 ~ /\/mcp$/ { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/$apache_config"; then
        echo "MCP resource proxy is missing: $apache_config" >&2
        exit 1
    fi
    if ! awk '$1 == "ProxyPass" && $2 == "/.well-known/oauth-protected-resource/mcp" && $3 ~ /oauth-protected-resource\/mcp$/ { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/$apache_config"; then
        echo "MCP protected-resource metadata proxy is missing: $apache_config" >&2
        exit 1
    fi
    if ! awk '$1 == "ProxyPass" && $2 == "/auth/mcp-interactions/" && $3 ~ /\/auth\/mcp-interactions\/$/ { found = NR } $1 == "ProxyPass" && $2 == "/auth/" { ordinary = NR } END { exit found && ordinary && found < ordinary ? 0 : 1 }' "$root/deploy/apache/$apache_config"; then
        echo "MCP consent UI must proxy to the API before ordinary auth proxy: $apache_config" >&2
        exit 1
    fi
    if ! awk '$1 == "ProxyPass" && $2 == "/auth/mcp-grants" && $3 == "!" { excluded = NR } $1 == "ProxyPass" && $2 == "/auth/" { ordinary = NR } END { exit excluded && ordinary && excluded < ordinary ? 0 : 1 }' "$root/deploy/apache/$apache_config"; then
        echo "MCP grants route must stay excluded before ordinary auth proxy: $apache_config" >&2
        exit 1
    fi
done
for discovery in \
    '/.well-known/openid-configuration/oauth' \
    '/.well-known/oauth-authorization-server/oauth'; do
    if ! awk -v path="$discovery" '$1 == "ProxyPass" && $2 == path && $3 == "http://127.0.0.1:3000" path { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/cookops.conf.example"; then
        echo "OAuth discovery proxy is missing or not loopback: $discovery" >&2
        exit 1
    fi
done
if ! awk '$1 == "ProxyPass" && $2 == "/api/v1/sync/hints" && $3 == "http://127.0.0.1:8000/api/v1/sync/hints" && $4 == "upgrade=websocket" { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/cookops.conf.example"; then
    echo 'Sync hints WebSocket proxy is missing or not loopback' >&2
    exit 1
fi
if ! awk '$1 == "ProxyPassReverse" && $2 == "/api/v1/sync/hints" && $3 == "http://127.0.0.1:8000/api/v1/sync/hints" { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/cookops.conf.example"; then
    echo 'Sync hints WebSocket reverse proxy is missing' >&2
    exit 1
fi
if awk '$1 ~ /^ProxyPass(Reverse)?$/ && $2 == "/api/v1/sync/notifications" { found = 1 } END { exit found ? 0 : 1 }' "$root/deploy/apache/cookops.conf.example"; then
    echo 'Stale sync notifications WebSocket route must not be proxied' >&2
    exit 1
fi

if docker run --rm --env 'COOKOPS_TRUSTED_PROXY_IPS=*' cookops-api-image-test; then
    echo 'wildcard proxy trust unexpectedly accepted' >&2
    exit 1
fi

container_id=$(docker run --detach --publish 127.0.0.1::8080 cookops-web-image-test)
cleanup() {
    docker rm --force "$container_id" >/dev/null
}
trap cleanup EXIT
port=$(docker port "$container_id" 8080/tcp | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')
test -n "$port"
for attempt in 1 2 3 4 5; do
    if curl --fail --silent --show-error "http://127.0.0.1:$port/health/live" | grep -qx 'ok'; then
        break
    fi
    sleep 1
done
curl --fail --silent --show-error "http://127.0.0.1:$port/health/live" | grep -qx 'ok'
curl --fail --silent --show-error "http://127.0.0.1:$port/not-a-file" | grep -q '<div id="root"></div>'
