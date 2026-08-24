import { beforeEach, expect, it } from "vitest";
import { localDb } from "./local-db";
import {
  queueScheduledRecipeNote,
  replayScheduledRecipeNote,
} from "./scheduled-recipe";

const user = "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  organization = "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  event = "6ce17d2f-8365-4b1f-a80b-34d10425d51c",
  scheduled = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const base = {
  userId: user,
  organizationId: organization,
  recordSchemaVersion: 1,
  immutable: false,
  updatedAt: "2026-08-08T12:00:00.000Z",
};
beforeEach(async () => {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
});

it("canonicalizes and queues notes, rejecting invalid input", async () => {
  await localDb.canonicalRecords.bulkPut([
    {
      ...base,
      entityType: "event",
      entityId: event,
      lifecycle: "active",
      fields: { id: event, lifecycle: "active" },
      fieldClocks: {},
    },
    {
      ...base,
      entityType: "scheduled_recipe",
      entityId: scheduled,
      lifecycle: "active",
      fields: { id: scheduled, event_id: event },
      fieldClocks: {},
    },
  ]);
  await queueScheduledRecipeNote(user, organization, {
    eventId: event,
    scheduledRecipeId: scheduled,
    note: "Cafe\u0301\r\nsecond",
  });
  await expect(localDb.outbox.toArray()).resolves.toEqual([
    expect.objectContaining({
      payload: expect.objectContaining({ note: "Café\nsecond" }),
    }),
  ]);
  await expect(
    queueScheduledRecipeNote(user, organization, {
      eventId: event,
      scheduledRecipeId: scheduled,
      note: "\0",
    }),
  ).rejects.toThrow("selection");
});

it("fails closed for stale, malformed, and retired replay", async () => {
  await localDb.canonicalRecords.bulkPut([
    {
      ...base,
      entityType: "event",
      entityId: event,
      lifecycle: "active",
      fields: { id: event, lifecycle: "active" },
      fieldClocks: {},
    },
    {
      ...base,
      entityType: "scheduled_recipe",
      entityId: scheduled,
      lifecycle: "active",
      fields: { id: scheduled, event_id: event },
      fieldClocks: {
        note: {
          winning_client_wall_time: "2026-08-08T13:00:00.000Z",
          winning_mutation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
        },
      },
    },
  ]);
  const command = {
    id: "00000000-0000-4000-8000-000000000000",
    actionAt: "2026-08-08T12:00:00.000Z",
    payload: { scheduled_recipe_id: scheduled, event_id: event, note: "old" },
  };
  await replayScheduledRecipeNote(user, organization, command);
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  await replayScheduledRecipeNote(user, organization, {
    ...command,
    payload: { ...command.payload, note: "\0" },
  });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
});
