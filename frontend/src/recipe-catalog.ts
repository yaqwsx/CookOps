import type { CanonicalRecord } from "./local-db";
import {
  readIngredientCatalog,
  type CatalogIngredient,
  type IngredientUnit,
} from "./ingredient-catalog";
import { readVisibleRecords } from "./visible-records";
import { add, decimal, divide, money, multiply, zeroFraction, type Fraction } from "./event-cost-projections";

export type CatalogRecipe = {
  id: string;
  retired: boolean;
  versionId: string;
  versionHistory: RecipeVersionHistoryEntry[];
  name: string;
  description: string | null;
  estimatedDinersPerScalingUnit?: string;
  roundSuggestionsUp?: boolean;
  scalingUnitId: string;
  baseScalingAmount: string;
  ingredientLines: {
    id: string;
    lineKey?: string;
    positionKey?: string;
    preferredDisplayUnitId?: string;
    ingredientVersionId: string;
    baseQuantity: string;
    scalingBehavior: "proportional" | "fixed";
    includeInPortionWeight: boolean;
    note: string;
  }[];
  hasRetiredIngredientReference: boolean;
  catalogUpdateAvailable: boolean;
  recipeTagIds: string[];
};
export type RecipeVersionHistoryEntry = { id: string; publishedAt?: string; publishedByUserId?: string; name?: string };
type ValidCatalogRecipeCandidate = CatalogRecipe & { versionId: string };

export type RecipeScalingUnit = { id: string; name: string };
export type RecipeCatalogProjection = {
  recipes: CatalogRecipe[];
  scalingUnits: RecipeScalingUnit[];
  ingredients: CatalogIngredient[];
  units: IngredientUnit[];
  tags: { id: string; name: string; retired?: boolean }[];
  costs: Record<string, RecipeCostProjection>;
};
export type RecipeCostUnit = { id: string; name?: string; dimension: string; baseUnitFactor?: string };
export type RecipeCostProjection = { currency: string; total: string | null; missingCount: number };

export type RecipeCatalogUpdateLine = {
  lineId: string;
  lineKey?: string;
  positionKey?: string;
  oldIngredient: CatalogIngredient | undefined;
  newIngredient: CatalogIngredient | undefined;
  oldQuantity: string;
  newQuantity: string | null;
  oldUnitName: string;
  newUnitName: string;
  compatible: boolean;
  reason?: "missing" | "incompatible";
};

export type RecipeCatalogUpdateProjection = {
  lines: RecipeCatalogUpdateLine[];
  blocked: boolean;
};

function fractionText(value: Fraction): string | undefined {
  const sign = value.numerator < 0n ? "-" : "";
  const numerator = value.numerator < 0n ? -value.numerator : value.numerator;
  const integer = numerator / value.denominator;
  let remainder = numerator % value.denominator;
  if (remainder === 0n) return `${sign}${integer}`;
  let digits = "";
  for (let index = 0; remainder !== 0n && index < 12; index += 1) {
    remainder *= 10n;
    digits += (remainder / value.denominator).toString();
    remainder %= value.denominator;
  }
  if (remainder !== 0n) return undefined;
  return `${sign}${integer}.${digits.replace(/0+$/, "") || "0"}`;
}

function conversionFactor(
  sourceUnitId: string | undefined,
  targetUnitId: string | undefined,
  byId: Map<string, RecipeCostUnit>,
): Fraction | undefined {
  if (!sourceUnitId || !targetUnitId) return undefined;
  const sourceUnit = byId.get(sourceUnitId);
  const targetUnit = byId.get(targetUnitId);
  if (!sourceUnit || !targetUnit || sourceUnit.dimension !== targetUnit.dimension)
    return undefined;
  if (sourceUnit.id === targetUnit.id) return decimal("1");
  const sourceFactor = sourceUnit.baseUnitFactor && decimal(sourceUnit.baseUnitFactor);
  const targetFactor = targetUnit.baseUnitFactor && decimal(targetUnit.baseUnitFactor);
  return sourceFactor && targetFactor ? divide(sourceFactor, targetFactor) : undefined;
}

