import { beforeEach, describe, expect, it } from "vitest";
import { localDb } from "./local-db";
import { queueIngredientVersionPublish, replayIngredientVersionPublish } from "./ingredient-version-publish";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const baseId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
const sectionId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";

beforeEach(async () => {
  await Promise.all([localDb.canonicalRecords.clear(), localDb.optimisticOverlays.clear(), localDb.outbox.clear()]);
  await localDb.canonicalRecords.bulkAdd([
    { userId, organizationId, entityType: "ingredient", entityId: ingredientId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: ingredientId, organization_id: organizationId, current_version_id: baseId }, fieldClocks: {}, immutable: false, updatedAt: "2026-01-01T00:00:00Z" },
    { userId, organizationId, entityType: "ingredient_version", entityId: baseId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: baseId, ingredient_id: ingredientId, canonical_unit_id: unitId, dietary_tag_ids: [] }, fieldClocks: {}, immutable: true, updatedAt: "2026-01-01T00:00:00Z" },
    { userId, organizationId, entityType: "unit_definition", entityId: unitId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: unitId, organization_id: null, dimension: "mass", base_unit_factor: "1", allows_ingredient_quantity: true }, fieldClocks: {}, immutable: false, updatedAt: "2026-01-01T00:00:00Z" },
    { userId, organizationId, entityType: "store_section", entityId: sectionId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: sectionId, organization_id: organizationId, name: "Produce" }, fieldClocks: {}, immutable: false, updatedAt: "2026-01-01T00:00:00Z" },
  ]);
});

describe("offline ingredient version publishing", () => {
  it("queues exact immutable payload and advances only optimistic pointer", async () => {
    const versionId = await queueIngredientVersionPublish(userId, organizationId, { ingredientId, basedOnVersionId: baseId, name: "Tomatoes", canonicalUnitId: unitId, massPerCanonicalQuantity: "1", dietaryTagIds: [] });
    const command = await localDb.outbox.toArray();
    expect(command[0]).toMatchObject({ commandType: "ingredient.publish_version", payload: { ingredient_id: ingredientId, based_on_version_id: baseId, ingredient_version_id: versionId } });
    await expect(localDb.optimisticOverlays.get([userId, organizationId, "ingredient", ingredientId])).resolves.toMatchObject({ fields: { current_version_id: versionId } });
    await expect(localDb.canonicalRecords.get([userId, organizationId, "ingredient_version", baseId])).resolves.toMatchObject({ fields: { id: baseId } });
  });

  it("fails closed on stale replay and keeps the command available for failure handling", async () => {
    const command = { id: crypto.randomUUID(), actionAt: "2026-01-01T00:00:00Z", payload: { ingredient_id: ingredientId, based_on_version_id: "00000000-0000-0000-0000-000000000001", ingredient_version_id: crypto.randomUUID(), name: "Tomatoes", canonical_unit_id: unitId, mass_per_canonical_quantity: "1", dietary_tag_ids: [] } };
    await expect(replayIngredientVersionPublish(userId, organizationId, command)).resolves.toBe(false);
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("rejects malformed logical operation IDs before writing", async () => {
    await expect(queueIngredientVersionPublish(userId, organizationId, { ingredientId, basedOnVersionId: baseId, name: "Tomatoes", canonicalUnitId: unitId, massPerCanonicalQuantity: "1", dietaryTagIds: [], logicalOperationId: "bad" })).rejects.toThrow("invalid");
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("includes the selected section in the publish payload and optimistic version", async () => {
    const versionId = await queueIngredientVersionPublish(userId, organizationId, { ingredientId, basedOnVersionId: baseId, name: "Tomatoes", canonicalUnitId: unitId, massPerCanonicalQuantity: "1", dietaryTagIds: [], defaultStoreSectionId: sectionId });
    expect((await localDb.outbox.toArray())[0]?.payload).toMatchObject({ default_store_section_id: sectionId });
    await expect(localDb.optimisticOverlays.get([userId, organizationId, "ingredient_version", versionId])).resolves.toMatchObject({ fields: { default_store_section_id: sectionId } });
  });

  it.each(["retired", "foreign"] as const)("fails closed for a %s selected section during queue and replay", async (kind) => {
    await localDb.canonicalRecords.update([userId, organizationId, "store_section", sectionId], kind === "retired" ? { lifecycle: "retired" } : { fields: { organization_id: "ace17d2f-8365-4b1f-a80b-34d10425d51c" } });
    const input = { ingredientId, basedOnVersionId: baseId, name: "Tomatoes", canonicalUnitId: unitId, massPerCanonicalQuantity: "1", dietaryTagIds: [], defaultStoreSectionId: sectionId };
    await expect(queueIngredientVersionPublish(userId, organizationId, input)).rejects.toThrow("unavailable");
    const command = { id: crypto.randomUUID(), actionAt: "2026-01-01T00:00:00Z", payload: { ingredient_id: ingredientId, based_on_version_id: baseId, ingredient_version_id: crypto.randomUUID(), name: "Tomatoes", canonical_unit_id: unitId, mass_per_canonical_quantity: "1", dietary_tag_ids: [], default_store_section_id: sectionId } };
    await expect(replayIngredientVersionPublish(userId, organizationId, command)).resolves.toBe(false);
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });
});
