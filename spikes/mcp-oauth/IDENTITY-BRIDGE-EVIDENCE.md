# Identity bridge spike evidence

This is disposable evidence for the mandatory interoperability spike in
`specification/20-mcp-oauth-research-and-decision.md`. It does not change the
decision status and does not establish production equivalence.

## Executed evidence

| Mandatory gate | Evidence | Result |
| --- | --- | --- |
| Dummy and Google identities use the same interaction bridge | `cd backend && uv run pytest -q tests/test_oauth_identity_bridge.py` | PASS: 3 tests; the real `GoogleIdentityProvider` and a separately configured dummy adapter stub each produce an adapter-derived browser session, and the same `OAuthInteractionApprovalService.submit` path forwards the current CookOps identity. Each happy-path fixture seeds that current identity from its completed adapter session. |
| No raw credentials cross the approval boundary | The focused test asserts the private approval call contains only interaction UID, subject, and decision; the opaque Google token is not present in the mocked bridge calls. | PASS |
| Failed or mismatched identity is rejected | The focused test rejects an unknown dummy selection, invalid Google token claims, and a missing current CookOps identity without calling the private approval client. A separate case proves a current CookOps identity, not an arbitrary adapter/caller UUID, is the approval subject. | PASS |
| Existing OAuth/MCP E2E behavior | `cd backend && uv run pytest -q ../spikes/mcp-oauth/resource-server/tests/test_authenticated_mcp_e2e.py` with `OAUTH_E2E_DATABASE_URL` configured | Existing test remains the authoritative opaque-token MCP E2E check; this slice does not replace or weaken it. |

The dummy side is intentionally a deterministic adapter stub in this unit slice;
the production-like database adapter is covered by the existing backend tests.
The Google side uses the repository's actual provider implementation with a
verifier stub, so no Google network or credential is used.

## Remaining blockers

The spike remains **not accepted**. The following mandatory evidence is still
missing or intentionally outside this slice:

- the official MCP conformance suite executable and its run; no such executable
  is present in this repository, so it must be supplied as an external
  prerequisite rather than fabricated here;
- representative agent-client coverage beyond the existing official SDK
  fixtures;
- production FastAPI-to-`oauth-server` authenticated approval transport,
  consent UI/CSRF boundary, and private RFC 7662 resource-server verifier;
- full reverse-proxy path-preservation and production deployment evidence;
- the remaining negative and restart/concurrency gates listed in specification
  20.

`PRODUCTION-BLOCKER.md` remains authoritative. The production `/mcp` route and
OAuth interaction route must stay unmounted until those boundaries and all
mandatory spike gates pass.