/** Preview one atomic recipe version update; incompatible cached metadata blocks confirmation. */
export function projectRecipeCatalogUpdate(
  recipe: CatalogRecipe,
  ingredients: CatalogIngredient[],
  units: RecipeCostUnit[],
): RecipeCatalogUpdateProjection {
  const byVersion = new Map(ingredients.map((ingredient) => [ingredient.versionId, ingredient]));
  const byUnit = new Map(units.map((unit) => [unit.id, unit]));
  const currentByIngredient = new Map(
    ingredients.filter((ingredient) => !ingredient.historical).map((ingredient) => [ingredient.id, ingredient]),
  );
  const lines = recipe.ingredientLines.flatMap<RecipeCatalogUpdateLine>((line) => {
    const oldIngredient = byVersion.get(line.ingredientVersionId);
    const newIngredient = oldIngredient ? currentByIngredient.get(oldIngredient.id) : undefined;
    if (oldIngredient && newIngredient && oldIngredient.versionId === newIngredient.versionId) return [];
    if (!oldIngredient || !newIngredient) {
      return [{
        lineId: line.id,
        ...(line.lineKey ? { lineKey: line.lineKey } : {}),
        ...(line.positionKey ? { positionKey: line.positionKey } : {}),
        oldIngredient,
        newIngredient,
        oldQuantity: line.baseQuantity,
        newQuantity: null,
        oldUnitName: oldIngredient?.canonicalUnitName ?? "—",
        newUnitName: newIngredient?.canonicalUnitName ?? "—",
        compatible: false,
        reason: "missing" as const,
      }];
    }
    const factor = conversionFactor(oldIngredient.canonicalUnitId, newIngredient.canonicalUnitId, byUnit);
    const quantity = decimal(line.baseQuantity);
    const convertedQuantity = factor && quantity ? fractionText(multiply(quantity, factor)) : undefined;
    const metadataComplete = uuid.test(line.lineKey ?? "") && positionPattern.test(line.positionKey ?? "");
    const sourceUnit = oldIngredient.canonicalUnitId ? byUnit.get(oldIngredient.canonicalUnitId) : undefined;
    const targetUnit = newIngredient.canonicalUnitId ? byUnit.get(newIngredient.canonicalUnitId) : undefined;
    return [{
      lineId: line.id,
      ...(line.lineKey ? { lineKey: line.lineKey } : {}),
      ...(line.positionKey ? { positionKey: line.positionKey } : {}),
      oldIngredient,
      newIngredient,
      oldQuantity: line.baseQuantity,
      newQuantity: convertedQuantity && metadataComplete ? convertedQuantity : null,
      oldUnitName: sourceUnit?.name ?? oldIngredient.canonicalUnitName,
      newUnitName: targetUnit?.name ?? newIngredient.canonicalUnitName,
      compatible: Boolean(factor && quantity && convertedQuantity && metadataComplete),
      ...(factor && quantity && convertedQuantity && metadataComplete ? {} : { reason: oldIngredient.canonicalUnitId && newIngredient.canonicalUnitId && metadataComplete ? "incompatible" as const : "missing" as const }),
    }];
  });
  return { lines, blocked: lines.some((line) => !line.compatible) };
}

export function projectRecipeCost(recipe: CatalogRecipe, ingredients: CatalogIngredient[], units: RecipeCostUnit[], currency: string): RecipeCostProjection {
  const byVersion = new Map(ingredients.map((ingredient) => [ingredient.versionId, ingredient]));
  const byUnit = new Map(units.map((unit) => [unit.id, unit]));
  let total = zeroFraction;
  let missingCount = 0;
  for (const line of recipe.ingredientLines) {
    const ingredient = byVersion.get(line.ingredientVersionId);
    const price = ingredient?.currentPrice;
    const quantity = decimal(line.baseQuantity);
    const amount = price && decimal(price.amount);
    const pricedQuantity = price && decimal(price.quantity);
    const factor = conversionFactor(ingredient?.canonicalUnitId, price?.unitId, byUnit);
    const cost = quantity && amount && pricedQuantity && price?.currency === currency && factor
      ? divide(multiply(amount, multiply(quantity, factor)), pricedQuantity)
      : undefined;
    if (cost) total = add(total, cost);
    else missingCount++;
  }
  return { currency, total: missingCount ? null : money(total), missingCount };
}

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimalPattern = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const positionPattern = /^[0-9A-Za-z]+$/;

