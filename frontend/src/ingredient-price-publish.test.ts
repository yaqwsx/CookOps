import { beforeEach, expect, it, vi } from "vitest";
import { localDb } from "./local-db";
import { bootstrapOrganization } from "./sync-bootstrap";
import {
  queueIngredientPricePublish,
  replayIngredientPricePublish,
} from "./ingredient-price-publish";

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
      entityType: "organization",
      entityId: organizationId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { default_currency: "EUR" },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    },
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
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    },
    {
      userId,
      organizationId,
      entityType: "ingredient_version",
      entityId: versionId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { ingredient_id: ingredientId, canonical_unit_id: unitId },
      fieldClocks: {},
      immutable: true,
      updatedAt: new Date().toISOString(),
    },
    {
      userId,
      organizationId,
      entityType: "unit_definition",
      entityId: unitId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        organization_id: null,
        dimension: "mass",
        allows_ingredient_quantity: true,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    },
  ]);
});

it("queues a strict price and rejects malformed replay", async () => {
  const id = await queueIngredientPricePublish(userId, organizationId, {
    ingredientId,
    amount: "12.50",
    pricedQuantity: "1",
    unitId,
    currency: "EUR",
  });
  const overlays = await localDb.optimisticOverlays.toArray();
  expect(id).toMatch(/[0-9a-f-]{36}/);
  expect(
    overlays.find((item) => item.entityType === "ingredient")?.fields
      .current_price_estimate_id,
  ).toBe(id);
  expect(
    await replayIngredientPricePublish(userId, organizationId, {
      id: "bad",
      actionAt: "bad",
      payload: {},
    }),
  ).toBe(false);
});

it("accepts a zero price and rejects oversized decimal text", async () => {
  await expect(
    queueIngredientPricePublish(userId, organizationId, {
      ingredientId,
      amount: "0",
      pricedQuantity: "1",
      unitId,
      currency: "EUR",
    }),
  ).resolves.toMatch(/[0-9a-f-]{36}/);
  await expect(
    queueIngredientPricePublish(userId, organizationId, {
      ingredientId,
      amount: "1".repeat(101),
      pricedQuantity: "1",
      unitId,
      currency: "EUR",
    }),
  ).rejects.toThrow("unavailable");
});

it("replays a pending price overlay after bootstrap replaces canonical records", async () => {
  await queueIngredientPricePublish(userId, organizationId, {
    ingredientId,
    amount: "12",
    pricedQuantity: "1",
    unitId,
    currency: "EUR",
  });
  const response = {
    sync_schema_version: 1,
    server_time: new Date().toISOString(),
    cursor: "cursor",
    records: [
      {
        organization_id: organizationId,
        entity_id: organizationId,
        entity_kind: "organization",
        operation: "upsert",
        payload: {
          record_schema_version: 1,
          record: { id: organizationId, default_currency: "EUR" },
        },
      },
      {
        organization_id: organizationId,
        entity_id: ingredientId,
        entity_kind: "ingredient",
        operation: "upsert",
        payload: {
          record_schema_version: 1,
          record: {
            id: ingredientId,
            organization_id: organizationId,
            current_version_id: versionId,
            current_price_estimate_id: null,
            lifecycle: "active",
          },
        },
      },
      {
        organization_id: organizationId,
        entity_id: versionId,
        entity_kind: "ingredient_version",
        operation: "upsert",
        payload: {
          record_schema_version: 1,
          record: {
            id: versionId,
            ingredient_id: ingredientId,
            canonical_unit_id: unitId,
          },
        },
      },
      {
        organization_id: organizationId,
        entity_id: unitId,
        entity_kind: "unit_definition",
        operation: "upsert",
        payload: {
          record_schema_version: 1,
          record: {
            id: unitId,
            organization_id: null,
            dimension: "mass",
            allows_ingredient_quantity: true,
          },
        },
      },
    ],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ),
  );
  await bootstrapOrganization(userId, organizationId);
  const overlays = await localDb.optimisticOverlays.toArray();
  expect(overlays.map((item) => item.entityType)).toEqual(
    expect.arrayContaining(["ingredient", "ingredient_price_estimate"]),
  );
  vi.unstubAllGlobals();
});
