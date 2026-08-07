import type { CanonicalRecord } from "./local-db";
import { readVisibleRecords } from "./visible-records";

export type CatalogRecipe = {
  id: string;
  name: string;
  description: string | null;
  scalingUnitId: string;
  baseScalingAmount: string;
};
type ValidCatalogRecipeCandidate = CatalogRecipe & { versionId: string };

export type RecipeScalingUnit = { id: string; name: string };
export type RecipeCatalogProjection = {
  recipes: CatalogRecipe[];
  scalingUnits: RecipeScalingUnit[];
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
): Promise<RecipeCatalogProjection> {
  if (!uuid.test(userId) || !uuid.test(organizationId))
    return { recipes: [], scalingUnits: [] };
  const [recipeRecords, versionRecords, unitRecords] = await Promise.all([
    readVisibleRecords(userId, organizationId, "recipe"),
    readVisibleRecords(userId, organizationId, "recipe_version"),
    readVisibleRecords(userId, organizationId, "unit_definition"),
  ]);
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
      return {
        id: record.entityId,
        versionId,
        name,
        scalingUnitId,
        baseScalingAmount,
        description,
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
    .map(({ versionId: _versionId, ...recipe }) => recipe);
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
  return { recipes, scalingUnits };
}
