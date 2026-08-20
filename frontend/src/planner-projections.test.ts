import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CanonicalRecord } from "./local-db";
import { localDb } from "./local-db";
import { readEventPlanner, suggestedScale } from "./planner-projections";
import { queueClearAddedOverride, queueClearReplacementOverride } from "./scheduled-ingredient-override";

const organizationId = "55555555-5555-4555-8555-555555555555";
const userId = "66666666-6666-4666-8666-666666666666";
const ids = {
  event: "11111111-1111-4111-8111-111111111111",
  day: "22222222-2222-4222-8222-222222222222",
  role: "33333333-3333-4333-8333-333333333333",
  recipe: "44444444-4444-4444-8444-444444444444",
  recipeVersion: "77777777-7777-4777-8777-777777777777",
  ingredient: "88888888-8888-4888-8888-888888888888",
  oldVersion: "99999999-9999-4999-8999-999999999999",
  currentVersion: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  line: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  scheduled: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  tag: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  exception: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  replacement: "ffffffff-ffff-4fff-8fff-ffffffffffff",
  addedVersion: "12121212-1212-4121-8121-121212121212",
};

it("matches backend decimal scaling without rounding fractional suggestions", () => {
  expect(suggestedScale(12, "100", "portion", "5", "1", false)).toBe("2.4");
  expect(suggestedScale(12, "100", "portion", "5", "1", true)).toBe("3");
  expect(suggestedScale(10, "100", "portion", "3", "1", false)).toBeUndefined();
  expect(suggestedScale(0, "100", "portion", "3", "1", false)).toBe("0");
  expect(suggestedScale(1, "0.00000000000001", "person", undefined, "1", false)).toBe("0.0000000000000001");
});

const readVisibleRecords = vi.hoisted(() => vi.fn());
vi.mock("./visible-records", () => ({ readVisibleRecords }));

function record(
  entityType: string,
  entityId: string,
  fields: Record<string, unknown>,
  options: Partial<CanonicalRecord> = {},
): CanonicalRecord {
  return {
    userId,
    organizationId,
    entityType,
    entityId,
    recordSchemaVersion: 1,
    fieldClocks: {},
    immutable: true,
    lifecycle: "active",
    updatedAt: "2026-08-15T00:00:00Z",
    ...options,
    fields: { id: entityId, organization_id: organizationId, ...fields, ...(options.fields ?? {}) },
  };
}

