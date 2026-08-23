# Deployment foundation

## Deploy a selected Git revision

Run the workflow from a clean checkout. It fetches and verifies the requested
revision into a temporary worktree, validates that selected worktree's
deployment configuration, tags the built images with its full commit hash,
runs both migration services, recreates the application services, and
waits for loopback and Apache health checks. It never checks out or resets the
operator's worktree.

Set `COOKOPS_APPLICATION_DIR` to the checkout, and optionally set
`COOKOPS_COMPOSE_COMMAND` to a Docker-compatible CLI (default: `docker`),
`COOKOPS_GIT_REMOTE` (default: `origin`), `COOKOPS_COMPOSE_PROJECT`, and the
loopback port variables from `.env`. `COOKOPS_PUBLIC_HEALTH_URL` must be an
HTTPS public Apache URL, for example
`https://cookops.example/health/ready`. The operator checkout's `deploy/.env`
is deliberately used for credentials and host configuration; `deploy/compose.yaml`
is always read from the selected detached revision.

```sh
COOKOPS_APPLICATION_DIR=/srv/cookops \
COOKOPS_PUBLIC_HEALTH_URL=https://cookops.example/health/ready \
deploy/deploy.sh v1.2.3
```

Validate command construction without fetching, building, or changing services:

```sh
COOKOPS_APPLICATION_DIR=/srv/cookops \
COOKOPS_PUBLIC_HEALTH_URL=https://cookops.example/health/ready \
deploy/deploy.sh --dry-run HEAD
```

The script refuses dirty worktrees, unresolved revisions, missing deployment
configuration, and failed health checks. Run `deploy/test-deploy-script.sh` for
the non-Docker dry-run contract.

Copy `.env.example` to `.env`, replace every placeholder, then run:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build
```

All published service ports bind to loopback; the host Apache virtual host remains
the only public entry point. Apache forwards the OAuth protocol path to its
loopback provider for browser interaction completion and forwards `/mcp` to the
authenticated FastAPI resource. OAuth private endpoints remain blocked at Apache.

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

The backup service performs a read-only disk and inode check for the PostgreSQL,
receipt-media, and backup mounts immediately before creating the archive. It
continues on a warning and stops on a critical or missing target. Configure
`COOKOPS_DISK_WARNING_PERCENT` (default `80`) and
`COOKOPS_DISK_CRITICAL_PERCENT` (default `90`) in `deploy/.env`.

For host monitoring, run the same check with explicit host paths (for example
from cron or a systemd timer):

```sh
COOKOPS_POSTGRES_DATA_TARGET=/var/lib/docker/volumes/cookops_postgres-data/_data \
COOKOPS_RECEIPT_MEDIA_TARGET=/var/lib/docker/volumes/cookops_receipt-media/_data \
COOKOPS_BACKUP_DIR_TARGET=/srv/cookops-backups \
deploy/check-disk-space.sh
```

The command is read-only and returns `0` when healthy, `1` on warning, and `2`
for critical or missing/unreadable targets. A cron entry can run it hourly;
the same command is suitable as `ExecStart=` in a systemd service triggered by
a timer. Keep the output in the operator log and alert on exit code `2`.

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
