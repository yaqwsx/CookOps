import type { CanonicalRecord } from "./local-db";
import { readVisibleRecords } from "./visible-records";

export type PlannerRecipe = { id: string; versionId: string; name: string };
export type PlannerIngredient = { id: string; versionId: string; name: string };
export type PlannerDay = { id: string; date: string; note: string | null; visible: boolean };
export type PlannerRole = { id: string; name: string; position: string };
export type PlannedRecipe = {
  id: string;
  dayId: string;
  roleId: string;
  name: string;
  dinerCount: number;
  consumptionPercentage: string;
  selectedScaleAmount: string;
  position: string;
  retired: boolean;
  lines: { id: string; quantity: string; ingredientId?: string }[];
  localAddedIngredients: { id: string; name: string; quantity: string }[];
};
export type EventPlannerProjection = {
  name: string;
  startDate: string;
  endDate: string;
  attendance: number;
  lifecycle: "active" | "archived";
  days: PlannerDay[];
  hiddenDays: PlannerDay[];
  roles: PlannerRole[];
  recipes: PlannerRecipe[];
  ingredients: PlannerIngredient[];
  scheduled: PlannedRecipe[];
};

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function value(record: CanonicalRecord, key: string): string | undefined {
  const item = record.fields[key];
  return typeof item === "string" ? item : undefined;
}

function hasId(record: CanonicalRecord): boolean {
  return uuid.test(record.entityId) && value(record, "id") === record.entityId;
}

function belongsToOrganization(
  record: CanonicalRecord,
  organizationId: string,
): boolean {
  const owner = value(record, "organization_id");
  return owner === undefined || owner === organizationId;
}

