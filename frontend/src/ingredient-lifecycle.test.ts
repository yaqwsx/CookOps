import { beforeEach, describe, expect, it } from "vitest";

import {
  queueIngredientLifecycle,
  replayIngredientLifecycle,
} from "./ingredient-lifecycle";
import { readIngredientCatalog } from "./ingredient-catalog";
import { localDb } from "./local-db";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const versionId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";

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
      entityType: "ingredient",
      entityId: ingredientId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: ingredientId,
        organization_id: organizationId,
        current_version_id: versionId,
        current_price_estimate_id: null,
        retired_at: null,
        retired_by_user_id: null,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-10T12:00:00.000000Z",
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
        ingredient_id: ingredientId,
        name: "Tomatoes",
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
        allows_ingredient_quantity: true,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-10T12:00:00.000000Z",
    },
  ]);
});

describe("offline ingredient lifecycle", () => {
  it("queues retirement and restoration while retaining the root and version", async () => {
    await queueIngredientLifecycle(userId, organizationId, {
      ingredientId,
      operation: "retire",
    });
    await expect(readIngredientCatalog(userId, organizationId)).resolves.toMatchObject({
      ingredients: [],
    });
    await expect(
      readIngredientCatalog(userId, organizationId, true),
    ).resolves.toMatchObject({
      ingredients: [expect.objectContaining({ id: ingredientId, retired: true })],
    });
    await queueIngredientLifecycle(userId, organizationId, {
      ingredientId,
      operation: "restore",
    });
    await expect(readIngredientCatalog(userId, organizationId)).resolves.toMatchObject({
      ingredients: [expect.objectContaining({ id: ingredientId })],
    });
    await expect(localDb.canonicalRecords.get([
      userId,
      organizationId,
      "ingredient_version",
      versionId,
    ])).resolves.toBeDefined();
  });

  it("does not replay a stale command over a newer canonical lifecycle clock", async () => {
    await localDb.canonicalRecords.update(
      [userId, organizationId, "ingredient", ingredientId],
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
    await replayIngredientLifecycle(userId, organizationId, {
      id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-10T12:00:00.000000Z",
      payload: { ingredient_id: ingredientId, operation: "restore" },
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });
});
