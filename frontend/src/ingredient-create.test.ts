import { beforeEach, describe, expect, it } from "vitest";

import { readIngredientCatalog } from "./ingredient-catalog";
import {
  queueIngredientCreate,
  validateIngredientCreate,
} from "./ingredient-create";
import { localDb } from "./local-db";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const input = {
  name: "  Rajčata  ",
  canonicalUnitId: unitId,
  massPerCanonicalQuantity: "1",
};

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
}

async function addUnit(allowsIngredientQuantity = true) {
  await localDb.canonicalRecords.add({
    userId,
    organizationId,
    entityType: "unit_definition",
    entityId: unitId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      id: unitId,
      organization_id: null,
      code: "g",
      dimension: "mass",
      base_unit_factor: "1",
      allows_ingredient_quantity: allowsIngredientQuantity,
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

describe("offline ingredient creation", () => {
  beforeEach(clearDatabase);

  it("accepts only locally safe typed create values", () => {
    expect(validateIngredientCreate(input)).toBeUndefined();
    for (const candidate of [
      { ...input, name: " " },
      { ...input, name: "x".repeat(201) },
      { ...input, canonicalUnitId: "not-a-uuid" },
      { ...input, massPerCanonicalQuantity: "0" },
      { ...input, massPerCanonicalQuantity: "-1" },
      { ...input, massPerCanonicalQuantity: "1e3" },
      { ...input, massPerCanonicalQuantity: "01" },
    ])
      expect(validateIngredientCreate(candidate)).toBeDefined();
  });

  it("fuzzes string inputs without accepting malformed intent", () => {
    for (let index = 0; index < 200; index += 1) {
      const fuzz = String.fromCharCode(index) + "e".repeat(index % 4);
      expect(
        validateIngredientCreate({
          ...input,
          name: fuzz,
          massPerCanonicalQuantity: `-${fuzz}`,
        }),
      ).toBeDefined();
    }
  });

  it("atomically queues the intent and shows its complete optimistic catalog projection", async () => {
    await addUnit();
    const ingredientId = await queueIngredientCreate(
      userId,
      organizationId,
      input,
    );
    const [command] = await localDb.outbox.toArray();
    expect(command).toEqual(
      expect.objectContaining({
        commandType: "ingredient.create",
        state: "pending",
        payload: expect.objectContaining({
          ingredient_id: ingredientId,
          name: "Rajčata",
          canonical_unit_id: unitId,
          mass_per_canonical_quantity: "1",
        }),
      }),
    );
    await expect(
      readIngredientCatalog(userId, organizationId),
    ).resolves.toEqual({
      units: [
        { id: unitId, name: "g", dimension: "mass", baseUnitFactor: "1" },
      ],
      ingredients: [
        {
          id: ingredientId,
          versionId: expect.any(String),
          name: "Rajčata",
          canonicalUnitName: "g",
          massPerCanonicalQuantity: "1",
        },
      ],
    });
  });

  it("leaves no partial work when the cached unit is absent or unsuitable", async () => {
    await expect(
      queueIngredientCreate(userId, organizationId, input),
    ).rejects.toThrow("unit");
    await addUnit(false);
    await expect(
      queueIngredientCreate(userId, organizationId, input),
    ).rejects.toThrow("unit");
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("does not optimistically publish an impossible mass-unit conversion", async () => {
    await addUnit();
    await expect(
      queueIngredientCreate(userId, organizationId, {
        ...input,
        massPerCanonicalQuantity: "2",
      }),
    ).rejects.toThrow("mass");
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("does not attach another ingredient's immutable version to a cached root", async () => {
    const rootId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const otherId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const versionId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
    await addUnit();
    await localDb.canonicalRecords.bulkAdd([
      {
        userId,
        organizationId,
        entityType: "ingredient",
        entityId: rootId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: rootId,
          organization_id: organizationId,
          current_version_id: versionId,
        },
        fieldClocks: {},
        immutable: false,
        updatedAt: "2026-08-07T12:00:00.000Z",
      },
      {
        userId,
        organizationId,
        entityType: "ingredient_version",
        entityId: versionId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: versionId,
          organization_id: organizationId,
          ingredient_id: otherId,
          name: "Leaked version",
          canonical_unit_id: unitId,
          mass_per_canonical_quantity: "1",
        },
        fieldClocks: {},
        immutable: true,
        updatedAt: "2026-08-07T12:00:00.000Z",
      },
    ]);
    await expect(
      readIngredientCatalog(userId, organizationId),
    ).resolves.toMatchObject({ ingredients: [] });
  });
});
