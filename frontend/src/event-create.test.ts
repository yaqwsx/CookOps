import { beforeEach, describe, expect, it } from "vitest";

import { queueEventCreate, validateEventCreate } from "./event-create";
import { readVisibleEventSummaries } from "./event-projections";
import { localDb } from "./local-db";

const userId = "user-a";
const organizationId = "organization-a";
const validInput = {
  name: "Weekend cook",
  startDate: "2026-08-10",
  endDate: "2026-08-12",
  baseExpectedAttendance: "12",
  budgetAmount: "1200.50",
  location: "Community kitchen",
  generalNote: "Bring aprons.",
};

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
}

async function addOrganization(currency = "CZK") {
  await localDb.canonicalRecords.add({
    userId,
    organizationId,
    entityType: "organization",
    entityId: organizationId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: { id: organizationId, default_currency: currency },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

describe("offline event creation", () => {
  beforeEach(clearDatabase);

  it("accepts only values that fit the event-create command contract", () => {
    expect(validateEventCreate(validInput)).toBeUndefined();
    for (const input of [
      { ...validInput, name: " " },
      { ...validInput, startDate: "2026-02-30" },
      { ...validInput, endDate: "2026-08-09" },
      { ...validInput, baseExpectedAttendance: "1.5" },
      { ...validInput, baseExpectedAttendance: "9007199254740992" },
      { ...validInput, budgetAmount: "1e3" },
      { ...validInput, budgetAmount: "-1" },
      { ...validInput, location: "x".repeat(301) },
    ]) {
      expect(validateEventCreate(input)).toBeDefined();
    }
  });

  it("writes the optimistic event and ordered outbox intent in one local transaction", async () => {
    await addOrganization();

    const eventId = await queueEventCreate(userId, organizationId, validInput);

    await expect(
      readVisibleEventSummaries(userId, organizationId),
    ).resolves.toEqual([
      expect.objectContaining({
        id: eventId,
        name: "Weekend cook",
        currency: "CZK",
        budgetAmount: "1200.50",
      }),
    ]);
    await expect(localDb.outbox.toArray()).resolves.toEqual([
      expect.objectContaining({
        commandType: "event.create",
        state: "pending",
        payload: {
          event_id: eventId,
          name: "Weekend cook",
          start_date: "2026-08-10",
          end_date: "2026-08-12",
          base_expected_attendance: 12,
          budget_amount: "1200.50",
          location: "Community kitchen",
          general_note: "Bring aprons.",
        },
      }),
    ]);
  });

  it("preserves a nonempty note verbatim for server-side canonicalization", async () => {
    await addOrganization();

    await queueEventCreate(userId, organizationId, {
      ...validInput,
      generalNote: "\n  Keep this whitespace.  \n",
    });

    await expect(localDb.outbox.toArray()).resolves.toEqual([
      expect.objectContaining({
        payload: expect.objectContaining({
          general_note: "\n  Keep this whitespace.  \n",
        }),
      }),
    ]);
  });

  it("does not leave a partial projection when organization currency is unavailable", async () => {
    await expect(
      queueEventCreate(userId, organizationId, validInput),
    ).rejects.toThrow("organizationCurrency");

    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });
});
