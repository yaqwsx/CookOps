#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
project="cookops-oauth-smoke-$$"
temporary=$(mktemp -d)
compose="docker compose --project-name $project --env-file $temporary/.env -f $root/deploy/compose.yaml -f $root/deploy/compose.oauth-consent-smoke.yaml"

free_port() {
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

api_port=$(free_port)
oauth_port=$(free_port)
public_port=$(free_port)
origin="https://127.0.0.1:$public_port"
cleanup() {
    result=$?
    test "$result" -eq 0 || sh -c "$compose logs" >&2 || true
    sh -c "$compose down --volumes --remove-orphans" >/dev/null 2>&1 || true
    rm -rf "$temporary"
    exit "$result"
}
trap cleanup EXIT INT TERM

secret() { node -e 'process.stdout.write(require("node:crypto").randomBytes(32).toString("base64url"))'; }
jwks=$(node -e 'const c=require("node:crypto"); const pair=c.generateKeyPairSync("ec",{namedCurve:"P-256"}); process.stdout.write(JSON.stringify({keys:[pair.privateKey.export({format:"jwk"})]}))')
{
    printf '%s\n' 'POSTGRES_DB=cookops'
    printf '%s\n' 'POSTGRES_USER=smoke'
    printf '%s\n' 'POSTGRES_PASSWORD=smoke-postgres-password'
    printf '%s\n' 'COOKOPS_API_DB_PASSWORD=smoke-api-password'
    printf '%s\n' 'OAUTH_DB_PASSWORD=smoke-oauth-password'
    printf '%s\n' 'COOKOPS_GOOGLE_CLIENT_ID=not-used-by-dummy-smoke'
    printf 'COOKOPS_BROWSER_SESSION_HMAC_KEY=%s\n' "$(secret)"
    printf '%s\n' 'COOKOPS_TRUSTED_PROXY_IPS=127.0.0.1'
    printf 'OAUTH_ISSUER=%s/oauth\nMCP_RESOURCE=%s/mcp\nOAUTH_INTERACTION_URL=%s/auth/mcp-interactions\nCOOKOPS_BROWSER_ORIGIN=%s\nCOOKOPS_PUBLIC_ORIGIN=%s\n' "$origin" "$origin" "$origin" "$origin" "$origin"
    printf '%s\n' 'OAUTH_COOKIE_KEYS=smoke-cookie-key-one-at-least-thirty-two,smoke-cookie-key-two-at-least-thirty-two'
    printf '%s\n' 'OAUTH_RESOURCE_SERVER_SECRET=smoke-resource-server-secret-at-least-32'
    printf 'OAUTH_JWKS=%s\n' "$jwks"
    printf 'OAUTH_ADAPTER_SECRET_BASE64URL=%s\n' "$(secret)"
    printf 'OAUTH_INTERACTION_APPROVAL_SECRET_BASE64URL=%s\n' "$(secret)"
    printf 'OAUTH_APPROVAL_API_CREDENTIAL_BASE64URL=%s\n' "$(secret)"
    printf 'OAUTH_INTERACTION_DETAILS_API_CREDENTIAL_BASE64URL=%s\n' "$(secret)"
    printf 'COOKOPS_API_PORT=%s\nCOOKOPS_OAUTH_PORT=%s\nCOOKOPS_EDGE_PORT=%s\nSMOKE_TMP=%s\n' "$api_port" "$oauth_port" "$public_port" "$temporary"
} >"$temporary/.env"

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=127.0.0.1' \
    -keyout "$temporary/key.pem" -out "$temporary/certificate.pem" >/dev/null 2>&1
sh -c "$compose up --build --detach postgres api oauth-server edge"

wait_for() {
    url=$1
    attempt=0
    until curl --fail --silent --show-error "$url" >/dev/null; do
        attempt=$((attempt + 1))
        test "$attempt" -lt 60 || { sh -c "$compose logs" >&2; exit 1; }
        sleep 1
    done
}
wait_for "http://127.0.0.1:$api_port/health/ready"
wait_for "http://127.0.0.1:$oauth_port/health/ready"
sh -c "$compose run --rm --no-deps api python -m cookops.development_seed" >/dev/null
attempt=0
until curl --insecure --fail --silent --show-error "$origin/auth/dummy/identities" >/dev/null; do
    attempt=$((attempt + 1))
    test "$attempt" -lt 20 || exit 1
    sleep 1
done
test "$(curl --insecure --silent --output /dev/null --write-out '%{http_code}' "$origin/mcp")" = 404
test "$(curl --insecure --silent --output /dev/null --write-out '%{http_code}' "$origin/mcp/probe")" = 404
test "$(curl --insecure --silent --output /dev/null --write-out '%{http_code}' "$origin/oauth/private/interactions/approval")" = 404
test "$(curl --insecure --silent --output /dev/null --write-out '%{http_code}' "$origin/oauth/private/interactions/AAAAAAAAAAAAAAAA")" = 404
for discovery_path in /.well-known/openid-configuration/oauth /.well-known/oauth-authorization-server/oauth; do
    metadata=$(curl --insecure --fail --silent --show-error "$origin$discovery_path")
    OAUTH_SMOKE_METADATA="$metadata" node -e 'process.exit(JSON.parse(process.env.OAUTH_SMOKE_METADATA).issuer === process.argv[1] ? 0 : 1)' "$origin/oauth"
done
COOKOPS_OAUTH_SMOKE_ORIGIN="$origin" npx --prefix "$root/frontend" playwright test \
    -c "$root/frontend/playwright.oauth-consent-smoke.config.ts"
