import { describe, expect, it } from "vitest";

import { fulfilmentAttribution } from "./shopping-projections";

const base = {
  userId: "00000000-0000-4000-8000-000000000001",
  organizationId: "00000000-0000-4000-8000-000000000002",
  entityType: "shopping_ingredient_row" as const,
  entityId: "00000000-0000-4000-8000-000000000003",
  recordSchemaVersion: 1,
  lifecycle: "active" as const,
  fieldClocks: {},
  immutable: false,
  updatedAt: "2026-08-22T00:00:00Z",
};

describe("shopping fulfilment attribution projection", () => {
  it("maps only a valid replicated timestamp and user id pair", () => {
    expect(
      fulfilmentAttribution({
        ...base,
        fields: {
          fulfilment_updated_at: "2026-08-22T12:34:56Z",
          fulfilment_updated_by_user_id: "00000000-0000-4000-8000-000000000004",
        },
      }),
    ).toEqual({
      updatedAt: "2026-08-22T12:34:56Z",
      updatedByUserId: "00000000-0000-4000-8000-000000000004",
    });
    expect(fulfilmentAttribution({ ...base, fields: {} })).toBeNull();
    expect(
      fulfilmentAttribution({
        ...base,
        fields: {
          fulfilment_updated_at: "not-a-timestamp",
          fulfilment_updated_by_user_id: "00000000-0000-4000-8000-000000000004",
        },
      }),
    ).toBeNull();
  });
});
