# Deployment, Storage, and Backup

Status: Draft architectural decision

## Target environment

The primary deployment target is one Linux VPS with an existing host-level Apache
HTTP Server. Docker Compose defines the CookOps application runtime and supports
local development, CI integration tests, and production. Apache owns the public
virtual host and TLS termination outside Compose. Kubernetes and managed cloud
services are not required.

## Production services

The production Compose project contains:

- `web`: a minimal unprivileged static web server containing the compiled PWA;
- `api`: FastAPI application;
- `postgres`: PostgreSQL 18, reachable only on the internal Compose network.

The `web` and `api` services publish configurable ports bound explicitly to
`127.0.0.1` on the VPS. PostgreSQL publishes no host port. Apache is the only
process listening publicly and routes frontend requests to `web` and API,
authentication, synchronization, WebSocket, media, and MCP paths to `api`.

Persistent named or bind-mounted volumes contain:

- PostgreSQL data;
- receipt photos and generated thumbnails;
- optionally, generated backup archives.

Redis, Celery, MinIO, and PowerSync are not required. The frontend is compiled into
static assets during the image build and served by the `web` container. The static
server has no access to application secrets, PostgreSQL, or receipt storage.

The MCP Streamable HTTP endpoint is served by the API application and routed by
Apache. It does not require another container or public port. Production
configuration MUST define the accepted MCP origin and host policy and MUST NOT
enable dummy authentication.

## Apache reverse proxy contract

The repository MUST provide a reviewed example Apache 2.4 virtual-host
configuration and document required modules. The initial supported baseline is
Apache 2.4.47 or newer so `mod_proxy_http` can forward WebSocket protocol upgrades.

The configuration MUST:

- terminate HTTPS and redirect plain HTTP to HTTPS;
- keep `ProxyRequests` disabled;
- route more-specific API, WebSocket, media, and MCP paths before the frontend
  catch-all route;
- use `ProxyPass` and `ProxyPassReverse` with consistent trailing slashes;
- preserve the public host and forward the original scheme and client address;
- forward WebSocket upgrades on the synchronization notification endpoint;
- allow long-lived streamed MCP responses with appropriate proxy timeouts;
- set explicit request-body limits compatible with receipt uploads and stricter
  MCP limits;
- set safe cache headers: hashed frontend assets are immutable, while the HTML app
  entry point and service-worker update path must revalidate;
- add baseline HTTPS security headers without preventing PWA installation, Google
  Sign-In, or the configured MCP clients;
- proxy only to the configured loopback-bound CookOps ports.

The backend MUST trust forwarded headers only from the known reverse-proxy path.
Apache configuration changes require `apachectl configtest` before a graceful
reload. Production integration tests MUST exercise ordinary HTTP, a WebSocket
upgrade, a receipt upload, and an MCP request through Apache rather than only
against the containers directly.

## Configuration and secrets

- Production configuration is provided through environment variables and mounted
  secret files.
- `.env.example` documents every required value without containing credentials.
- Production `.env` files, Google credentials, session secrets, and backup keys MUST
  NOT be committed.
- OAuth issuer and canonical MCP resource URLs MUST match the public HTTPS URLs
  routed by Apache. OAuth signing or encryption secrets remain deployment secrets;
  grants, hashed tokens, and revocation records are application data in PostgreSQL.
- The PostgreSQL port and media filesystem are not exposed publicly.
- Docker-published `web` and `api` ports bind to loopback only.
- Host Apache is the only public network entry point for CookOps.
- Apache TLS private keys and virtual-host configuration are host operational
  assets outside the CookOps application backup.

## Deployment from Git

The repository provides a documented deployment command or script that performs:

1. fetch the selected Git revision;
2. build immutable application images;
3. run database migrations as a one-off container;
4. start or recreate services with Docker Compose;
5. wait for health checks through both loopback and the public Apache virtual host;
6. report the deployed revision.

Production SHOULD deploy a tag or commit hash instead of an unpinned moving branch.
Database migrations MUST be backward-safe for the short interval in which old and
new containers may overlap, or the deployment script must stop the API before a
breaking migration.

Automatic deployment on every Git push is not required initially. CI must pass
before a revision is deployed manually. The Apache virtual-host configuration is
normally installed once; when a release changes it, deployment validates the new
configuration and requires an explicit graceful reload.

## Health and observability

- The static web, API, and PostgreSQL containers have health checks.
- Apache availability and public HTTPS routing are checked from outside the
  Compose network.
- The API exposes liveness and readiness endpoints.
- Readiness verifies required migrations and database connectivity.
- Application logs use structured JSON in production and include request or
  mutation correlation identifiers without sensitive payloads.
- Docker log rotation is configured so logs cannot fill the VPS disk.
- Disk-space monitoring MUST cover the database, media, and backup volumes.

No external monitoring service is required for the MVP, but the endpoints and logs
must permit one to be added.

## Backup archive

CookOps provides a one-command backup that produces a single ZIP archive containing:

- a PostgreSQL custom-format dump created with `pg_dump`;
- all retained receipt-photo and thumbnail files;
- a manifest containing schema version, application revision, creation time, file
  counts, and expected restore procedure;
- SHA-256 checksums for the database dump and media files.

Receipt-photo objects are immutable after finalization. The backup creates the
database snapshot first and copies media afterwards. Extra media files not
referenced by that database snapshot are harmless; soft-deletion and deferred
garbage collection prevent a referenced file from disappearing during backup.

The backup command writes to a temporary directory, verifies the contents, and only
then atomically publishes the final ZIP. Partial archives MUST NOT appear as valid
backups.

## Restore

The repository provides a restore command that:

1. validates the ZIP manifest and checksums;
2. refuses to overwrite a non-empty target unless explicitly authorized;
3. restores PostgreSQL with `pg_restore`;
4. restores media into a new storage directory;
5. runs compatibility migrations when supported;
6. verifies database/media references;
7. reports the restored application and schema versions.

Backup capability is not complete until a production-like backup/restore cycle has
passed an automated integration test.

## Backup operations

- Backup creation and restore are operator-only VPS workflows in the MVP. They are
  not exposed through the web administration interface, public API, or MCP.
- The repository MUST provide documented example commands using one-off Docker
  Compose services for both backup and restore.
- Manual backup creation through the Docker Compose command is sufficient for the
  MVP; scheduling can invoke the same command without introducing another backup
  implementation.
- The resulting archive must be copied off the VPS; a backup left only on the same
  disk is not considered sufficient protection.
- Scheduling through cron or a systemd timer SHOULD be documented.
- Automatic upload to third-party storage is not required.
- Backup retention and encryption policy remain deployment decisions.

## Source references

- [Docker Compose](https://docs.docker.com/compose)
- [Using Compose in production](https://docs.docker.com/compose/how-tos/production/)
- [Apache HTTP Server reverse-proxy guide](https://httpd.apache.org/docs/2.4/howto/reverse_proxy.html)
- [Apache `mod_proxy` and WebSocket upgrade](https://httpd.apache.org/docs/current/mod/mod_proxy.html)
- [PostgreSQL SQL dump and restore](https://www.postgresql.org/docs/current/backup-dump.html)
