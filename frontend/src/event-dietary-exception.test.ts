import { beforeEach, describe, expect, it } from "vitest";
import { localDb } from "./local-db";
import {
  queueEventDietaryExceptionCreate,
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
