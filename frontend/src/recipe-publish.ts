import {
  appendOutboxCommand,
  localDb,
  readVisibleCanonicalRecord,
  type CanonicalRecord,
} from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const recipeVersionTagNamespace = "82baf1fecee84306b6a84d92c10f5c4a";

export type RecipeVersionLineInput = {
  id: string;
  lineKey?: string;
  positionKey?: string;
  ingredientVersionId: string;
  baseQuantity: string;
  scalingBehavior: "proportional" | "fixed";
  includeInPortionWeight: boolean;
  note: string;
  preferredDisplayUnitId?: string;
};
export type RecipeVersionInput = {
  recipeId: string;
  basedOnVersionId: string;
  name: string;
  description: string;
  scalingUnitId: string;
  baseScalingAmount: string;
  ingredientLines: RecipeVersionLineInput[];
  recipeTagIds: string[];
  estimatedDinersPerScalingUnit?: string | null;
  roundSuggestionsUp?: boolean;
  catalogUpdate?: boolean;
  expectedCurrentIngredientVersions?: {
    ingredientId: string;
    versionId: string;
  }[];
};

export function validateRecipeVersion(
  input: RecipeVersionInput,
): string | undefined {
  if (!uuid.test(input.recipeId) || !uuid.test(input.basedOnVersionId))
    return "recipe";
  if (!input.name.normalize("NFC").trim() || input.name.length > 200)
    return "name";
  if (input.description.normalize("NFC").length > 20_000) return "description";
  if (!uuid.test(input.scalingUnitId)) return "scalingUnit";
  if (
    !decimal.test(input.baseScalingAmount) ||
    Number(input.baseScalingAmount) <= 0
  )
    return "baseScalingAmount";
  if (
    new Set(input.recipeTagIds).size !== input.recipeTagIds.length ||
    !input.recipeTagIds.every((id) => uuid.test(id))
  )
    return "tags";
  if (
    input.expectedCurrentIngredientVersions?.some(
      ({ ingredientId, versionId }) =>
        !uuid.test(ingredientId) || !uuid.test(versionId),
    ) ||
    (input.expectedCurrentIngredientVersions &&
      new Set(
        input.expectedCurrentIngredientVersions.map(
          ({ ingredientId }) => ingredientId,
        ),
      ).size !== input.expectedCurrentIngredientVersions.length)
  )
    return "ingredientLines";
  for (const line of input.ingredientLines) {
    if (
      !uuid.test(line.ingredientVersionId) ||
      (line.lineKey !== undefined && !uuid.test(line.lineKey)) ||
      (line.positionKey !== undefined &&
        !/^[0-9A-Za-z]+$/.test(line.positionKey)) ||
      (line.preferredDisplayUnitId !== undefined &&
        !uuid.test(line.preferredDisplayUnitId)) ||
      !decimal.test(line.baseQuantity) ||
      (line.scalingBehavior !== "proportional" &&
        line.scalingBehavior !== "fixed") ||
      typeof line.includeInPortionWeight !== "boolean" ||
      line.note.normalize("NFC").length > 20_000
    )
      return "ingredientLines";
  }
}

function overlay(
  userId: string,
  organizationId: string,
  entityType: string,
  entityId: string,
  fields: Record<string, unknown>,
  mutationId: string,
  actionAt: string,
  immutable = false,
): CanonicalRecord {
  return {
    userId,
    organizationId,
    entityType,
    entityId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields,
    fieldClocks: { optimistic: { mutationId, actionAt } },
    immutable,
    updatedAt: actionAt,
  };
}

