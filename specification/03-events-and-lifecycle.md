# Events and Lifecycle

Status: Draft

## Ownership and attendance

- Every event belongs to one organization.
- An event has a name, start date, end date, optional location, overall budget,
  currency, lifecycle state, and optional general note.
- A new event inherits its initial currency from the organization and uses one
  currency for its budget, estimates, and receipts.
- An event has a base expected attendance.
- A scheduled recipe instance MAY add or remove diners relative to the event base.
- The application does not maintain a complete named participant roster.
- Members MAY record named dietary exceptions, each with a required name,
  structured requirement tags, and an optional free-form note.
- Dietary exceptions are treated as potentially present at every scheduled recipe;
  per-meal attendance for named exceptions is not tracked in the MVP.

## Schedule

- Creating an event automatically creates every calendar day in its inclusive date
  range.
- Members can subsequently add a day outside the original range or hide an
  automatically created day.
- Every day has an editable note.
- A day contains an arbitrary number of scheduled recipe instances.
- Each scheduled recipe instance has one meal-role tag.
- An arbitrary number of scheduled recipe instances MAY share the same role on
  the same day.
- Built-in role presets include breakfast, morning snack, soup, lunch, afternoon
  snack, and dinner.
- Organizations or events MAY manage additional role tags.
- Meal-role tags have an explicit display order which determines grouping and
  ordering in the event planner.
- A scheduled recipe instance does not require a serving time in the MVP.
- Scheduled recipe instances can be moved freely between days and roles.

## Scheduled attendance

- A scheduled recipe instance stores an explicit final diner count.
- The diner count is initially copied from the event base attendance.
- Members edit the final number directly rather than entering only a relative
  adjustment.
- The UI SHOULD preserve and display the relationship to the event base, for
  example "event base 42, adjusted to 46."

## Active events

An active event is dynamically editable. Members can add recipes, create catalog
recipes, adjust recipe instances, generate shopping lists, and record costs.

Catalog changes MUST NOT silently modify an existing scheduled recipe instance.
Active events SHOULD show when a newer catalog recipe version exists and provide
an explicit update action.

## Archived events

- Archiving MUST materialize a self-contained historical representation of the
  event.
- The materialized record MUST include resolved recipe versions, local overrides,
  ingredients, relevant estimated prices, schedule, shopping lists and their
  completion state, budget, and receipts.
- An archived event is read-only during normal use.
- An authorized administrator MUST be able to reactivate an archived event.
- An authorized member MUST be able to create a new event by duplicating an
  archived event.

Every archive operation creates an immutable, schema-versioned
`EventArchiveSnapshot` containing the complete resolved historical projection while
the normalized event graph remains retained and locked. Reactivation preserves the
snapshot as history and unlocks the graph. The detailed model and invariants are
defined in `16-domain-model.md`.
