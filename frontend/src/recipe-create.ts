import { appendOutboxCommand, localDb, type CanonicalRecord } from "./local-db";
import { recipeVersionTagId } from "./recipe-publish";
import { readEventPlanner } from "./planner-projections";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

export type RecipeCreateInput = {
  name: string;
  description: string;
  scalingUnitId: string;
  baseScalingAmount: string;
  recipeTagIds?: string[];
};
export type RecipeCreateValidationError =
  | "name"
  | "description"
  | "scalingUnit"
  | "baseScalingAmount"
  | "tags";

export async function assertPlannerTarget(
  userId: string,
  organizationId: string,
  eventId: string,
  eventDayId: string,
  eventMealRoleId: string,
): Promise<void> {
  const planner = await readEventPlanner(userId, organizationId, eventId);
  if (
    planner?.lifecycle !== "active" ||
    !planner.days.some((day) => day.id === eventDayId) ||
    !planner.roles.some((role) => role.id === eventMealRoleId)
  )
    throw new Error("selection");
}

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
  if (
    input.recipeTagIds &&
    (new Set(input.recipeTagIds).size !== input.recipeTagIds.length ||
      !input.recipeTagIds.every((id) => uuid.test(id)))
  )
    return "tags";
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
    recipe_tag_ids: input.recipeTagIds ?? [],
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
      for (const tagId of payload.recipe_tag_ids) {
        const tag =
          (await localDb.optimisticOverlays.get([
            userId,
            organizationId,
            "recipe_tag",
            tagId,
          ])) ??
          (await localDb.canonicalRecords.get([
            userId,
            organizationId,
            "recipe_tag",
            tagId,
          ]));
        if (tag?.lifecycle !== "active") throw new Error("tags");
      }
      await localDb.optimisticOverlays.bulkPut([
        recipeOverlay(userId, organizationId, mutationId, actionAt, payload),
        versionOverlay(userId, organizationId, mutationId, actionAt, payload),
        ...(await Promise.all(
          payload.recipe_tag_ids.map(async (tagId: string) => {
            const id = await recipeVersionTagId(recipeVersionId, tagId);
            return {
              userId,
              organizationId,
              entityType: "recipe_version_tag",
              entityId: id,
              recordSchemaVersion: 1,
              lifecycle: "active" as const,
              immutable: true,
              fields: {
                id,
                recipe_version_id: recipeVersionId,
                recipe_tag_id: tagId,
                organization_id: organizationId,
              },
              fieldClocks: { optimistic: { mutationId, actionAt } },
              updatedAt: actionAt,
            } satisfies CanonicalRecord;
          }),
        )),
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
  const recipeTagIds =
    payload.recipe_tag_ids === undefined ? [] : payload.recipe_tag_ids;
  if (
    !Array.isArray(recipeTagIds) ||
    new Set(recipeTagIds).size !== recipeTagIds.length ||
    !recipeTagIds.every(
      (id): id is string => typeof id === "string" && uuid.test(id),
    )
  )
    return;
  const tags = await Promise.all(
    recipeTagIds.map(
      async (tagId) =>
        (await localDb.optimisticOverlays.get([
          userId,
          organizationId,
          "recipe_tag",
          tagId,
        ])) ??
        (await localDb.canonicalRecords.get([
          userId,
          organizationId,
          "recipe_tag",
          tagId,
        ])),
    ),
  );
  if (tags.some((tag) => tag?.lifecycle !== "active")) return;
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
    ...(await Promise.all(
      recipeTagIds.map(async (tagId) => {
        const id = await recipeVersionTagId(
          payload.recipe_version_id as string,
          tagId,
        );
        return {
          userId,
          organizationId,
          entityType: "recipe_version_tag",
          entityId: id,
          recordSchemaVersion: 1,
          lifecycle: "active" as const,
          immutable: true,
          fields: {
            id,
            recipe_version_id: payload.recipe_version_id,
            recipe_tag_id: tagId,
            organization_id: organizationId,
          },
          fieldClocks: {
            optimistic: { mutationId: command.id, actionAt: command.actionAt },
          },
          updatedAt: command.actionAt,
        } satisfies CanonicalRecord;
      }),
    )),
  ]);
}
