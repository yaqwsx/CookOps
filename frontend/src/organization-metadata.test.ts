import { beforeEach, describe, expect, it } from "vitest";
import { localDb } from "./local-db";
import {
  queueOrganizationMetadata,
  replayOrganizationMetadata,
} from "./organization-metadata";

const userId = "11111111-1111-4111-8111-111111111111";
const organizationId = "22222222-2222-4222-8222-222222222222";
const now = "2026-08-22T12:00:00.000Z";

async function clear() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
    localDb.syncMetadata.clear(),
  ]);
}
async function addOrganization(lifecycle: "active" | "retired" = "active") {
  await localDb.canonicalRecords.put({
    userId,
    organizationId,
    entityType: "organization",
    entityId: organizationId,
    recordSchemaVersion: 1,
    lifecycle,
    fields: {
      id: organizationId,
      organization_id: organizationId,
      name: "Old",
      description: null,
      default_currency: "CZK",
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: now,
  });
}

describe("organization metadata queue", () => {
  beforeEach(async () => {
    await clear();
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: true,
    });
  });
  it("queues the exact command and optimistic organization projection", async () => {
    await addOrganization();
    await expect(
      queueOrganizationMetadata(userId, organizationId, {
        name: "New",
        description: "Note",
        default_currency: "EUR",
      }),
    ).resolves.toMatch(/[0-9a-f-]{36}/);
    await expect(localDb.outbox.toCollection().first()).resolves.toMatchObject({
      commandType: "organization.update",
      payload: {
        organization_id: organizationId,
        name: "New",
        description: "Note",
        default_currency: "EUR",
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "organization",
        organizationId,
      ]),
    ).resolves.toMatchObject({
      fields: { name: "New", default_currency: "EUR" },
    });
  });
  it("accepts offline only with a valid lease and fails closed for retired or malformed replay", async () => {
    await addOrganization();
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: false,
    });
    await localDb.syncMetadata.put({
      userId,
      organizationId,
      activity: "caughtUp",
      lastAuthorizedAt: "2026-08-22T11:00:00.000Z",
    });
    await expect(
      queueOrganizationMetadata(userId, organizationId, {
        name: "Offline",
        description: null,
        default_currency: "CZK",
      }),
    ).resolves.toBeDefined();
    await clear();
    await addOrganization("retired");
    await expect(
      queueOrganizationMetadata(userId, organizationId, {
        name: "No",
        description: null,
        default_currency: "CZK",
      }),
    ).resolves.toBeUndefined();
    await replayOrganizationMetadata(userId, organizationId, {
      id: "bad",
      actionAt: "bad",
      payload: {},
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });
  it("keeps a newer field clock when replaying stale metadata", async () => {
    await addOrganization();
    await localDb.canonicalRecords.update(
      [userId, organizationId, "organization", organizationId],
      {
        fieldClocks: {
          name: {
            winning_mutation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
            winning_client_wall_time: "2026-08-22T12:00:00.000001+00:00",
          },
        },
      },
    );
    await replayOrganizationMetadata(userId, organizationId, {
      id: "33333333-3333-4333-8333-333333333333",
      actionAt: "2026-08-22T11:00:00.000Z",
      payload: {
        organization_id: organizationId,
        name: "Stale",
        description: null,
        default_currency: "CZK",
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "organization",
        organizationId,
      ]),
    ).resolves.toMatchObject({ fields: { name: "Old" } });
  });
  it("rejects an unsupported currency without overlay or outbox", async () => {
    await expect(
      queueOrganizationMetadata(userId, organizationId, {
        name: "Invalid",
        description: null,
        default_currency: "ZZZ",
      }),
    ).resolves.toBeUndefined();
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "organization",
        organizationId,
      ]),
    ).resolves.toBeUndefined();
    await expect(
      localDb.outbox.toCollection().first(),
    ).resolves.toBeUndefined();
  });
});
