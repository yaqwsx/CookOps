import { beforeEach, describe, expect, it } from "vitest";

import { queueEventDuplicate } from "./event-duplicate";
import { localDb } from "./local-db";

const userId = "user-a";
const organizationId = "organization-a";
const eventId = "event-a";
const snapshotId = "snapshot-a";

async function seed(lifecycle: "active" | "archived", snapshot: string | null) {
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
      current_archive_snapshot_id: snapshot,
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-08T12:00:00.000Z",
  });
}

describe("archived event duplication intents", () => {
  beforeEach(async () => {
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.outbox.clear(),
    ]);
  });

  it("queues a snapshot-guarded copy without inventing a partial local graph", async () => {
    await seed("archived", snapshotId);
    await queueEventDuplicate(
      userId,
      organizationId,
      eventId,
      snapshotId,
      "Copied event",
    );
    const [source, command] = await Promise.all([
      localDb.canonicalRecords.get([userId, organizationId, "event", eventId]),
      localDb.outbox.toCollection().first(),
    ]);
    expect(source?.fields.lifecycle).toBe("archived");
    expect(command).toMatchObject({
      commandType: "event.duplicate",
      payload: {
        source_event_id: eventId,
        source_archive_snapshot_id: snapshotId,
        name: "Copied event",
      },
      state: "pending",
    });
  });

  it("does not queue a stale or active source", async () => {
    await seed("active", null);
    await expect(
      queueEventDuplicate(
        userId,
        organizationId,
        eventId,
        snapshotId,
        "Copied event",
      ),
    ).rejects.toThrow("event");
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });
});
