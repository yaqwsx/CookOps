import { localDb, type CanonicalRecord } from "./local-db";
import { readVisibleRecords } from "./visible-records";
import { parseArchivedDietaryTagDescriptor } from "./archive-cache";
import { decimal as parseDecimal } from "./shopping-projections";
import { divide, multiply, type Fraction } from "./exact-decimal";

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
  recipeDescription?: string;
  recipeVersionName?: string;
  scalingUnitName?: string;
  scaleMode?: "suggested" | "manual";
  hasLocalOverrides: boolean;
  detailLines: { id: string; name: string; quantity: string; unitName: string; note?: string; includeInPortionWeight?: boolean; massPerCanonicalQuantity?: string; localOverride?: true; replacementOverrideId?: string; replacementOverrideActive?: true; addedOverrideId?: string; addedOverrideActive?: true }[];
  preparedWeight: string | null;
  perDinerWeight: string | null;
  dietaryWarnings?: { exceptionName: string; tagNames: string[]; ingredientNames: string[]; tagDescriptors?: { id: string; seedKey?: string; name?: string }[] }[];
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

function scaledQuantity(quantity: string, selected: string, base: string, fixed: boolean): string | undefined {
  if (fixed) return quantity;
  const source = parseDecimal(quantity);
  const ratio = parseDecimal(selected);
  const divisor = parseDecimal(base);
  if (!source || !ratio || !divisor || divisor.value === 0n) return undefined;
  return formatTerminatingFraction(divide(multiply({ numerator: source.value, denominator: 10n ** BigInt(source.scale) }, { numerator: ratio.value, denominator: 10n ** BigInt(ratio.scale) }), { numerator: divisor.value, denominator: 10n ** BigInt(divisor.scale) }) as Fraction);
}
function formatTerminatingFraction(value: Fraction): string | undefined {
  if (value.denominator === 0n) return undefined;
  let denominator = value.denominator;
  for (const factor of [2n, 5n]) while (denominator % factor === 0n) denominator /= factor;
  if (denominator !== 1n) return undefined;
  const whole = value.numerator / value.denominator;
  let remainder = value.numerator % value.denominator, digits = "";
  while (remainder !== 0n) { remainder *= 10n; digits += (remainder / value.denominator).toString(); remainder %= value.denominator; }
  return remainder === 0n ? (digits ? `${whole}.${digits}` : `${whole}`) : undefined;
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

function archivedWarnings(value: unknown): PlannedRecipe["dietaryWarnings"] {
  if (!Array.isArray(value)) return undefined;
  const warnings = value.map((item) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return undefined;
    const fields = item as Record<string, unknown>;
    const descriptors = fields.tag_descriptors;
    const ingredientNames = fields.ingredient_names;
    const tagDescriptors = Array.isArray(descriptors) ? descriptors.map(parseArchivedDietaryTagDescriptor) : [];
    return typeof fields.exception_name === "string" && fields.exception_name.length > 0 &&
        tagDescriptors.length > 0 && tagDescriptors.every((descriptor) => descriptor !== undefined) &&
        Array.isArray(ingredientNames) && ingredientNames.length > 0 && ingredientNames.every((name) => typeof name === "string" && name.length > 0)
      ? {
          exceptionName: fields.exception_name,
          tagNames: tagDescriptors.map((descriptor) => descriptor?.name ?? descriptor?.seedKey ?? ""),
          ingredientNames: ingredientNames as string[],
          tagDescriptors: tagDescriptors as { id: string; seedKey?: string; name?: string }[],
        }
      : undefined;
  });
  return warnings.every((warning): warning is NonNullable<typeof warning> => warning !== undefined)
    ? warnings
    : undefined;
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
  const eventRecords = await readVisibleRecords(userId, organizationId, "event", true);
  const event = eventRecords.find(
    (record) =>
      record.entityId === eventId &&
      hasId(record) &&
      value(record, "organization_id") === organizationId,
  );
  const lifecycle = event?.fields.lifecycle;
  const snapshotId = event?.fields.current_archive_snapshot_id;
  const archiveRows = lifecycle === "archived" && typeof snapshotId === "string"
    ? await localDb.archiveRecords.where("[userId+organizationId+eventId+snapshotId]").equals([userId, organizationId, eventId, snapshotId]).toArray()
    : [];
  const readRecords = (entityType: string, includeRetired = false) =>
    lifecycle === "archived"
      ? Promise.resolve(archiveRows.filter((record) => record.entityType === entityType && (includeRetired || record.lifecycle === "active")))
      : readVisibleRecords(userId, organizationId, entityType, includeRetired);
  const [dayRecords, roleRecords, recipeRecords, versionRecords, lineRecords, scheduledRecords, ingredientRecords, ingredientVersionRecords, overrideRecords, unitRecords, dietaryTagRecords, exceptionRecords, exceptionTagRecords, archivedWarningRecords] = await Promise.all([
    readRecords("event_day", true), readRecords("event_meal_role", true), readRecords("recipe"), readRecords("recipe_version"), readRecords("recipe_ingredient_line"), readRecords("scheduled_recipe", true), readRecords("ingredient", true), readRecords("ingredient_version"), readRecords("scheduled_ingredient_override"), readRecords("unit_definition"), readRecords("dietary_tag", true), readRecords("event_dietary_exception", true), readRecords("event_dietary_exception_tag", true), readRecords("resolved_dietary_warning", true),
  ]);
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
      name: value(record, "custom_name") ??
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
  const versionRecordsById = new Map(versionRecords.map((record) => [record.entityId, record]));
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
    PlannedRecipe["lines"]
  >();
  const detailLines = new Map<PlannedRecipe["recipeVersionId"], PlannedRecipe["detailLines"]>();
  const rootsById = new Map(ingredientRecords.filter((record) => hasId(record) && value(record, "organization_id") === organizationId).map((record) => [record.entityId, record]));
  const ingredientUnitNames = new Map(unitRecords.filter((unit) => hasId(unit) && (value(unit, "organization_id") === organizationId || unit.fields.organization_id === null) && unit.fields.allows_ingredient_quantity === true).map((unit) => [unit.entityId, value(unit, "custom_name") ?? value(unit, "code")]).filter((entry): entry is [string, string] => Boolean(entry[1])));
  const scalingUnitNames = new Map(unitRecords.filter((unit) => hasId(unit) && (value(unit, "organization_id") === organizationId || unit.fields.organization_id === null) && unit.fields.allows_recipe_scaling === true).map((unit) => [unit.entityId, value(unit, "custom_name") ?? value(unit, "code")]).filter((entry): entry is [string, string] => Boolean(entry[1])));
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
    const version = versionId ? versionRecordsById.get(versionId) : undefined;
    const ingredientVersionId = value(record, "ingredient_version_id");
    const ingredient = ingredientVersionId ? ingredientVersions.get(ingredientVersionId) : undefined;
    const ingredientName = ingredient && value(ingredient, "name");
    const unitId = value(record, "preferred_display_unit_id") ?? (ingredient && value(ingredient, "canonical_unit_id"));
    const unitName = unitId ? ingredientUnitNames.get(unitId) : undefined;
    const ingredientRoot = ingredient && rootsById.get(value(ingredient, "ingredient_id") ?? "");
    const parsedBaseQuantity = parseDecimal(baseQuantity);
    const includeInPortionWeight = record.fields.include_in_portion_weight;
    if (hasId(record) && value(record, "organization_id") === organizationId && versionId && version?.immutable === true && hasId(version) && value(version, "organization_id") === organizationId && ingredient?.immutable === true && hasId(ingredient) && value(ingredient, "organization_id") === organizationId && ingredientRoot && ingredientName && unitName && baseQuantity && parsedBaseQuantity && parsedBaseQuantity.value >= 0n && (includeInPortionWeight === undefined || typeof includeInPortionWeight === "boolean") && lineKey && uuid.test(lineKey)) {
      const note = typeof record.fields.note === "string" && record.fields.note ? record.fields.note : undefined;
      detailLines.set(versionId, [...(detailLines.get(versionId) ?? []), { id: lineKey, name: ingredientName, quantity: baseQuantity, unitName, ...(note ? { note } : {}), includeInPortionWeight: includeInPortionWeight !== false, ...(value(ingredient, "mass_per_canonical_quantity") ? { massPerCanonicalQuantity: value(ingredient, "mass_per_canonical_quantity") } : {}) }]);
    }
  }

  const dietaryTagNames = new Map(
    dietaryTagRecords
      .filter((tag) => hasId(tag) && value(tag, "organization_id") === organizationId)
      .map((tag) => [tag.entityId, value(tag, "name") ?? value(tag, "seed_key")])
      .filter((entry): entry is [string, string] => Boolean(entry[1])),
  );
  const warningExceptions = lifecycle === "active"
    ? exceptionRecords
      .filter((exception) => exception.lifecycle === "active" && hasId(exception) && value(exception, "organization_id") === organizationId && value(exception, "event_id") === eventId)
      .map((exception) => {
        const selected = Array.isArray(exception.fields.tag_ids)
          ? exception.fields.tag_ids.filter((id): id is string => typeof id === "string")
          : exceptionTagRecords
            .filter((association) => association.lifecycle === "active" && hasId(association) && value(association, "organization_id") === organizationId && value(association, "exception_id") === exception.entityId)
            .map((association) => value(association, "dietary_tag_id"))
            .filter((id): id is string => Boolean(id));
        const tags = [...new Set(selected)].filter((id) => dietaryTagNames.has(id));
        return { id: exception.entityId, name: value(exception, "name"), tags };
      })
      .filter((exception): exception is { id: string; name: string; tags: string[] } => Boolean(exception.name && exception.tags.length))
      .sort((left, right) => left.name.localeCompare(right.name) || left.id.localeCompare(right.id))
    : [];
  const dietaryWarningsByScheduled = new Map<string, PlannedRecipe["dietaryWarnings"]>();
  if (lifecycle === "archived") {
    for (const record of archivedWarningRecords) {
      const warnings = record.fields.warnings;
      const mapped = archivedWarnings(warnings);
      if (typeof record.fields.scheduled_recipe_id === "string" && mapped)
        dietaryWarningsByScheduled.set(record.fields.scheduled_recipe_id, mapped);
    }
  }
  if (warningExceptions.length) {
    const nonzero = (quantity: unknown) => {
      const parsed = parseDecimal(quantity);
      return Boolean(parsed && parsed.value >= 0n && parsed.value !== 0n);
    };
    for (const item of scheduledRecords) {
      if (item.lifecycle !== "active" || value(item, "organization_id") !== organizationId || value(item, "event_id") !== eventId) continue;
      const recipeVersionId = value(item, "recipe_version_id");
      const version = recipeVersionId ? versionRecords.find((candidate) => candidate.entityId === recipeVersionId) : undefined;
      const selected = parseDecimal(value(item, "selected_scale_amount"));
      const base = version && parseDecimal(version.fields.base_scaling_amount);
      const resolved = new Set<string>();
      for (const line of lineRecords.filter((candidate) => value(candidate, "recipe_version_id") === recipeVersionId)) {
        const replacement = overrideRecords.find((candidate) => candidate.lifecycle === "active" && value(candidate, "organization_id") === organizationId && value(candidate, "event_id") === eventId && value(candidate, "scheduled_recipe_id") === item.entityId && candidate.fields.override_kind === "replace" && value(candidate, "target_line_key") === value(line, "line_key"));
        const quantity = replacement ? value(replacement, "quantity") : value(line, "base_quantity");
        const ingredientVersionId = replacement ? value(replacement, "ingredient_version_id") : value(line, "ingredient_version_id");
        const parsedQuantity = parseDecimal(quantity);
        const scaled = value(line, "scaling_behavior") === "fixed"
          ? Boolean(parsedQuantity && parsedQuantity.value !== 0n)
          : Boolean(parsedQuantity && selected && base && selected.value !== 0n && base.value !== 0n && parsedQuantity.value !== 0n);
        if (scaled && ingredientVersionId && uuid.test(ingredientVersionId)) resolved.add(ingredientVersionId);
      }
      for (const override of overrideRecords.filter((candidate) => candidate.lifecycle === "active" && value(candidate, "organization_id") === organizationId && value(candidate, "event_id") === eventId && value(candidate, "scheduled_recipe_id") === item.entityId && candidate.fields.override_kind === "add")) {
        const ingredientVersionId = value(override, "ingredient_version_id");
        if (ingredientVersionId && uuid.test(ingredientVersionId) && nonzero(override.fields.quantity)) resolved.add(ingredientVersionId);
      }
      const warning = warningExceptions.map((exception) => {
        const tagIds = new Set(exception.tags);
        const matchingTagIds = new Set<string>();
        const ingredientNames = [...resolved]
          .map((id) => ingredientVersions.get(id))
          .filter((ingredient): ingredient is CanonicalRecord => Boolean(ingredient && ingredient.immutable === true && value(ingredient, "organization_id") === organizationId))
          .map((ingredient) => {
            const tags = Array.isArray(ingredient.fields.dietary_tag_ids) ? ingredient.fields.dietary_tag_ids.filter((id): id is string => typeof id === "string" && dietaryTagNames.has(id)) : [];
            const matching = tags.filter((id) => tagIds.has(id));
            matching.forEach((id) => { matchingTagIds.add(id); });
            return matching.length ? value(ingredient, "name") : undefined;
          })
          .filter((name): name is string => Boolean(name));
        return { exceptionName: exception.name, tagNames: [...matchingTagIds].map((id) => dietaryTagNames.get(id) as string).sort((a, b) => a.localeCompare(b)), tagDescriptors: [...matchingTagIds].map((id) => { const tag = dietaryTagRecords.find((record) => record.entityId === id); return { id, seedKey: tag ? value(tag, "seed_key") : undefined, name: tag ? value(tag, "name") : undefined }; }), ingredientNames: [...new Set(ingredientNames)].sort((a, b) => a.localeCompare(b)) };
      }).filter((warning) => warning.ingredientNames.length);
      if (warning.length) dietaryWarningsByScheduled.set(item.entityId, warning);
    }
  }
  const localAddedIngredients = new Map<
    string,
    { id: string; name: string; quantity: string; unitName?: string; includeInPortionWeight: boolean; massPerCanonicalQuantity?: string; localOverride?: true; addedOverrideId?: string; addedOverrideActive?: true }[]
  >();
  for (const record of overrideRecords) {
    const scheduledRecipeId = value(record, "scheduled_recipe_id");
    const quantity = value(record, "quantity");
    const version = ingredientVersions.get(
      value(record, "ingredient_version_id") ?? "",
    );
    const name = version && value(version, "name");
    const ingredientRoot = version && rootsById.get(value(version, "ingredient_id") ?? "");
    const parsedQuantity = parseDecimal(quantity);
    const includeInPortionWeight = record.fields.include_in_portion_weight;
    if (
      hasId(record) &&
      record.lifecycle === "active" &&
      value(record, "organization_id") === organizationId &&
      value(record, "event_id") === eventId &&
      record.fields.override_kind === "add" &&
      scheduledRecipeId &&
      quantity &&
      parsedQuantity && parsedQuantity.value >= 0n &&
      name && ingredientRoot && ingredientUnitNames.has(value(version, "canonical_unit_id") ?? "") &&
      (includeInPortionWeight === undefined || typeof includeInPortionWeight === "boolean")
    )
      localAddedIngredients.set(scheduledRecipeId, [
        ...(localAddedIngredients.get(scheduledRecipeId) ?? []),
        { id: record.entityId, name, quantity, includeInPortionWeight: includeInPortionWeight !== false, unitName: ingredientUnitNames.get(value(version, "canonical_unit_id") ?? "") as string, massPerCanonicalQuantity: value(version, "mass_per_canonical_quantity"), localOverride: true, addedOverrideId: record.entityId, addedOverrideActive: true },
      ]);
  }
  const scheduled = (scheduledRecords
    .filter(
      (record) =>
        hasId(record) &&
        belongsToOrganization(record, organizationId) &&
        value(record, "event_id") === eventId,
    )
    .map((record) => {
      const pinnedVersion = versionRecordsById.get(value(record, "recipe_version_id") ?? "");
      const recipeRoot = recipeRecords.find((candidate) => candidate.entityId === value(record, "recipe_id"));
      const pinnedBase = pinnedVersion && value(pinnedVersion, "base_scaling_amount");
      const parsedPinnedBase = parseDecimal(pinnedBase);
      const pinnedVersionValid = pinnedVersion?.immutable === true && hasId(pinnedVersion) && recipeRoot !== undefined && hasId(recipeRoot) && value(recipeRoot, "organization_id") === organizationId && value(pinnedVersion, "organization_id") === organizationId && value(pinnedVersion, "recipe_id") === value(record, "recipe_id") && typeof value(pinnedVersion, "name") === "string" && Boolean(value(pinnedVersion, "name")) && Boolean(parsedPinnedBase && parsedPinnedBase.value > 0n) && scalingUnitNames.has(value(pinnedVersion, "scaling_unit_id") ?? "");
      const pinnedUnitName = pinnedVersionValid ? scalingUnitNames.get(value(pinnedVersion, "scaling_unit_id") ?? "") : undefined;
      const resolvedDetailLines = pinnedVersionValid ? (detailLines.get(value(record, "recipe_version_id") ?? "") ?? []).flatMap((line) => {
        const source = lineRecords.find((candidate) => value(candidate, "line_key") === line.id && value(candidate, "recipe_version_id") === value(record, "recipe_version_id"));
        const replacement = source && overrideRecords.find((candidate) => candidate.lifecycle === "active" && value(candidate, "organization_id") === organizationId && value(candidate, "event_id") === eventId && value(candidate, "scheduled_recipe_id") === record.entityId && candidate.fields.override_kind === "replace" && value(candidate, "target_line_key") === line.id);
        const quantity = replacement ? value(replacement, "quantity") : line.quantity;
        const parsedQuantity = quantity ? parseDecimal(quantity) : undefined;
        const scaled = quantity && source ? scaledQuantity(quantity, value(record, "selected_scale_amount") ?? "", value(pinnedVersion, "base_scaling_amount") ?? "", value(source, "scaling_behavior") === "fixed") : undefined;
        const replacementVersionId = replacement ? value(replacement, "ingredient_version_id") : undefined;
        const resolved = replacementVersionId ? ingredientVersions.get(replacementVersionId) : undefined;
        const replacementValid = !replacementVersionId || Boolean(
          resolved &&
          uuid.test(replacementVersionId) &&
          rootsById.get(value(resolved, "ingredient_id") ?? "") &&
          value(resolved, "name") &&
          ingredientUnitNames.get(value(resolved, "canonical_unit_id") ?? ""),
        );
        const resolvedMetadata = replacementVersionId && resolved && replacementValid ? { name: value(resolved, "name"), unitName: ingredientUnitNames.get(value(resolved, "canonical_unit_id") ?? ""), massPerCanonicalQuantity: value(resolved, "mass_per_canonical_quantity") } : undefined;
        const name = resolvedMetadata?.name ?? line.name;
        const unitName = resolvedMetadata?.unitName ?? line.unitName;
        return parsedQuantity && parsedQuantity.value >= 0n && scaled && parseDecimal(scaled)?.value !== 0n && name && unitName && replacementValid ? [{ ...line, quantity: scaled, name, unitName, ...(replacement ? { localOverride: true as const, replacementOverrideId: replacement.entityId, replacementOverrideActive: true as const } : {}), ...(resolvedMetadata ? { massPerCanonicalQuantity: resolvedMetadata.massPerCanonicalQuantity } : {}) }] : [];
      }).concat((localAddedIngredients.get(record.entityId) ?? []).flatMap((ingredient) => parseDecimal(ingredient.quantity)?.value !== 0n ? [ingredient] : []) as typeof detailLines extends Map<string, infer T> ? T : never) : [];
      const toFraction = (value: string): Fraction | undefined => { const parsed = parseDecimal(value); return parsed ? { numerator: parsed.value, denominator: 10n ** BigInt(parsed.scale) } : undefined; };
      const weightValues = resolvedDetailLines.filter((line) => line.includeInPortionWeight).map((line) => { const quantity = toFraction(line.quantity), mass = toFraction(line.massPerCanonicalQuantity ?? ""); return quantity && mass && mass.numerator > 0n ? multiply(quantity, mass) : undefined; });
      const weightTotal = weightValues.length === resolvedDetailLines.filter((line) => line.includeInPortionWeight).length && weightValues.every(Boolean) && weightValues.length ? weightValues.reduce<Fraction>((sum, value) => ({ numerator: sum.numerator * (value as Fraction).denominator + (value as Fraction).numerator * sum.denominator, denominator: sum.denominator * (value as Fraction).denominator }), { numerator: 0n, denominator: 1n }) : undefined;
      const preparedWeight = weightTotal ? formatTerminatingFraction(weightTotal) ?? null : null;
      const perDinerWeight = preparedWeight && typeof record.fields.diner_count === "number" && record.fields.diner_count > 0 ? formatTerminatingFraction(divide(weightTotal as Fraction, { numerator: BigInt(record.fields.diner_count), denominator: 1n }) as Fraction) ?? null : null;
      return ({
      id: record.entityId,
      recipeId: value(record, "recipe_id") ?? "",
      recipeVersionId: value(record, "recipe_version_id") ?? "",
      dayId: value(record, "event_day_id"),
      roleId: value(record, "event_meal_role_id"),
      name: pinnedVersionValid ? value(pinnedVersion, "name") ?? "" : "",
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
      recipeDescription: pinnedVersionValid && typeof pinnedVersion?.fields.description === "string" ? pinnedVersion.fields.description : undefined,
      recipeVersionName: pinnedVersionValid ? value(pinnedVersion, "name") : undefined,
      scalingUnitName: pinnedUnitName,
      scaleMode: record.fields.scale_mode === "manual" || record.fields.scale_mode === "suggested" ? record.fields.scale_mode : undefined,
      hasLocalOverrides: overrideRecords.some((override) => override.lifecycle === "active" && value(override, "organization_id") === organizationId && value(override, "event_id") === eventId && value(override, "scheduled_recipe_id") === record.entityId),
      detailLines: resolvedDetailLines,
      preparedWeight,
      perDinerWeight,
      dietaryWarnings: dietaryWarningsByScheduled.get(record.entityId) ?? [],
      dinerCount: record.fields.diner_count,
      consumptionPercentage: value(record, "consumption_percentage"),
      selectedScaleAmount: value(record, "selected_scale_amount"),
      position: value(record, "position_key"),
      retired: record.lifecycle === "retired",
      });
    })
    .filter((item) =>
      Boolean(
        item.dayId &&
          item.roleId &&
          item.position &&
          uuid.test(item.dayId) &&
          uuid.test(item.roleId) &&
          uuid.test(item.recipeId) &&
          uuid.test(item.recipeVersionId) &&
          Number.isSafeInteger(item.dinerCount) &&
          item.consumptionPercentage !== undefined &&
          item.selectedScaleAmount !== undefined,
      ),
    ) as PlannedRecipe[]).sort(
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
