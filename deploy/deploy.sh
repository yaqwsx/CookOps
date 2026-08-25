#!/bin/sh
set -eu

usage() {
    echo "usage: $0 [--dry-run] REVISION" >&2
    exit 2
}

dry_run=false
if [ "${1:-}" = "--dry-run" ]; then
    dry_run=true
    shift
fi
[ "$#" -eq 1 ] || usage
revision=$1

application_dir=${COOKOPS_APPLICATION_DIR:-}
compose_command=${COOKOPS_COMPOSE_COMMAND:-docker}
public_health_url=${COOKOPS_PUBLIC_HEALTH_URL:-}
git_remote=${COOKOPS_GIT_REMOTE:-origin}
project_name=${COOKOPS_COMPOSE_PROJECT:-cookops}
timeout=${COOKOPS_HEALTH_TIMEOUT_SECONDS:-120}

[ -n "$application_dir" ] || { echo "COOKOPS_APPLICATION_DIR is required" >&2; exit 2; }
[ -n "$compose_command" ] || { echo "COOKOPS_COMPOSE_COMMAND must name the Docker-compatible CLI" >&2; exit 2; }
[ -n "$public_health_url" ] || { echo "COOKOPS_PUBLIC_HEALTH_URL is required" >&2; exit 2; }
case "$project_name" in *[!A-Za-z0-9_.-]*|'') echo "invalid COOKOPS_COMPOSE_PROJECT" >&2; exit 2;; esac
case "$timeout" in *[!0-9]*|'') echo "invalid COOKOPS_HEALTH_TIMEOUT_SECONDS" >&2; exit 2;; esac
case "$git_remote" in -*) echo "invalid COOKOPS_GIT_REMOTE" >&2; exit 2;; esac
case "$public_health_url" in https://?*) ;; *) echo "COOKOPS_PUBLIC_HEALTH_URL must be an HTTPS URL" >&2; exit 2;; esac

git -C "$application_dir" rev-parse --is-inside-work-tree 2>/dev/null | grep -qx true || {
    echo "application directory is not a Git worktree: $application_dir" >&2
    exit 1
}
[ -z "$(git -C "$application_dir" status --porcelain)" ] || { echo "refusing dirty application worktree" >&2; exit 1; }

if ! git check-ref-format --allow-onelevel "$revision" >/dev/null 2>&1; then
    case "$revision" in [0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]*) ;; *) echo "invalid Git revision" >&2; exit 2;; esac
fi

fetch_ref=
override=
worktree=
cleanup() {
    [ -z "$fetch_ref" ] || git -C "$application_dir" update-ref -d "$fetch_ref" >/dev/null 2>&1 || true
    [ -z "$override" ] || rm -f "$override"
    if [ -n "$worktree" ] && git -C "$worktree" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "$application_dir" worktree remove --force "$worktree" >/dev/null 2>&1 || true
    fi
    [ -z "$worktree" ] || rmdir "$worktree" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

if [ "$dry_run" = false ]; then
    fetch_token_file=$(mktemp "${TMPDIR:-/tmp}/cookops-fetch.XXXXXX")
    fetch_token=${fetch_token_file##*/}
    rm -f "$fetch_token_file"
    fetch_ref="refs/cookops-deploy/$fetch_token"
    git -C "$application_dir" fetch --no-tags "$git_remote" "$revision:$fetch_ref"
    resolved=$(git -C "$application_dir" rev-parse --verify "$fetch_ref^{commit}" 2>/dev/null) || {
        echo "fetched revision does not resolve to a commit: $revision" >&2
        exit 1
    }
else
    resolved=$(git -C "$application_dir" rev-parse --verify "$revision^{commit}" 2>/dev/null) || {
        echo "revision does not resolve to a commit: $revision" >&2
        exit 1
    }
fi

override=$(mktemp "${TMPDIR:-/tmp}/cookops-deploy.XXXXXX.yaml")
worktree=$(mktemp -d "${TMPDIR:-/tmp}/cookops-release.XXXXXX")

if [ "$dry_run" = false ]; then
    rmdir "$worktree"
fi
git -C "$application_dir" worktree add --detach "$worktree" "$resolved" >/dev/null
compose_file=$worktree/deploy/compose.yaml
[ -f "$compose_file" ] || { echo "missing deploy/compose.yaml at selected source" >&2; exit 1; }
if [ "$dry_run" = true ]; then
    echo "DRY RUN: would deploy $resolved"
    echo "DRY RUN: would fetch $git_remote $revision"
    echo "DRY RUN: would build immutable images tagged $resolved"
    echo "DRY RUN: would run api-migrate and oauth-migrate"
    echo "DRY RUN: would recreate web, api, and oauth-server"
    echo "DRY RUN: would wait for loopback and $public_health_url"
    exit 0
fi

cat >"$override" <<EOF
services:
  web:
    image: cookops/web:$resolved
  api:
    image: cookops/api:$resolved
  api-migrate:
    image: cookops/api:$resolved
  backup:
    image: cookops/api:$resolved
  restore:
    image: cookops/api:$resolved
  oauth-server:
    image: cookops/oauth:$resolved
  oauth-migrate:
    image: cookops/oauth:$resolved
EOF

compose() { "$compose_command" compose --project-name "$project_name" --env-file "$application_dir/deploy/.env" -f "$worktree/deploy/compose.yaml" -f "$override" "$@"; }
[ -f "$application_dir/deploy/.env" ] || { echo "missing deploy/.env" >&2; exit 1; }
compose build --pull web api oauth-server
compose up --detach postgres
compose run --rm api-migrate
compose run --rm oauth-migrate
compose up --detach --force-recreate web api oauth-server

wait_for() {
    url=$1
    deadline=$(($(date +%s) + timeout))
    while :; do
        if curl --fail --silent --show-error --connect-timeout 5 --max-time 10 "$url" >/dev/null 2>&1; then return 0; fi
        [ "$(date +%s)" -lt "$deadline" ] || { echo "health check timed out: $url" >&2; return 1; }
        sleep 2
    done
}
web_port=${COOKOPS_WEB_PORT:-8080}
api_port=${COOKOPS_API_PORT:-8000}
oauth_port=${COOKOPS_OAUTH_PORT:-3000}
wait_for "http://127.0.0.1:$web_port/health/live"
wait_for "http://127.0.0.1:$api_port/health/ready"
wait_for "http://127.0.0.1:$oauth_port/health/ready"
wait_for "$public_health_url"
echo "deployed revision $resolved"
