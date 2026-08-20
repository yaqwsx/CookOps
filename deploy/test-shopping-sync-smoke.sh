#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
project="cookops-shopping-sync-$$"
temporary=$(mktemp -d)
compose="docker compose --project-name $project --env-file $temporary/.env -f $root/deploy/compose.yaml -f $root/deploy/compose.shopping-sync-smoke.yaml"
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }
secret() { node -e 'process.stdout.write(require("node:crypto").randomBytes(32).toString("base64url"))'; }
api_port=$(free_port); web_port=$(free_port); edge_port=$(free_port)
origin="https://127.0.0.1:$edge_port"
cleanup() {
  result=$?
  test "$result" -eq 0 || sh -c "$compose logs" >&2 || true
  sh -c "$compose down --volumes --remove-orphans" >/dev/null 2>&1 || true
  rm -rf "$temporary"
  exit "$result"
}
trap cleanup EXIT INT TERM
jwks=$(node -e 'const c=require("node:crypto"); const p=c.generateKeyPairSync("ec",{namedCurve:"P-256"}); process.stdout.write(JSON.stringify({keys:[p.privateKey.export({format:"jwk"})]}))')
{
  printf '%s\n' 'POSTGRES_DB=cookops' 'POSTGRES_USER=smoke' 'POSTGRES_PASSWORD=smoke-postgres-password'
  printf '%s\n' 'COOKOPS_APPLICATION_REVISION=shopping-sync-smoke' 'COOKOPS_SCHEMA_VERSION=1'
  printf '%s\n' 'COOKOPS_API_DB_PASSWORD=smoke-api-password' 'OAUTH_DB_PASSWORD=smoke-oauth-password' 'COOKOPS_GOOGLE_CLIENT_ID=unused'
  printf 'COOKOPS_BROWSER_SESSION_HMAC_KEY=%s\n' "$(secret)"
  printf '%s\n' 'COOKOPS_TRUSTED_PROXY_IPS=127.0.0.1'
  printf 'OAUTH_ISSUER=%s/oauth\nMCP_RESOURCE=%s/mcp\nOAUTH_INTERACTION_URL=%s/auth/mcp-interactions\nCOOKOPS_BROWSER_ORIGIN=%s\nCOOKOPS_PUBLIC_ORIGIN=%s\n' "$origin" "$origin" "$origin" "$origin" "$origin"
  printf '%s\n' 'OAUTH_COOKIE_KEYS=smoke-cookie-key-one-at-least-thirty-two,smoke-cookie-key-two-at-least-thirty-two' 'OAUTH_RESOURCE_SERVER_SECRET=smoke-resource-server-secret-at-least-32'
  printf 'OAUTH_JWKS=%s\n' "$jwks"
  printf 'OAUTH_ADAPTER_SECRET_BASE64URL=%s\nOAUTH_INTERACTION_APPROVAL_SECRET_BASE64URL=%s\nOAUTH_APPROVAL_API_CREDENTIAL_BASE64URL=%s\nOAUTH_INTERACTION_DETAILS_API_CREDENTIAL_BASE64URL=%s\nOAUTH_GRANTS_API_CREDENTIAL_BASE64URL=%s\n' "$(secret)" "$(secret)" "$(secret)" "$(secret)" "$(secret)"
  printf 'COOKOPS_API_PORT=%s\nCOOKOPS_WEB_PORT=%s\nCOOKOPS_EDGE_PORT=%s\nCOOKOPS_BACKUP_DIR=%s/backups\nCOOKOPS_BACKUP_ARCHIVE=%s/backups/archive.tar.zst\nCOOKOPS_RESTORE_DIR=%s/restore\nCOOKOPS_RESTORE_MEDIA_SUBDIR=media\nSMOKE_TMP=%s\n' "$api_port" "$web_port" "$edge_port" "$temporary" "$temporary" "$temporary" "$temporary"
} >"$temporary/.env"
mkdir "$temporary/backups"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=127.0.0.1' -keyout "$temporary/key.pem" -out "$temporary/certificate.pem" >/dev/null 2>&1
sh -c "$compose up --build --detach postgres api web edge"
wait_for() { url=$1; attempt=0; until curl --insecure --fail --silent --show-error "$url" >/dev/null; do attempt=$((attempt + 1)); test "$attempt" -lt 60 || { sh -c "$compose logs" >&2; exit 1; }; sleep 1; done; }
wait_for "http://127.0.0.1:$api_port/health/ready"
wait_for "http://127.0.0.1:$web_port/health/live"
sh -c "$compose run --rm --no-deps api python -m cookops.development_seed" >/dev/null
wait_for "$origin/auth/dummy/identities"
COOKOPS_SHOPPING_SYNC_ORIGIN="$origin" npx --prefix "$root/frontend" playwright test -c "$root/frontend/playwright.shopping-sync.config.ts"
