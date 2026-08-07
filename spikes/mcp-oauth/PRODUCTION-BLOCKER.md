# Production OAuth/MCP blocker

This directory is a disposable interoperability spike.  It is not a production
OAuth server or a source for a production FastAPI MCP mount.

## Evidence

- `oauth-server/src/runtime.ts` rejects both `NODE_ENV=production` and
  `COOKOPS_ENVIRONMENT=production`.
- `oauth-server/src/oauth-flow.test.ts` completes every login as the fixed
  `INTERNAL_USER_ID`; it has no CookOps browser-session, membership, or consent
  bridge.
- The production backend has no `mcp` dependency, MCP ASGI adapter, OAuth
  resource settings, or private interaction-approval credential.
- The only OAuth package is under `spikes/`; there is no production
  `oauth-server/` package, Compose service, migration, or private API boundary.
- The required spike gates remain incomplete: CIMD/DCR SSRF and redirect tests,
  two independent clients/conformance coverage, Google and dummy traversal of
  the same interaction bridge, and PostgreSQL-backed end-to-end coverage.  The
  current Node suite skips its PostgreSQL flows without `TEST_DATABASE_URL`.

## Required production boundary

Before mounting `/mcp`, implement a separately versioned `oauth-server` with
the pinned provider and PostgreSQL migration, then authenticate one-time,
short-lived interaction approvals between it and FastAPI.  FastAPI must use the
normal browser authentication and membership gate to issue those approvals;
the resource-server adapter must use private RFC 7662 introspection and reload
CookOps authority for every operation.  Client-metadata fetching must be
validated at the OAuth service boundary, including DNS revalidation after every
redirect.

Until that boundary and the mandatory specification 20 spike pass, no production
MCP endpoint or OAuth interaction route may be exposed.  A stub would either
turn the spike's test identity into an authentication bypass or invent an
unauthenticated cross-service approval protocol.
