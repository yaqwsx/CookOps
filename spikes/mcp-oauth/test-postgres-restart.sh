#!/usr/bin/env bash
set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
compose_file="${script_directory}/compose.yaml"
project="cookops-oauth-restart-${PPID}-${BASHPID}"
compose=(docker compose --project-name "${project}" --file "${compose_file}")

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${compose[@]}" \
  up --detach --wait --wait-timeout 60 postgres

"${compose[@]}" \
  run --build --rm oauth-test \
  node --import tsx src/postgres-restart-probe.ts write

postgres_container="$("${compose[@]}" ps --quiet postgres)"
if [[ -z "${postgres_container}" ]]; then
  echo "PostgreSQL container was not found" >&2
  exit 1
fi
started_before="$(docker inspect --format '{{.State.StartedAt}}' "${postgres_container}")"

echo "restarting PostgreSQL container ${postgres_container}"
"${compose[@]}" restart postgres
"${compose[@]}" \
  up --detach --wait --wait-timeout 60 postgres

started_after="$(docker inspect --format '{{.State.StartedAt}}' "${postgres_container}")"
if [[ -z "${started_before}" || -z "${started_after}" || "${started_before}" == "${started_after}" ]]; then
  echo "PostgreSQL container start time did not change across restart" >&2
  exit 1
fi

"${compose[@]}" run --rm oauth-test \
  node --import tsx src/postgres-restart-probe.ts read

echo "PostgreSQL restart persistence proof passed"
