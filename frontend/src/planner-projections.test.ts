import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CanonicalRecord } from "./local-db";
import { readEventPlanner } from "./planner-projections";

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
};

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
    fields: { id: entityId, organization_id: organizationId, ...fields },
    fieldClocks: {},
    immutable: true,
    lifecycle: "active",
    updatedAt: "2026-08-15T00:00:00Z",
    ...options,
  };
}

function cache(options: {
  ingredientVersion?: Partial<CanonicalRecord>;
  currentVersion?: Partial<CanonicalRecord>;
  currentRoot?: Partial<CanonicalRecord>;
  lineVersionId?: string;
  lineVersion?: Partial<CanonicalRecord>;
}) {
  const currentVersion = record("ingredient_version", ids.currentVersion, {
    ingredient_id: ids.ingredient,
    name: "Current",
    ...(options.currentVersion?.fields ?? {}),
  }, options.currentVersion);
  const oldVersion = record("ingredient_version", ids.oldVersion, {
    ingredient_id: ids.ingredient,
    name: "Old",
    ...(options.ingredientVersion?.fields ?? {}),
  }, options.ingredientVersion);
  const root = record("ingredient", ids.ingredient, {
    current_version_id: ids.currentVersion,
  }, options.currentRoot);
  const records: Record<string, CanonicalRecord[]> = {
    event: [record("event", ids.event, { name: "Event", start_date: "2026-08-15", end_date: "2026-08-15", base_expected_attendance: 4, lifecycle: "active" })],
    event_day: [record("event_day", ids.day, { event_id: ids.event, calendar_date: "2026-08-15", is_visible: true })],
    event_meal_role: [record("event_meal_role", ids.role, { event_id: ids.event, position_key: "a", custom_name: "Dinner", built_in_translation_key: null })],
    recipe: [record("recipe", ids.recipe, { current_version_id: ids.recipeVersion })],
    recipe_version: [record("recipe_version", ids.recipeVersion, { recipe_id: ids.recipe, name: "Soup" })],
    recipe_ingredient_line: [record("recipe_ingredient_line", ids.line, { recipe_version_id: ids.recipeVersion, ingredient_version_id: options.lineVersionId ?? ids.oldVersion, base_quantity: "1", line_key: ids.line })],
    scheduled_recipe: [record("scheduled_recipe", ids.scheduled, { event_id: ids.event, event_day_id: ids.day, event_meal_role_id: ids.role, recipe_id: ids.recipe, recipe_version_id: ids.recipeVersion, diner_count: 2, consumption_percentage: "100", selected_scale_amount: "2", position_key: "a" })],
    ingredient: [root],
    ingredient_version: [oldVersion, currentVersion],
    scheduled_ingredient_override: [],
  };
  readVisibleRecords.mockImplementation(async (_user: string, _org: string, entityType: string) => records[entityType] ?? []);
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
});