function cache(options: {
  ingredientVersion?: Partial<CanonicalRecord>;
  currentVersion?: Partial<CanonicalRecord>;
  currentRoot?: Partial<CanonicalRecord>;
  lineVersionId?: string;
  lineVersion?: Partial<CanonicalRecord>;
  recipeCurrentVersionId?: string;
  recipeCurrentVersion?: Partial<CanonicalRecord>;
  recipeVersionBaseScalingAmount?: string;
  scheduledDinerCount?: number;
  dietary?: CanonicalRecord[];
  overrides?: CanonicalRecord[];
  dietaryTag?: Partial<CanonicalRecord>;
  extraRecords?: Partial<Record<string, CanonicalRecord[]>>;
}): Record<string, CanonicalRecord[]> {
  const currentVersion = record("ingredient_version", ids.currentVersion, {
    ingredient_id: ids.ingredient,
    name: "Current",
    canonical_unit_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    ...(options.currentVersion?.fields ?? {}),
  }, options.currentVersion);
  const oldVersion = record("ingredient_version", ids.oldVersion, {
    ingredient_id: ids.ingredient,
    name: "Old",
    canonical_unit_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    ...(options.ingredientVersion?.fields ?? {}),
  }, options.ingredientVersion);
  const root = record("ingredient", ids.ingredient, {
    current_version_id: ids.currentVersion,
  }, options.currentRoot);
  const records: Record<string, CanonicalRecord[]> = {
    event: [record("event", ids.event, { name: "Event", start_date: "2026-08-15", end_date: "2026-08-15", base_expected_attendance: 4, lifecycle: "active" })],
    event_day: [record("event_day", ids.day, { event_id: ids.event, calendar_date: "2026-08-15", is_visible: true })],
    event_meal_role: [record("event_meal_role", ids.role, { event_id: ids.event, position_key: "a", custom_name: "Dinner", built_in_translation_key: null })],
    recipe: [record("recipe", ids.recipe, { current_version_id: options.recipeCurrentVersionId ?? ids.recipeVersion })],
    recipe_version: [record("recipe_version", ids.recipeVersion, { recipe_id: ids.recipe, name: "Soup", scaling_unit_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", base_scaling_amount: options.recipeVersionBaseScalingAmount ?? "1" }), ...(options.recipeCurrentVersionId ? [record("recipe_version", options.recipeCurrentVersionId, { recipe_id: ids.recipe, name: "Soup new", scaling_unit_id: "ffffffff-ffff-4fff-8fff-ffffffffffff", base_scaling_amount: "1", estimated_diners_per_scaling_unit: "2", round_suggestions_up: false }, options.recipeCurrentVersion)] : [])],
    recipe_ingredient_line: [record("recipe_ingredient_line", ids.line, { recipe_version_id: ids.recipeVersion, ingredient_version_id: options.lineVersionId ?? ids.oldVersion, base_quantity: "1", line_key: ids.line }, options.lineVersion), ...(options.recipeCurrentVersionId ? [record("recipe_ingredient_line", "dddddddd-dddd-4ddd-8ddd-dddddddddddd", { recipe_version_id: options.recipeCurrentVersionId, ingredient_version_id: ids.oldVersion, base_quantity: "2", line_key: ids.line })] : [])],
    scheduled_recipe: [record("scheduled_recipe", ids.scheduled, { event_id: ids.event, event_day_id: ids.day, event_meal_role_id: ids.role, recipe_id: ids.recipe, recipe_version_id: ids.recipeVersion, diner_count: options.scheduledDinerCount ?? 2, consumption_percentage: "100", selected_scale_amount: "2", position_key: "a" })],
    ingredient: [root],
    ingredient_version: [oldVersion, currentVersion],
    scheduled_ingredient_override: options.overrides ?? [],
    unit_definition: [record("unit_definition", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", { code: "portion", custom_name: "portion", allows_ingredient_quantity: true, allows_recipe_scaling: true }), record("unit_definition", "ffffffff-ffff-4fff-8fff-ffffffffffff", { code: "portion", custom_name: "portion", allows_ingredient_quantity: true, allows_recipe_scaling: true })],
    dietary_tag: [record("dietary_tag", ids.tag, { name: "Vegan", ...(options.dietaryTag?.fields ?? {}) }, options.dietaryTag)],
    event_dietary_exception: options.dietary?.filter((item) => item.entityType === "event_dietary_exception") ?? [],
    event_dietary_exception_tag: options.dietary?.filter((item) => item.entityType === "event_dietary_exception_tag") ?? [],
  };
  readVisibleRecords.mockImplementation(async (_user: string, _org: string, entityType: string) => [...(records[entityType] ?? []), ...(options.extraRecords?.[entityType] ?? [])]);
  return records;
}

describe("readEventPlanner catalog update projection", () => {
  beforeEach(() => vi.clearAllMocks());

  it.each([
    ["old immutable version", {}, ids.oldVersion, true],
    ["missing referenced version", {}, "dddddddd-dddd-4ddd-8ddd-dddddddddddd", false],
    ["mutable referenced version", { ingredientVersion: { immutable: false } }, ids.oldVersion, false],
    ["wrong-org current target", { currentVersion: { fields: { organization_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee" } } }, ids.oldVersion, false],
    ["missing-org current target", { currentVersion: { fields: { organization_id: undefined } } }, ids.oldVersion, false],
    ["retired root", { currentRoot: { lifecycle: "retired" as const } }, ids.oldVersion, false],
    ["current reference", {}, ids.currentVersion, false],
  ])("projects %s correctly", async (_name, options, lineVersionId, expected) => {
    cache({ ...options, lineVersionId });
    const planner = await readEventPlanner(userId, organizationId, ids.event);
    expect(planner?.recipes[0]?.name).toBe("Soup");
    expect(planner?.scheduled[0]?.catalogUpdateAvailable).toBe(expected);
  });

  it("shows a recipe-version quantity update even without an ingredient-version update", async () => {
    const target = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
    cache({ recipeCurrentVersionId: target });
    const planner = await readEventPlanner(userId, organizationId, ids.event);
    expect(planner?.scheduled[0]?.catalogUpdateAvailable).toBe(true);
    expect(planner?.scheduled[0]?.catalogUpdateChanges.changed).toBe(1);
  });

  it("uses manual scheduled diners for the catalog scale suggestion", async () => {
    const target = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
    cache({ recipeCurrentVersionId: target, scheduledDinerCount: 7 });
    const planner = await readEventPlanner(userId, organizationId, ids.event);
    expect(planner?.scheduled[0]?.catalogScaleImpact.suggestedAmount).toBe("3.5");
  });

  it("uses the pinned immutable version for detail and drops malformed detail records", async () => {
    const newer = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
    cache({ recipeCurrentVersionId: newer, recipeCurrentVersion: { fields: { name: "New catalog name" } }, lineVersionId: ids.oldVersion, lineVersion: { fields: { note: "pinned note" } } });
    const planner = await readEventPlanner(userId, organizationId, ids.event);
    expect(planner?.scheduled[0]).toMatchObject({ recipeVersionName: "Soup", detailLines: [{ name: "Old" }] });
    expect(planner?.scheduled[0]?.recipeVersionName).not.toBe("New catalog name");
    const records = await readVisibleRecords(userId, organizationId, "recipe_version");
    const malformed = records.find((record: CanonicalRecord) => record.entityId === ids.recipeVersion);
    if (malformed) malformed.immutable = false;
    const retained = await readEventPlanner(userId, organizationId, ids.event);
    expect(retained?.scheduled).toHaveLength(1);
    expect(retained?.scheduled[0]?.detailLines).toEqual([]);
  });

  it("keeps weight unavailable when cached mass metadata is incomplete", async () => {
    cache({ ingredientVersion: { fields: { mass_per_canonical_quantity: "not-a-mass" } } });
    const planner = await readEventPlanner(userId, organizationId, ids.event);
    expect(planner?.scheduled[0]).toMatchObject({ preparedWeight: null, perDinerWeight: null });
    cache({ ingredientVersion: { fields: { mass_per_canonical_quantity: "-0.5" } } });
    const negativeMassPlanner = await readEventPlanner(userId, organizationId, ids.event);
    expect(negativeMassPlanner?.scheduled[0]).toMatchObject({ preparedWeight: null, perDinerWeight: null });
  });

  it("resolves detail quantities, units, overrides, and exact weights", async () => {
    const fixedLine = "abababab-abab-4aba-8aba-abababababab", replacementVersion = "acacacac-acac-4aca-8aca-acacacacacac", replacementRoot = "adadadad-adad-4ada-8dad-adadadadadad", replacementUnit = "aeaeaeae-aeae-4aea-8eae-aeaeaeaeaeae", addVersion = "afafafaf-afaf-4afa-8faf-afafafafafaf", addRoot = "babababa-baba-4bab-8bab-babababababa", addUnit = "bcbcbcbc-bcbc-4bcb-8bcb-bcbcbcbcbcbc";
    const add = (id: string, version: string, quantity: string, include: boolean, lifecycle: "active" | "retired" = "active") => record("scheduled_ingredient_override", id, { event_id: ids.event, scheduled_recipe_id: ids.scheduled, override_kind: "add", ingredient_version_id: version, quantity, include_in_portion_weight: include }, { lifecycle });
    cache({ scheduledDinerCount: 4, ingredientVersion: { fields: { mass_per_canonical_quantity: "0.1" } }, currentVersion: { fields: { mass_per_canonical_quantity: "0.1" } }, lineVersion: { fields: { scaling_behavior: "proportional", include_in_portion_weight: true } }, extraRecords: {
      recipe_ingredient_line: [record("recipe_ingredient_line", fixedLine, { recipe_version_id: ids.recipeVersion, ingredient_version_id: ids.currentVersion, base_quantity: "1.25", line_key: fixedLine, scaling_behavior: "fixed", include_in_portion_weight: true })],
      ingredient: [record("ingredient", replacementRoot, { current_version_id: replacementVersion }), record("ingredient", addRoot, { current_version_id: addVersion })],
      ingredient_version: [record("ingredient_version", replacementVersion, { ingredient_id: replacementRoot, name: "Replacement", canonical_unit_id: replacementUnit, mass_per_canonical_quantity: "0.125" }), record("ingredient_version", addVersion, { ingredient_id: addRoot, name: "Added", canonical_unit_id: addUnit, mass_per_canonical_quantity: "0.2" })],
      unit_definition: [record("unit_definition", replacementUnit, { code: "ml", custom_name: "ml", allows_ingredient_quantity: true }), record("unit_definition", addUnit, { code: "g", custom_name: "g", allows_ingredient_quantity: true })],
      scheduled_ingredient_override: [record("scheduled_ingredient_override", "cdcdcdcd-cdcd-4cdc-8dcd-cdcdcdcdcdcd", { event_id: ids.event, scheduled_recipe_id: ids.scheduled, override_kind: "replace", target_line_key: ids.line, ingredient_version_id: replacementVersion, quantity: "0.75" }, { immutable: false }), add("cececece-cece-4cec-8ece-cececececece", addVersion, "0.625", true), add("cfcfcfcf-cfcf-4fcf-8fcf-cfcfcfcfcfcf", addVersion, "0.5", false), add("d0d0d0d0-d0d0-4d0d-8d0d-d0d0d0d0d0d0", addVersion, "0", true), add("d1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1", addVersion, "1", true, "retired")],
    } });
    const planner = await readEventPlanner(userId, organizationId, ids.event);
    expect(planner?.scheduled[0]?.detailLines.map((line) => ({ name: line.name, quantity: line.quantity, unit: line.unitName }))).toEqual(expect.arrayContaining([{ name: "Replacement", quantity: "1.5", unit: "ml" }, { name: "Current", quantity: "1.25", unit: "portion" }, { name: "Added", quantity: "0.625", unit: "g" }]));
    expect(planner?.scheduled[0]?.detailLines).toHaveLength(4);
    expect(planner?.scheduled[0]?.preparedWeight).toBe("0.4375");
    expect(planner?.scheduled[0]?.perDinerWeight).toBe("0.109375");
  });

  it("fails closed for negative override quantities and malformed inclusion", async () => {
    const replacement = record("scheduled_ingredient_override", "cdcdcdcd-cdcd-4cdc-8dcd-cdcdcdcdcdcd", { event_id: ids.event, scheduled_recipe_id: ids.scheduled, override_kind: "replace", target_line_key: ids.line, ingredient_version_id: ids.currentVersion, quantity: "-1" }, { immutable: false });
    const added = record("scheduled_ingredient_override", "cececece-cece-4cec-8ece-cececececece", { event_id: ids.event, scheduled_recipe_id: ids.scheduled, override_kind: "add", ingredient_version_id: ids.oldVersion, quantity: "-1" }, { immutable: false });
    const malformed = record("scheduled_ingredient_override", "cfcfcfcf-cfcf-4fcf-8fcf-cfcfcfcfcfcf", { event_id: ids.event, scheduled_recipe_id: ids.scheduled, override_kind: "add", ingredient_version_id: ids.oldVersion, quantity: "1", include_in_portion_weight: "yes" }, { immutable: false });
    cache({ overrides: [replacement, added, malformed] });
    const planner = await readEventPlanner(userId, organizationId, ids.event);
    expect(planner?.scheduled[0]?.detailLines).toEqual([]);
    expect(planner?.scheduled[0]?.preparedWeight).toBeNull();
  });

  it("keeps pinned ingredient metadata for quantity-only replacements and rejects invalid IDs", async () => {
    const quantityOnly = record("scheduled_ingredient_override", "cdcdcdcd-cdcd-4cdc-8dcd-cdcdcdcdcdcd", { event_id: ids.event, scheduled_recipe_id: ids.scheduled, override_kind: "replace", target_line_key: ids.line, quantity: "2" }, { immutable: false });
    cache({ overrides: [quantityOnly] });
    const retained = await readEventPlanner(userId, organizationId, ids.event);
    expect(retained?.scheduled[0]?.detailLines[0]).toMatchObject({ name: "Old", unitName: "portion", quantity: "4" });

    const invalid = record("scheduled_ingredient_override", "cececece-cece-4cec-8ece-cececececece", { event_id: ids.event, scheduled_recipe_id: ids.scheduled, override_kind: "replace", target_line_key: ids.line, ingredient_version_id: "not-a-uuid", quantity: "2" }, { immutable: false });
    cache({ overrides: [invalid] });
    expect((await readEventPlanner(userId, organizationId, ids.event))?.scheduled[0]?.detailLines).toEqual([]);
  });

  it("projects the catalog line after a durable replacement clear", async () => {
    const replacement = record("scheduled_ingredient_override", ids.replacement, { event_id: ids.event, scheduled_recipe_id: ids.scheduled, override_kind: "replace", target_line_key: ids.line, ingredient_version_id: ids.currentVersion, quantity: "3" }, { immutable: false });
    const records = cache({ overrides: [replacement] });
    await localDb.canonicalRecords.bulkPut([records.event[0], records.scheduled_recipe[0], records.recipe_ingredient_line[0], replacement]);
    await queueClearReplacementOverride(userId, organizationId, { eventId: ids.event, scheduledRecipeId: ids.scheduled, targetLineKey: ids.line, overrideId: ids.replacement });
    const overlay = await localDb.optimisticOverlays.get([userId, organizationId, "scheduled_ingredient_override", ids.replacement]);
    expect(overlay).toMatchObject({ lifecycle: "retired" });
    readVisibleRecords.mockImplementation(async (_user: string, _org: string, entityType: string) => {
      const base = [...(records[entityType] ?? [])];
      if (entityType !== "scheduled_ingredient_override") return base;
      return base.filter((item) => item.entityId !== ids.replacement).concat(overlay ? [overlay] : []);
    });
    const planner = await readEventPlanner(userId, organizationId, ids.event);
    expect(planner?.scheduled[0]?.detailLines[0]).toMatchObject({ quantity: "2" });
    expect(planner?.scheduled[0]?.detailLines[0]).not.toHaveProperty("replacementOverrideId");
    expect(planner?.scheduled[0]?.detailLines[0]).not.toHaveProperty("replacementOverrideActive");
  });

  it("removes only an added line after a durable added clear", async () => {
    const added = record("scheduled_ingredient_override", ids.addedVersion, { event_id: ids.event, scheduled_recipe_id: ids.scheduled, override_kind: "add", ingredient_version_id: ids.oldVersion, quantity: "3", include_in_portion_weight: true }, { immutable: false });
    const records = cache({ overrides: [added] });
    await localDb.canonicalRecords.bulkPut([records.event[0], records.scheduled_recipe[0], records.recipe_ingredient_line[0], added]);
    const before = await readEventPlanner(userId, organizationId, ids.event);
    expect(before?.scheduled[0]?.detailLines.map((line) => line.name)).toEqual(["Old", "Old"]);
    await queueClearAddedOverride(userId, organizationId, { eventId: ids.event, scheduledRecipeId: ids.scheduled, overrideId: ids.addedVersion });
    const overlay = await localDb.optimisticOverlays.get([userId, organizationId, "scheduled_ingredient_override", ids.addedVersion]);
    readVisibleRecords.mockImplementation(async (_user: string, _org: string, entityType: string) => {
      const base = [...(records[entityType] ?? [])];
      if (entityType !== "scheduled_ingredient_override") return base;
      return base.filter((item) => item.entityId !== ids.addedVersion).concat(overlay ? [overlay] : []);
    });
    const after = await readEventPlanner(userId, organizationId, ids.event);
    expect(after?.scheduled[0]?.detailLines).toHaveLength(1);
    expect(after?.scheduled[0]?.detailLines[0]).toMatchObject({ name: "Old", quantity: "2" });
  });

  it("scales detail quantities when the pinned base is 0.5", async () => {
    cache({ recipeVersionBaseScalingAmount: "0.5" });
    const planner = await readEventPlanner(userId, organizationId, ids.event);
    expect(planner?.scheduled[0]?.detailLines[0]?.quantity).toBe("4");
  });
});

it("projects active dietary conflicts from resolved nonzero ingredients", async () => {
  const exception = record("event_dietary_exception", ids.exception, {
    event_id: ids.event,
    name: "Alex",
    tag_ids: [ids.tag],
  });
  const replacement = record("scheduled_ingredient_override", ids.replacement, {
    event_id: ids.event,
    scheduled_recipe_id: ids.scheduled,
    override_kind: "replace",
    target_line_key: ids.line,
    ingredient_version_id: ids.currentVersion,
    quantity: "1",
  }, { immutable: false });
  const added = record("scheduled_ingredient_override", ids.addedVersion, {
    event_id: ids.event,
    scheduled_recipe_id: ids.scheduled,
    override_kind: "add",
    ingredient_version_id: ids.oldVersion,
    quantity: "0",
  }, { immutable: false });
  cache({
    currentVersion: { fields: { id: ids.currentVersion, organization_id: organizationId, ingredient_id: ids.ingredient, name: "Current", dietary_tag_ids: [ids.tag] } },
    dietary: [exception],
    overrides: [replacement, added],
  });
  const planner = await readEventPlanner(userId, organizationId, ids.event);
  expect(planner?.scheduled[0]?.dietaryWarnings).toEqual([
    { exceptionName: "Alex", tagNames: ["Vegan"], tagDescriptors: [{ id: ids.tag, name: "Vegan" }], ingredientNames: ["Current"] },
  ]);
});

it("matches retired seeded dietary tags and preserves their descriptor", async () => {
  const exception = record("event_dietary_exception", ids.exception, {
    event_id: ids.event,
    name: "Alex",
    tag_ids: [ids.tag],
  });
  cache({
    dietaryTag: { lifecycle: "retired", fields: { name: null, seed_key: "vegan" } },
    ingredientVersion: { fields: { dietary_tag_ids: [ids.tag] } },
    dietary: [exception],
  });
  const planner = await readEventPlanner(userId, organizationId, ids.event);
  expect(planner?.scheduled[0]?.dietaryWarnings?.[0]?.tagDescriptors).toEqual([
    { id: ids.tag, seedKey: "vegan", name: undefined },
  ]);
});
