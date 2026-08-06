# Organizations and Access

Status: Draft

## Authentication gate

- The application MUST authenticate users through Google Sign-In.
- A Google-authenticated identity MUST NOT gain application access unless its
  account is a member of at least one CookOps organization.
- Authentication alone MUST NOT create a usable personal workspace.
- A system administrator MAY access system administration without ordinary
  organization membership.

Google Sign-In is the production human authentication provider. Local development
and automated tests use the development identity provider described below.

## Development identity provider

CookOps MUST provide a deterministic dummy identity provider for local development,
end-to-end tests, and agent-driven testing before Google Sign-In is configured.

- The dummy provider MUST be disabled by default outside the development and test
  environments.
- The backend MUST refuse to start when the dummy provider is selected together
  with production mode. A frontend-only flag is not a sufficient safety boundary.
- Selecting a dummy identity MUST create the same CookOps HTTP-only session used
  after Google authentication. Application endpoints MUST NOT contain a separate
  authorization bypass for dummy users.
- Local seed data MUST include stable identities covering `system_admin`,
  `organization_admin`, `member`, membership in multiple organizations, and a
  recognized identity with no organization access.
- The development login screen MUST allow a tester to select one of these named
  identities and clearly indicate that development authentication is active.
- Tests MAY create additional deterministic identities through test fixtures. An
  unrestricted "enter any email and become administrator" production-reachable
  endpoint is forbidden.
- Authorization tests MUST run against both dummy sessions and the same role checks
  used by production sessions.
- The development provider MUST also participate in the full MCP OAuth authorization
  flow. Local agents and automated clients authorize as a selected seeded identity
  through OAuth with PKCE; they do not receive a hard-coded bearer-token bypass.

The provider boundary MUST allow Google Sign-In to replace the dummy provider
without changing domain users, memberships, authorization policies, or frontend
application behavior after a session has been established.

## Organization membership

- A user MAY belong to multiple organizations.
- The active organization MUST be clearly visible and switchable from the
  application header.
- Events, recipes, and ingredients belong to exactly one organization.
- An organization has a default currency, initially CZK unless configured
  otherwise.
- Organization members can read and edit organization content by default.
- Anonymous users and users outside the organization MUST NOT have access.

## Roles

CookOps has three authorization roles:

- `member`: organization-scoped collaborative access;
- `organization_admin`: organization-scoped administration in addition to member
  access;
- `system_admin`: system-wide organization administration.

A user MAY hold different organization-scoped roles in different organizations.

### Member

A member can read and edit existing organization content by default, including
recipes, ingredients, active events, shopping lists, receipts, and local event
overrides. A member can publish catalog versions and retire or restore catalog
records. A member cannot create an event or manage membership.

### Organization administrator

An organization administrator has all member capabilities and can additionally:

- create events;
- invite members by their Google account email address;
- remove organization members;
- copy recipes and ingredients into an organization when also authorized for the
  source organization;
- reactivate and duplicate archived events.

An organization administrator cannot grant or revoke the `organization_admin` role,
remove their own administrative role, or leave the organization in a way that
would change its administrative ownership.

### System administrator

A system administrator can:

- create, edit, retire, and restore organizations;
- assign and revoke organization administrators;
- manage organization membership when required;
- inspect and edit all organization-owned operational content without separate
  organization membership;
- perform authorized cross-organization administration.

Creating an organization does not automatically grant that ability to ordinary
users. The system administrator establishes its initial organization administrator.

## Invitation and first login

- An organization administrator invites a member using an exact Google account
  email address.
- Membership exists before the invited person's first successful login.
- Google Sign-In matches the verified Google email address exactly against active
  membership or system-administrator records.
- A person with neither active membership nor system administration is denied
  application access after authentication.

## Cross-organization copying

- Copying recipes or ingredients requires at least membership in the source
  organization and the `organization_admin` role in the destination organization.
- System administrators MAY perform the operation as part of system-wide
  administration.

## Change attribution

The MVP does not require a separate audit-log interface. Records and mutations MUST
retain their author and timestamp where relevant for version history,
synchronization, and operational attribution.

MCP actions are attributed to the CookOps user represented by the OAuth grant and
additionally record the OAuth client identity. Using MCP grants exactly that user's
current CookOps capabilities: no more, and no separately reduced organization or
role subset.
