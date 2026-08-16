import type { CanonicalRecord } from "./local-db";
import { readVisibleRecords } from "./visible-records";
import { decimal as parseDecimal } from "./shopping-projections";

export type PlannerRecipe = { id: string; versionId: string; name: string; scalingUnitId: string; scalingUnitName: string; baseScalingAmount: string; roundSuggestionsUp: boolean };
export type PlannerIngredient = { id: string; versionId: string; name: string };
export type PlannerDay = { id: string; date: string; note: string | null; visible: boolean; retired: boolean };
export type PlannerRole = { id: string; name: string; position: string; retired: boolean; custom: boolean };
export type PlannedRecipe = {
  id: string;
  recipeId: string;
  recipeVersionId: string;
  dayId: string;
  roleId: string;
  name: string;
  dinerCount: number;
  consumptionPercentage: string;
  selectedScaleAmount: string;
  position: string;
  retired: boolean;
  catalogUpdateAvailable: boolean;
  catalogUpdateChanges: { added: number; removed: number; changed: number };
  catalogScaleImpact: { currentUnitId: string | undefined; targetUnitId: string | undefined; currentUnitName: string | undefined; targetUnitName: string | undefined; reset: boolean; targetBase: string | undefined; suggestedAmount: string | undefined };
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
  retiredDays: PlannerDay[];
  roles: PlannerRole[];
  retiredRoles: PlannerRole[];
  recipes: PlannerRecipe[];
  ingredients: PlannerIngredient[];
  scheduled: PlannedRecipe[];
};

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function suggestedScale(attendance: number, consumption: string, code: string | undefined, estimated: string | undefined, base: string | undefined, round: boolean): string | undefined {
  const percentage = parseDecimal(consumption);
  const capacity = code === "person" ? { value: 1n, scale: 0 } : estimated ? parseDecimal(estimated) : undefined;
  if (!percentage || !capacity || capacity.value <= 0n) return base;
  const numerator = BigInt(attendance) * percentage.value * 10n ** BigInt(capacity.scale);
  const denominator = 100n * 10n ** BigInt(percentage.scale) * capacity.value;
  if (numerator === 0n) return "0";
  const whole = numerator / denominator;
  if (round) return String(numerator % denominator === 0n ? whole : whole + 1n);
  let reduced = denominator;
  let common = numerator;
  while (common !== 0n) {
    const remainder = reduced % common;
    reduced = common;
    common = remainder;
  }
  reduced = denominator / reduced;
  for (const factor of [2n, 5n]) {
    while (reduced % factor === 0n) reduced /= factor;
  }
  if (reduced !== 1n) return undefined;
  let remainder = numerator % denominator;
  if (remainder === 0n) return String(whole);
  let fraction = "";
  while (remainder) {
    remainder *= 10n;
    fraction += (remainder / denominator).toString();
    remainder %= denominator;
  }
  return `${whole}.${fraction.replace(/0+$/, "")}`;
}

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

