import { beforeEach, describe, expect, it } from "vitest";

import {
  queueCatalogConfiguration,
  replayCatalogConfiguration,
} from "./catalog-configuration";
import { localDb } from "./local-db";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";

beforeEach(async () => {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
});

describe("catalog configuration outbox", () => {
  it("keeps a create command and complete optimistic record together", async () => {
    const id = await queueCatalogConfiguration(
      userId,
      organizationId,
      "recipe_tag",
      "create",
      { name: "Soup", color: "#112233" },
    );
    await expect(localDb.outbox.toArray()).resolves.toEqual([
      expect.objectContaining({
        commandType: "catalog_configuration.mutate",
        payload: expect.objectContaining({
          entity_id: id,
          entity_kind: "recipe_tag",
        }),
      }),
    ]);
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "recipe_tag",
        id,
      ]),
    ).resolves.toEqual(
      expect.objectContaining({
        lifecycle: "active",
        fields: expect.objectContaining({ name: "Soup" }),
      }),
    );
  });

  it("replays an update without discarding canonical fields", async () => {
    const id = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    await localDb.canonicalRecords.put({
      userId,
      organizationId,
      entityType: "recipe_tag",
      entityId: id,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id,
        organization_id: organizationId,
        name: "Old",
        color: "#111111",
        retired_at: null,
      },
      fieldClocks: { lifecycle: { mutationId: "old", actionAt: "2026-08-08T00:00:00Z" } },
      immutable: false,
      updatedAt: "2026-08-08T00:00:00Z",
    });
    await replayCatalogConfiguration(userId, organizationId, {
      id: "7ce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-08T00:00:01Z",
      payload: {
        entity_id: id,
        entity_kind: "recipe_tag",
        operation: "update",
        name: "New",
        color: "#222222",
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "recipe_tag",
        id,
      ]),
    ).resolves.toMatchObject({
      fields: { name: "New", color: "#222222", retired_at: null },
      fieldClocks: {
        lifecycle: { mutationId: "old" },
        name: { mutationId: "7ce17d2f-8365-4b1f-a80b-34d10425d51c" },
        color: { mutationId: "7ce17d2f-8365-4b1f-a80b-34d10425d51c" },
      },
    });
  });

  it("queues a store-section reorder without replacing unrelated clocks", async () => {
    const id = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
    await localDb.canonicalRecords.put({
      userId,
      organizationId,
      entityType: "store_section",
      entityId: id,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { id, organization_id: organizationId, name: "Pantry", position_key: "a" },
      fieldClocks: { lifecycle: { mutationId: "created", actionAt: "2026-08-08T00:00:00Z" } },
      immutable: false,
      updatedAt: "2026-08-08T00:00:00Z",
    });
    await queueCatalogConfiguration(
      userId,
      organizationId,
      "store_section",
      "update",
      { name: "Pantry", position_key: "z" },
      id,
    );
    await expect(
      localDb.optimisticOverlays.get([userId, organizationId, "store_section", id]),
    ).resolves.toMatchObject({
      fields: { position_key: "z" },
      fieldClocks: { lifecycle: { mutationId: "created" }, name: expect.any(Object), position_key: expect.any(Object) },
    });
  });

  it("does not replay stale name, color, position, or lifecycle changes", async () => {
    const records = [
      {
        entityType: "recipe_tag" as const,
        entityId: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
        fields: { name: "Fresh", color: "#112233", retired_at: null },
        clocks: { name: {}, color: {} },
        payload: { operation: "update", name: "Stale", color: "#445566" },
      },
      {
        entityType: "store_section" as const,
        entityId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
        fields: { name: "Fresh", position_key: "z", retired_at: null },
        clocks: { name: {}, position_key: {} },
        payload: { operation: "update", name: "Stale", position_key: "a" },
      },
      {
        entityType: "dietary_tag" as const,
        entityId: "ace17d2f-8365-4b1f-a80b-34d10425d51c",
        fields: { name: "Active", color: "#112233", retired_at: null },
        clocks: { lifecycle: {} },
        payload: { operation: "retire" },
      },
    ];
    for (const record of records) {
      await localDb.canonicalRecords.put({
        userId,
        organizationId,
        entityType: record.entityType,
        entityId: record.entityId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: { id: record.entityId, organization_id: organizationId, ...record.fields },
        fieldClocks: Object.fromEntries(
          Object.keys(record.clocks).map((field) => [
            field,
            {
              winning_client_wall_time: "2026-08-09T00:00:00Z",
              winning_mutation_id: "ffffffff-ffff-ffff-ffff-ffffffffffff",
            },
          ]),
        ),
        immutable: false,
        updatedAt: "2026-08-09T00:00:00Z",
      });
      await replayCatalogConfiguration(userId, organizationId, {
        id: "00000000-0000-0000-0000-000000000001",
        actionAt: "2026-08-08T00:00:00Z",
        payload: {
          entity_id: record.entityId,
          entity_kind: record.entityType,
          ...record.payload,
        },
      });
      await expect(
        localDb.optimisticOverlays.get([
          userId,
          organizationId,
          record.entityType,
          record.entityId,
        ]),
      ).resolves.toBeUndefined();
    }
  });

  it("uses the mutation id to break equal-time clock ties", async () => {
    const id = "bce17d2f-8365-4b1f-a80b-34d10425d51c";
    await localDb.canonicalRecords.put({
      userId,
      organizationId,
      entityType: "recipe_tag",
      entityId: id,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { id, organization_id: organizationId, name: "Winner", color: "#112233" },
      fieldClocks: {
        name: {
          winning_client_wall_time: "2026-08-08T00:00:00+00:00",
          winning_mutation_id: "ffffffff-ffff-ffff-ffff-ffffffffffff",
        },
      },
      immutable: false,
      updatedAt: "2026-08-08T00:00:00Z",
    });
    await replayCatalogConfiguration(userId, organizationId, {
      id: "00000000-0000-0000-0000-000000000001",
      actionAt: "2026-08-08T00:00:00Z",
      payload: {
        entity_id: id,
        entity_kind: "recipe_tag",
        operation: "update",
        name: "Loser",
        color: "#112233",
      },
    });
    await expect(
      localDb.optimisticOverlays.get([userId, organizationId, "recipe_tag", id]),
    ).resolves.toMatchObject({ fields: { name: "Winner", color: "#112233" } });
  });
});
