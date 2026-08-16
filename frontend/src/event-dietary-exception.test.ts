import { beforeEach, describe, expect, it } from "vitest";
import { localDb } from "./local-db";
import {
  queueEventDietaryExceptionCreate,
  queueEventDietaryExceptionUpdate,
  replayEventDietaryExceptionUpdate,
  replayEventDietaryExceptionLifecycle,
  readVisibleEventDietaryExceptions,
} from "./event-dietary-exception";

const userId = "00000000-0000-4000-8000-000000000001";
const organizationId = "00000000-0000-4000-8000-000000000002";
const eventId = "00000000-0000-4000-8000-000000000003";
const record = (
  entityType: string,
  entityId: string,
  fields: Record<string, unknown>,
  lifecycle: "active" | "retired" = "active",
  org = organizationId,
) => ({
  userId,
  organizationId: org,
  entityType,
  entityId,
  fields,
  lifecycle,
  recordSchemaVersion: 1,
  fieldClocks: {},
  immutable: false,
  updatedAt: new Date().toISOString(),
});

describe("event dietary exception create", () => {
  beforeEach(async () => {
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.optimisticOverlays.clear(),
      localDb.outbox.clear(),
    ]);
    await localDb.canonicalRecords.bulkPut([
      record("event", eventId, { lifecycle: "active" }),
      record("dietary_tag", "00000000-0000-4000-8000-000000000010", {
        name: "Vegan",
      }),
      record(
        "dietary_tag",
        "00000000-0000-4000-8000-000000000011",
        { name: "Retired", retired_at: "2020" },
        "retired",
      ),
      record(
        "dietary_tag",
        "00000000-0000-4000-8000-000000000012",
        { name: "Other" },
        "active",
        "other-org",
      ),
    ]);
  });
  it("stores selected tags without synthetic association overlays", async () => {
    const exceptionId = "00000000-0000-4000-8000-000000000004";
    const mutationId = "00000000-0000-4000-8000-000000000005";
    const vegan = "00000000-0000-4000-8000-000000000010";
    await queueEventDietaryExceptionCreate(
      userId,
      organizationId,
      eventId,
      { name: "  Alex  ", note: "café", tagIds: [vegan, vegan] },
      exceptionId,
      mutationId,
    );
    expect(await localDb.outbox.get(mutationId)).toMatchObject({
      commandType: "event_dietary_exception.create",
      payload: { name: "Alex", tag_ids: [vegan] },
    });
    expect(
      await localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event_dietary_exception",
        exceptionId,
      ]),
    ).toMatchObject({ fields: { tag_ids: [vegan] } });
    expect(
      await localDb.optimisticOverlays
        .where("[userId+organizationId+entityType]")
        .equals([userId, organizationId, "event_dietary_exception_tag"])
        .count(),
    ).toBe(0);
    expect(
      await localDb.optimisticOverlays
        .where("[userId+organizationId]")
        .equals([userId, organizationId])
        .count(),
    ).toBe(1);
  });
  it.each([
    ["00000000-0000-4000-8000-000000000011"],
    ["00000000-0000-4000-8000-000000000012"],
  ])("fails closed for unavailable tag %s", async (tagId) => {
    await expect(
      queueEventDietaryExceptionCreate(userId, organizationId, eventId, {
        name: "Alex",
        tagIds: [tagId],
      }),
    ).rejects.toThrow("tag");
    expect(await localDb.outbox.count()).toBe(0);
  });
  it("fails closed for archived events", async () => {
    await localDb.canonicalRecords.put(
      record("event", eventId, { lifecycle: "archived" }),
    );
    await expect(
      queueEventDietaryExceptionCreate(userId, organizationId, eventId, {
        name: "Alex",
        tagIds: [],
      }),
    ).rejects.toThrow("event");
  });
  it("enforces UTF-8 byte and name limits", async () => {
    await expect(
      queueEventDietaryExceptionCreate(userId, organizationId, eventId, {
        name: "Alex",
        note: "😀".repeat(40000),
        tagIds: [],
      }),
    ).rejects.toThrow("validation");
    await expect(
      queueEventDietaryExceptionCreate(userId, organizationId, eventId, {
        name: "😀".repeat(201),
        tagIds: [],
      }),
    ).rejects.toThrow("validation");
  });
  it("queues an edit against the visible active exception", async () => {
    const exceptionId = "00000000-0000-4000-8000-000000000030";
    await localDb.canonicalRecords.put(record("event_dietary_exception", exceptionId, {
      event_id: eventId, name: "Old", note: null, retired_at: null,
    }));
    await queueEventDietaryExceptionUpdate(userId, organizationId, eventId, exceptionId, { name: "New", note: "Note", tagIds: [] });
    expect((await localDb.outbox.toArray())[0].commandType).toBe("event_dietary_exception.update");
    expect((await localDb.optimisticOverlays.get([userId, organizationId, "event_dietary_exception", exceptionId]))?.fields.name).toBe("New");
  });
  it.each([
    null,
    [],
    { exception_id: "00000000-0000-4000-8000-000000000031", event_id: eventId, name: "New", note: null },
  ])("fails closed for malformed update payload", async (payload) => {
    const exceptionId = "00000000-0000-4000-8000-000000000031";
    await localDb.canonicalRecords.put(record("event_dietary_exception", exceptionId, { event_id: eventId, name: "Old", retired_at: null }));
    await expect(replayEventDietaryExceptionUpdate(userId, organizationId, { id: crypto.randomUUID(), userId, organizationId, commandType: "event_dietary_exception.update", payload: payload as Record<string, unknown>, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" })).rejects.toThrow("validation");
  });
  it("keeps a newer microsecond canonical field and rejects archived edits", async () => {
    const exceptionId = "00000000-0000-4000-8000-000000000032";
    await localDb.canonicalRecords.put(record("event_dietary_exception", exceptionId, { event_id: eventId, name: "Newer", retired_at: null }, "active"));
    await localDb.canonicalRecords.update([userId, organizationId, "event_dietary_exception", exceptionId], { fieldClocks: { name: { actionAt: "2999-08-16T10:00:00.000001+00:00", mutationId: "ffffffff-ffff-4fff-8fff-ffffffffffff" } } });
    await expect(queueEventDietaryExceptionUpdate(userId, organizationId, eventId, exceptionId, { name: "Old", note: null, tagIds: [] }, "00000000-0000-4000-8000-000000000033")).resolves.toBeUndefined();
    expect((await localDb.optimisticOverlays.get([userId, organizationId, "event_dietary_exception", exceptionId]))?.fields.name).toBe("Newer");
    await localDb.canonicalRecords.update([userId, organizationId, "event", eventId], { lifecycle: "retired" });
    await expect(queueEventDietaryExceptionUpdate(userId, organizationId, eventId, exceptionId, { name: "Blocked", note: null, tagIds: [] })).rejects.toThrow("event");
  });
  it("retains a retired tag only through an active exception association", async () => {
    const exceptionId = "00000000-0000-4000-8000-000000000034";
    const tagId = "00000000-0000-4000-8000-000000000035";
    await localDb.canonicalRecords.bulkPut([
      record("event_dietary_exception", exceptionId, { event_id: eventId, name: "Old", retired_at: null }),
      record("event_dietary_exception_tag", "00000000-0000-4000-8000-000000000036", { exception_id: exceptionId, dietary_tag_id: tagId, retired_at: null }),
      record("dietary_tag", tagId, { name: "Retired", retired_at: "2026-01-01" }, "retired"),
    ]);
    await expect(queueEventDietaryExceptionUpdate(userId, organizationId, eventId, exceptionId, { name: "Kept", note: null, tagIds: [tagId] })).resolves.toBeUndefined();
    await localDb.optimisticOverlays.clear(); await localDb.outbox.clear();
    await localDb.canonicalRecords.update([userId, organizationId, "event_dietary_exception_tag", "00000000-0000-4000-8000-000000000036"], { lifecycle: "retired", fields: { exception_id: exceptionId, dietary_tag_id: tagId, retired_at: "2026-01-02" } });
    await expect(queueEventDietaryExceptionUpdate(userId, organizationId, eventId, exceptionId, { name: "Rejected", note: null, tagIds: [tagId] })).rejects.toThrow("tag");
  });
  it("joins authoritative association records and falls back to optimistic tag_ids", async () => {
    const exceptionId = "00000000-0000-4000-8000-000000000020";
    const tagId = "00000000-0000-4000-8000-000000000021";
    await localDb.canonicalRecords.bulkPut([
      record("event_dietary_exception", exceptionId, {
        event_id: eventId,
        name: "Sam",
        retired_at: null,
      }),
      record(
        "event_dietary_exception_tag",
        "00000000-0000-4000-8000-000000000022",
        { exception_id: exceptionId, dietary_tag_id: tagId, retired_at: null },
      ),
      record(
        "dietary_tag",
        tagId,
        { name: "Vegan", retired_at: "2026-08-01" },
        "retired",
      ),
    ]);
    await expect(
      readVisibleEventDietaryExceptions(userId, organizationId, eventId),
    ).resolves.toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          fields: expect.objectContaining({ selected_tag_names: ["Vegan"] }),
        }),
      ]),
    );
  });
});

