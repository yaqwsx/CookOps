import { beforeEach, describe, expect, it } from "vitest";

import { readRecipeCatalog } from "./recipe-catalog";
import { queueRecipeCreate, validateRecipeCreate } from "./recipe-create";
import { localDb } from "./local-db";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const input = {
  name: "  Čočková polévka  ",
  description: "Bring bread.\r\n",
  scalingUnitId: unitId,
  baseScalingAmount: "10.5",
};

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
}

async function addScalingUnit(allowsRecipeScaling = true) {
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
      code: "person",
      allows_recipe_scaling: allowsRecipeScaling,
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

describe("offline recipe creation", () => {
  beforeEach(clearDatabase);

  it("accepts only the locally safe subset of the recipe.create payload", () => {
    expect(validateRecipeCreate(input)).toBeUndefined();
    for (const candidate of [
      { ...input, name: " " },
      { ...input, name: "x".repeat(201) },
      { ...input, description: "x".repeat(20_001) },
      { ...input, scalingUnitId: "not-a-uuid" },
      { ...input, baseScalingAmount: "0" },
      { ...input, baseScalingAmount: "-1" },
      { ...input, baseScalingAmount: "1e4" },
      { ...input, baseScalingAmount: "01" },
    ]) {
      expect(validateRecipeCreate(candidate)).toBeDefined();
    }
  });

  it("fuzzes untrusted string inputs without accepting a malformed payload", () => {
    for (let index = 0; index < 200; index += 1) {
      const fuzz = String.fromCharCode(index) + "e".repeat(index % 4);
      expect(
        validateRecipeCreate({
          ...input,
          name: fuzz,
          baseScalingAmount: `-${fuzz}`,
        }),
      ).toBeDefined();
    }
  });

  it("atomically queues the typed create intent and complete local recipe projection", async () => {
    await addScalingUnit();

    const recipeId = await queueRecipeCreate(userId, organizationId, input);
    const [command] = await localDb.outbox.toArray();

    expect(command).toEqual(
      expect.objectContaining({
        commandType: "recipe.create",
        state: "pending",
        payload: expect.objectContaining({
          recipe_id: recipeId,
          name: "Čočková polévka",
          scaling_unit_id: unitId,
          base_scaling_amount: "10.5",
          ingredient_lines: [],
          description: "Bring bread.\n",
        }),
      }),
    );
    await expect(readRecipeCatalog(userId, organizationId)).resolves.toEqual({
      scalingUnits: [{ id: unitId, name: "person" }],
      ingredients: [],
      tags: [],
      costs: { [recipeId]: { currency: "", total: "0.00", missingCount: 0 } },
      recipes: [
        expect.objectContaining({
          id: recipeId,
          name: "Čočková polévka",
          baseScalingAmount: "10.5",
          description: "Bring bread.\n",
        }),
      ],
    });
  });

  it("leaves no local partial work when a cached unit is absent or unsuitable", async () => {
    await expect(
      queueRecipeCreate(userId, organizationId, input),
    ).rejects.toThrow("scalingUnit");
    await addScalingUnit(false);
    await expect(
      queueRecipeCreate(userId, organizationId, input),
    ).rejects.toThrow("scalingUnit");
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("does not attach another recipe's immutable version to a cached root", async () => {
    const recipeId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const otherRecipeId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const versionId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
    await localDb.canonicalRecords.bulkAdd([
      {
        userId,
        organizationId,
        entityType: "recipe",
        entityId: recipeId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: recipeId,
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
        entityType: "recipe_version",
        entityId: versionId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: versionId,
          organization_id: organizationId,
          recipe_id: otherRecipeId,
          name: "Leaked version",
          scaling_unit_id: unitId,
          base_scaling_amount: "1",
        },
        fieldClocks: {},
        immutable: true,
        updatedAt: "2026-08-07T12:00:00.000Z",
      },
    ]);

    await expect(
      readRecipeCatalog(userId, organizationId),
    ).resolves.toMatchObject({
      recipes: [],
    });
  });
});
