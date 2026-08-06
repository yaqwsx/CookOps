# Engineering Quality

Status: Draft

## Repository structure

The repository is a monorepo containing at least:

```text
backend/          FastAPI application, migrations, and Python tests
frontend/         React PWA, IndexedDB layer, and frontend tests
deploy/           Docker Compose and host Apache example configuration
scripts/          backup, restore, deployment, and maintenance entry points
specification/    product and technical specification
```

Generated files are clearly identified. Secrets, local databases, receipt media,
and production backups are excluded from Git.

The MCP adapter lives inside the backend package beside the HTTP adapters. MCP
client fixtures and compatibility tests live in the backend or end-to-end test
trees; MCP is not maintained as a separate application with its own domain layer.

The future one-time Google Sheets ingredient importer SHOULD live as an isolated
maintenance command with dry-run and validation output. It is not part of the core
runtime or the initial MVP implementation.

## Backend quality gates

- pytest for unit, API, authorization, and PostgreSQL integration tests;
- type checking for application code;
- Ruff formatting and linting;
- Alembic migration checks from an empty database and from the previous supported
  schema;
- property-oriented tests for proportional and fixed scaling, unit and mass
  conversion, money, and LWW comparison;
- contract tests proving that HTTP and MCP adapters invoke the same application
  services and authorization policies;

Backend integration tests run against PostgreSQL rather than silently substituting
SQLite for database-specific behavior.

## Frontend quality gates

- TypeScript type checking independently of Vite transpilation;
- Vitest and React Testing Library for logic and component tests;
- linting and formatting through one checked-in tool configuration;
- IndexedDB migration tests containing pending outbox mutations;
- accessibility checks for primary forms, dialogs, and drag-and-drop alternatives.

## End-to-end testing

Playwright tests run against a production PWA build and cover at least:

- Google authentication through a test-only identity provider seam;
- organization authorization and switching;
- recipe creation, publication, version update, and local overrides;
- scaling modes, attendance-following transitions, fixed lines, portion-weight
  inclusion, and weight/cost calculations;
- desktop drag-and-drop and mobile non-drag movement;
- shopping-list generation and contribution completion;
- shopping fulfilment-credit calculations, partial contributions after refresh,
  aggregate check and uncheck, retained retired-source credit, and zero-target
  filtering;
- two browser contexts observing real-time shopping changes;
- offline edits, reload while offline, reconnect, and convergence;
- last-write-wins ordering and tombstone recovery;
- offline receipt creation and deferred photo upload;
- Czech default UI and English switching;
- event archival, reactivation, and duplication.

Dummy authentication is the default E2E provider. A smaller opt-in integration
suite verifies the Google identity-token adapter without making ordinary local and
CI testing depend on an interactive Google account.

## MCP testing

Automated tests MUST use an MCP client, rather than calling handler functions
directly, and cover at least:

- capability discovery and structured schemas;
- complete OAuth discovery, client registration, authorization-code plus PKCE,
  refresh rotation, revocation, and invalid-token behavior;
- Google-backed production authorization and dummy-backed development authorization
  producing equivalent CookOps identities;
- authorization failure and revoked grants;
- organization isolation for every tool and resource family;
- parity of member, organization-administrator, and system-administrator role
  checks with the web API;
- pagination and stable identifiers;
- idempotent retries of mutations;
- validation and safe error responses;
- an MCP write appearing in a browser client through normal synchronization;
- concurrent MCP and offline-browser writes obeying the documented conflict rules;
- protection against invalid origins and oversized requests.
- SSRF protection, redirect validation, and size limits for Client ID Metadata
  Documents;
- base64 and upload-ticket receipt-photo attachment, validation, download, and
  authorization.

A generated tool manifest or snapshot SHOULD make accidental MCP surface changes
visible in code review. At least one compatibility test runs against the protocol
inspector or another independent conforming client before release.

Service-worker behavior MUST be tested using a production build because development
service-worker behavior differs from production.

## Synchronization invariants

Automated tests MUST demonstrate:

- pushing the same mutation repeatedly is idempotent;
- mutations eventually converge after arbitrary disconnect/reconnect sequences;
- a missed WebSocket hint is recovered by cursor pull;
- pull pagination never skips or duplicates committed server changes;
- independent creations survive synchronization;
- later wall-clock field writes win deterministically;
- equal wall-clock times use the same deterministic tie-breaker everywhere;
- pending changes survive browser reload and local schema migration;
- a rejected mutation remains inspectable and does not disappear silently.

## Continuous integration

Every pull request and main-branch push runs:

1. backend formatting, linting, type checking, and tests;
2. frontend formatting, linting, type checking, and tests;
3. OpenAPI client regeneration drift check;
4. production frontend and container builds;
5. PostgreSQL migration tests;
6. selected Playwright end-to-end and offline scenarios;
7. dependency and container vulnerability scanning where practical.

Release tags SHOULD run the full end-to-end suite and produce versioned container
images. Production deployment remains an explicit operator action.

## Open-source project files

Before the first public release, the repository MUST contain:

- the MIT License in the repository root;
- `README.md` with local development and deployment instructions;
- `CONTRIBUTING.md`;
- `SECURITY.md` with private vulnerability-reporting instructions;
- a code of conduct;
- architecture and backup/restore documentation.

First-party source code and project documentation are distributed under the MIT
License. Package metadata MUST use the SPDX identifier `MIT`. Bundled or vendored
third-party material retains its own notices and MUST be documented when present.
