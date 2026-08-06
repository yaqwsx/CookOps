# CookOps Specification

Status: Draft

This directory contains the living product and domain specification for CookOps.
The specification is written in English. The application's default user-interface
language is Czech, with English provided as the second built-in locale.

## Documents

1. [Product scope](01-product-scope.md)
2. [Organizations and access](02-organizations-and-access.md)
3. [Events and lifecycle](03-events-and-lifecycle.md)
4. [Recipes, versions, and scaling](04-recipes-versions-and-scaling.md)
5. [Ingredient catalog and dietary tags](05-ingredient-catalog-and-dietary-rules.md)
6. [Shopping](06-shopping.md)
7. [Costs and receipts](07-costs-and-receipts.md)
8. [Localization](08-localization.md)
9. [Planning interface](09-planning-interface.md)
10. [Offline operation and synchronization](10-offline-and-synchronization.md)
11. [Technical architecture](11-technical-architecture.md)
12. [Deployment, storage, and backup](12-deployment-storage-and-backup.md)
13. [Engineering quality](13-engineering-quality.md)
14. [Open questions](14-open-questions.md)
15. [Agent interface and MCP](15-agent-interface-mcp.md)
16. [Domain model](16-domain-model.md)
17. [Application services and API contracts](17-application-services-and-api.md)
18. [Synchronization protocol](18-synchronization-protocol.md)
19. [Information architecture and responsive navigation](19-information-architecture.md)
20. [MCP OAuth research and decision](20-mcp-oauth-research-and-decision.md)

## Specification conventions

- **MUST** identifies required behavior.
- **SHOULD** identifies preferred behavior that may be revised after UX testing.
- **MAY** identifies explicitly optional behavior.
- Decisions not yet agreed are kept in `14-open-questions.md` instead of being
  silently turned into requirements.
