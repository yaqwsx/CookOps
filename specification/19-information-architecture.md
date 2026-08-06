# Information Architecture and Responsive Navigation

Status: Draft UX structure

## Goals

CookOps presents the same organization and event model on desktop and mobile while
adapting density and interaction mechanics. Navigation MUST:

- keep the active organization and synchronization state visible;
- open an event directly into useful work;
- make planning, shopping, and receipt entry efficient on mobile;
- keep recipe and ingredient catalogs organization-level resources;
- expose equivalent non-drag, non-hover, and keyboard interactions;
- avoid an unnecessary dashboard or notification system in the MVP.

## Application shell

The authenticated shell contains:

- CookOps identity/home affordance;
- active organization switcher;
- primary organization navigation;
- global connectivity and synchronization indicator;
- current-user menu containing locale, authorized MCP clients, and logout;
- access to organization or system administration when authorized.

The active organization is explicit in both visible navigation and URLs. Switching
organization returns to that organization's event overview rather than attempting
to reinterpret the currently open event or catalog ID in another organization.
Pending changes for the previous organization remain in its independent outbox and
continue synchronizing when allowed.

Desktop navigation MAY use a header, sidebar, or combination. Mobile navigation MAY
use a compact tab bar and overflow menu or a navigation drawer. The information
hierarchy and route availability remain identical; narrow screens do not lose a
feature merely because it moves into an overflow surface.

## Organization-level destinations

The primary organization destinations are:

- **Events**;
- **Recipes**;
- **Ingredients**;
- **Organization settings**.

Organization settings contains ordinary member-editable configuration such as
store sections, meal-role presets, units, recipe tags, and dietary tags. Membership
management is visible only to organization or system administrators as allowed by
the authorization matrix. System-wide organization and administrator management
lives in a clearly separate system-administration destination.

## Organization event overview

After login or organization switching, CookOps opens the organization event
overview. It prioritizes:

1. active events;
2. a compact list of recently archived events;
3. links to the recipe and ingredient catalogs.

Each event entry includes name, date range, lifecycle, and enough basic context to
distinguish similarly named events. An organization administrator sees the create
event action; ordinary members do not see an enabled control they cannot use.

The MVP does not require organization analytics, charts, a cross-event budget
dashboard, or a notification feed on this page. Archived-event search or pagination
remains available without loading every archive payload.

## Event workspace

Opening an event navigates directly to its **Planner**, the default event section.
The event workspace contains these peer sections:

- **Planner**;
- **Shopping**;
- **Costs and receipts**;
- **Event settings**.

Desktop may render these as tabs or local sidebar navigation. Mobile uses a
horizontally scrollable tab row, compact selector, or equivalent reachable control.
Changing event section preserves event identity and does not reset locally edited
planner or shopping state.

### Event summary strip

A compact summary strip above the event section contains:

- event name and date range;
- active or archived lifecycle;
- base attendance;
- budget and current actual/estimated summary when available;
- event-level pending synchronization state;
- context actions appropriate to the section and role.

The strip may collapse secondary values on mobile. The MVP has no separate event
dashboard: summary information belongs here and detailed information belongs in the
corresponding event section.

An archived event shows a persistent read-only banner. Organization administrators
can access reactivation and duplication actions; ordinary members can read the
archive but cannot edit or see misleading enabled controls.

### Planner

The Planner uses the vertical day layout defined in
`09-planning-interface.md`. On desktop it may display the recipe catalog beside the
days. On mobile the same catalog opens as a drawer, sheet, or full-screen selection
surface. A prominent planner action can create a new shopping list from selected
days and scheduled recipes without making shopping generation part of a permanent
planner mode.

### Shopping

The Shopping section first lists the event's named shopping lists and provides the
new-shopping workflow. Opening a list enters the mobile-first operational view with
store-section grouping, expandable ingredient contributions, fulfilled-item
filtering, and the dedicated pending-change status bar.

The list name and back path to all event shopping lists remain visible. The app does
not treat one list as a permanent event-wide current list.

### Costs and receipts

This section combines:

- event budget;
- estimated scheduled-recipe cost;
- expected materialized-shopping cost;
- actual receipt total;
- receipt list and create/edit/photo workflows.

Receipt capture is a primary mobile action. The section does not expose receipt
line-item or reimbursement navigation absent from the MVP domain.

### Event settings

