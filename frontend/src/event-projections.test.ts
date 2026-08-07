import { beforeEach, describe, expect, it } from "vitest";

import { readVisibleEventSummaries } from "./event-projections";
import { localDb } from "./local-db";

const userId = "user-a";
const organizationId = "organization-a";

describe("readVisibleEventSummaries", () => {
  beforeEach(async () => {
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.optimisticOverlays.clear(),
    ]);
  });

  it("selects a valid pending overlay without leaking another organization record", async () => {
    const base = {
      userId,
      entityType: "event",
      recordSchemaVersion: 1,
      lifecycle: "active" as const,
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
      fields: {
        id: "event-a",
        organization_id: organizationId,
        name: "Canonical",
        start_date: "2026-08-10",
        end_date: "2026-08-10",
        base_expected_attendance: 3,
        budget_amount: "10",
        currency: "CZK",
        lifecycle: "active",
        archived_at: null,
      },
    };
    await localDb.canonicalRecords.bulkAdd([
      { ...base, organizationId, entityId: "event-a" },
      {
        ...base,
        organizationId: "organization-b",
        entityId: "event-b",
        fields: { ...base.fields, id: "event-b", name: "Private" },
      },
    ]);
    await localDb.optimisticOverlays.add({
      ...base,
      organizationId,
      entityId: "event-a",
      fields: { ...base.fields, name: "Pending" },
    });

    await expect(
      readVisibleEventSummaries(userId, organizationId),
    ).resolves.toEqual([
      expect.objectContaining({ id: "event-a", name: "Pending" }),
    ]);
  });
});
