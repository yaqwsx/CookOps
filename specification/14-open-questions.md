# Open Questions

Status: Living document

## Catalog and versioning

- What merge UI reconciles a recipe with a newer or retired ingredient version?

## Dietary requirements

- What initial ingredient labels and dietary requirement presets are seeded for a
  new organization?
- What is the final iconography and compact/mobile presentation of dietary warning
  details?

## Offline synchronization

- What device-clock skew threshold triggers a warning, and can a severely incorrect
  clock temporarily block synchronization?
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

- Which maintained OAuth authorization-server library or local component provides
  the required OAuth 2.1, PKCE, metadata, CIMD, DCR, refresh, and revocation support
  with the least operational weight?
- Which high-impact operations require an explicit confirmation argument, and
  how should clients present that confirmation to users?

## UX and operations

- What summary information belongs above the vertical planner and on a separate
  event dashboard, if one is needed?
- How much detail is visible on a scheduled recipe card before expansion?
- Is the recipe editor a modal dialog, side panel, or dedicated route on narrow
  screens?
- Which notifications, if any, are required for catalog updates and shopping-list
  changes?
