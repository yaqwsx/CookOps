# Deployment foundation

Copy `.env.example` to `.env`, replace every placeholder, then run:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build
```

All published service ports bind to loopback; the host Apache virtual host remains
the only public entry point. Apache forwards the OAuth protocol path to its
loopback provider for browser interaction completion. It deliberately does **not**
mount MCP: FastAPI still needs an RFC 7662 verifier before that path may be added.

The bootstrap PostgreSQL user is used only while the empty volume is initialized.
The API uses `cookops_api` and the OAuth provider uses `cookops_oauth` in its own
`oauth` schema; set both URL-safe application passwords independently.

## Operator backup and restore

Set the operator-only `COOKOPS_BACKUP_DIR`, `COOKOPS_BACKUP_ARCHIVE`,
`COOKOPS_APPLICATION_REVISION`, `COOKOPS_SCHEMA_VERSION`, and `COOKOPS_RESTORE_DIR`
values in `.env`. Keep both host directories private and outside the web root.
They must be owned and permissioned for the operator workflow (for example,
`0700`); the profiled services run as root only to write those private bind
mounts and are not application services.
The backup directory is an explicit host bind mount; copy the resulting archive
off the VPS after creation.

Create a backup with the one-off service:

```sh
docker compose --profile operations --env-file deploy/.env -f deploy/compose.yaml \
  run --rm backup
```

Restore into the new subdirectory named by `COOKOPS_RESTORE_MEDIA_SUBDIR`:

```sh
docker compose --profile operations --env-file deploy/.env -f deploy/compose.yaml \
  run --rm restore
```

The restore command deliberately does not pass `--allow-nonempty`; it refuses
to replace existing media unless an operator explicitly authorizes that action.
It does pass `--clean-database`, so it replaces database objects in the selected
database. Before starting a full `pg_restore --clean`, stop the API, the OAuth
provider, and every other writer to the database. The one-off restore then runs
the existing API Alembic migrations as `cookops_api` against the restored public
schema, and finally verifies every READY receipt attachment's object size,
SHA-256, thumbnail, and safe media path before reporting success. The full
`pg_restore` includes the `oauth` schema and its provider data; only the OAuth
provider's migration workflow is separate from this command. Run that workflow
after the restore and before resuming traffic. Complete the full restore
sequence before switching application storage to the new media directory.
Neither service publishes a port or exposes a web/API/MCP route.

After restore, run the OAuth provider migration explicitly before resuming traffic:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml \
  run --rm oauth-migrate
```