describe("event dietary exception lifecycle", () => {
  beforeEach(async () => {
    await Promise.all([localDb.canonicalRecords.clear(), localDb.optimisticOverlays.clear(), localDb.outbox.clear()]);
    await localDb.canonicalRecords.bulkPut([
      record("event", eventId, { id: eventId, lifecycle: "active" }),
      record("event_dietary_exception", "00000000-0000-4000-8000-000000000004", { id: "00000000-0000-4000-8000-000000000004", event_id: eventId, name: "Vegan", note: "keep", tag_ids: [] }),
    ]);
  });
  it("replays lifecycle while preserving pending visible fields and fails closed on malformed clocks", async () => {
    const exceptionId = "00000000-0000-4000-8000-000000000004";
    await localDb.optimisticOverlays.put(record("event_dietary_exception", exceptionId, { id: exceptionId, event_id: eventId, name: "Pending", note: "edited", tag_ids: [] }));
    const command = { id: "00000000-0000-4000-8000-000000000005", userId, organizationId, commandType: "event_dietary_exception.lifecycle", payload: { exception_id: exceptionId, event_id: eventId, operation: "retire" }, actionAt: "2026-08-07T12:00:00.123456Z", createdAt: "2026-08-07T12:00:00.123456Z", state: "pending" as const };
    await replayEventDietaryExceptionLifecycle(userId, organizationId, command);
    expect((await localDb.optimisticOverlays.get([userId, organizationId, "event_dietary_exception", exceptionId]))?.fields.name).toBe("Pending");
    await expect(replayEventDietaryExceptionLifecycle(userId, organizationId, { ...command, id: "00000000-0000-4000-8000-000000000006", actionAt: "bad" })).rejects.toThrow("clock");
    await replayEventDietaryExceptionLifecycle(userId, organizationId, { ...command, id: "00000000-0000-4000-8000-000000000007", actionAt: "2026-08-07T14:00:00.123456+02:00", payload: { ...command.payload, operation: "restore" } });
    expect((await localDb.optimisticOverlays.get([userId, organizationId, "event_dietary_exception", exceptionId]))?.lifecycle).toBe("active");
    await expect(replayEventDietaryExceptionLifecycle(userId, organizationId, { ...command, id: "00000000-0000-4000-8000-000000000008", actionAt: "2026-02-31T12:00:00Z" })).rejects.toThrow("clock");
    const overlay = await localDb.optimisticOverlays.get([userId, organizationId, "event_dietary_exception", exceptionId]);
    await localDb.optimisticOverlays.put({ ...overlay!, fieldClocks: { ...overlay!.fieldClocks, lifecycle: { mutationId: "not-a-uuid", actionAt: command.actionAt } } });
    await expect(replayEventDietaryExceptionLifecycle(userId, organizationId, { ...command, id: "00000000-0000-4000-8000-000000000009", actionAt: "2026-08-07T13:00:00Z" })).rejects.toThrow("clock");
  });
  it("uses the current overlay lifecycle clock and restores a pending create", async () => {
    const exceptionId = "00000000-0000-4000-8000-000000000004";
    const current = await localDb.canonicalRecords.get([userId, organizationId, "event_dietary_exception", exceptionId]);
    await localDb.optimisticOverlays.put({ ...current!, lifecycle: "retired", fieldClocks: { lifecycle: { mutationId: "00000000-0000-4000-8000-000000000099", actionAt: "2026-08-07T14:00:00.000000Z" } } });
    await replayEventDietaryExceptionLifecycle(userId, organizationId, { id: "ffffffff-ffff-4fff-8fff-ffffffffffff", userId, organizationId, commandType: "event_dietary_exception.lifecycle", payload: { exception_id: exceptionId, event_id: eventId, operation: "restore" }, actionAt: "2026-08-07T13:00:00Z", createdAt: "2026-08-07T13:00:00Z", state: "pending" });
    expect((await localDb.optimisticOverlays.get([userId, organizationId, "event_dietary_exception", exceptionId]))?.lifecycle).toBe("retired");
    await localDb.canonicalRecords.delete([userId, organizationId, "event_dietary_exception", exceptionId]);
    await localDb.optimisticOverlays.delete([userId, organizationId, "event_dietary_exception", exceptionId]);
    await localDb.optimisticOverlays.put(record("event_dietary_exception", exceptionId, { id: exceptionId, event_id: eventId, name: "Pending create", note: null, tag_ids: [] }, "retired"));
    await replayEventDietaryExceptionLifecycle(userId, organizationId, { id: "00000000-0000-4000-8000-000000000010", userId, organizationId, commandType: "event_dietary_exception.lifecycle", payload: { exception_id: exceptionId, event_id: eventId, operation: "restore" }, actionAt: "2026-08-07T15:00:00Z", createdAt: "2026-08-07T15:00:00Z", state: "pending" });
    expect((await localDb.optimisticOverlays.get([userId, organizationId, "event_dietary_exception", exceptionId]))?.lifecycle).toBe("active");
  });
});
