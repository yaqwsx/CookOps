import { beforeEach, expect, it } from "vitest";

import { queueEventDayCreate, queueEventDayLifecycle, queueEventDayNote, queueEventDayVisibility, replayEventDayLifecycle, replayEventDayNote, replayEventDayVisibility } from "./event-day";
import { localDb } from "./local-db";
import { readVisibleRecords } from "./visible-records";

const user = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organization = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const event = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const day = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const base = { userId: user, organizationId: organization, recordSchemaVersion: 1, lifecycle: "active" as const, immutable: false, updatedAt: "2026-08-08T12:00:00.000Z" };

beforeEach(async () => {
  await Promise.all([localDb.canonicalRecords.clear(), localDb.optimisticOverlays.clear(), localDb.outbox.clear()]);
  await localDb.canonicalRecords.bulkPut([
    { ...base, entityType: "event", entityId: event, fields: { id: event, organization_id: organization, lifecycle: "active" }, fieldClocks: {} },
    { ...base, entityType: "event_day", entityId: day, fields: { id: day, event_id: event, is_visible: true }, fieldClocks: {} },
  ]);
});

it("queues visibility atomically and keeps a newer canonical visibility winner", async () => {
  await queueEventDayVisibility(user, organization, { eventDayId: day, eventId: event, isVisible: false });
  await expect(localDb.outbox.toArray()).resolves.toEqual([expect.objectContaining({ commandType: "event_day.visibility" })]);
  await expect(localDb.optimisticOverlays.get([user, organization, "event_day", day])).resolves.toMatchObject({ fields: { is_visible: false } });

  await localDb.optimisticOverlays.clear();
  await localDb.canonicalRecords.put({ ...base, entityType: "event_day", entityId: day, fields: { id: day, event_id: event, is_visible: true }, fieldClocks: { is_visible: { winning_client_wall_time: "2026-08-08T14:00:00.000Z", winning_mutation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff" } } });
  await replayEventDayVisibility(user, organization, { id: "00000000-0000-4000-8000-000000000000", actionAt: "2026-08-08T13:00:00.000Z", payload: { event_day_id: day, event_id: event, is_visible: false } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
});

it("keeps a microsecond-newer canonical lifecycle winner", async () => {
  await localDb.canonicalRecords.put({ ...base, entityType: "event_day", entityId: day, lifecycle: "retired", fields: { id: day, event_id: event, is_visible: true, retired_at: "2026-08-08T13:00:00.000001+00:00" }, fieldClocks: { lifecycle: { winning_client_wall_time: "2026-08-08T13:00:00.000001+00:00", winning_mutation_id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c" } } });
  await replayEventDayLifecycle(user, organization, { id: "fce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-08T13:00:00.000Z", payload: { event_day_id: day, event_id: event, operation: "restore" } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
});

it("queues a normalized note and keeps a newer canonical note winner", async () => {
  await queueEventDayNote(user, organization, { eventDayId: day, eventId: event, note: "Menu\r\npro hosty" });
  await expect(localDb.outbox.toArray()).resolves.toEqual([expect.objectContaining({ commandType: "event_day.note", payload: expect.objectContaining({ note: "Menu\npro hosty" }) })]);
  await expect(localDb.optimisticOverlays.get([user, organization, "event_day", day])).resolves.toMatchObject({ fields: { note: "Menu\npro hosty" } });

  await localDb.optimisticOverlays.clear();
  await localDb.canonicalRecords.put({ ...base, entityType: "event_day", entityId: day, fields: { id: day, event_id: event, note: "Newer" }, fieldClocks: { note: { winning_client_wall_time: "2026-08-08T14:00:00.000Z", winning_mutation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff" } } });
  await replayEventDayNote(user, organization, { id: "00000000-0000-4000-8000-000000000000", actionAt: "2026-08-08T13:00:00.000Z", payload: { event_day_id: day, event_id: event, note: "Older" } });
  await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  await expect(queueEventDayNote(user, organization, { eventDayId: day, eventId: event, note: "bad\0note" })).rejects.toThrow("selection");
});

it("creates one visible manual day locally and refuses a duplicate date", async () => {
  await queueEventDayCreate(user, organization, { eventId: event, calendarDate: "2026-08-11" });
  await expect(localDb.outbox.toArray()).resolves.toEqual([expect.objectContaining({ commandType: "event_day.create" })]);
  await expect(localDb.optimisticOverlays.toArray()).resolves.toEqual(expect.arrayContaining([expect.objectContaining({ entityType: "event_day", fields: expect.objectContaining({ calendar_date: "2026-08-11", provenance: "manually_added" }) })]));
  await expect(queueEventDayCreate(user, organization, { eventId: event, calendarDate: "2026-08-11" })).rejects.toThrow("selection");
  await localDb.canonicalRecords.put({ ...base, entityType: "event_day", entityId: "8ce17d2f-8365-4b1f-a80b-34d10425d51c", fields: { id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c", event_id: event, calendar_date: "2026-08-12" }, fieldClocks: {} });
  await expect(queueEventDayCreate(user, organization, { eventId: event, calendarDate: "2026-08-12" })).rejects.toThrow("selection");
});

it("retires a day and renders an optimistic restore", async () => {
  await queueEventDayLifecycle(user, organization, { eventDayId: day, eventId: event, operation: "retire" });
  await expect(localDb.optimisticOverlays.get([user, organization, "event_day", day])).resolves.toMatchObject({ lifecycle: "retired" });
  await localDb.optimisticOverlays.clear();
  await localDb.canonicalRecords.put({ ...base, entityType: "event_day", entityId: day, lifecycle: "retired", fields: { id: day, event_id: event, is_visible: true, retired_at: "2026-08-08T13:00:00.000Z" }, fieldClocks: {} });
  await queueEventDayLifecycle(user, organization, { eventDayId: day, eventId: event, operation: "restore" });
  await expect(readVisibleRecords(user, organization, "event_day", true)).resolves.toEqual([expect.objectContaining({ entityId: day, lifecycle: "active" })]);
});