async function writeRecipePublication(
  userId: string,
  organizationId: string,
  mutationId: string,
  actionAt: string,
  payload: Record<string, unknown>,
) {
  const recipeId = payload.recipe_id as string;
  const basedOn = payload.based_on_version_id as string;
  const versionId = payload.recipe_version_id as string;
  const current = await readVisibleCanonicalRecord(
    userId,
    organizationId,
    "recipe",
    recipeId,
  );
  if (
    current?.lifecycle !== "active" ||
    current.fields.organization_id !== organizationId ||
    current.fields.id !== recipeId
  )
    throw new Error("recipe");
  const currentVersionId = current.fields.current_version_id;
  const currentVersion =
    typeof currentVersionId === "string"
      ? await readVisibleCanonicalRecord(
          userId,
          organizationId,
          "recipe_version",
          currentVersionId,
        )
      : undefined;
  if (
    currentVersion?.immutable !== true ||
    currentVersion.fields.organization_id !== organizationId ||
    currentVersion.fields.recipe_id !== recipeId ||
    currentVersion.fields.id !== currentVersionId
  )
    throw new Error("recipe");
  const version = await readVisibleCanonicalRecord(
    userId,
    organizationId,
    "recipe_version",
    basedOn,
  );
  if (
    version?.immutable !== true ||
    version.fields.organization_id !== organizationId ||
    version.fields.recipe_id !== recipeId ||
    version.fields.id !== basedOn
  )
    throw new Error("recipe");
  const unit = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "unit_definition",
    payload.scaling_unit_id as string,
  ]);
  if (
    unit?.lifecycle !== "active" ||
    unit.fields.allows_recipe_scaling !== true
  )
    throw new Error("scalingUnit");
  const lines = payload.ingredient_lines as Array<Record<string, unknown>>;
  const tags = payload.recipe_tag_ids as string[];
  for (const tagId of tags) {
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
  for (const line of lines) {
    const ingredient = await localDb.canonicalRecords.get([
      userId,
      organizationId,
      "ingredient_version",
      line.ingredient_version_id as string,
    ]);
    if (!ingredient) throw new Error("ingredientLines");
  }
  const tagRecords = await Promise.all(
    tags.map(async (tagId) => {
      const id = await recipeVersionTagId(versionId, tagId);
      return overlay(
        userId,
        organizationId,
        "recipe_version_tag",
        id,
        {
          id,
          recipe_version_id: versionId,
          recipe_tag_id: tagId,
          organization_id: organizationId,
        },
        mutationId,
        actionAt,
        true,
      );
    }),
  );
  const records: CanonicalRecord[] = [
    overlay(
      userId,
      organizationId,
      "recipe",
      recipeId,
      { ...current.fields, current_version_id: versionId },
      mutationId,
      actionAt,
    ),
    overlay(
      userId,
      organizationId,
      "recipe_version",
      versionId,
      {
        id: versionId,
        recipe_id: recipeId,
        organization_id: organizationId,
        based_on_version_id: basedOn,
        name: payload.name,
        description: payload.description,
        scaling_unit_id: payload.scaling_unit_id,
        base_scaling_amount: payload.base_scaling_amount,
        ...(payload.estimated_diners_per_scaling_unit !== undefined
          ? {
              estimated_diners_per_scaling_unit:
                payload.estimated_diners_per_scaling_unit,
            }
          : {}),
        ...(payload.round_suggestions_up !== undefined
          ? { round_suggestions_up: payload.round_suggestions_up }
          : {}),
        immutable: true,
      },
      mutationId,
      actionAt,
      true,
    ),
    ...lines.map((line) =>
      overlay(
        userId,
        organizationId,
        "recipe_ingredient_line",
        line.id as string,
        {
          ...line,
          recipe_id: recipeId,
          recipe_version_id: versionId,
          organization_id: organizationId,
          ...(line.preferred_display_unit_id !== undefined
            ? { preferred_display_unit_id: line.preferred_display_unit_id }
            : {}),
        },
        mutationId,
        actionAt,
        true,
      ),
    ),
    ...tagRecords,
  ];
  await localDb.optimisticOverlays.bulkPut(records);
}

