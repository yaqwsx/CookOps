#!/bin/sh
set -eu

: "${COOKOPS_API_DB_PASSWORD:?COOKOPS_API_DB_PASSWORD is required}"
: "${OAUTH_DB_PASSWORD:?OAUTH_DB_PASSWORD is required}"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --set api_password="$COOKOPS_API_DB_PASSWORD" \
  --set oauth_password="$OAUTH_DB_PASSWORD" <<'SQL'
CREATE ROLE cookops_api LOGIN PASSWORD :'api_password';
CREATE ROLE cookops_oauth LOGIN PASSWORD :'oauth_password';
CREATE EXTENSION IF NOT EXISTS btree_gist;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO cookops_api;
GRANT USAGE, CREATE ON SCHEMA public TO cookops_api;
CREATE SCHEMA oauth AUTHORIZATION cookops_oauth;
GRANT CONNECT ON DATABASE :"DBNAME" TO cookops_api, cookops_oauth;
SQL
