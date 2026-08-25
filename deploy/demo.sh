#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
project="cookops-demo-$$"
temporary=$(mktemp -d)
compose() {
  docker compose --project-name "$project" --env-file "$temporary/.env" \
    -f "$root/deploy/compose.yaml" -f "$root/deploy/compose.shopping-sync-smoke.yaml" "$@"
}

free_port() {
  node -e 'const net=require("node:net"); const s=net.createServer(); s.listen(0,"127.0.0.1",()=>{console.log(s.address().port);s.close()})'
}
secret() { node -e 'process.stdout.write(require("node:crypto").randomBytes(32).toString("base64url"))'; }

api_port=$(free_port)
web_port=$(free_port)
edge_port=$(free_port)
origin="https://127.0.0.1:$edge_port"

cleanup() {
  result=$?
  if test "$result" -ne 0; then compose logs >&2 || true; fi
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$temporary"
  exit "$result"
}
trap cleanup EXIT INT TERM

jwks=$(node -e 'const c=require("node:crypto"); const p=c.generateKeyPairSync("ec",{namedCurve:"P-256"}); process.stdout.write(JSON.stringify({keys:[p.privateKey.export({format:"jwk"})]}))')
{
  printf '%s\n' 'POSTGRES_DB=cookops' 'POSTGRES_USER=demo' 'POSTGRES_PASSWORD=demo-postgres-password'
  printf '%s\n' 'COOKOPS_APPLICATION_REVISION=local-demo' 'COOKOPS_SCHEMA_VERSION=1'
  printf '%s\n' 'COOKOPS_API_DB_PASSWORD=demo-api-password' 'OAUTH_DB_PASSWORD=demo-oauth-password' 'COOKOPS_GOOGLE_CLIENT_ID=unused'
  printf 'COOKOPS_BROWSER_SESSION_HMAC_KEY=%s\n' "$(secret)"
  printf '%s\n' 'COOKOPS_TRUSTED_PROXY_IPS=127.0.0.1'
  printf 'OAUTH_ISSUER=%s/oauth\nMCP_RESOURCE=%s/mcp\nOAUTH_INTERACTION_URL=%s/auth/mcp-interactions\nCOOKOPS_BROWSER_ORIGIN=%s\nCOOKOPS_PUBLIC_ORIGIN=%s\n' "$origin" "$origin" "$origin" "$origin" "$origin"
  printf '%s\n' 'OAUTH_COOKIE_KEYS=demo-cookie-key-one-at-least-thirty-two,demo-cookie-key-two-at-least-thirty-two' 'OAUTH_RESOURCE_SERVER_SECRET=demo-resource-server-secret-at-least-32'
  printf 'OAUTH_JWKS=%s\n' "$jwks"
  printf 'OAUTH_ADAPTER_SECRET_BASE64URL=%s\nOAUTH_INTERACTION_APPROVAL_SECRET_BASE64URL=%s\nOAUTH_APPROVAL_API_CREDENTIAL_BASE64URL=%s\nOAUTH_INTERACTION_DETAILS_API_CREDENTIAL_BASE64URL=%s\nOAUTH_GRANTS_API_CREDENTIAL_BASE64URL=%s\n' "$(secret)" "$(secret)" "$(secret)" "$(secret)" "$(secret)"
  printf 'COOKOPS_API_PORT=%s\nCOOKOPS_WEB_PORT=%s\nCOOKOPS_EDGE_PORT=%s\nCOOKOPS_BACKUP_DIR=%s/backups\nCOOKOPS_BACKUP_ARCHIVE=archive.tar.zst\nCOOKOPS_RESTORE_DIR=%s/restore\nCOOKOPS_RESTORE_MEDIA_SUBDIR=media\nSMOKE_TMP=%s\n' "$api_port" "$web_port" "$edge_port" "$temporary" "$temporary" "$temporary"
} >"$temporary/.env"
mkdir "$temporary/backups" "$temporary/restore"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=127.0.0.1' -keyout "$temporary/key.pem" -out "$temporary/certificate.pem" >/dev/null 2>&1

compose up --build --detach postgres api web edge
wait_for() {
  url=$1
  attempt=0
  until curl --insecure --fail --silent --show-error "$url" >/dev/null; do
    attempt=$((attempt + 1))
    test "$attempt" -lt 60 || { compose logs >&2; exit 1; }
    sleep 1
  done
}
wait_for "http://127.0.0.1:$api_port/health/ready"
wait_for "$origin/auth/dummy/identities"
compose run --rm --no-deps api python -m cookops.development_seed >/dev/null

printf '\nCookOps demo is running at %s\n' "$origin"
printf '%s\n' 'Accept the self-signed certificate warning, then choose a persona:'
printf '%s\n' '  dummy-system-admin' '  dummy-organization-admin' '  dummy-member' '  dummy-multi-organization-member' '  dummy-no-access'
printf '%s\n' 'Press Ctrl-C to stop and remove this demo stack.\n'
while :; do sleep 3600; done
