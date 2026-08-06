# Recipes, Versions, and Scaling

Status: Draft

## Catalog recipes

- Recipes belong to an organization.
- A recipe consists of metadata and catalog-backed ingredient requirements.
- Recipe metadata includes:
  - name;
  - optional Markdown description or operational note, which may contain links;
  - organization-defined recipe tags;
  - scaling-unit configuration;
  - catalog-backed ingredient requirements.
- Publishing changes to a catalog recipe creates a new immutable recipe version.
- Previous versions remain available for existing event instances and history.
- A version retains its author and publication time. The MVP does not require a
  manually written change message or a persistent version-diff view.
- The MVP has no shared recipe-draft lifecycle. Closing the editor discards
  unpublished changes; saving publishes a new immutable version immediately.

Removing a recipe from the active catalog is a reversible retirement rather than a
physical deletion:

- retired recipes are excluded from normal catalog browsing and new scheduling;
- existing event instances and historical versions remain readable;
- members MAY restore a retired recipe to the active catalog;
- retirement MUST NOT alter active event instances or archived events.

Creating a recipe from inside an event MUST create it in the organization's recipe
catalog and immediately create a scheduled event instance from its initial version.

## Event recipe instances

A scheduled recipe instance references a specific catalog recipe version and adds
event context:

- day and meal-role tag;
- final diner count, initially following event base attendance;
- consumption percentage;
- selected scaling amount;
- local ingredient overrides;
- event-specific notes.

The final diner count has one of two modes:

- `follows_event`: changes to event base attendance automatically update the
  scheduled instance; or
- `manual`: the scheduled instance keeps an explicitly entered value.

A newly scheduled recipe starts in `follows_event` mode. Directly editing its diner
count changes it to `manual`; an explicit reset action returns it to
`follows_event`.

When editing ingredients, the user MUST explicitly choose between:

- publishing a new catalog recipe version; or
- applying an override only to the current event instance.

Quick edits initiated directly from the event planner are always local ad-hoc
overrides. Catalog changes are authored only in the dedicated recipe editor dialog;
an inline planner edit MUST NOT silently publish a catalog version.

When a new catalog version is published from a scheduled instance, that instance
MUST switch to the newly published version. Before switching, the application MUST
ask whether existing local overrides should be preserved or discarded.

A local ingredient override is one of:

- changing an ingredient quantity, including changing it to zero; or
- adding an ingredient that does not exist in the referenced recipe version.

An added override ingredient MUST still reference an ingredient from the
organization catalog. Free-text ingredients are not allowed. It defaults to being
included in prepared portion weight, and the user MAY change that flag for the
added line. A quantity replacement inherits the catalog line's portion-weight
flag; changing that catalog property requires publishing a recipe version.

Overrides MUST be visually distinguishable from catalog-derived ingredient rows.
A dedicated color treatment is required, subject to later visual design.

Active event instances MUST remain pinned to their selected recipe version until a
member explicitly requests an update from the catalog. Updating MUST preserve or
reconcile local overrides instead of silently deleting them.

Before applying a catalog update, the application MUST present a transient preview
of its practical ingredient changes. This preview is part of the update workflow
and is not retained as a version-history diff.

When applying the update while preserving overrides:

- newly added catalog ingredient lines are added to the instance;
- changed quantities are accepted for lines without a local override;
- locally overridden quantities remain unchanged;
- locally added ingredients remain present;
- an ingredient removed by the catalog version remains as a locally added
  ingredient when the instance has a local override for it.

The user chooses once between preserving all local overrides and discarding all
local overrides. Per-override merge decisions are not required in the MVP.

If the new catalog version retains a compatible scaling unit, the scheduled
instance preserves its selected scaling amount and `suggested` or `manual` mode.
If the scaling unit changes incompatibly, the update preview MUST call this out and
require confirmation; applying the update resets the selected amount to the new
version's current suggestion, or to its base scaling amount when no attendance-based
suggestion is available. The existing preserve-or-discard choice for ingredient
overrides remains a separate decision.

## Recipe editor and discovery

- Catalog recipes are created and edited in a dedicated dialog or focused editor,
  not inline in the event planner.
- Opening the catalog editor from an event instance loads its pinned catalog
  version without applying event-local overrides.