function hasCatalogIngredientUpdate(
  line: CanonicalRecord,
  ingredientVersions: Map<string, CanonicalRecord>,
  currentVersionByIngredientId: Map<string, string | undefined>,
  organizationId: string,
): boolean {
  const ingredientVersionId = value(line, "ingredient_version_id");
  const version = ingredientVersionId
    ? ingredientVersions.get(ingredientVersionId)
    : undefined;
  const ingredientId = version ? value(version, "ingredient_id") : undefined;
  const currentVersionId = ingredientId
    ? currentVersionByIngredientId.get(ingredientId)
    : undefined;
  const currentVersion = currentVersionId
    ? ingredientVersions.get(currentVersionId)
    : undefined;
  return Boolean(
    version?.immutable === true &&
      value(version, "organization_id") === organizationId &&
      ingredientId &&
      uuid.test(ingredientId) &&
      currentVersionId &&
      currentVersionId !== ingredientVersionId &&
      currentVersion?.immutable === true &&
      value(currentVersion, "organization_id") === organizationId &&
      value(currentVersion, "ingredient_id") === ingredientId,
  );
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
    unitRecords,
  ] = await Promise.all([
    readVisibleRecords(userId, organizationId, "event", true),
    readVisibleRecords(userId, organizationId, "event_day", true),
    readVisibleRecords(userId, organizationId, "event_meal_role", true),
    readVisibleRecords(userId, organizationId, "recipe"),
    readVisibleRecords(userId, organizationId, "recipe_version"),
    readVisibleRecords(userId, organizationId, "recipe_ingredient_line"),
    readVisibleRecords(userId, organizationId, "scheduled_recipe", true),
    readVisibleRecords(userId, organizationId, "ingredient", true),
    readVisibleRecords(userId, organizationId, "ingredient_version"),
    readVisibleRecords(userId, organizationId, "scheduled_ingredient_override"),
    readVisibleRecords(userId, organizationId, "unit_definition"),
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
      retired: record.lifecycle === "retired",
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
      retired: record.lifecycle === "retired",
      custom: record.fields.built_in_translation_key === null,
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
        scalingUnitId: (versionRecords.find((version) => version.entityId === versionId)?.fields.scaling_unit_id as string | undefined) ?? "",
        scalingUnitName: (() => {
          const unitId = versionRecords.find((version) => version.entityId === versionId)?.fields.scaling_unit_id;
          const unit = unitRecords.find((candidate) => candidate.entityId === unitId);
          return value(unit ?? ({ fields: {} } as CanonicalRecord), "custom_name") ?? value(unit ?? ({ fields: {} } as CanonicalRecord), "code") ?? "";
        })(),
        baseScalingAmount: (versionRecords.find((version) => version.entityId === versionId)?.fields.base_scaling_amount as string | undefined) ?? "",
        roundSuggestionsUp: versionRecords.find((version) => version.entityId === versionId)?.fields.round_suggestions_up === true,
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
          hasId(record) &&
          record.immutable === true &&
          value(record, "organization_id") === organizationId &&
          uuid.test(value(record, "ingredient_id") ?? ""),
      )
      .map((record) => [record.entityId, record]),
  );
  const currentIngredientVersionByIngredientId = new Map(
    ingredientRecords
      .filter(
        (record) =>
          record.lifecycle === "active" &&
          hasId(record) &&
          value(record, "organization_id") === organizationId &&
          (() => {
            const versionId = value(record, "current_version_id");
            const version = versionId ? ingredientVersions.get(versionId) : undefined;
            return Boolean(
              versionId &&
                version?.immutable === true &&
                value(version, "organization_id") === organizationId &&
                value(version, "ingredient_id") === record.entityId,
            );
          })(),
      )
      .map((record) => [record.entityId, value(record, "current_version_id")]),
  );
  const ingredients = ingredientRecords
    .filter(
      (record) =>
        record.lifecycle === "active" &&
        hasId(record) &&
        belongsToOrganization(record, organizationId),
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
  const catalogUpdateRecipeVersionIds = new Set(
    lineRecords
      .filter((line) => {
        if (!belongsToOrganization(line, organizationId)) return false;
        return Boolean(
          value(line, "recipe_version_id") &&
            hasCatalogIngredientUpdate(
              line,
              ingredientVersions,
              currentIngredientVersionByIngredientId,
              organizationId,
            ),
        );
      })
      .map((line) => value(line, "recipe_version_id"))
      .filter((id): id is string => Boolean(id)),
  );
  const currentVersionByRecipe = new Map(
    recipeRecords
      .filter((record) => belongsToOrganization(record, organizationId) && hasId(record))
      .map((record) => [record.entityId, value(record, "current_version_id")]),
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
      recipeId: value(record, "recipe_id") ?? "",
      recipeVersionId: value(record, "recipe_version_id") ?? "",
      dayId: value(record, "event_day_id"),
      roleId: value(record, "event_meal_role_id"),
      name: names.get(value(record, "recipe_id") ?? ""),
      catalogUpdateAvailable:
        catalogUpdateRecipeVersionIds.has(value(record, "recipe_version_id") ?? "") ||
        currentVersionByRecipe.get(value(record, "recipe_id") ?? "") !==
          value(record, "recipe_version_id"),
      catalogUpdateChanges: (() => {
        const current = lines.get(value(record, "recipe_version_id") ?? "") ?? [];
        const targetVersion = recipes.find((recipe) => recipe.id === value(record, "recipe_id"))?.versionId ?? "";
        const target = lines.get(targetVersion) ?? [];
        const currentById = new Map(current.map((line) => [line.id, line.quantity]));
        const targetById = new Map(target.map((line) => [line.id, line.quantity]));
        return {
          added: target.filter((line) => !currentById.has(line.id)).length,
          removed: current.filter((line) => !targetById.has(line.id)).length,
          changed: target.filter((line) => currentById.get(line.id) !== undefined && currentById.get(line.id) !== line.quantity).length,
        };
      })(),
      catalogScaleImpact: (() => {
        const current = versionRecords.find((version) => version.entityId === value(record, "recipe_version_id"));
        const target = recipes.find((recipe) => recipe.id === value(record, "recipe_id"));
        const currentUnitId = current?.fields.scaling_unit_id as string | undefined;
        const targetUnitId = target?.scalingUnitId;
        const reset = Boolean(currentUnitId && targetUnitId && currentUnitId !== targetUnitId);
        const currentUnit = unitRecords.find((unit) => unit.entityId === currentUnitId);
        const targetUnit = unitRecords.find((unit) => unit.entityId === targetUnitId);
        const currentUnitName = currentUnit
          ? value(currentUnit, "custom_name") ?? value(currentUnit, "code")
          : undefined;
        const code = value(targetUnit ?? ({ fields: {} } as CanonicalRecord), "code");
        const round = target?.versionId ? versionRecords.find((version) => version.entityId === target.versionId)?.fields.round_suggestions_up === true : false;
        const dinerCount = typeof record.fields.diner_count === "number" ? record.fields.diner_count : attendance;
        const suggestedAmount = suggestedScale(dinerCount, value(record, "consumption_percentage") ?? "", code, target?.versionId ? String(versionRecords.find((version) => version.entityId === target.versionId)?.fields.estimated_diners_per_scaling_unit ?? "") : undefined, target?.baseScalingAmount, round);
        return { currentUnitId, targetUnitId, currentUnitName, targetUnitName: target?.scalingUnitName, reset, targetBase: target?.baseScalingAmount, suggestedAmount: reset ? suggestedAmount : undefined };
      })(),
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
          uuid.test(item.recipeId) &&
          uuid.test(item.recipeVersionId) &&
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
    days: projectedDays.filter((day) => !day.retired && day.visible),
    hiddenDays: projectedDays.filter((day) => !day.retired && !day.visible),
    retiredDays: projectedDays.filter((day) => day.retired),
    roles: roles.filter((role) => !role.retired),
    retiredRoles: roles.filter((role) => role.retired),
    recipes,
    ingredients,
    scheduled,
  };
}
