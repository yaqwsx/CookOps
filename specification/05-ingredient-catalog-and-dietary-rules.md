# Ingredient Catalog and Dietary Rules

Status: Draft

## Catalog identity

- Ingredients belong to an organization.
- Every recipe ingredient line MUST reference an ingredient from the organization's
  catalog. Free-text recipe ingredients are not allowed.
- Recipe editing MUST provide fuzzy catalog search to tolerate inflection,
  misspellings, and similar names while steering members toward an existing
  ingredient instead of creating a duplicate.
- If no suitable ingredient exists, an authorized member MAY create one without
  leaving the recipe-editing workflow.

## Ingredient versions

- Ingredient changes MUST be versioned.
- An ingredient version is immutable after publication.
- Historical recipe versions and archived events MUST retain their original
  ingredient-version references.
- Active recipe versions SHOULD indicate that a newer ingredient version exists.

Removing an ingredient from the catalog is a reversible retirement rather than a
physical deletion:

- retired ingredients are excluded from normal ingredient search and new recipes;
- existing references remain readable;
- recipes that reference a retired ingredient MUST display a warning;
- members can publish updated recipe versions that replace or remove the retired
  ingredient;
- retirement MUST NOT alter historical recipe versions or archived events.

## Canonical units

Each ingredient version has exactly one canonical quantity unit. Recipe quantities
are normalized to that unit for calculation and aggregation.

The application MUST support compatible built-in unit conversion, including at
least:

- grams and kilograms for mass;
- milliliters, centiliters, deciliters, and liters for volume;
- standardized teaspoons of 5 ml and tablespoons of 15 ml.

A user MAY enter a compatible display unit while editing, but the stored normalized
quantity MUST remain unambiguous. Units of different dimensions are not converted
without explicit ingredient-specific conversion metadata.

Count-like units piece, package, and bunch are built in but are not inherently
interchangeable. Each organization MAY define additional non-SI units for its own
catalog. Custom unit names are organization-authored content and are not
automatically translated.

## Weight estimation

Every published ingredient version MUST provide enough information to calculate its
contribution to prepared-recipe weight:

- mass-based canonical units derive weight directly;
- volume-based units require a positive density or equivalent mass per canonical
  quantity;
- count-like and custom units require a positive average mass per canonical
  quantity.

Publishing an ingredient version without a valid mass conversion is rejected. Mass
conversion metadata is immutable within that ingredient version and is versioned
with all other ingredient data.

Recipe and scheduled-instance weight estimates derive from resolved ingredient
quantities whose recipe lines are marked as included in portion weight. They are
estimates of prepared serving mass, not procurement or transported shopping weight.

## Recipe ingredient lines

A recipe ingredient line contains:

- a reference to a specific ingredient version;
- a quantity expressed in a unit compatible with the ingredient's canonical unit;
- scaling behavior: `proportional` or `fixed`;
- whether the line is included in prepared serving and per-diner weight;
- an optional preparation, product-selection, or shopping note.

Scaling behavior defaults to `proportional`. Inclusion in portion weight defaults
to true. A line excluded from portion weight still participates normally in cost,
shopping generation, dietary warnings, and all non-weight calculations. For
example, a recipe can exclude fryer oil from prepared portion weight without
removing it from the shopping list.

An event-local added ingredient has the same portion-weight flag, defaulting to
true. A local quantity override of a catalog line retains that line's catalog
flag.

The note is copied into an event instance and shopping-list contribution snapshot
so that information such as "large tomatoes" or "finely chopped" remains available
at the point of use.

## Price estimates

- An ingredient version MAY define an estimated price per canonical unit.
- Prices are advisory and are used for recipe, event, and shopping estimates.
- Package rounding and exact store prices are outside the MVP.
- Whether price changes use ordinary ingredient versions or a separately refreshable
  price mechanism remains undecided.

## Store sections

- Each organization manages an ordered list of store sections.
- An ingredient version has a default store section.
- Shopping lists use the ingredient's default section when generated.
- A member MAY override the section for a specific shopping-list item without
  changing the catalog ingredient.

## Dietary labels and requirements

- Each organization manages its own dietary and problematic-ingredient labels.
- Ingredient versions MAY carry any number of these labels.
- A new organization is seeded with common dietary labels and requirement rules.
- Organization administrators MAY modify the seeded definitions and create new
  ones, including changing the definitions of vegetarian and vegan requirements.
- Dietary requirements assigned to named event exceptions are defined through
  incompatibility rules against ingredient labels.
- The warning engine inspects the resolved ingredients of a scheduled recipe
  instance, including event-local added ingredients and quantity overrides.
- An ingredient overridden to zero MUST NOT trigger a warning for that instance.

Every named dietary exception is checked against every scheduled recipe instance;
the MVP does not track whether that person attends a particular meal. A conflict is
informational only:

- it MUST be displayed as a visible warning on the scheduled recipe instance;
- details MUST identify the affected named people, their requirements, and the
  conflicting ingredients;
- it MUST NOT block editing, shopping-list generation, or event operation;
- the MVP does not require acknowledgement or resolution states.

Vegetarian and vegan requirements MUST remain distinct. For example, an
organization can define vegetarian as incompatible with meat and fish labels, while
vegan is incompatible with all animal-product labels. The exact built-in presets
remain to be designed, but all presets remain organization-configurable.
