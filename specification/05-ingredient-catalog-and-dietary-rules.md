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

The application MUST support compatible SI unit conversion, including at least:

- grams and kilograms for mass;
- milliliters and liters for volume.

A user MAY enter a compatible display unit while editing, but the stored normalized
quantity MUST remain unambiguous. Units of different dimensions are not converted
without explicit ingredient-specific conversion metadata.

Count-like and culinary units such as piece, package, bunch, loaf, tray, or custom
units are not inherently interchangeable. Each organization MAY define additional
non-SI units for its own catalog.

## Weight estimation

Ingredient versions MUST support calculation of their contribution to total recipe
weight:

- mass-based canonical units derive weight directly;
- volume-based units require a mass-per-canonical-unit value or equivalent density;
- count-like and custom units require an average mass per canonical unit;
- an ingredient for which no useful mass can be established MUST be represented as
  having unknown mass, not zero mass.

Recipe and scheduled-instance weight estimates MUST derive from resolved ingredient
quantities. When one or more ingredients have unknown mass, the numeric estimate
remains visible and is accompanied by a warning icon. The icon's tooltip MUST list
or summarize the missing mass information. No additional persistent warning text is
required in the compact view.

## Recipe ingredient lines

A recipe ingredient line contains:

- a reference to a specific ingredient version;
- a quantity expressed in a unit compatible with the ingredient's canonical unit;
- an optional preparation, product-selection, or shopping note.

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