/** Read only valid cached records, keeping malformed remote data out of the UI and outbox. */
export async function readEventPlanner(
  userId: string,
  organizationId: string,
  eventId: string,
): Promise<EventPlannerProjection | undefined> {
  if (!uuid.test(eventId)) return undefined;
  const [
    eventRecords,
    dayRecords,
    roleRecords,
    recipeRecords,
    versionRecords,
    lineRecords,
    scheduledRecords,
    ingredientRecords,
    ingredientVersionRecords,
    overrideRecords,
  ] = await Promise.all([
    readVisibleRecords(userId, organizationId, "event", true),
    readVisibleRecords(userId, organizationId, "event_day"),
    readVisibleRecords(userId, organizationId, "event_meal_role"),
    readVisibleRecords(userId, organizationId, "recipe"),
    readVisibleRecords(userId, organizationId, "recipe_version"),
    readVisibleRecords(userId, organizationId, "recipe_ingredient_line"),
    readVisibleRecords(userId, organizationId, "scheduled_recipe", true),
    readVisibleRecords(userId, organizationId, "ingredient"),
    readVisibleRecords(userId, organizationId, "ingredient_version"),
    readVisibleRecords(userId, organizationId, "scheduled_ingredient_override"),
  ]);
  const event = eventRecords.find(
    (record) =>
      record.entityId === eventId &&
      hasId(record) &&
      value(record, "organization_id") === organizationId,
  );
  const lifecycle = event?.fields.lifecycle;
  const name = event && value(event, "name");
  const startDate = event && value(event, "start_date");
  const endDate = event && value(event, "end_date");
  const attendance = event?.fields.base_expected_attendance;
  if (
    !event ||
    !name ||
    !startDate ||
    !endDate ||
    typeof attendance !== "number" ||
    !Number.isSafeInteger(attendance) ||
    (lifecycle !== "active" && lifecycle !== "archived")
  )
    return undefined;
  const projectedDays = dayRecords
    .filter(
      (record) =>
        hasId(record) &&
        belongsToOrganization(record, organizationId) &&
        value(record, "event_id") === eventId,
    )
    .map((record) => ({
      id: record.entityId,
      date: value(record, "calendar_date"),
      note: record.fields.note,
      visible: record.fields.is_visible !== false,
    }))
    .filter(
      (day): day is PlannerDay =>
        Boolean(day.date) &&
        (day.note === null || typeof day.note === "string"),
    )
    .sort(
      (left, right) =>
        left.date.localeCompare(right.date) || left.id.localeCompare(right.id),
    );
  const roles = roleRecords
    .filter(
      (record) =>
        hasId(record) &&
        belongsToOrganization(record, organizationId) &&
        value(record, "event_id") === eventId,
    )
    .map((record) => ({
      id: record.entityId,
      position: value(record, "position_key"),
      name:
        value(record, "custom_name") ??
        value(record, "built_in_translation_key"),
    }))
    .filter((role): role is PlannerRole => Boolean(role.name && role.position))
    .sort(
      (left, right) =>
        left.position.localeCompare(right.position) ||
        left.id.localeCompare(right.id),
    );
  const versions = new Map(
    versionRecords
      .filter(
        (record) =>
          hasId(record) && belongsToOrganization(record, organizationId),
      )
      .map((record) => [record.entityId, value(record, "name")]),
  );
  const recipes = recipeRecords
    .filter(
      (record) =>
        hasId(record) && belongsToOrganization(record, organizationId),
    )
    .map((record) => {
      const versionId =
        value(record, "current_version_id") ??
        value(record, "recipe_version_id");
      return {
        id: record.entityId,
        versionId,
        name: versions.get(versionId ?? "") ?? value(record, "name"),
      };
    })
    .filter((recipe): recipe is PlannerRecipe =>
      Boolean(
        recipe.name &&
          recipe.versionId &&
          uuid.test(recipe.id) &&
          uuid.test(recipe.versionId),
      ),
    )
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) || left.id.localeCompare(right.id),
    );
  const names = new Map(recipes.map((recipe) => [recipe.id, recipe.name]));
  const ingredientVersions = new Map(
    ingredientVersionRecords
      .filter(
        (record) =>
          hasId(record) && belongsToOrganization(record, organizationId),
      )
      .map((record) => [record.entityId, record]),
  );
  const ingredients = ingredientRecords
    .filter(
      (record) => hasId(record) && belongsToOrganization(record, organizationId),
    )
    .map((record) => {
      const versionId = value(record, "current_version_id");
      const version = versionId ? ingredientVersions.get(versionId) : undefined;
      return {
        id: record.entityId,
        versionId,
        name:
          version && value(version, "ingredient_id") === record.entityId
            ? value(version, "name")
            : undefined,
      };
    })
    .filter((item): item is PlannerIngredient =>
      Boolean(
        item.name &&
          item.versionId &&
          uuid.test(item.id) &&
          uuid.test(item.versionId),
      ),
    )
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) || left.id.localeCompare(right.id),
    );
  const lines = new Map<
    string,
    { id: string; quantity: string; ingredientId?: string }[]
  >();
  for (const record of lineRecords) {
    const versionId = value(record, "recipe_version_id");
    const baseQuantity = value(record, "base_quantity");
    const lineKey = value(record, "line_key");
    const ingredientId = value(
      ingredientVersions.get(value(record, "ingredient_version_id") ?? "") ?? record,
      "ingredient_id",
    );
    if (hasId(record) && versionId && baseQuantity && lineKey && uuid.test(lineKey))
      lines.set(versionId, [
        ...(lines.get(versionId) ?? []),
        { id: lineKey, quantity: baseQuantity, ingredientId },
      ]);
  }
  const localAddedIngredients = new Map<
    string,
    { id: string; name: string; quantity: string }[]
  >();
  for (const record of overrideRecords) {
    const scheduledRecipeId = value(record, "scheduled_recipe_id");
    const quantity = value(record, "quantity");
    const version = ingredientVersions.get(
      value(record, "ingredient_version_id") ?? "",
    );
    const name = version && value(version, "name");
    if (
      hasId(record) &&
      value(record, "event_id") === eventId &&
      record.fields.override_kind === "add" &&
      scheduledRecipeId &&
      quantity &&
      name
    )
      localAddedIngredients.set(scheduledRecipeId, [
        ...(localAddedIngredients.get(scheduledRecipeId) ?? []),
        { id: record.entityId, name, quantity },
      ]);
  }
  const scheduled = scheduledRecords
    .filter(
      (record) =>
        hasId(record) &&
        belongsToOrganization(record, organizationId) &&
        value(record, "event_id") === eventId,
    )
    .map((record) => ({
      id: record.entityId,
      dayId: value(record, "event_day_id"),
      roleId: value(record, "event_meal_role_id"),
      name: names.get(value(record, "recipe_id") ?? ""),
      lines: lines.get(value(record, "recipe_version_id") ?? "") ?? [],
      localAddedIngredients: localAddedIngredients.get(record.entityId) ?? [],
      dinerCount: record.fields.diner_count,
      consumptionPercentage: value(record, "consumption_percentage"),
      selectedScaleAmount: value(record, "selected_scale_amount"),
      position: value(record, "position_key"),
      retired: record.lifecycle === "retired",
    }))
    .filter((item): item is PlannedRecipe =>
      Boolean(
        item.dayId &&
          item.roleId &&
          item.name &&
          item.position &&
          uuid.test(item.dayId) &&
          uuid.test(item.roleId) &&
          Number.isSafeInteger(item.dinerCount) &&
          item.consumptionPercentage !== undefined &&
          item.selectedScaleAmount !== undefined,
      ),
    )
    .sort(
      (left, right) =>
        left.position.localeCompare(right.position) ||
        left.id.localeCompare(right.id),
    );
  return {
    name,
    startDate,
    endDate,
    attendance,
    lifecycle,
    days: projectedDays.filter((day) => day.visible),
    hiddenDays: projectedDays.filter((day) => !day.visible),
    roles,
    recipes,
    ingredients,
    scheduled,
  };
}
