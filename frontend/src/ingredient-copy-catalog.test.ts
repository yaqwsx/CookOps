import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CanonicalRecord } from "./local-db";
import { localDb } from "./local-db";
import { readIngredientCopyCatalog } from "./ingredient-copy-catalog";

const { readVisibleRecords } = vi.hoisted(() => ({
  readVisibleRecords: vi.fn(),
}));
vi.mock("./visible-records", () => ({ readVisibleRecords }));

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const sourceId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const sourceUnitId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationUnitId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
const retiredDestinationUnitId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
const sourceTagId = "ace17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationTagId = "bce17d2f-8365-4b1f-a80b-34d10425d51c";

function record(
  entityType: CanonicalRecord["entityType"],
  entityId: string,
  organizationId: string | null,
  lifecycle: CanonicalRecord["lifecycle"],
  fields: Record<string, unknown>,
): CanonicalRecord {
  return {
    userId,
    organizationId: organizationId ?? "",
    entityType,
    entityId,
    recordSchemaVersion: 1,
    lifecycle,
    fields: { id: entityId, organization_id: organizationId, ...fields },
    fieldClocks: {},
    immutable: true,
    updatedAt: "2026-08-18T00:00:00.000Z",
  };
}

describe("readIngredientCopyCatalog historical source records", () => {
  beforeEach(async () => {
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.optimisticOverlays.clear(),
    ]);
    readVisibleRecords.mockImplementation(
      async (
        _user: string,
        organizationId: string,
        entityType: string,
        includeRetired = false,
      ) => {
        if (entityType === "unit_definition" && organizationId === sourceId)
          return includeRetired
            ? [
                record("unit_definition", sourceUnitId, sourceId, "retired", {
                  custom_name: "old gram",
                  dimension: "mass",
                  base_unit_factor: "1",
                  allows_ingredient_quantity: true,
                }),
              ]
            : [];
        if (entityType === "dietary_tag" && organizationId === sourceId)
          return includeRetired
            ? [
                record("dietary_tag", sourceTagId, sourceId, "retired", {
                  name: "Old tag",
                  seed_key: "old",
                }),
              ]
            : [];
        if (entityType === "dietary_tag" && organizationId === destinationId)
          return [
            record("dietary_tag", destinationTagId, destinationId, "active", {
              name: "Tag",
              seed_key: "old",
            }),
          ];
        return [];
      },
    );
  });

  it("includes retired source dependencies while keeping destination candidates active", async () => {
    await localDb.canonicalRecords.bulkAdd([
      record("unit_definition", destinationUnitId, destinationId, "active", {
        custom_name: "gramme",
        dimension: "mass",
        base_unit_factor: "1",
        allows_ingredient_quantity: true,
      }),
      record(
        "unit_definition",
        retiredDestinationUnitId,
        destinationId,
        "retired",
        {
          custom_name: "retired gramme",
          dimension: "mass",
          base_unit_factor: "1",
          allows_ingredient_quantity: true,
        },
      ),
      record("dietary_tag", destinationTagId, destinationId, "active", {
        name: "Tag",
        seed_key: "old",
      }),
    ]);
    await localDb.optimisticOverlays.put(
      record(
        "unit_definition",
        "cce17d2f-8365-4b1f-a80b-34d10425d51c",
        destinationId,
        "active",
        {
          custom_name: "pending gramme",
          dimension: "mass",
          base_unit_factor: "1",
          allows_ingredient_quantity: true,
        },
      ),
    );
    const source = await readIngredientCopyCatalog(userId, sourceId, "source");
    const destination = await readIngredientCopyCatalog(userId, destinationId);

    expect(source.units.map(({ id }) => id)).toEqual([sourceUnitId]);
    expect(source.dietaryTags.map(({ id }) => id)).toEqual([sourceTagId]);
    expect(destination.units.map(({ id }) => id)).toEqual([destinationUnitId]);
    expect(destination.dietaryTags.map(({ id }) => id)).toEqual([
      destinationTagId,
    ]);
    expect(readVisibleRecords).toHaveBeenCalledWith(
      userId,
      sourceId,
      "unit_definition",
      true,
    );
    expect(destination.units.map(({ id }) => id)).not.toContain(
      "cce17d2f-8365-4b1f-a80b-34d10425d51c",
    );
  });
});
