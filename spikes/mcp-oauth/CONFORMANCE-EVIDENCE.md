# MCP conformance-suite evidence

This is the durable official-runner gate for the MCP production boundary. It
does not claim an official PASS when the protected endpoint cannot be supplied
with a real OAuth authorization flow.

## Official runner

The official package is reproducibly addressable as
`@modelcontextprotocol/conformance@0.1.16`. Its published binary is
`conformance`; the server invocation used by this spike is preserved in
[`run-conformance.sh`](./run-conformance.sh).

The executable precondition is Docker Compose plus a reachable Docker daemon;
the wrapper validates exactly one safe `https://.../mcp` URL, then runs the
official command in the pinned Node 22 one-shot service:

```bash
./spikes/mcp-oauth/run-conformance.sh https://<disposable-origin>/mcp
```

Discovery commands and results on 2026-08-19:

```text
npm view @modelcontextprotocol/conformance@0.1.16 version bin
0.1.16 / conformance

npm pack --ignore-scripts @modelcontextprotocol/conformance@0.1.16
package/README.md documents: conformance server --url <url> --suite active
```

The package was not added to either project manifest or lockfile. The runner
requires an already-running MCP URL. The production deployment now provides
`/mcp` through the Apache path-preserving proxy, and
`run-backend-live-proxy-smoke.sh` exercises that topology with discovery, PKCE,
bearer authorization, and an authenticated `get_event_summary` call.
The official server CLI does not accept a bearer token, browser session, or PKCE
client configuration; running it against this protected endpoint therefore
receives the intentional unauthenticated challenge rather than an authenticated
MCP session. The package requires Node.js 22 or newer in practice; Compose
supplies that runtime.

## Existing evidence and remaining gap

The existing authenticated disposable path remains covered by:

- `resource-server/tests/test_authenticated_mcp_e2e.py` — opaque-token,
  RFC 9728/RFC 8414, PKCE, and official TypeScript SDK client behavior;
- `resource-server/tests/test_resource_server.py` — exact issuer/resource
  checks and malformed introspection rejection;
- `oauth-server/src/*.test.ts` — provider, registration, metadata, redirect,
  persistence, and approval-protocol checks.

These tests are not a substitute for an official conformance run. Once an
authenticated official-runner flow is supported upstream (or an operator
supplies a separately authorized public URL), run:

```bash
./spikes/mcp-oauth/run-conformance.sh https://<disposable-origin>/mcp
```

The report from that run must be retained with the package version, URL
topology, scenario results, and known failures. The wrapper input validation and
invocation contract are covered by `test-run-conformance.sh`; that is not a
conformance result. The official criterion remains open until the command
completes against the real authenticated public resource.
