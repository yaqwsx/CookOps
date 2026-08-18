import { beforeEach, expect, it } from "vitest";

import { localDb } from "./local-db";
import { readVisibleRecords } from "./visible-records";

const userId = "user-a";
const organizationId = "organization-a";
const ingredientId = "00000000-0000-4000-8000-000000000001";

beforeEach(async () => {
  await Promise.all([localDb.canonicalRecords.clear(), localDb.optimisticOverlays.clear()]);
});

it("keeps a canonical tombstone authoritative over an ingredient restore overlay", async () => {
  await localDb.canonicalRecords.add({
    userId,
    organizationId,
    entityType: "ingredient",
    entityId: ingredientId,
    recordSchemaVersion: 1,
    lifecycle: "tombstone",
    fields: { id: ingredientId, retired_at: null },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
  await localDb.optimisticOverlays.add({
    userId,
    organizationId,
    entityType: "ingredient",
    entityId: ingredientId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: { id: ingredientId, retired_at: null },
    fieldClocks: {
      lifecycle: {
        mutationId: "11111111-1111-4111-8111-111111111111",
        actionAt: "2026-08-07T13:00:00.000Z",
      },
    },
    immutable: false,
    updatedAt: "2026-08-07T13:00:00.000Z",
  });

  await expect(readVisibleRecords(userId, organizationId, "ingredient")).resolves.toEqual([]);
});