- The MVP does not provide a separate action that promotes an instance's resolved
  local overrides into a catalog version.
- The description editor defaults to a WYSIWYG experience backed by Markdown.
- Members can switch between WYSIWYG and raw Markdown source editing.
- Hyperlinks are authored directly inside the Markdown description; external links
  are not a separate recipe field.
- Recipe tags are organization-owned records with a name and color.
- Tags can be created while editing a recipe and used for filtering.
- Removing a tag is a reversible soft-delete; historical references remain
  readable and the tag can be restored.
- Recipe search covers recipe names, tags, Markdown description text, and names of
  referenced ingredients.
- Search SHOULD use fuzzy matching where practical.

## Scaling model

Every recipe version has exactly one base scaling variable in the MVP.

The recipe defines:

- a scaling unit, such as person, tray, pot, batch, loaf, liter, piece, or a custom
  organization-defined unit;
- the base scaling amount represented by the stored ingredient quantities;
- optionally, an estimated number of diners served by one scaling unit;
- whether suggestions for the scaling unit are rounded upward to whole units.

When a recipe is scheduled, the application calculates a suggested scaling amount:

1. Start with the scheduled instance's explicit final diner count, initially copied
   from event base attendance.
2. Apply its consumption percentage.
3. Convert the effective attendance through the recipe's estimated diners per
   scaling unit when applicable.
4. Round upward to a whole number when the recipe requests whole-unit suggestions.

The selected scaling amount has one of two modes:

- `suggested`: the selected value follows the current calculated suggestion; or
- `manual`: an explicitly entered selected value is preserved.

A newly scheduled recipe starts in `suggested` mode. Directly editing its selected
scaling amount changes it to `manual`. If attendance, consumption percentage, or
recipe capacity subsequently changes, a manual value remains unchanged while the
new suggestion is shown beside it with a "Use suggestion" action. Using that action
returns the instance to `suggested` mode.

Whole-unit rounding applies only to automatic suggestions. A manual scaling amount
MAY be decimal for every unit, including tray, pot, batch, loaf, and piece. Scaling
amounts MUST be non-negative.

Each recipe ingredient line independently declares one of two MVP scaling
behaviors:

- `proportional` (the default), calculated as:

  `resolved quantity = base ingredient quantity * selected scaling amount / base scaling amount`

- `fixed`, for which the resolved quantity equals the base ingredient quantity
  regardless of the selected scaling amount.

The user MAY override either kind of resolved ingredient quantity afterwards,
including setting it to zero.

### Examples

- A soup defined for 10 persons and selected for 45 persons scales by `45 / 10`.
- A cake defined for one tray, estimated at 20 diners per tray, suggests two trays
  for 40 diners at an 80% consumption factor. The user may replace that suggestion.
- A recipe defined as one pot may suggest three pots while still displaying the
  estimated diner capacity for context.
- Frying oil may use `fixed` scaling while the remaining soup or pastry ingredients
  use proportional scaling in the same recipe.

## Scaling units

The MVP provides these built-in units:

- mass: gram (`g`) and kilogram (`kg`);
- volume: milliliter (`ml`), centiliter (`cl`), deciliter (`dl`), and liter (`l`);
- standardized culinary volume: teaspoon (`tsp`, 5 ml) and tablespoon (`tbsp`,
  15 ml);
- count-like units: piece, package, and bunch;
- additional recipe-scaling units: person, tray, batch, pot, and loaf.

Piece and liter can be used as recipe-scaling units as well as in their applicable
quantity contexts. Organizations MAY add custom named units. A custom name is
organization-authored content and is displayed as entered rather than translated.
Changing a unit's conversion or semantic dimension after it has been used is not
allowed.

Person has an implicit capacity of one diner per unit. Other scaling units use the
recipe version's optional estimated diners per scaling unit. When no usable capacity
is defined, the base scaling amount is the initial suggestion and attendance cannot
produce a new suggestion.

## Future formula compatibility

The MVP exposes only one scaling variable and proportional or fixed ingredient
calculations.
The domain and persistence model SHOULD allow a future recipe version to declare
multiple named inputs and formulas without changing the identity or versioning
semantics of recipes.

The MVP MUST NOT expose arbitrary user formulas.
