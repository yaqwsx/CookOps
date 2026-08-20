import { beforeEach, describe, expect, it } from "vitest";

import { readIngredientCatalog } from "./ingredient-catalog";
import {
  queueIngredientCreate,
  queueIngredientCreateWithVersion,
  replayIngredientCreate,
  validateIngredientCreate,
} from "./ingredient-create";
import { localDb } from "./local-db";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const tagId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const sectionId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
const input = {
  name: "  Rajčata  ",
  canonicalUnitId: unitId,
  massPerCanonicalQuantity: "1",
  dietaryTagIds: [],
};

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
}

async function addUnit(allowsIngredientQuantity = true) {
  await localDb.canonicalRecords.add({
    userId,
    organizationId,
    entityType: "unit_definition",
    entityId: unitId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      id: unitId,
      organization_id: null,
      code: "g",
      dimension: "mass",
      base_unit_factor: "1",
      allows_ingredient_quantity: allowsIngredientQuantity,
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

async function addTag(lifecycle: "active" | "retired" = "active") {
  await localDb.canonicalRecords.add({
    userId,
    organizationId,
    entityType: "dietary_tag",
    entityId: tagId,
    recordSchemaVersion: 1,
    lifecycle,
    fields: { id: tagId, organization_id: organizationId, name: "Vegan" },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

async function addSection(lifecycle: "active" | "retired" = "active", owner = organizationId) {
  await localDb.canonicalRecords.add({
    userId,
    organizationId,
    entityType: "store_section",
    entityId: sectionId,
    recordSchemaVersion: 1,
    lifecycle,
    fields: { id: sectionId, organization_id: owner, name: "Produce" },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

describe("offline ingredient creation", () => {
  beforeEach(clearDatabase);

  it("accepts only locally safe typed create values", () => {
    expect(validateIngredientCreate(input)).toBeUndefined();
    for (const candidate of [
      { ...input, name: " " },
      { ...input, name: "x".repeat(201) },
      { ...input, canonicalUnitId: "not-a-uuid" },
      { ...input, massPerCanonicalQuantity: "0" },
      { ...input, massPerCanonicalQuantity: "-1" },
      { ...input, massPerCanonicalQuantity: "1e3" },
      { ...input, massPerCanonicalQuantity: "01" },
    ])
      expect(validateIngredientCreate(candidate)).toBeDefined();
  });

  it("fuzzes string inputs without accepting malformed intent", () => {
    for (let index = 0; index < 200; index += 1) {
      const fuzz = String.fromCharCode(index) + "e".repeat(index % 4);
      expect(
        validateIngredientCreate({
          ...input,
          name: fuzz,
          massPerCanonicalQuantity: `-${fuzz}`,
        }),
      ).toBeDefined();
    }
  });

  it("atomically queues the intent and shows its complete optimistic catalog projection", async () => {
    await addUnit();
    const ingredientId = await queueIngredientCreate(
      userId,
      organizationId,
      input,
    );
    const [command] = await localDb.outbox.toArray();
    expect(command).toEqual(
      expect.objectContaining({
        commandType: "ingredient.create",
        state: "pending",
        payload: expect.objectContaining({
          ingredient_id: ingredientId,
          name: "Rajčata",
          canonical_unit_id: unitId,
          mass_per_canonical_quantity: "1",
          dietary_tag_ids: [],
        }),
      }),
    );
    await expect(
      readIngredientCatalog(userId, organizationId),
    ).resolves.toEqual({
      organizationDefaultCurrency: "",
      storeSections: [],
      units: [
        { id: unitId, name: "g", dimension: "mass", baseUnitFactor: "1" },
      ],
      dietaryTags: [],
      ingredients: [
        {
          id: ingredientId,
          versionId: expect.any(String),
          name: "Rajčata",
          canonicalUnitName: "g",
          massPerCanonicalQuantity: "1",
        },
      ],
    });
  });

  it("returns the exact immutable version represented by the optimistic outbox", async () => {
    await addUnit();
    const result = await queueIngredientCreateWithVersion(userId, organizationId, input);
    const [command] = await localDb.outbox.toArray();
    expect(result).toEqual({
      ingredientId: command.payload.ingredient_id,
      ingredientVersionId: command.payload.ingredient_version_id,
    });
    expect(await localDb.optimisticOverlays.get([userId, organizationId, "ingredient_version", result.ingredientVersionId])).toEqual(
      expect.objectContaining({ entityId: result.ingredientVersionId, immutable: true }),
    );
  });

  it("leaves no partial work when the cached unit is absent or unsuitable", async () => {
    await expect(
      queueIngredientCreate(userId, organizationId, input),
    ).rejects.toThrow("unit");
    await addUnit(false);
    await expect(
      queueIngredientCreate(userId, organizationId, input),
    ).rejects.toThrow("unit");
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("queues only active locally available dietary tags", async () => {
    await addUnit();
    await expect(
      queueIngredientCreate(userId, organizationId, {
        ...input,
        dietaryTagIds: [tagId],
      }),
    ).rejects.toThrow("tag");
    await addTag("retired");
    await expect(
      queueIngredientCreate(userId, organizationId, {
        ...input,
        dietaryTagIds: [tagId],
      }),
    ).rejects.toThrow("tag");
    await localDb.canonicalRecords.delete([
      userId,
      organizationId,
      "dietary_tag",
      tagId,
    ]);
    await addTag();
    await queueIngredientCreate(userId, organizationId, {
      ...input,
      dietaryTagIds: [tagId],
    });
    await expect(localDb.outbox.toArray()).resolves.toEqual([
      expect.objectContaining({
        payload: expect.objectContaining({ dietary_tag_ids: [tagId] }),
      }),
    ]);
  });

  it("does not replay malformed, missing, or retired dietary-tag intent", async () => {
    const command = (dietary_tag_ids: unknown) => ({
      id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-08T00:00:00Z",
      payload: {
        ingredient_id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
        ingredient_version_id: "ace17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Tomatoes",
        canonical_unit_id: unitId,
        mass_per_canonical_quantity: "1",
        dietary_tag_ids,
      },
    });
    await replayIngredientCreate(userId, organizationId, command("not-an-array"));
    await replayIngredientCreate(userId, organizationId, command([tagId]));
    await addTag("retired");
    await replayIngredientCreate(userId, organizationId, command([tagId]));
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("fails closed for extra keys and non-canonical persisted names", async () => {
    await addUnit();
    const payload = {
      ingredient_id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
      ingredient_version_id: "ace17d2f-8365-4b1f-a80b-34d10425d51c",
      name: "Tomatoes",
      canonical_unit_id: unitId,
      mass_per_canonical_quantity: "1",
      dietary_tag_ids: [],
    };
    const command = (name: string, extra = {}) => ({
      id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-08T00:00:00Z",
      payload: { ...payload, name, ...extra },
    });
    await replayIngredientCreate(
      userId,
      organizationId,
      command("Tomatoes", { unexpected: true }),
    );
    await replayIngredientCreate(userId, organizationId, command(" Tomatoes"));
    await replayIngredientCreate(userId, organizationId, command("\u0000"));
    await replayIngredientCreate(userId, organizationId, command("\ud800"));
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("ignores a null persisted payload without throwing", async () => {
    await expect(
      replayIngredientCreate(userId, organizationId, {
        id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
        actionAt: "2026-08-08T00:00:00Z",
        payload: null as unknown as Record<string, unknown>,
      }),
    ).resolves.toBeUndefined();
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("normalizes a legacy six-key replay payload to a null store section", async () => {
    await addUnit();
    await replayIngredientCreate(userId, organizationId, {
      id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
      actionAt: "2026-08-08T00:00:00Z",
      payload: {
        ingredient_id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
        ingredient_version_id: "ace17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Tomatoes",
        canonical_unit_id: unitId,
        mass_per_canonical_quantity: "1",
        dietary_tag_ids: [],
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "ingredient_version",
        "ace17d2f-8365-4b1f-a80b-34d10425d51c",
      ]),
    ).resolves.toMatchObject({ fields: { default_store_section_id: null } });
  });

  it("does not optimistically publish an impossible mass-unit conversion", async () => {
    await addUnit();
    await expect(
      queueIngredientCreate(userId, organizationId, {
        ...input,
        massPerCanonicalQuantity: "2",
      }),
    ).rejects.toThrow("mass");
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("includes an active section in the payload and optimistic version", async () => {
    await addUnit();
    await addSection();
    const result = await queueIngredientCreateWithVersion(userId, organizationId, { ...input, defaultStoreSectionId: sectionId });
    expect((await localDb.outbox.toArray())[0]?.payload).toMatchObject({ default_store_section_id: sectionId });
    await expect(localDb.optimisticOverlays.get([userId, organizationId, "ingredient_version", result.ingredientVersionId])).resolves.toMatchObject({ fields: { default_store_section_id: sectionId } });
  });

  it.each([["retired", "retired"], ["foreign", organizationId.replace("5", "a")]])("does not queue a %s selected section", async (_label, ownerOrLifecycle) => {
    await addUnit();
    await addSection(ownerOrLifecycle === "retired" ? "retired" : "active", ownerOrLifecycle === "retired" ? organizationId : ownerOrLifecycle);
    await expect(queueIngredientCreate(userId, organizationId, { ...input, defaultStoreSectionId: sectionId })).rejects.toThrow("storeSection");
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("does not replay a create whose selected section is retired or foreign", async () => {
    await addUnit();
    const command = { id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-08T00:00:00Z", payload: { ingredient_id: "ace17d2f-8365-4b1f-a80b-34d10425d51c", ingredient_version_id: "bce17d2f-8365-4b1f-a80b-34d10425d51c", name: "Tomatoes", canonical_unit_id: unitId, mass_per_canonical_quantity: "1", dietary_tag_ids: [], default_store_section_id: sectionId } };
    await addSection("retired");
    await replayIngredientCreate(userId, organizationId, command);
    await localDb.canonicalRecords.delete([userId, organizationId, "store_section", sectionId]);
    await addSection("active", organizationId.replace("5", "a"));
    await replayIngredientCreate(userId, organizationId, command);
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("does not attach another ingredient's immutable version to a cached root", async () => {
    const rootId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const otherId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const versionId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
    await addUnit();
    await localDb.canonicalRecords.bulkAdd([
      {
        userId,
        organizationId,
        entityType: "ingredient",
        entityId: rootId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: rootId,
          organization_id: organizationId,
          current_version_id: versionId,
        },
        fieldClocks: {},
        immutable: false,
        updatedAt: "2026-08-07T12:00:00.000Z",
      },
      {
        userId,
        organizationId,
        entityType: "ingredient_version",
        entityId: versionId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: versionId,
          organization_id: organizationId,
          ingredient_id: otherId,
          name: "Leaked version",
          canonical_unit_id: unitId,
          mass_per_canonical_quantity: "1",
        },
        fieldClocks: {},
        immutable: true,
        updatedAt: "2026-08-07T12:00:00.000Z",
      },
    ]);
    await expect(
      readIngredientCatalog(userId, organizationId),
    ).resolves.toMatchObject({ ingredients: [] });
  });
});
