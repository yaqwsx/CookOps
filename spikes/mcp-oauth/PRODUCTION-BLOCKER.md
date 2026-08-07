# Production OAuth/MCP blocker

This directory is a disposable interoperability spike.  It is not a production
OAuth server or a source for a production FastAPI MCP mount.

## Evidence

- `oauth-server/src/runtime.ts` rejects both `NODE_ENV=production` and
  `COOKOPS_ENVIRONMENT=production`.
- `oauth-server/src/oauth-flow.test.ts` completes every login as the fixed
  `INTERNAL_USER_ID`; it has no CookOps browser-session, membership, or consent
  bridge.
- The disposable OAuth service now proves a loopback-only private approval
  protocol: it HMAC-keys persisted one-time records to an interaction and binds
  client, resource, scope, prompt, internal user UUID, decision, and expiry.
  Its FastAPI counterpart is deliberately transport-free and applies the normal
  opaque browser-session and current-membership gate for both dummy and Google
  adapters. It does not mount a consent route or carry an OAuth credential.
- The production backend still has no `mcp` dependency, MCP ASGI adapter, OAuth
  resource settings, private interaction-approval credential, or safe consent
  UI/CSRF transport. The spike remains loopback-only and rejects production.
- The only OAuth package is under `spikes/`; there is no production
  `oauth-server/` package, Compose service, migration, or private API boundary.
- The reviewed Apache example intentionally mounts neither OAuth discovery or
  protocol paths nor `/mcp`; the deployment smoke check rejects reintroducing
  those routes before the missing resource-verifier and OAuth-service boundaries
  exist.
- The OAuth tests now exercise CIMD private-IP rejection, redirect refusal,
  malformed and oversized metadata, DCR exact redirects, DCR JWKS redirects,
  public-client secret suppression, and encrypted PostgreSQL-backed OAuth flow
  storage. PostgreSQL E2E coverage drives opaque, resource-bound tokens through
  both the official Python MCP SDK and the official TypeScript MCP SDK;
  the latter performs RFC 9728/RFC 8414 discovery and PKCE itself. The required
  spike gates still lack official conformance-suite coverage and Google and
  dummy traversal of the same interaction bridge. The PostgreSQL tests require
  `TEST_DATABASE_URL` (the disposable Compose test service supplies it).

## Required production boundary

Before mounting `/mcp`, implement a separately versioned `oauth-server` with
the pinned provider and PostgreSQL migration, then connect its already-proven
one-time approval protocol to FastAPI over an authenticated private network.
FastAPI must use the normal browser authentication and membership gate to issue
those approvals through a CSRF-safe consent UI;
the resource-server adapter must use private RFC 7662 introspection and reload
CookOps authority for every operation.  Client-metadata fetching must be
validated at the OAuth service boundary, including DNS revalidation after every
redirect.

Until that boundary and the mandatory specification 20 spike pass, no production
MCP endpoint or OAuth interaction route may be exposed.  A stub would either
turn the spike's test identity into an authentication bypass or invent an
unauthenticated cross-service approval protocol.
