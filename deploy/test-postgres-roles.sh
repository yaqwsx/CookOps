#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
project="cookops_roles_test_$$"
cleanup() {
    docker compose --project-name "$project" --env-file "$root/deploy/.env.example" \
        -f "$root/deploy/compose.yaml" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose --project-name "$project" --env-file "$root/deploy/.env.example" \
    -f "$root/deploy/compose.yaml" up --detach postgres >/dev/null
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if docker compose --project-name "$project" --env-file "$root/deploy/.env.example" \
        -f "$root/deploy/compose.yaml" exec -T postgres pg_isready -U cookops -d cookops >/dev/null; then
        break
    fi
    sleep 1
done
docker compose --project-name "$project" --env-file "$root/deploy/.env.example" \
    -f "$root/deploy/compose.yaml" exec -T postgres pg_isready -U cookops -d cookops >/dev/null

docker compose --project-name "$project" --env-file "$root/deploy/.env.example" \
    -f "$root/deploy/compose.yaml" exec -T -e PGPASSWORD=replace-with-url-safe-api-db-password \
    postgres psql -v ON_ERROR_STOP=1 -U cookops_api -d cookops \
    -c 'CREATE TABLE public.api_role_probe (id integer); DROP TABLE public.api_role_probe;' >/dev/null
docker compose --project-name "$project" --env-file "$root/deploy/.env.example" \
    -f "$root/deploy/compose.yaml" exec -T -e PGPASSWORD=replace-with-url-safe-oauth-db-password \
    -e 'PGOPTIONS=-c search_path=oauth' postgres psql -v ON_ERROR_STOP=1 -U cookops_oauth -d cookops \
    -c 'CREATE TABLE oauth_role_probe (id integer); DROP TABLE oauth_role_probe;' >/dev/null
if docker compose --project-name "$project" --env-file "$root/deploy/.env.example" \
    -f "$root/deploy/compose.yaml" exec -T -e PGPASSWORD=replace-with-url-safe-oauth-db-password \
    postgres psql -v ON_ERROR_STOP=1 -U cookops_oauth -d cookops \
    -c 'CREATE TABLE public.oauth_role_probe (id integer);' >/dev/null 2>&1; then
    echo 'OAuth role can unexpectedly create public tables' >&2
    exit 1
fi
