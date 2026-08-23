import { beforeEach, describe, expect, it } from "vitest";

import { localDb } from "./local-db";
import {
  queueEventMetadataUpdate,
  replayEventMetadataUpdate,
} from "./event-metadata";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "3d8b2b21-c378-4574-9e46-9338c81305ef";

async function seed() {
  await localDb.canonicalRecords.put({
    userId,
    organizationId,
    entityType: "event",
    entityId: eventId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      id: eventId,
      organization_id: organizationId,
      name: "Summer cooking",
      location: null,
      general_note: null,
      budget_amount: "10",
      lifecycle: "active",
      start_date: "2026-08-10",
      end_date: "2026-08-12",
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-10T12:00:00.000Z",
  });
}

describe("event metadata offline command", () => {
  beforeEach(async () => {
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.optimisticOverlays.clear(),
      localDb.outbox.clear(),
    ]);
    await seed();
  });

  it("queues all metadata fields into one optimistic command", async () => {
    await queueEventMetadataUpdate(userId, organizationId, {
      eventId,
      name: "  Summer kitchen  ",
      location: "  Prague  ",
      budgetAmount: "25.50",
      generalNote: "Bring pots",
      startDate: "2026-08-10",
      endDate: "2026-08-12",
    });
    await expect(localDb.outbox.toArray()).resolves.toEqual([
      expect.objectContaining({
        commandType: "event.metadata",
        payload: {
          event_id: eventId,
          name: "Summer kitchen",
          location: "Prague",
          budget_amount: "25.50",
          general_note: "Bring pots",
          start_date: "2026-08-10",
          end_date: "2026-08-12",
        },
      }),
    ]);
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]),
    ).resolves.toMatchObject({
      fields: {
        name: "Summer kitchen",
        location: "Prague",
        budget_amount: "25.50",
        general_note: "Bring pots",
      },
    });
  });

  it("does not resurrect a stale fractional-clock replay", async () => {
    const canonical = await localDb.canonicalRecords.get([
      userId,
      organizationId,
      "event",
      eventId,
    ]);
    if (!canonical) throw new Error("seed failed");
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event", eventId],
      {
        fields: {
          ...canonical.fields,
          name: "Canonical winner",
        },
        fieldClocks: {
          name: {
            winning_client_wall_time: "2026-08-10T12:00:00.000001Z",
            winning_mutation_id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
          },
        },
      },
    );
    await replayEventMetadataUpdate(userId, organizationId, {
      id: "fce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-10T12:00:00.000000Z",
      payload: {
        event_id: eventId,
        name: "Stale",
        location: null,
        budget_amount: "10",
        general_note: null,
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]),
    ).resolves.toMatchObject({
      fields: {
        name: "Canonical winner",
        location: null,
        budget_amount: "10",
        general_note: null,
      },
    });
    await replayEventMetadataUpdate(userId, organizationId, {
      id: "fce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-10T12:00:00.000001Z",
      payload: {
        event_id: eventId,
        name: "Equal timestamp loser",
        location: null,
        budget_amount: "10",
        general_note: null,
        extra: true,
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]),
    ).resolves.toMatchObject({ fields: { name: "Canonical winner" } });
  });

  it("replays legacy metadata without overwriting the event date range", async () => {
    await replayEventMetadataUpdate(userId, organizationId, {
      id: "fce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-10T12:00:00.000000Z",
      payload: {
        event_id: eventId,
        name: "Legacy",
        location: null,
        budget_amount: "11",
        general_note: null,
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]),
    ).resolves.toMatchObject({
      fields: {
        name: "Legacy",
        budget_amount: "11",
        start_date: "2026-08-10",
        end_date: "2026-08-12",
      },
    });
  });

  it("rejects malformed modern replay dates", async () => {
    await replayEventMetadataUpdate(userId, organizationId, {
      id: "fce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-10T12:00:00.000000Z",
      payload: {
        event_id: eventId,
        name: "Bad",
        location: null,
        budget_amount: "11",
        general_note: null,
        start_date: "2026-02-30",
        end_date: "2026-03-01",
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]),
    ).resolves.toBeUndefined();
  });

  it("ignores an oversized decimal replay before it reaches the overlay", async () => {
    await replayEventMetadataUpdate(userId, organizationId, {
      id: "4d8b2b21-c378-4574-9e46-9338c81305ef",
      actionAt: "2026-08-10T12:00:00.000000Z",
      payload: {
        event_id: eventId,
        name: "Oversized",
        location: null,
        budget_amount: "1e100000000",
        general_note: null,
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]),
    ).resolves.toBeUndefined();
  });
});
