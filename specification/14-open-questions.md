# Open Questions

Status: Living document

## Dietary tags

- What is the final iconography and compact/mobile presentation of dietary warning
  details?

## Offline synchronization

- Which non-administrative operations, if any, are unavailable offline despite the
  general offline requirement?

## Costs

- How long are unreferenced or soft-deleted receipt-photo objects retained?

## Architecture and operations

- Which browser and operating-system versions define the supported PWA baseline?
- Are backup archives encrypted by CookOps, or by deployment-level tooling?
- What is the backup schedule and retention policy for the initial VPS?
- Is production deployment always manual, or should a later CI workflow deploy
  signed release tags over SSH?
- Which Apache 2.4 version and modules are available on the target VPS?
- Does the existing Apache host already manage TLS certificate issuance and
  renewal, or must the CookOps deployment documentation include that setup?

## Agent interface and MCP

- Does the mandatory interoperability spike in specification 20 confirm that
  `oidc-provider` can provide current MCP CIMD behavior, PostgreSQL persistence,
  and the CookOps identity/consent bridge without patching provider internals?
