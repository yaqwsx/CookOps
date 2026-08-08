import { beforeEach, expect, it } from "vitest";

import { queueEventMealRoleCreate, replayEventMealRoleCreate } from "./event-meal-role";
import { localDb } from "./local-db";

const user = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organization = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const event = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const role = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const base = { userId: user, organizationId: organization, recordSchemaVersion: 1, lifecycle: "active" as const, immutable: false, updatedAt: "2026-08-08T12:00:00.000Z" };

beforeEach(async () => {
  await Promise.all([localDb.canonicalRecords.clear(), localDb.optimisticOverlays.clear(), localDb.outbox.clear()]);
  await localDb.canonicalRecords.add({ ...base, entityType: "event", entityId: event, fields: { id: event, organization_id: organization, lifecycle: "active" }, fieldClocks: {} });
});

it("queues one normalized custom role and rejects a duplicate", async () => {
  await queueEventMealRoleCreate(user, organization, { eventId: event, customName: "  Late supper  " });
  await expect(localDb.outbox.toArray()).resolves.toEqual([expect.objectContaining({ commandType: "event_meal_role.create", payload: expect.objectContaining({ custom_name: "Late supper" }) })]);
  await expect(localDb.optimisticOverlays.toArray()).resolves.toEqual([expect.objectContaining({ entityType: "event_meal_role", fields: expect.objectContaining({ custom_name: "Late supper" }) })]);
  await expect(queueEventMealRoleCreate(user, organization, { eventId: event, customName: "late SUPPER" })).rejects.toThrow("selection");
});

it("replays only a strict command for an active event", async () => {
  await replayEventMealRoleCreate(user, organization, { id: role, actionAt: "2026-08-08T13:00:00.000Z", payload: { event_meal_role_id: role, event_id: event, custom_name: "Late supper" } });
  await expect(localDb.optimisticOverlays.get([user, organization, "event_meal_role", role])).resolves.toMatchObject({ fields: { custom_name: "Late supper" } });
  await localDb.optimisticOverlays.clear();
  await replayEventMealRoleCreate(user, organization, { id: role, actionAt: "2026-08-08T13:00:00.000Z", payload: { event_meal_role_id: role, event_id: event, custom_name: "Late supper", extra: true } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  await localDb.canonicalRecords.put({ ...base, entityType: "event", entityId: event, lifecycle: "retired", fields: { id: event, organization_id: organization, lifecycle: "archived" }, fieldClocks: {} });
  await replayEventMealRoleCreate(user, organization, { id: role, actionAt: "2026-08-08T13:00:00.000Z", payload: { event_meal_role_id: role, event_id: event, custom_name: "Late supper" } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
});
