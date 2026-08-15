import type { CanonicalRecord } from "./local-db";
import {
  readIngredientCatalog,
  type CatalogIngredient,
} from "./ingredient-catalog";
import { readVisibleRecords } from "./visible-records";

export type CatalogRecipe = {
  id: string;
  retired: boolean;
  versionId: string;
  name: string;
  description: string | null;
  scalingUnitId: string;
  baseScalingAmount: string;
  ingredientLines: {
    id: string;
    ingredientVersionId: string;
    baseQuantity: string;
    scalingBehavior: "proportional" | "fixed";
    includeInPortionWeight: boolean;
    note: string;
  }[];
  hasRetiredIngredientReference: boolean;
  recipeTagIds: string[];
};
type ValidCatalogRecipeCandidate = CatalogRecipe & { versionId: string };

export type RecipeScalingUnit = { id: string; name: string };
export type RecipeCatalogProjection = {
  recipes: CatalogRecipe[];
  scalingUnits: RecipeScalingUnit[];
  ingredients: CatalogIngredient[];
  tags: { id: string; name: string }[];
};

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

function text(record: CanonicalRecord, key: string) {
  const value = record.fields[key];
  return typeof value === "string" ? value : undefined;
}

/** Return only cached, organization-scoped recipe data that is safe to display or select. */
export async function readRecipeCatalog(
  userId: string,
  organizationId: string,
  includeRetired = false,
): Promise<RecipeCatalogProjection> {
  if (!uuid.test(userId) || !uuid.test(organizationId))
    return { recipes: [], scalingUnits: [], ingredients: [], tags: [] };
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
    readVisibleRecords(userId, organizationId, "recipe_tag"),
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
  const versions = new Map(
    versionRecords
      .filter(
        (record) =>
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
      const ingredientLines = version
        ? lineRecords
            .filter(
              (line) =>
                text(line, "recipe_version_id") === versionId &&
                typeof text(line, "ingredient_version_id") === "string" &&
                decimal.test(text(line, "base_quantity") ?? "") &&
                (line.fields.scaling_behavior === "proportional" ||
                  line.fields.scaling_behavior === "fixed") &&
                typeof line.fields.include_in_portion_weight === "boolean",
            )
            .map((line) => ({
              id: line.entityId,
              ingredientVersionId: text(line, "ingredient_version_id") ?? "",
              baseQuantity: text(line, "base_quantity") ?? "0",
              scalingBehavior: line.fields.scaling_behavior as
                | "proportional"
                | "fixed",
              includeInPortionWeight: line.fields
                .include_in_portion_weight as boolean,
              note: text(line, "note") ?? "",
            }))
        : [];
      const hasRetiredIngredientReference = ingredientLines.some((line) =>
        retiredIngredientVersionIds.has(line.ingredientVersionId),
      );
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
        name,
        scalingUnitId,
        baseScalingAmount,
        description,
        ingredientLines,
        hasRetiredIngredientReference,
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
          decimal.test(recipe.baseScalingAmount) &&
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
  const tags = tagRecords
    .filter(
      (record) =>
        record.lifecycle === "active" &&
        text(record, "organization_id") === organizationId,
    )
    .map((record) => ({ id: record.entityId, name: text(record, "name") }))
    .filter((tag): tag is { id: string; name: string } =>
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
          decimal.test(text(record, "mass_per_canonical_quantity") ?? ""),
      );
    })
    .map((record) => {
      const ingredientId = text(record, "ingredient_id") ?? "";
      const root = ingredientRoots.get(ingredientId);
      return {
        id: ingredientId,
        versionId: record.entityId,
        name: text(record, "name") ?? "",
        canonicalUnitName: text(record, "canonical_unit_id") ?? "",
        massPerCanonicalQuantity:
          text(record, "mass_per_canonical_quantity") ?? "",
        historical: true,
        ...(root?.lifecycle === "retired" ? { retired: true } : {}),
      } satisfies CatalogIngredient;
    });
  return {
    recipes,
    scalingUnits,
    ingredients: [...ingredientCatalog.ingredients, ...historicalIngredients],
    tags,
  };
}
