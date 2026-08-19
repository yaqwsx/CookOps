# MCP conformance-suite evidence

This is disposable evidence for the mandatory interoperability spike. It does
not mount `/mcp`, expose OAuth routes, or change specification 20 from
`Proposed`.

## Official runner

The official package is reproducibly addressable as
`@modelcontextprotocol/conformance@0.1.16`. Its published binary is
`conformance`; the server invocation used by this spike is preserved in
[`run-conformance.sh`](./run-conformance.sh).

Discovery commands and results on 2026-08-19:

```text
npm view @modelcontextprotocol/conformance@0.1.16 version bin
0.1.16 / conformance

npm pack --ignore-scripts @modelcontextprotocol/conformance@0.1.16
package/README.md documents: conformance server --url <url> --suite active
```

The package was not added to either project manifest or lockfile. The runner
requires an already-running MCP URL, so it cannot be truthfully run against
this checkout: the production FastAPI application intentionally has no `/mcp`
mount and the disposable Compose file provides only the OAuth test service.
Running the command with a missing or invented URL would not be conformance
evidence. The package also requires Node.js 22 or newer in practice: the
published binary imports `fs.globSync`, while this checkout's local Node.js
20.20.2 fails during module loading. The harness reports that prerequisite
before invoking `npx`.

## Existing evidence and remaining gap

The existing authenticated disposable path remains covered by:

- `resource-server/tests/test_authenticated_mcp_e2e.py` — opaque-token,
  RFC 9728/RFC 8414, PKCE, and official TypeScript SDK client behavior;
- `resource-server/tests/test_resource_server.py` — exact issuer/resource
  checks and malformed introspection rejection;
- `oauth-server/src/*.test.ts` — provider, registration, metadata, redirect,
  persistence, and approval-protocol checks.

These tests are not a substitute for an official conformance run. A future
production-boundary slice must first provide a disposable path-preserving
proxy with a real `/mcp` resource endpoint, then run:

```bash
./spikes/mcp-oauth/run-conformance.sh https://<disposable-origin>/mcp
```

The report from that run must be retained with the version, URL topology,
scenario results, and known failures. Until then, the official conformance
criterion remains open, as do the production FastAPI resource-server boundary,
Google/dummy bridge through the real interaction route, and the other blockers
listed in `PRODUCTION-BLOCKER.md`.
