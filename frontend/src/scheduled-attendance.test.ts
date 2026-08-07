import { beforeEach, expect, it } from "vitest";

import { localDb } from "./local-db";
import {
  queueScheduledRecipeAttendance,
  replayScheduledRecipeAttendance,
} from "./scheduled-recipe";

const user = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organization = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const event = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const scheduled = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";

beforeEach(async () => {
  await Promise.all([localDb.canonicalRecords.clear(), localDb.optimisticOverlays.clear(), localDb.outbox.clear()]);
});

it("does not let a stale attendance replay replace the visible LWW winner", async () => {
  await localDb.canonicalRecords.bulkPut([
    {
      userId: user, organizationId: organization, entityType: "event", entityId: event,
      recordSchemaVersion: 1, lifecycle: "active",
      fields: { id: event, organization_id: organization, lifecycle: "active", base_expected_attendance: 12 },
      fieldClocks: {}, immutable: false, updatedAt: "2026-08-08T12:00:00.000Z",
    },
    {
      userId: user, organizationId: organization, entityType: "scheduled_recipe", entityId: scheduled,
      recordSchemaVersion: 1, lifecycle: "active",
      fields: { id: scheduled, organization_id: organization, event_id: event, diner_count: 19, attendance_mode: "manual" },
      fieldClocks: { attendance: { winning_client_wall_time: "2026-08-08T12:00:00.000Z", winning_mutation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff" } },
      immutable: false, updatedAt: "2026-08-08T12:00:00.000Z",
    },
  ]);

  await replayScheduledRecipeAttendance(user, organization, {
    id: "00000000-0000-4000-8000-000000000000",
    actionAt: "2026-08-08T11:00:00.000Z",
    payload: { scheduled_recipe_id: scheduled, event_id: event, operation: "set_manual", diner_count: 3 },
  });

  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
});

it("replays a valid command without a clock but fails closed for a malformed clock", async () => {
  const record = {
    userId: user, organizationId: organization, entityType: "scheduled_recipe", entityId: scheduled,
    recordSchemaVersion: 1, lifecycle: "active" as const,
    fields: { id: scheduled, organization_id: organization, event_id: event, diner_count: 12, attendance_mode: "follows_event" },
    immutable: false, updatedAt: "2026-08-08T12:00:00.000Z",
  };
  await localDb.canonicalRecords.bulkPut([
    {
      userId: user, organizationId: organization, entityType: "event", entityId: event,
      recordSchemaVersion: 1, lifecycle: "active",
      fields: { id: event, organization_id: organization, lifecycle: "active", base_expected_attendance: 12 },
      fieldClocks: {}, immutable: false, updatedAt: record.updatedAt,
    },
    { ...record, fieldClocks: {} },
  ]);
  const command = {
    id: "00000000-0000-4000-8000-000000000000",
    actionAt: "2026-08-08T13:00:00.000Z",
    payload: { scheduled_recipe_id: scheduled, event_id: event, operation: "set_manual", diner_count: 3 },
  } as const;
  await replayScheduledRecipeAttendance(user, organization, command);
  await expect(localDb.optimisticOverlays.get([user, organization, "scheduled_recipe", scheduled])).resolves.toMatchObject({ fields: { diner_count: 3 } });
  await localDb.optimisticOverlays.clear();
  await localDb.canonicalRecords.put({ ...record, fieldClocks: { attendance: {} } });
  await replayScheduledRecipeAttendance(user, organization, command);
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
});

it("does not queue or replay attendance through an optimistic archive or retirement", async () => {
  const records = [
    {
      userId: user, organizationId: organization, entityType: "event", entityId: event,
      recordSchemaVersion: 1, lifecycle: "active" as const,
      fields: { id: event, organization_id: organization, lifecycle: "active", base_expected_attendance: 12 },
      fieldClocks: {}, immutable: false, updatedAt: "2026-08-08T12:00:00.000Z",
    },
    {
      userId: user, organizationId: organization, entityType: "scheduled_recipe", entityId: scheduled,
      recordSchemaVersion: 1, lifecycle: "active" as const,
      fields: { id: scheduled, organization_id: organization, event_id: event, diner_count: 12, attendance_mode: "follows_event" },
      fieldClocks: {}, immutable: false, updatedAt: "2026-08-08T12:00:00.000Z",
    },
  ];
  await localDb.canonicalRecords.bulkPut(records);
  await localDb.optimisticOverlays.bulkPut([
    { ...records[0], lifecycle: "retired", fields: { ...records[0].fields, lifecycle: "archived" } },
    { ...records[1], lifecycle: "retired" },
  ]);
  await expect(queueScheduledRecipeAttendance(user, organization, { eventId: event, scheduledRecipeId: scheduled, dinerCount: 3 })).rejects.toThrow("selection");
  await localDb.optimisticOverlays.clear();
  await localDb.optimisticOverlays.bulkPut([
    { ...records[0], lifecycle: "retired", fields: { ...records[0].fields, lifecycle: "archived" } },
    { ...records[1], lifecycle: "retired" },
  ]);
  await replayScheduledRecipeAttendance(user, organization, {
    id: "00000000-0000-4000-8000-000000000000",
    actionAt: "2026-08-08T13:00:00.000Z",
    payload: { scheduled_recipe_id: scheduled, event_id: event, operation: "set_manual", diner_count: 3 },
  });
  await expect(localDb.outbox.count()).resolves.toBe(0);
  await expect(localDb.optimisticOverlays.toArray()).resolves.toEqual([
    expect.objectContaining({ entityType: "event", lifecycle: "retired" }),
    expect.objectContaining({ entityType: "scheduled_recipe", lifecycle: "retired" }),
  ]);
});