Event settings contains event metadata, date range, base attendance, notes,
event-owned meal roles, price-estimate refresh, and lifecycle actions allowed to the
current role. High-impact lifecycle actions are visually separated from routine
fields.

## Catalog navigation

Recipes and ingredients are organization-level searchable collections. Their list
views support active/retired filtering and fuzzy search. Opening a catalog record
shows its current version, relevant warnings, and version history without requiring
an event context.

The planner's recipe panel is another presentation of the same recipe catalog and
query services. Creating a recipe from inside an event publishes it to the
organization catalog and schedules it according to the domain workflow.

Catalog-update availability is shown inline on affected catalog records and
scheduled recipe instances. The MVP has no central catalog-update inbox.

## Scheduled recipe card hierarchy

The collapsed scheduled recipe card shows only the information needed to scan and
operate the plan:

- recipe name and meal role;
- final diner count;
- selected scaling amount and unit;
- compact indicators for local overrides, dietary conflicts, missing prices, and a
  newer catalog recipe version.

Cost, prepared weight, per-diner values, description, ingredient lines, warning
details, and update details appear after expansion or in the scheduled-instance
detail surface. A changed automatic scaling suggestion is visible beside a manual
value because it may require action.

Where space permits, diner count, consumption factor, and selected scale may expose
direct controls on the expanded card. Mobile may open a focused edit sheet instead.
Both invoke the same event-local commands and mode transitions.

Color MAY reinforce override or warning state but is never the only signal. Icons
have accessible names. Hover tooltips have click/tap/focus popovers or detail views
containing the same information.

## Recipe editor presentation

The catalog recipe editor uses one form and command model in two responsive
presentations:

- desktop opens a large modal dialog or route-backed overlay that preserves planner
  context;
- mobile opens a dedicated full-screen route with ordinary back navigation.

The editor route is directly addressable. Loading it without an underlying planner
still produces a complete page. Closing or navigating away with unpublished changes
requires an explicit discard decision. Saving publishes according to the immutable
recipe-version rules; the editor never silently converts changes into an event
override.

Event-local ingredient changes use a distinct scheduled-instance edit surface and
the documented override styling. Editing the catalog recipe from that context opens
the catalog editor with its pinned catalog version, not the locally resolved
override values.

## Notifications and feedback

The MVP has no central notification inbox, push notifications, or email
notifications. Feedback uses:

- inline badges and icons for persistent actionable state;
- local validation beside the affected field;
- short toasts for completed immediate actions;
- persistent banners or status bars for connectivity, synchronization failures,
  clock skew, archived state, and recoverable rejected work;
- live data updates for ordinary collaborative shopping changes without a toast for
  every remote checkbox.

A toast is not the only record of an error requiring action. Catalog updates,
dietary warnings, missing prices, and synchronization problems remain discoverable
on the affected entity or persistent status surface after the toast disappears.

## Route model

Exact router syntax may evolve, but stable UUID-based routes follow this conceptual
shape:

```text
/organizations/:organizationId/events
/organizations/:organizationId/events/:eventId/planner
/organizations/:organizationId/events/:eventId/shopping
/organizations/:organizationId/events/:eventId/shopping/:shoppingListId
/organizations/:organizationId/events/:eventId/costs
/organizations/:organizationId/events/:eventId/settings
/organizations/:organizationId/recipes
/organizations/:organizationId/recipes/:recipeId
/organizations/:organizationId/recipes/:recipeId/edit
/organizations/:organizationId/ingredients
/organizations/:organizationId/ingredients/:ingredientId
/organizations/:organizationId/settings
/system/organizations
```

Names and slugs are presentation data, never routing identity. Unauthorized IDs use
the common non-enumerating error policy. Routes required for cached ordinary work
render from IndexedDB while offline; online-only controls show an explicit reason
when unavailable rather than failing after an apparently successful edit.

## Responsive and accessibility verification

The primary navigation, event tabs, planner, recipe editor, shopping list, and
receipt capture MUST be verified at representative narrow and wide viewports.
Automated and manual checks cover:

- keyboard traversal and visible focus;
- screen-reader names for icon-only indicators and controls;
- click/tap alternatives to hover content;
- non-drag alternatives for all reorder and movement operations;
- no horizontal page overflow at the supported mobile viewport;
- preservation of unsaved or pending work during route and organization changes;
- correct role-based visibility without relying on hidden controls for security.