function text(record: CanonicalRecord, key: string) {
  const value = record.fields[key];
  return typeof value === "string" ? value : undefined;
}

export function projectRecipeVersionHistory(recipeId: string, organizationId: string, records: CanonicalRecord[]): RecipeVersionHistoryEntry[] {
  return records.filter((record) => record.immutable === true && uuid.test(record.entityId) && text(record, "id") === record.entityId && text(record, "organization_id") === organizationId && text(record, "recipe_id") === recipeId).map((record) => {
    const publishedAt = text(record, "published_at");
    const publishedByUserId = text(record, "published_by_user_id");
    const name = text(record, "name");
    return { id: record.entityId, ...(publishedAt && Number.isFinite(Date.parse(publishedAt)) ? { publishedAt } : {}), ...(publishedByUserId && uuid.test(publishedByUserId) ? { publishedByUserId } : {}), ...(name ? { name } : {}) };
  }).sort((left, right) => (left.publishedAt ? Date.parse(left.publishedAt) : Number.POSITIVE_INFINITY) - (right.publishedAt ? Date.parse(right.publishedAt) : Number.POSITIVE_INFINITY) || left.id.localeCompare(right.id));
}

/** Return only cached, organization-scoped recipe data that is safe to display or select. */
export async function readRecipeCatalog(
  userId: string,
  organizationId: string,
  includeRetired = false,
): Promise<RecipeCatalogProjection> {
  if (!uuid.test(userId) || !uuid.test(organizationId))
    return { recipes: [], scalingUnits: [], ingredients: [], units: [], tags: [], costs: {} };
  const [
    recipeRecords,
    versionRecords,
    unitRecords,
    tagRecords,
    ingredientCatalog,
    lineRecords,
    versionTagRecords,
    ingredientRootRecords,
    ingredientVersionRecords,
  ] = await Promise.all([
    readVisibleRecords(userId, organizationId, "recipe", includeRetired),
    readVisibleRecords(userId, organizationId, "recipe_version"),
    readVisibleRecords(userId, organizationId, "unit_definition"),
    readVisibleRecords(userId, organizationId, "recipe_tag", true),
    readIngredientCatalog(userId, organizationId, true),
    readVisibleRecords(userId, organizationId, "recipe_ingredient_line"),
    readVisibleRecords(userId, organizationId, "recipe_version_tag"),
    readVisibleRecords(userId, organizationId, "ingredient", true),
    readVisibleRecords(userId, organizationId, "ingredient_version"),
  ]);
  const retiredIngredientIds = new Set(
    ingredientRootRecords
      .filter(
        (record) =>
          uuid.test(record.entityId) &&
          text(record, "id") === record.entityId &&
          text(record, "organization_id") === organizationId &&
          record.lifecycle === "retired",
      )
      .map((record) => record.entityId),
  );
  const retiredIngredientVersionIds = new Set(
    ingredientVersionRecords
      .filter(
        (record) =>
          record.immutable === true &&
          uuid.test(record.entityId) &&
          text(record, "id") === record.entityId &&
          text(record, "organization_id") === organizationId &&
          retiredIngredientIds.has(text(record, "ingredient_id") ?? ""),
      )
      .map((record) => record.entityId),
  );
  const currentIngredientVersionByIngredientId = new Map(
    ingredientRootRecords
      .filter(
        (record) =>
          record.lifecycle === "active" &&
          uuid.test(record.entityId) &&
          text(record, "id") === record.entityId &&
          text(record, "organization_id") === organizationId,
      )
      .map((record) => [record.entityId, text(record, "current_version_id")]),
  );
  const ingredientVersionIngredientIds = new Map(
    ingredientVersionRecords
      .filter(
        (record) =>
          record.immutable === true &&
          uuid.test(record.entityId) &&
          text(record, "id") === record.entityId &&
          text(record, "organization_id") === organizationId &&
          uuid.test(text(record, "ingredient_id") ?? ""),
      )
      .map((record) => [record.entityId, text(record, "ingredient_id") as string]),
  );
  const versions = new Map(
    versionRecords
      .filter(
        (record) =>
          record.immutable === true &&
          uuid.test(record.entityId) &&
          text(record, "id") === record.entityId &&
          text(record, "organization_id") === organizationId,
      )
      .map((record) => [record.entityId, record]),
  );
  const recipes = recipeRecords
    .filter(
      (record) =>
        uuid.test(record.entityId) &&
        text(record, "id") === record.entityId &&
        text(record, "organization_id") === organizationId,
    )
    .map((record) => {
      const versionId =
        text(record, "current_version_id") ?? text(record, "recipe_version_id");
      const candidateVersion = versionId ? versions.get(versionId) : undefined;
      const version =
        candidateVersion &&
        text(candidateVersion, "recipe_id") === record.entityId
          ? candidateVersion
          : undefined;
      const name = text(version ?? record, "name");
      const scalingUnitId = text(version ?? record, "scaling_unit_id");
      const baseScalingAmount = text(version ?? record, "base_scaling_amount");
      const description = (version ?? record).fields.description;
      const estimatedDinersPerScalingUnit = text(version ?? record, "estimated_diners_per_scaling_unit");
      const roundSuggestionsUp = (version ?? record).fields.round_suggestions_up;
      const ingredientLines = version
        ? lineRecords
            .filter(
                (line) =>
                  (text(line, "organization_id") === undefined ||
                    text(line, "organization_id") === organizationId) &&
                  text(line, "recipe_version_id") === versionId &&
                  typeof text(line, "ingredient_version_id") === "string" &&
                  decimalPattern.test(text(line, "base_quantity") ?? "") &&
                  (line.fields.scaling_behavior === "proportional" ||
                    line.fields.scaling_behavior === "fixed") &&
                  typeof line.fields.include_in_portion_weight === "boolean",
            )
            .map((line) => ({
              id: line.entityId,
              ...(text(line, "line_key") ? { lineKey: text(line, "line_key") } : {}),
              ...(text(line, "position_key") ? { positionKey: text(line, "position_key") } : {}),
              ingredientVersionId: text(line, "ingredient_version_id") ?? "",
              baseQuantity: text(line, "base_quantity") ?? "0",
              scalingBehavior: line.fields.scaling_behavior as
                | "proportional"
                | "fixed",
              includeInPortionWeight: line.fields
                .include_in_portion_weight as boolean,
              note: text(line, "note") ?? "",
              ...(text(line, "preferred_display_unit_id") ? { preferredDisplayUnitId: text(line, "preferred_display_unit_id") } : {}),
            }))
        : [];
      const hasRetiredIngredientReference = ingredientLines.some((line) =>
        retiredIngredientVersionIds.has(line.ingredientVersionId),
      );
      const catalogUpdateAvailable = ingredientLines.some((line) => {
        const ingredientId = ingredientVersionIngredientIds.get(line.ingredientVersionId);
        const currentVersionId = ingredientId
          ? currentIngredientVersionByIngredientId.get(ingredientId)
          : undefined;
        return Boolean(
          ingredientId &&
            currentVersionId &&
            currentVersionId !== line.ingredientVersionId &&
            ingredientVersionIngredientIds.get(currentVersionId) === ingredientId,
        );
      });
      const recipeTagIds = version
        ? versionTagRecords
            .filter((tag) => text(tag, "recipe_version_id") === versionId)
            .map((tag) => text(tag, "recipe_tag_id"))
            .filter((tag): tag is string => Boolean(tag && uuid.test(tag)))
        : [];
      return {
        id: record.entityId,
        retired: record.lifecycle === "retired",
        versionId,
        versionHistory: projectRecipeVersionHistory(record.entityId, organizationId, versionRecords),
        name,
        scalingUnitId,
        baseScalingAmount,
        description,
        ...(estimatedDinersPerScalingUnit !== undefined
          ? { estimatedDinersPerScalingUnit }
          : {}),
        ...(typeof roundSuggestionsUp === "boolean" ? { roundSuggestionsUp } : {}),
        ingredientLines,
        hasRetiredIngredientReference,
        catalogUpdateAvailable,
        recipeTagIds,
      };
    })
    .filter((recipe): recipe is ValidCatalogRecipeCandidate =>
      Boolean(
        recipe.versionId &&
          recipe.name &&
          recipe.scalingUnitId &&
          recipe.baseScalingAmount &&
          uuid.test(recipe.versionId) &&
          uuid.test(recipe.scalingUnitId) &&
          decimalPattern.test(recipe.baseScalingAmount) &&
          (recipe.description === null ||
            typeof recipe.description === "string"),
      ),
    )
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) || left.id.localeCompare(right.id),
    )
    .map((recipe) => recipe);
  const scalingUnits = unitRecords
    .filter((record) => {
      const owner = record.fields.organization_id;
      return (
        uuid.test(record.entityId) &&
        text(record, "id") === record.entityId &&
        (owner === null || owner === organizationId) &&
        record.fields.allows_recipe_scaling === true
      );
    })
    .map((record) => ({
      id: record.entityId,
      name: text(record, "custom_name") ?? text(record, "code"),
    }))
    .filter((unit): unit is RecipeScalingUnit => Boolean(unit.name))
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) || left.id.localeCompare(right.id),
    );
  const referencedTagIds = new Set(
    recipes.flatMap((recipe) => recipe.recipeTagIds),
  );
  const tags = tagRecords
    .filter(
      (record) =>
        (record.lifecycle === "active" ||
          referencedTagIds.has(record.entityId)) &&
        text(record, "organization_id") === organizationId,
    )
    .map((record) => ({ id: record.entityId, name: text(record, "name"), ...(record.lifecycle === "retired" ? { retired: true } : {}) }))
    .filter((tag): tag is { id: string; name: string; retired?: boolean } =>
      Boolean(uuid.test(tag.id) && tag.name),
    );
  const currentIngredientVersionIds = new Set(
    ingredientCatalog.ingredients.map((ingredient) => ingredient.versionId),
  );
  const ingredientRoots = new Map(
    ingredientRootRecords
      .filter(
        (record) =>
          uuid.test(record.entityId) &&
          text(record, "id") === record.entityId &&
          text(record, "organization_id") === organizationId,
      )
      .map((record) => [record.entityId, record]),
  );
  const referencedVersionIds = new Set(
    recipes.flatMap((recipe) =>
      recipe.ingredientLines.map((line) => line.ingredientVersionId),
    ),
  );
  const historicalIngredients = ingredientVersionRecords
    .filter((record) => {
      const ingredientId = text(record, "ingredient_id");
      return Boolean(
        record.immutable === true &&
          uuid.test(record.entityId) &&
          text(record, "id") === record.entityId &&
          text(record, "organization_id") === organizationId &&
          ingredientId &&
          ingredientRoots.has(ingredientId) &&
          referencedVersionIds.has(record.entityId) &&
          !currentIngredientVersionIds.has(record.entityId) &&
          text(record, "name") &&
          text(record, "canonical_unit_id") &&
          decimalPattern.test(text(record, "mass_per_canonical_quantity") ?? ""),
      );
    })
    .map((record) => {
      const ingredientId = text(record, "ingredient_id") ?? "";
      const root = ingredientRoots.get(ingredientId);
      const currentIngredient = ingredientCatalog.ingredients.find((item) => item.id === ingredientId);
      return {
        id: ingredientId,
        versionId: record.entityId,
        name: text(record, "name") ?? "",
        canonicalUnitName: text(record, "canonical_unit_id") ?? "",
        canonicalUnitId: text(record, "canonical_unit_id") ?? "",
        massPerCanonicalQuantity:
          text(record, "mass_per_canonical_quantity") ?? "",
        historical: true,
        ...(currentIngredient?.currentPrice ? { currentPrice: currentIngredient.currentPrice } : {}),
        ...(root?.lifecycle === "retired" ? { retired: true } : {}),
      } satisfies CatalogIngredient;
    });
  const ingredients = [...ingredientCatalog.ingredients, ...historicalIngredients];
  const costs = Object.fromEntries(recipes.map((recipe) => [
    recipe.id,
    projectRecipeCost(recipe, ingredients, ingredientCatalog.units, ingredientCatalog.organizationDefaultCurrency),
  ]));
  return {
    recipes,
    scalingUnits,
    ingredients,
    units: ingredientCatalog.units,
    tags,
    costs,
  };
}
