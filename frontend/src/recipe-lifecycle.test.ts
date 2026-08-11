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
  it("queues a retirement and keeps it visible only in the explicit retired view", async () => {
    await queueRecipeLifecycle(userId, organizationId, {
      recipeId,
      operation: "retire",
    });
    await expect(readRecipeCatalog(userId, organizationId)).resolves.toMatchObject({ recipes: [] });
    await expect(readRecipeCatalog(userId, organizationId, true)).resolves.toMatchObject({
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
