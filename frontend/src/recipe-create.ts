import { appendOutboxCommand, localDb, type CanonicalRecord } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

export type RecipeCreateInput = {
  name: string;
  description: string;
  scalingUnitId: string;
  baseScalingAmount: string;
};
export type RecipeCreateValidationError =
  | "name"
  | "description"
  | "scalingUnit"
  | "baseScalingAmount";

export function validateRecipeCreate(
  input: RecipeCreateInput,
): RecipeCreateValidationError | undefined {
  const name = input.name.normalize("NFC").trim();
  if (!name || name.length > 200) return "name";
  if (input.description.normalize("NFC").length > 20_000) return "description";
  if (!uuid.test(input.scalingUnitId)) return "scalingUnit";
  if (
    !decimal.test(input.baseScalingAmount) ||
    Number(input.baseScalingAmount) <= 0
  )
    return "baseScalingAmount";
}

function recipeOverlay(
  userId: string,
  organizationId: string,
  mutationId: string,
  actionAt: string,
  payload: Record<string, unknown>,
): CanonicalRecord {
  return {
    userId,
    organizationId,
    entityType: "recipe",
    entityId: payload.recipe_id as string,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      ...payload,
      id: payload.recipe_id,
      organization_id: organizationId,
      current_version_id: payload.recipe_version_id,
      lifecycle: "active",
      retired_at: null,
    },
    fieldClocks: { optimistic: { mutationId, actionAt } },
    immutable: false,
    updatedAt: actionAt,
  };
}

function versionOverlay(
  userId: string,
  organizationId: string,
  mutationId: string,
  actionAt: string,
  payload: Record<string, unknown>,
): CanonicalRecord {
  return {
    userId,
    organizationId,
    entityType: "recipe_version",
    entityId: payload.recipe_version_id as string,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      ...payload,
      id: payload.recipe_version_id,
      organization_id: organizationId,
      recipe_id: payload.recipe_id,
      immutable: true,
    },
    fieldClocks: { optimistic: { mutationId, actionAt } },
    immutable: true,
    updatedAt: actionAt,
  };
}

export async function queueRecipeCreate(
  userId: string,
  organizationId: string,
  input: RecipeCreateInput,
): Promise<string> {
  const error = validateRecipeCreate(input);
  if (error || !uuid.test(userId) || !uuid.test(organizationId))
    throw new Error(error ?? "scalingUnit");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  const recipeId = crypto.randomUUID();
  const recipeVersionId = crypto.randomUUID();
  const description = input.description
    .normalize("NFC")
    .replace(/\r\n?/g, "\n");
  const payload = {
    recipe_id: recipeId,
    recipe_version_id: recipeVersionId,
    name: input.name.normalize("NFC").trim(),
    scaling_unit_id: input.scalingUnitId,
    base_scaling_amount: input.baseScalingAmount,
    ingredient_lines: [],
    ...(description ? { description } : {}),
  };
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      const unit = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "unit_definition",
        input.scalingUnitId,
      ]);
      const owner = unit?.fields.organization_id;
      if (
        unit?.lifecycle !== "active" ||
        (owner !== null && owner !== organizationId) ||
        unit.fields.allows_recipe_scaling !== true
      )
        throw new Error("scalingUnit");
      await localDb.optimisticOverlays.bulkPut([
        recipeOverlay(userId, organizationId, mutationId, actionAt, payload),
        versionOverlay(userId, organizationId, mutationId, actionAt, payload),
      ]);
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "recipe.create",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
  return recipeId;
}

/** Rebuild the exact pending create projection after a canonical bootstrap replacement. */
export async function replayRecipeCreate(
  userId: string,
  organizationId: string,
  command: {
    id: string;
    actionAt: string;
    payload: Record<string, unknown>;
  },
) {
  const { payload } = command;
  if (
    typeof payload.recipe_id !== "string" ||
    typeof payload.recipe_version_id !== "string" ||
    typeof payload.name !== "string" ||
    typeof payload.scaling_unit_id !== "string" ||
    typeof payload.base_scaling_amount !== "string" ||
    ![
      payload.recipe_id,
      payload.recipe_version_id,
      payload.scaling_unit_id,
    ].every((id) => uuid.test(id)) ||
    !decimal.test(payload.base_scaling_amount)
  )
    return;
  await localDb.optimisticOverlays.bulkPut([
    recipeOverlay(
      userId,
      organizationId,
      command.id,
      command.actionAt,
      payload,
    ),
    versionOverlay(
      userId,
      organizationId,
      command.id,
      command.actionAt,
      payload,
    ),
  ]);
}
