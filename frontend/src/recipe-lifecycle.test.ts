import { beforeEach, describe, expect, it } from "vitest";

import {
  queueRecipeLifecycle,
  replayRecipeLifecycle,
} from "./recipe-lifecycle";
import { localDb } from "./local-db";
import { readRecipeCatalog } from "./recipe-catalog";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const recipeId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const versionId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientVersionId = "ace17d2f-8365-4b1f-a80b-34d10425d51c";
const newerIngredientVersionId = "bde17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "bce17d2f-8365-4b1f-a80b-34d10425d51c";

beforeEach(async () => {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
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
        retired_at: null,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-10T12:00:00.000000Z",
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
        recipe_id: recipeId,
        name: "Soup",
        scaling_unit_id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
        base_scaling_amount: "1",
        description: null,
      },
      fieldClocks: {},
      immutable: true,
      updatedAt: "2026-08-10T12:00:00.000000Z",
    },
  ]);
});

describe("offline recipe lifecycle", () => {
  it("keeps a retired referenced tag searchable in the catalog projection", async () => {
    const tagId = "1ce17d2f-8365-4b1f-a80b-34d10425d51c";
    await localDb.canonicalRecords.bulkAdd([
      {
        userId,
        organizationId,
        entityType: "recipe_tag",
        entityId: tagId,
        recordSchemaVersion: 1,
        lifecycle: "retired",
        fields: {
          id: tagId,
          organization_id: organizationId,
          name: "Seasonal",
        },
        fieldClocks: {},
        immutable: false,
        updatedAt: "2026-08-10T12:00:00.000000Z",
      },
      {
        userId,
        organizationId,
        entityType: "recipe_version_tag",
        entityId: "2ce17d2f-8365-4b1f-a80b-34d10425d51c",
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: "2ce17d2f-8365-4b1f-a80b-34d10425d51c",
          organization_id: organizationId,
          recipe_version_id: versionId,
          recipe_tag_id: tagId,
        },
        fieldClocks: {},
        immutable: true,
        updatedAt: "2026-08-10T12:00:00.000000Z",
      },
    ]);
    await expect(
      readRecipeCatalog(userId, organizationId),
    ).resolves.toMatchObject({
      tags: [{ id: tagId, name: "Seasonal" }],
      recipes: [expect.objectContaining({ recipeTagIds: [tagId] })],
    });
  });

  async function addIngredient(
    retired: boolean,
    organization = organizationId,
  ) {
    await localDb.canonicalRecords.bulkAdd([
      {
        userId,
        organizationId: organization,
        entityType: "unit_definition",
        entityId: unitId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: unitId,
          organization_id: organization,
          code: "g",
          dimension: "mass",
          base_unit_factor: "1",
          allows_ingredient_quantity: true,
        },
        fieldClocks: {},
        immutable: true,
        updatedAt: "2026-08-10T12:00:00.000000Z",
      },
      {
        userId,
        organizationId: organization,
        entityType: "ingredient",
        entityId: ingredientId,
        recordSchemaVersion: 1,
        lifecycle: retired ? "retired" : "active",
        fields: {
          id: ingredientId,
          organization_id: organization,
          current_version_id: ingredientVersionId,
          retired_at: retired ? "2026-08-10T12:00:00.000000Z" : null,
        },
        fieldClocks: {},
        immutable: false,
        updatedAt: "2026-08-10T12:00:00.000000Z",
      },
      {
        userId,
        organizationId: organization,
        entityType: "ingredient_version",
        entityId: ingredientVersionId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: ingredientVersionId,
          organization_id: organization,
          ingredient_id: ingredientId,
          name: "Carrot",
          canonical_unit_id: unitId,
          mass_per_canonical_quantity: "1",
        },
        fieldClocks: {},
        immutable: true,
        updatedAt: "2026-08-10T12:00:00.000000Z",
      },
      {
        userId,
        organizationId,
        entityType: "recipe_ingredient_line",
        entityId: crypto.randomUUID(),
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          recipe_version_id: versionId,
          ingredient_version_id: ingredientVersionId,
          base_quantity: "1",
          scaling_behavior: "proportional",
          include_in_portion_weight: true,
          note: "",
        },
        fieldClocks: {},
        immutable: true,
        updatedAt: "2026-08-10T12:00:00.000000Z",
      },
    ]);
  }

  it("warns only when the current recipe version references a retired ingredient", async () => {
    await addIngredient(true);
    await expect(
      readRecipeCatalog(userId, organizationId),
    ).resolves.toMatchObject({
      recipes: [
        expect.objectContaining({ hasRetiredIngredientReference: true }),
      ],
    });

    await localDb.canonicalRecords.update(
      [userId, organizationId, "ingredient", ingredientId],
      { lifecycle: "active", fields: { retired_at: null } },
    );
    await expect(
      readRecipeCatalog(userId, organizationId),
    ).resolves.toMatchObject({
      recipes: [
        expect.objectContaining({ hasRetiredIngredientReference: false }),
      ],
    });
  });

  it("keeps retired referenced names in the normal projection without source units", async () => {
    await addIngredient(true);
    await localDb.canonicalRecords.update(
      [userId, organizationId, "unit_definition", unitId],
      { lifecycle: "retired" },
    );

    const catalog = await readRecipeCatalog(userId, organizationId);
    expect(catalog).not.toHaveProperty("sourceUnits");
    expect(catalog.ingredients).toEqual([
      expect.objectContaining({ name: "Carrot", canonicalUnitName: "g" }),
    ]);
  });

  it("warns for an old immutable version retained by a retired ingredient", async () => {
    await addIngredient(true);
    await localDb.canonicalRecords.update(
      [userId, organizationId, "ingredient", ingredientId],
      {
        fields: {
          id: ingredientId,
          organization_id: organizationId,
          current_version_id: newerIngredientVersionId,
          retired_at: "2026-08-10T12:00:00.000000Z",
        },
      },
    );
    await localDb.canonicalRecords.add({
      userId,
      organizationId,
      entityType: "ingredient_version",
      entityId: newerIngredientVersionId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: newerIngredientVersionId,
        organization_id: organizationId,
        ingredient_id: ingredientId,
        name: "Carrot updated",
        canonical_unit_id: unitId,
        mass_per_canonical_quantity: "1",
      },
      fieldClocks: {},
      immutable: true,
      updatedAt: "2026-08-10T12:00:00.000000Z",
    });
    await expect(
      readRecipeCatalog(userId, organizationId),
    ).resolves.toMatchObject({
      recipes: [
        expect.objectContaining({ hasRetiredIngredientReference: true }),
      ],
      ingredients: expect.arrayContaining([
        expect.objectContaining({
          versionId: ingredientVersionId,
          historical: true,
          retired: true,
        }),
      ]),
    });
  });

  it("fails closed for wrong-organization and invalid ingredient records", async () => {
    await addIngredient(true, "cce17d2f-8365-4b1f-a80b-34d10425d51c");
    await localDb.canonicalRecords.add({
      userId,
      organizationId,
      entityType: "ingredient",
      entityId: "dce17d2f-8365-4b1f-a80b-34d10425d51c",
      recordSchemaVersion: 1,
      lifecycle: "retired",
      fields: {
        organization_id: organizationId,
        current_version_id: ingredientVersionId,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-10T12:00:00.000000Z",
    });
    await expect(
      readRecipeCatalog(userId, organizationId),
    ).resolves.toMatchObject({
      recipes: [
        expect.objectContaining({ hasRetiredIngredientReference: false }),
      ],
    });
  });

  it("queues a retirement and keeps it visible only in the explicit retired view", async () => {
    await queueRecipeLifecycle(userId, organizationId, {
      recipeId,
      operation: "retire",
    });
    const normalCatalog = await readRecipeCatalog(userId, organizationId);
    expect(normalCatalog).toMatchObject({ recipes: [] });
    expect(normalCatalog).not.toHaveProperty("sourceUnits");
    await expect(
      readRecipeCatalog(userId, organizationId, true),
    ).resolves.toMatchObject({
      recipes: [expect.objectContaining({ id: recipeId, retired: true })],
    });
  });

  it("does not replay a stale lifecycle command over a newer canonical clock", async () => {
    await localDb.canonicalRecords.update(
      [userId, organizationId, "recipe", recipeId],
      {
        lifecycle: "retired",
        fields: { retired_at: "2026-08-10T12:00:00.000001Z" },
        fieldClocks: {
          lifecycle: {
            winning_mutation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
            winning_client_wall_time: "2026-08-10T12:00:00.000001Z",
          },
        },
      },
    );
    await replayRecipeLifecycle(userId, organizationId, {
      id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-10T12:00:00.000000Z",
      payload: { recipe_id: recipeId, operation: "restore" },
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });
});
