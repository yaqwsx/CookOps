import { beforeEach, describe, expect, it } from "vitest";

import { localDb } from "./local-db";
import {
  queueRecipeVersionPublish,
  recipeVersionTagId,
  replayRecipeVersionPublish,
  validateRecipeVersion,
} from "./recipe-publish";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const recipeId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const versionId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientVersionId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
const tagId = "00000000-0000-0000-0000-000000000002";

const input = {
  recipeId,
  basedOnVersionId: versionId,
  name: "Soup",
  description: "",
  scalingUnitId: unitId,
  baseScalingAmount: "4",
  recipeTagIds: [],
  ingredientLines: [
    {
      id: "line",
      ingredientVersionId,
      baseQuantity: "500",
      scalingBehavior: "proportional" as const,
      includeInPortionWeight: true,
      note: "",
    },
  ],
};

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
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-01-01T00:00:00Z",
    },
    {
      userId,
      organizationId,
      entityType: "recipe_version",
      entityId: versionId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { id: versionId, recipe_id: recipeId },
      fieldClocks: {},
      immutable: true,
      updatedAt: "2026-01-01T00:00:00Z",
    },
    {
      userId,
      organizationId,
      entityType: "ingredient_version",
      entityId: ingredientVersionId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { id: ingredientVersionId },
      fieldClocks: {},
      immutable: true,
      updatedAt: "2026-01-01T00:00:00Z",
    },
    {
      userId,
      organizationId,
      entityType: "unit_definition",
      entityId: unitId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { id: unitId, allows_recipe_scaling: true },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-01-01T00:00:00Z",
    },
  ]);
});

describe("offline recipe version publication", () => {
  it("rejects malformed quantities and identities", () => {
    expect(validateRecipeVersion({ ...input, baseScalingAmount: "0" })).toBe(
      "baseScalingAmount",
    );
    expect(
      validateRecipeVersion({
        ...input,
        ingredientLines: [{ ...input.ingredientLines[0], baseQuantity: "1e3" }],
      }),
    ).toBe("ingredientLines");
  });

  it("rejects a blank ingredient version before any local publication write", async () => {
    await expect(
      queueRecipeVersionPublish(userId, organizationId, {
        ...input,
        ingredientLines: [{ ...input.ingredientLines[0], ingredientVersionId: "" }],
      }),
    ).rejects.toThrow("ingredientLines");
    await expect(localDb.outbox.count()).resolves.toBe(0);
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("fuzzes untrusted line quantities without admitting malformed commands", () => {
    for (let index = 0; index < 200; index += 1) {
      expect(
        validateRecipeVersion({
          ...input,
          ingredientLines: [
            { ...input.ingredientLines[0], baseQuantity: `${index}e-${index}` },
          ],
        }),
      ).toBe("ingredientLines");
    }
  });

  it("atomically queues a based-on immutable version and moves only the local root pointer", async () => {
    const published = await queueRecipeVersionPublish(
      userId,
      organizationId,
      input,
    );
    const [command] = await localDb.outbox.toArray();
    expect(command).toMatchObject({
      commandType: "recipe.publish_version",
      payload: {
        recipe_id: recipeId,
        recipe_version_id: published,
        based_on_version_id: versionId,
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "recipe",
        recipeId,
      ]),
    ).resolves.toMatchObject({ fields: { current_version_id: published } });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "recipe_version",
        published,
      ]),
    ).resolves.toMatchObject({
      fields: { based_on_version_id: versionId },
      immutable: true,
    });
  });

  it("does not replay a stale publication over a newer canonical version", async () => {
    await queueRecipeVersionPublish(userId, organizationId, input);
    const [command] = await localDb.outbox.toArray();
    await localDb.optimisticOverlays.clear();
    await localDb.canonicalRecords.update(
      [userId, organizationId, "recipe", recipeId],
      {
        fields: {
          id: recipeId,
          organization_id: organizationId,
          current_version_id: unitId,
        },
      },
    );
    await replayRecipeVersionPublish(userId, organizationId, command);
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("chains offline versions through the visible pending pointer", async () => {
    const first = await queueRecipeVersionPublish(
      userId,
      organizationId,
      input,
    );
    await expect(
      queueRecipeVersionPublish(userId, organizationId, {
        ...input,
        basedOnVersionId: first,
        name: "Soup two",
      }),
    ).resolves.toMatch(/^[0-9a-f-]{36}$/);
    await expect(localDb.outbox.count()).resolves.toBe(2);
  });

  it("uses the backend's deterministic tag-association identity", async () => {
    await localDb.canonicalRecords.add({
      userId,
      organizationId,
      entityType: "recipe_tag",
      entityId: tagId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { id: tagId },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-01-01T00:00:00Z",
    });
    await queueRecipeVersionPublish(userId, organizationId, {
      ...input,
      recipeTagIds: [tagId],
    });
    const [command] = await localDb.outbox.toArray();
    const version = command.payload.recipe_version_id as string;
    const tags = (await localDb.optimisticOverlays.toArray()).filter(
      (record) => record.entityType === "recipe_version_tag",
    );
    expect(tags).toHaveLength(1);
    expect(tags[0]?.fields).toMatchObject({
      recipe_version_id: version,
      recipe_tag_id: tagId,
    });
    await expect(
      recipeVersionTagId("00000000-0000-0000-0000-000000000001", tagId),
    ).resolves.toBe("c2edce83-7abc-5bd5-b677-6fe06cd6b9c4");
  });
});