export async function queueRecipeVersionPublish(
  userId: string,
  organizationId: string,
  input: RecipeVersionInput,
) {
  const error = validateRecipeVersion(input);
  if (error || !uuid.test(userId) || !uuid.test(organizationId))
    throw new Error(error ?? "recipe");
  const actionAt = new Date().toISOString(),
    mutationId = crypto.randomUUID(),
    versionId = crypto.randomUUID();
  const payload: Record<string, unknown> = {
    recipe_id: input.recipeId,
    recipe_version_id: versionId,
    based_on_version_id: input.basedOnVersionId,
    name: input.name.normalize("NFC").trim(),
    description:
      input.description.normalize("NFC").replace(/\r\n?/g, "\n") || null,
    scaling_unit_id: input.scalingUnitId,
    base_scaling_amount: input.baseScalingAmount,
    recipe_tag_ids: input.recipeTagIds,
    ingredient_lines: input.ingredientLines.map((line, position) => ({
      id: crypto.randomUUID(),
      line_key: line.lineKey ?? crypto.randomUUID(),
      ingredient_version_id: line.ingredientVersionId,
      base_quantity: line.baseQuantity,
      position_key: line.positionKey ?? position.toString(36),
      scaling_behavior: line.scalingBehavior,
      include_in_portion_weight: line.includeInPortionWeight,
      ...(line.note.normalize("NFC").trim()
        ? { note: line.note.normalize("NFC").trim() }
        : {}),
      ...(line.preferredDisplayUnitId
        ? { preferred_display_unit_id: line.preferredDisplayUnitId }
        : {}),
    })),
    ...(input.estimatedDinersPerScalingUnit !== undefined
      ? {
          estimated_diners_per_scaling_unit:
            input.estimatedDinersPerScalingUnit,
        }
      : {}),
    ...(input.roundSuggestionsUp !== undefined
      ? { round_suggestions_up: input.roundSuggestionsUp }
      : {}),
    ...(input.catalogUpdate ? { catalog_update: true } : {}),
    ...(input.expectedCurrentIngredientVersions?.length
      ? {
          expected_current_ingredient_versions:
            input.expectedCurrentIngredientVersions.map(
              ({ ingredientId, versionId }) => [ingredientId, versionId],
            ),
        }
      : {}),
  };
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      await writeRecipePublication(
        userId,
        organizationId,
        mutationId,
        actionAt,
        payload,
      );
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "recipe.publish_version",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
  return versionId;
}

export async function replayRecipeVersionPublish(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const payload = command.payload;
  if (
    !Object.values({
      recipe: payload.recipe_id,
      version: payload.recipe_version_id,
      base: payload.based_on_version_id,
      unit: payload.scaling_unit_id,
    }).every((value) => typeof value === "string" && uuid.test(value)) ||
    !Array.isArray(payload.ingredient_lines)
  )
    return;
  if (
    !Array.isArray(payload.recipe_tag_ids) ||
    new Set(payload.recipe_tag_ids).size !== payload.recipe_tag_ids.length ||
    !payload.recipe_tag_ids.every(
      (id): id is string => typeof id === "string" && uuid.test(id),
    )
  )
    return;
  try {
    await writeRecipePublication(
      userId,
      organizationId,
      command.id,
      command.actionAt,
      payload,
    );
  } catch {
    /* Retain malformed or stale intent for recovery. */
  }
}

export async function recipeVersionTagId(
  recipeVersionId: string,
  recipeTagId: string,
) {
  const namespace = Uint8Array.from(
    recipeVersionTagNamespace.match(/../g) ?? [],
    (byte) => Number.parseInt(byte, 16),
  );
  const name = new TextEncoder().encode(`${recipeVersionId}:${recipeTagId}`);
  const digest = new Uint8Array(
    await crypto.subtle.digest(
      "SHA-1",
      new Uint8Array([...namespace, ...name]),
    ),
  );
  digest[6] = (digest[6] & 0x0f) | 0x50;
  digest[8] = (digest[8] & 0x3f) | 0x80;
  const hex = [...digest.slice(0, 16)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
