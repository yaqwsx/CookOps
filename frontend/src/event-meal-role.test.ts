import { beforeEach, expect, it } from "vitest";

import { queueEventMealRoleCreate, queueEventMealRoleLifecycle, queueEventMealRoleName, queueEventMealRolePosition, replayEventMealRoleCreate, replayEventMealRoleLifecycle, replayEventMealRoleName, replayEventMealRolePosition } from "./event-meal-role";
import { localDb } from "./local-db";
import { readVisibleRecords } from "./visible-records";

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
  const retiredRole = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
  await localDb.canonicalRecords.add({ ...base, entityType: "event_meal_role", entityId: retiredRole, lifecycle: "retired", fields: { id: retiredRole, event_id: event, built_in_translation_key: null, custom_name: "Archived", normalized_custom_name: "archived", position_key: "n", retired_at: "2026-08-07T12:00:00.000Z" }, fieldClocks: {} });
  await expect(queueEventMealRoleCreate(user, organization, { eventId: event, customName: " archived " })).rejects.toThrow("selection");
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

it("keeps a newer canonical role position over a stale replay", async () => {
  await localDb.canonicalRecords.add({ ...base, entityType: "event_meal_role", entityId: role, fields: { id: role, event_id: event, position_key: "m", retired_at: null }, fieldClocks: { position_key: { winning_client_wall_time: "2026-08-07T14:00:00.000Z", winning_mutation_id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c" } } });
  await replayEventMealRolePosition(user, organization, { id: "fce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-07T13:00:00.000Z", payload: { event_meal_role_id: role, event_id: event, position_key: "a" } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  await queueEventMealRolePosition(user, organization, { eventId: event, eventMealRoleId: role, positionKey: "z9" });
  await expect(localDb.optimisticOverlays.get([user, organization, "event_meal_role", role])).resolves.toMatchObject({ fields: { position_key: "z9" } });
});

it("renames only a custom role through the current name clock", async () => {
  await localDb.canonicalRecords.add({ ...base, entityType: "event_meal_role", entityId: role, fields: { id: role, event_id: event, built_in_translation_key: null, custom_name: "Late supper", normalized_custom_name: "late supper", position_key: "m", retired_at: null }, fieldClocks: { custom_name: { winning_client_wall_time: "2026-08-07T13:00:00.000001+00:00", winning_mutation_id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c" } } });
  await replayEventMealRoleName(user, organization, { id: "fce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-07T13:00:00.000Z", payload: { event_meal_role_id: role, event_id: event, custom_name: "Supper" } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  await replayEventMealRoleName(user, organization, { id: "fce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-07T14:00:00.000Z", payload: { event_meal_role_id: role, event_id: event, custom_name: "Supper", extra: true } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  const retiredRole = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
  await localDb.canonicalRecords.add({ ...base, entityType: "event_meal_role", entityId: retiredRole, lifecycle: "retired", fields: { id: retiredRole, event_id: event, built_in_translation_key: null, custom_name: "Archived", normalized_custom_name: "archived", position_key: "n", retired_at: "2026-08-07T12:00:00.000Z" }, fieldClocks: {} });
  await expect(queueEventMealRoleName(user, organization, { eventId: event, eventMealRoleId: role, customName: "Archived" })).rejects.toThrow("selection");
  const builtInRole = "ace17d2f-8365-4b1f-a80b-34d10425d51c";
  await localDb.canonicalRecords.add({ ...base, entityType: "event_meal_role", entityId: builtInRole, fields: { id: builtInRole, event_id: event, built_in_translation_key: "meal_role.dinner", custom_name: null, normalized_custom_name: null, position_key: "o", retired_at: null }, fieldClocks: {} });
  await expect(queueEventMealRoleName(user, organization, { eventId: event, eventMealRoleId: builtInRole, customName: "Dinner" })).rejects.toThrow("selection");
  await queueEventMealRoleName(user, organization, { eventId: event, eventMealRoleId: role, customName: "  Late dinner  " });
  await expect(localDb.optimisticOverlays.get([user, organization, "event_meal_role", role])).resolves.toMatchObject({ fields: { custom_name: "Late dinner", normalized_custom_name: "late dinner" } });
});

it("positions a pending created role and compares mixed wire timestamps by epoch", async () => {
  await queueEventMealRoleCreate(user, organization, { eventId: event, customName: "Late supper" });
  const pending = (await localDb.optimisticOverlays.toArray()).find((item) => item.entityType === "event_meal_role");
  if (!pending) throw new Error("missing pending role");
  await queueEventMealRolePosition(user, organization, { eventId: event, eventMealRoleId: pending.entityId, positionKey: "z9" });
  await expect(localDb.optimisticOverlays.get([user, organization, "event_meal_role", pending.entityId])).resolves.toMatchObject({ fields: { position_key: "z9" } });
  await localDb.optimisticOverlays.clear();
  await localDb.canonicalRecords.put({ ...base, entityType: "event_meal_role", entityId: role, fields: { id: role, event_id: event, position_key: "m", retired_at: null }, fieldClocks: { position_key: { winning_client_wall_time: "2026-08-07T13:00:00.000001+00:00", winning_mutation_id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c" } } });
  await replayEventMealRolePosition(user, organization, { id: "fce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-07T13:00:00.000Z", payload: { event_meal_role_id: role, event_id: event, position_key: "a" } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
});

it("retires and restores only through a current lifecycle winner", async () => {
  await localDb.canonicalRecords.add({ ...base, entityType: "event_meal_role", entityId: role, fields: { id: role, event_id: event, position_key: "m", retired_at: null }, fieldClocks: {} });
  await queueEventMealRoleLifecycle(user, organization, { eventId: event, eventMealRoleId: role, operation: "retire" });
  await expect(localDb.optimisticOverlays.get([user, organization, "event_meal_role", role])).resolves.toMatchObject({ lifecycle: "retired" });
  await localDb.optimisticOverlays.clear();
  await localDb.canonicalRecords.put({ ...base, entityType: "event_meal_role", entityId: role, lifecycle: "retired", fields: { id: role, event_id: event, position_key: "m", retired_at: "2026-08-08T13:00:00.000Z" }, fieldClocks: { lifecycle: { winning_client_wall_time: "2026-08-08T13:00:00.000001+00:00", winning_mutation_id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c" } } });
  await replayEventMealRoleLifecycle(user, organization, { id: "fce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-08T13:00:00.000Z", payload: { event_meal_role_id: role, event_id: event, operation: "restore" } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
});

it("shows an optimistic restore over the retired canonical role", async () => {
  await localDb.canonicalRecords.add({ ...base, entityType: "event_meal_role", entityId: role, lifecycle: "retired", fields: { id: role, event_id: event, position_key: "m", retired_at: "2026-08-08T13:00:00.000Z" }, fieldClocks: {} });
  await queueEventMealRoleLifecycle(user, organization, { eventId: event, eventMealRoleId: role, operation: "restore" });
  await expect(readVisibleRecords(user, organization, "event_meal_role", true)).resolves.toEqual([expect.objectContaining({ entityId: role, lifecycle: "active" })]);
});
