import { readOrCreateBrowserInstallationId } from "../local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const zeroUuid = /^0{8}-0{4}-0{4}-0{4}-0{12}$/;

export class RecipeCopyRequestError extends Error {
  constructor(readonly status: number) {
    super("Recipe copy request failed.");
  }
}

export type RecipeCopyInput = {
  sourceOrganizationId: string;
  sourceRecipeId: string;
  sourceCurrentRecipeVersionId: string;
  destinationRecipeId: string;
  destinationRecipeVersionId: string;
  ingredientVersionMappings: Record<string, string>;
  recipeTagMappings: Record<string, string>;
  scalingUnitMappings: Record<string, string>;
  preferredDisplayUnitMappings: Record<string, string>;
  mutationId: string;
  clientWallTime: string;
  logicalOperationId?: string;
};

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function id(value: unknown): string {
  if (typeof value !== "string" || !uuid.test(value) || zeroUuid.test(value))
    throw new Error("Invalid recipe copy response.");
  return value;
}
function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0)
    throw new Error("Invalid recipe copy response.");
  return value;
}

function parseResult(value: unknown) {
  if (!object(value) || typeof value.replayed !== "boolean")
    throw new Error("Invalid recipe copy response.");
  const result = {
    mutationId: id(value.mutation_id),
    sourceOrganizationId: id(value.source_organization_id),
    destinationOrganizationId: id(value.destination_organization_id),
    sourceRecipeId: id(value.source_recipe_id),
    destinationRecipeId: id(value.destination_recipe_id),
    sourceRecipeVersionId: id(value.source_recipe_version_id),
    destinationRecipeVersionId: id(value.destination_recipe_version_id),
    firstChangeSequence: number(value.first_change_sequence),
    lastChangeSequence: number(value.last_change_sequence),
    replayed: value.replayed,
  };
  if (result.lastChangeSequence < result.firstChangeSequence)
    throw new Error("Invalid recipe copy response.");
  return result;
}

export async function copyRecipe(
  userId: string,
  destinationOrganizationId: string,
  input: RecipeCopyInput,
) {
  if (!uuid.test(input.mutationId))
    throw new Error("Invalid recipe copy request.");
  const response = await fetch(
    `/api/v1/organizations/${destinationOrganizationId}/recipe-copy`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        source_organization_id: input.sourceOrganizationId,
        source_recipe_id: input.sourceRecipeId,
        source_current_recipe_version_id: input.sourceCurrentRecipeVersionId,
        destination_recipe_id: input.destinationRecipeId,
        destination_recipe_version_id: input.destinationRecipeVersionId,
        ingredient_version_mappings: input.ingredientVersionMappings,
        recipe_tag_mappings: input.recipeTagMappings,
        scaling_unit_mappings: input.scalingUnitMappings,
        preferred_display_unit_mappings: input.preferredDisplayUnitMappings,
        client_installation_id: await readOrCreateBrowserInstallationId(userId),
        mutation_id: input.mutationId,
        client_wall_time: input.clientWallTime,
        ...(input.logicalOperationId
          ? { logical_operation_id: input.logicalOperationId }
          : {}),
      }),
    },
  );
  if (!response.ok) throw new RecipeCopyRequestError(response.status);
  return parseResult(await response.json());
}
