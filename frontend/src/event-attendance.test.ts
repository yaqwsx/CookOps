import { beforeEach, describe, expect, it } from "vitest";

import {
  queueEventAttendanceUpdate,
  validateEventAttendance,
} from "./event-attendance";
import { localDb } from "./local-db";

const userId = "user-a";
const organizationId = "organization-a";
const eventId = "event-a";

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
}

async function addEvent(lifecycle: "active" | "archived" = "active") {
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
      base_expected_attendance: 4,
    },
    fieldClocks: { name: { actionAt: "old" } },
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

describe("offline base attendance edits", () => {
  beforeEach(clearDatabase);

  it("rejects values outside the shared command contract", () => {
    for (const value of ["", "-1", "1.2", "1e3", "9007199254740992"]) {
      expect(validateEventAttendance(value)).toBe("attendance");
    }
    expect(validateEventAttendance("0")).toBeUndefined();
  });

  it("atomically writes an active event overlay and its LWW command identity", async () => {
    await addEvent();

    await queueEventAttendanceUpdate(userId, organizationId, eventId, "9");

    const [command, overlay] = await Promise.all([
      localDb.outbox.toCollection().first(),
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]),
    ]);
    expect(command).toMatchObject({
      commandType: "event.update_base_attendance",
      payload: { event_id: eventId, base_expected_attendance: 9 },
      state: "pending",
    });
    expect(overlay).toMatchObject({
      fields: { base_expected_attendance: 9 },
      fieldClocks: {
        name: { actionAt: "old" },
        base_expected_attendance: {
          mutationId: command?.id,
          actionAt: command?.actionAt,
        },
      },
      updatedAt: command?.actionAt,
    });
  });

  it("leaves no partial work for invalid, missing, or archived events", async () => {
    await expect(
      queueEventAttendanceUpdate(userId, organizationId, eventId, "-1"),
    ).rejects.toThrow("attendance");
    await expect(
      queueEventAttendanceUpdate(userId, organizationId, eventId, "4"),
    ).rejects.toThrow("event");
    await addEvent("archived");
    await localDb.optimisticOverlays.add({
      userId,
      organizationId,
      entityType: "event",
      entityId: eventId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { id: eventId, lifecycle: "active", base_expected_attendance: 4 },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await expect(
      queueEventAttendanceUpdate(userId, organizationId, eventId, "4"),
    ).rejects.toThrow("event");
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(1);
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });
});
