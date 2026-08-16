import { beforeEach, describe, expect, it } from "vitest";

import { localDb } from "./local-db";
import {
  eventPriceRefreshPending,
  queueEventPriceRefresh,
} from "./event-price-refresh";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";

describe("offline event price refresh", () => {
  beforeEach(async () => {
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.outbox.clear(),
    ]);
  });

  it("queues only an active event's server-current refresh intent", async () => {
    await localDb.canonicalRecords.add({
      userId,
      organizationId,
      entityType: "event",
      entityId: eventId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: eventId,
        organization_id: organizationId,
        lifecycle: "active",
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await Promise.all(
      Array.from({ length: 10 }, () =>
        queueEventPriceRefresh(userId, organizationId, eventId),
      ),
    );
    expect(
      await eventPriceRefreshPending(userId, organizationId, eventId),
    ).toBe(true);
    expect(await localDb.outbox.count()).toBe(1);
    expect((await localDb.outbox.toArray())[0]).toMatchObject({
      commandType: "event.update_price_estimates",
      payload: { event_id: eventId },
      state: "pending",
    });
  });

  it("fuzzes invalid IDs and archived records without enqueueing a refresh", async () => {
    for (let index = 0; index < 200; index += 1)
      await expect(
        queueEventPriceRefresh(userId, organizationId, `${index}`),
      ).rejects.toThrow("event");
    await expect(
      queueEventPriceRefresh(userId, organizationId, eventId),
    ).rejects.toThrow("event");
    await localDb.canonicalRecords.add({
      userId,
      organizationId,
      entityType: "event",
      entityId: eventId,
      recordSchemaVersion: 1,
      lifecycle: "retired",
      fields: {
        id: eventId,
        organization_id: organizationId,
        lifecycle: "archived",
      },
      fieldClocks: {},
      immutable: true,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await expect(
      queueEventPriceRefresh(userId, organizationId, eventId),
    ).rejects.toThrow("event");
    expect(await localDb.outbox.count()).toBe(0);
  });

  it("rejects a canonical active record whose event fields are archived", async () => {
    await localDb.canonicalRecords.add({
      userId,
      organizationId,
      entityType: "event",
      entityId: eventId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: eventId,
        organization_id: organizationId,
        lifecycle: "archived",
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await expect(
      queueEventPriceRefresh(userId, organizationId, eventId),
    ).rejects.toThrow("event");
  });

  it("rejects a retired canonical record even when legacy fields are active", async () => {
    await localDb.canonicalRecords.add({
      userId,
      organizationId,
      entityType: "event",
      entityId: eventId,
      recordSchemaVersion: 1,
      lifecycle: "retired",
      fields: {
        id: eventId,
        organization_id: organizationId,
        lifecycle: "active",
      },
      fieldClocks: {},
      immutable: true,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await expect(
      queueEventPriceRefresh(userId, organizationId, eventId),
    ).rejects.toThrow("event");
  });
});
