# Disposable backend MCP interoperability evidence

This disposable harness reuses the existing OAuth fixture and
HTTPS path-preserving proxy, while replacing only the in-process spike resource
with a separately started FastAPI process importing
`cookops.mcp_resource.create_mcp_protected_resource`.

Topology:

```text
official MCP Python client
        | HTTPS mcp.localtest.me/mcp
Apache (temporary self-signed certificate)
        | /mcp -> disposable backend uvicorn
FastAPI + CookOps MCP adapter -> PostgreSQL event projection
        | private HTTP introspection
OAuth fixture (real oidc-provider + PostgreSQL)
```

The wrapper creates a pinned disposable PostgreSQL container, applies current
migrations, seeds one deterministic active member in one organization with an
event, plus a foreign organization/event without membership for that user, and
removes the container on every exit:

```sh
./spikes/mcp-oauth/run-backend-live-proxy-smoke.sh
```

The flow performs protected-resource and authorization-server discovery,
authorization-code PKCE with the resource indicator, a bearer MCP
`get_event_summary` call, no-bearer/invalid-token rejection, wrong host/origin
rejection (421/403), wrong-audience rejection, a real cross-organization/event
denial from `get_event_summary`, expiry, and token revocation. The OAuth
fixture's isolated schema keeps its provider state disposable; the application
event rows are seeded in the disposable PostgreSQL container so membership
authorization is evaluated by the real `get_event_summary` service on every
call.

The command is intentionally not run with invented IDs or a shared database.
The successful local run used `mcp==1.29.0`, a five-second disposable access
token TTL, and ended with `live HTTPS proxy smoke: PASS`; Docker reported no
remaining `cookops-mcp-postgres-*` container. `@modelcontextprotocol/conformance`
was not substituted for this real MCP-client smoke. The official wrapper
remains a separate follow-up because its server CLI does not perform the OAuth
PKCE/browser authorization needed by this protected endpoint:

```sh
./spikes/mcp-oauth/run-conformance.sh https://mcp.localtest.me:<port>/mcp
```
