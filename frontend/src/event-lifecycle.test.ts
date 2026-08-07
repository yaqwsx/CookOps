import { beforeEach, describe, expect, it } from "vitest";

import { queueEventLifecycle } from "./event-lifecycle";
import { localDb } from "./local-db";

const userId = "user-a";
const organizationId = "organization-a";
const eventId = "event-a";

function setOnline(value: boolean) {
  Object.defineProperty(navigator, "onLine", { configurable: true, value });
}

async function seed(lifecycle: "active" | "archived") {
  await localDb.canonicalRecords.add({
    userId,
    organizationId,
    entityType: "event",
    entityId: eventId,
    recordSchemaVersion: 1,
    lifecycle: lifecycle === "active" ? "active" : "retired",
    fields: {
      id: eventId,
      organization_id: organizationId,
      lifecycle,
      archived_at: null,
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

describe("guarded event lifecycle intents", () => {
  beforeEach(async () => {
    setOnline(true);
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.optimisticOverlays.clear(),
      localDb.outbox.clear(),
    ]);
  });

  it("atomically stages an online archive command without a false local archive", async () => {
    await seed("active");
    await queueEventLifecycle(userId, organizationId, eventId, "archive");
    const [canonical, overlay, command] = await Promise.all([
      localDb.canonicalRecords.get([userId, organizationId, "event", eventId]),
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]),
      localDb.outbox.toCollection().first(),
    ]);
    expect(canonical).toMatchObject({ fields: { lifecycle: "active" } });
    expect(overlay).toBeUndefined();
    expect(command).toMatchObject({
      commandType: "event.lifecycle",
      payload: { event_id: eventId, operation: "archive" },
      state: "pending",
    });
  });

  it("rejects offline and invalid lifecycle transitions without partial work", async () => {
    setOnline(false);
    await expect(
      queueEventLifecycle(userId, organizationId, eventId, "archive"),
    ).rejects.toThrow("online");
    setOnline(true);
    await seed("archived");
    await expect(
      queueEventLifecycle(userId, organizationId, eventId, "archive"),
    ).rejects.toThrow("event");
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });
});
