import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CanonicalRecord } from "./local-db";
import { readIngredientCatalog } from "./ingredient-catalog";

const { readVisibleRecords } = vi.hoisted(() => ({ readVisibleRecords: vi.fn() }));
vi.mock("./visible-records", () => ({ readVisibleRecords }));

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const versionId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
const priceId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";

const record = (entityType: CanonicalRecord["entityType"], entityId: string, fields: Record<string, unknown>) => ({
  userId,
  organizationId,
  entityType,
  entityId,
  recordSchemaVersion: 1,
  lifecycle: "active" as const,
  fields,
  fieldClocks: {},
  immutable: entityType !== "ingredient" && entityType !== "organization",
  updatedAt: "2026-08-01T00:00:00.000Z",
});

describe("readIngredientCatalog current prices", () => {
  let selectedPriceId = priceId;
  beforeEach(() => {
    selectedPriceId = priceId;
    readVisibleRecords.mockImplementation(async (_user: string, _org: string, type: string) => {
      if (type === "ingredient") return [record("ingredient", ingredientId, { id: ingredientId, organization_id: organizationId, current_version_id: versionId, current_price_estimate_id: selectedPriceId })];
      if (type === "ingredient_version") return [record("ingredient_version", versionId, { id: versionId, organization_id: organizationId, ingredient_id: ingredientId, name: "Flour", canonical_unit_id: unitId, mass_per_canonical_quantity: "1" })];
      if (type === "unit_definition") return [record("unit_definition", unitId, { id: unitId, organization_id: null, code: "g", dimension: "mass", base_unit_factor: "1", allows_ingredient_quantity: true })];
      if (type === "organization") return [record("organization", organizationId, { id: organizationId, default_currency: "EUR" })];
      if (type === "ingredient_price_estimate") return [record("ingredient_price_estimate", priceId, { id: priceId, organization_id: organizationId, ingredient_id: ingredientId, state: "available", price_amount: "2", priced_quantity: "1", priced_unit_id: unitId, currency: "EUR" }), record("ingredient_price_estimate", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", { id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", organization_id: organizationId, ingredient_id: ingredientId, state: "available", price_amount: "9", priced_quantity: "1", priced_unit_id: unitId, currency: "EUR" })];
      return [];
    });
  });

  it.each([
    ["foreign ingredient", { ingredient_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }],
    ["foreign organization", { organization_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }],
    ["unavailable state", { state: "unavailable" }],
  ] as Array<[string, Record<string, string>]>)("ignores %s while preserving a valid estimate", async (_name, override) => {
    selectedPriceId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    readVisibleRecords.mockImplementation(async (_user: string, _org: string, type: string) => type === "ingredient" ? [record("ingredient", ingredientId, { id: ingredientId, organization_id: organizationId, current_version_id: versionId, current_price_estimate_id: selectedPriceId })] : type === "ingredient_version" ? [record("ingredient_version", versionId, { id: versionId, organization_id: organizationId, ingredient_id: ingredientId, name: "Flour", canonical_unit_id: unitId, mass_per_canonical_quantity: "1" })] : type === "unit_definition" ? [record("unit_definition", unitId, { id: unitId, organization_id: null, code: "g", dimension: "mass", base_unit_factor: "1", allows_ingredient_quantity: true })] : type === "organization" ? [record("organization", organizationId, { id: organizationId, default_currency: "EUR" })] : type === "ingredient_price_estimate" ? [record("ingredient_price_estimate", priceId, { id: priceId, organization_id: organizationId, ingredient_id: ingredientId, state: "available", price_amount: "2", priced_quantity: "1", priced_unit_id: unitId, currency: "EUR" }), record("ingredient_price_estimate", selectedPriceId, { id: selectedPriceId, organization_id: override.organization_id ?? organizationId, ingredient_id: override.ingredient_id ?? ingredientId, state: override.state ?? "available", price_amount: "9", priced_quantity: "1", priced_unit_id: unitId, currency: "EUR" })] : []);
    const result = await readIngredientCatalog(userId, organizationId);
    expect(result.ingredients[0]?.currentPrice).toBeUndefined();
  });

  it("preserves a valid current estimate", async () => {
    const result = await readIngredientCatalog(userId, organizationId);
    expect(result.ingredients[0]?.currentPrice).toEqual({ amount: "2", quantity: "1", unitId, currency: "EUR" });
  });

  it("keeps an active source ingredient whose current unit is retired", async () => {
    readVisibleRecords.mockImplementation(async (_user: string, _org: string, type: string, includeRetired = false) => {
      if (type === "ingredient")
        return [record("ingredient", ingredientId, { id: ingredientId, organization_id: organizationId, current_version_id: versionId })];
      if (type === "ingredient_version")
        return [record("ingredient_version", versionId, { id: versionId, organization_id: organizationId, ingredient_id: ingredientId, name: "Flour", canonical_unit_id: unitId, mass_per_canonical_quantity: "1" })];
      if (type === "unit_definition")
        return includeRetired
          ? [{ ...record("unit_definition", unitId, { id: unitId, organization_id: null, code: "old-g", dimension: "mass", base_unit_factor: "1", allows_ingredient_quantity: true }), lifecycle: "retired" as const }]
          : [];
      return [];
    });
    const result = await readIngredientCatalog(userId, organizationId, true);
    expect(result.units).toEqual([]);
    expect(result.sourceUnits).toEqual([expect.objectContaining({ id: unitId, name: "old-g" })]);
    expect(result.ingredients).toEqual([expect.objectContaining({ id: ingredientId, canonicalUnitName: "old-g" })]);
  });

  it("orders store sections by position key before name", async () => {
    readVisibleRecords.mockImplementation(async (_user: string, _org: string, type: string) =>
      type === "store_section"
        ? [
            record("store_section", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", { id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", organization_id: organizationId, name: "Zeta", position_key: "a" }),
            record("store_section", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", { id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", organization_id: organizationId, name: "Alpha", position_key: "z" }),
          ]
        : [],
    );
    const result = await readIngredientCatalog(userId, organizationId);
    expect(result.storeSections.map((section) => section.name)).toEqual(["Zeta", "Alpha"]);
  });
});
