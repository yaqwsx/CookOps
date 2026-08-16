import { describe, expect, it } from "vitest";

import { projectRecipeCatalogUpdate, projectRecipeCost, type CatalogRecipe } from "./recipe-catalog";

const recipe: CatalogRecipe = {
  id: "11111111-1111-4111-8111-111111111111",
  retired: false,
  versionId: "22222222-2222-4222-8222-222222222222",
  name: "Soup",
  description: null,
  scalingUnitId: "33333333-3333-4333-8333-333333333333",
  baseScalingAmount: "1",
  ingredientLines: [
    {
      id: "44444444-4444-4444-8444-444444444444",
      ingredientVersionId: "55555555-5555-4555-8555-555555555555",
      baseQuantity: "500",
      scalingBehavior: "proportional",
      includeInPortionWeight: true,
      note: "",
    },
  ],
  hasRetiredIngredientReference: false,
  catalogUpdateAvailable: false,
  recipeTagIds: [],
};

const ingredient = (
  amount: string,
  unitId = "g",
  currency = "EUR",
  quantity = "1000",
) => ({
  id: "66666666-6666-4666-8666-666666666666",
  versionId: "55555555-5555-4555-8555-555555555555",
  name: "Flour",
  canonicalUnitName: "g",
  canonicalUnitId: "g",
  massPerCanonicalQuantity: "1",
  currentPrice: { amount, quantity, unitId, currency },
});

const units = [
  { id: "g", dimension: "mass", baseUnitFactor: "1" },
  { id: "kg", dimension: "mass", baseUnitFactor: "1000" },
  { id: "piece", dimension: "count" },
];

describe("projectRecipeCost", () => {
  it("recomputes from the current price and converts compatible units", () => {
    expect(
      projectRecipeCost(recipe, [ingredient("2")], units, "EUR"),
    ).toMatchObject({ total: "1.00", missingCount: 0 });
    expect(
      projectRecipeCost(recipe, [ingredient("4")], units, "EUR"),
    ).toMatchObject({ total: "2.00", missingCount: 0 });
    expect(
      projectRecipeCost(
        recipe,
        [ingredient("2", "kg", "EUR", "1")],
        units,
        "EUR",
      ),
    ).toMatchObject({ total: "1.00", missingCount: 0 });
  });

  it("marks missing, incompatible, and cross-currency prices incomplete", () => {
    expect(projectRecipeCost(recipe, [], units, "EUR").missingCount).toBe(1);
    expect(
      projectRecipeCost(recipe, [ingredient("2", "piece")], units, "EUR")
        .missingCount,
    ).toBe(1);
    expect(
      projectRecipeCost(recipe, [ingredient("2", "g", "USD")], units, "EUR")
        .missingCount,
    ).toBe(1);
  });

  it("prices a pinned historical version from the current root estimate", () => {
    const pinned = { ...ingredient("2"), historical: true, canonicalUnitId: "g" };
    expect(projectRecipeCost(recipe, [pinned], units, "EUR")).toMatchObject({
      total: "1.00",
      missingCount: 0,
    });
  });
});

describe("projectRecipeCatalogUpdate", () => {
  it("previews every stale line and converts compatible canonical units", () => {
    const old = { ...ingredient("2", "g"), versionId: "old-version", name: "Old flour", historical: true };
    const current = { ...ingredient("2", "g"), versionId: "new-version", name: "Flour" };
    const projection = projectRecipeCatalogUpdate(
      { ...recipe, ingredientLines: [{ ...recipe.ingredientLines[0], lineKey: "66666666-6666-4666-8666-666666666666", positionKey: "a", ingredientVersionId: "old-version", baseQuantity: "1000" }] },
      [old, current],
      units,
    );
    expect(projection).toMatchObject({ blocked: false, lines: [{ oldQuantity: "1000", newQuantity: "1000", oldIngredient: { name: "Old flour" }, newIngredient: { name: "Flour" } }] });
  });

  it("blocks missing and incompatible metadata instead of guessing quantities", () => {
    const old = { ...ingredient("2", "g"), versionId: "old-version", historical: true };
    const current = { ...ingredient("2", "piece"), versionId: "new-version", canonicalUnitId: "piece" };
    const projection = projectRecipeCatalogUpdate(
      { ...recipe, ingredientLines: [{ ...recipe.ingredientLines[0], lineKey: "66666666-6666-4666-8666-666666666666", positionKey: "a", ingredientVersionId: "old-version" }] },
      [old, current],
      units,
    );
    expect(projection).toMatchObject({ blocked: true, lines: [{ compatible: false, newQuantity: null, reason: "incompatible" }] });
  });

  it("blocks a conversion that cannot be represented exactly", () => {
    const old = { ...ingredient("2", "third"), versionId: "old-version", historical: true, canonicalUnitId: "third" };
    const current = { ...ingredient("2", "g"), versionId: "new-version", canonicalUnitId: "g" };
    const projection = projectRecipeCatalogUpdate(
      { ...recipe, ingredientLines: [{ ...recipe.ingredientLines[0], lineKey: "66666666-6666-4666-8666-666666666666", positionKey: "a", ingredientVersionId: "old-version" }] },
      [old, current],
      [
        ...units,
        { id: "third", name: "third", dimension: "mass", baseUnitFactor: "0.333333333333333333333333333333" },
      ],
    );
    expect(projection).toMatchObject({ blocked: true, lines: [{ compatible: false, newQuantity: null }] });
  });
});
