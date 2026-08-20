import { appendOutboxCommand, localDb } from "./local-db";
import { readVisibleRecords } from "./visible-records";
import { timestampNanoseconds } from "./timestamp";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const quantity = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const replacementPayloadKeys = [
  "override_id",
  "event_id",
  "scheduled_recipe_id",
  "operation",
  "override_kind",
  "target_line_key",
  "quantity",
];
const addedPayloadKeys = [
  "override_id",
  "event_id",
  "scheduled_recipe_id",
  "operation",
  "override_kind",
  "ingredient_id",
  "ingredient_version_id",
  "quantity",
  "include_in_portion_weight",
  "position_key",
];
const clearReplacementPayloadKeys = [
  "override_id",
  "event_id",
  "scheduled_recipe_id",
  "operation",
  "override_kind",
  "target_line_key",
];
const clearAddedPayloadKeys = [
  "override_id",
  "event_id",
  "scheduled_recipe_id",
  "operation",
  "override_kind",
];

function wins(clock: unknown, mutationId: string, actionAt: string): boolean {
  if (clock === undefined || clock === null) return true;
  if (typeof clock !== "object" || Array.isArray(clock)) return false;
  const value = clock as Record<string, unknown>;
  const currentAt = typeof value.actionAt === "string" ? value.actionAt : value.winning_client_wall_time;
  const currentId = typeof value.mutationId === "string" ? value.mutationId : value.winning_mutation_id;
  const candidate = timestampNanoseconds(actionAt);
  const current = typeof currentAt === "string" ? timestampNanoseconds(currentAt) : undefined;
  return typeof currentId === "string" && uuid.test(currentId) && candidate !== undefined && current !== undefined && (candidate > current || (candidate === current && mutationId > currentId));
}

async function activeScheduledRecipe(
  userId: string,
  organizationId: string,
  eventId: string,
  scheduledRecipeId: string,
) {
  const event = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "event",
    eventId,
  ]);
  if (event?.lifecycle === "retired") return false;
  const visibleEvent =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "event",
      eventId,
    ])) ?? event;
  const canonicalScheduled = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "scheduled_recipe",
    scheduledRecipeId,
  ]);
  if (canonicalScheduled?.lifecycle === "retired") return false;
  const scheduled =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "scheduled_recipe",
      scheduledRecipeId,
    ])) ??
    canonicalScheduled;
  return (
    visibleEvent?.fields.lifecycle === "active" &&
    visibleEvent.fields.organization_id === organizationId &&
    scheduled?.lifecycle === "active" &&
    scheduled.fields.organization_id === organizationId &&
    scheduled.fields.event_id === eventId
  );
}

async function activeCatalogIngredient(
  userId: string,
  organizationId: string,
  ingredientId: string,
  ingredientVersionId: string,
) {
  const root = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "ingredient",
    ingredientId,
  ]);
  if (root?.lifecycle === "retired") return false;
  const visibleRoot =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "ingredient",
      ingredientId,
    ])) ?? root;
  const version = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "ingredient_version",
    ingredientVersionId,
  ]);
  if (version?.lifecycle === "retired") return false;
  const visibleVersion =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "ingredient_version",
      ingredientVersionId,
    ])) ?? version;
  return (
    visibleRoot?.lifecycle === "active" &&
    visibleRoot.fields.organization_id === organizationId &&
    visibleRoot.fields.current_version_id === ingredientVersionId &&
    visibleVersion?.lifecycle === "active" &&
    visibleVersion.fields.organization_id === organizationId &&
    visibleVersion.fields.ingredient_id === ingredientId
  );
}

async function pinnedRecipeContainsIngredient(
  userId: string,
  organizationId: string,
  scheduledRecipeId: string,
  ingredientId: string,
) {
  const scheduled =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "scheduled_recipe",
      scheduledRecipeId,
    ])) ??
    (await localDb.canonicalRecords.get([
      userId,
      organizationId,
      "scheduled_recipe",
      scheduledRecipeId,
    ]));
  const recipeVersionId = scheduled?.fields.recipe_version_id;
  if (typeof recipeVersionId !== "string" || !uuid.test(recipeVersionId))
    return true;
  const lines = await readVisibleRecords(
    userId,
    organizationId,
    "recipe_ingredient_line",
  );
  for (const line of lines) {
    if (line.fields.recipe_version_id !== recipeVersionId) continue;
    const versionId = line.fields.ingredient_version_id;
    if (typeof versionId !== "string" || !uuid.test(versionId)) return true;
    const version =
      (await localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "ingredient_version",
        versionId,
      ])) ??
      (await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "ingredient_version",
        versionId,
      ]));
    if (version?.fields.ingredient_id === ingredientId) return true;
  }
  return false;
}

async function pinnedRecipeContainsLine(userId: string, organizationId: string, scheduledRecipeId: string, lineKey: string) {
  const scheduled = (await localDb.optimisticOverlays.get([userId, organizationId, "scheduled_recipe", scheduledRecipeId])) ?? await localDb.canonicalRecords.get([userId, organizationId, "scheduled_recipe", scheduledRecipeId]);
  if (scheduled?.lifecycle !== "active") return false;
  const lines = await readVisibleRecords(userId, organizationId, "recipe_ingredient_line");
  return lines.some((line) => line.lifecycle === "active" && line.fields.recipe_version_id === scheduled.fields.recipe_version_id && line.fields.line_key === lineKey && line.fields.organization_id === organizationId);
}

export async function queueReplacementOverride(
  userId: string,
  organizationId: string,
  input: {
    eventId: string;
    scheduledRecipeId: string;
    targetLineKey: string;
    quantity: string;
  },
) {
  if (
    !Object.values(input)
      .slice(0, 3)
      .every((value) => uuid.test(value)) ||
    !quantity.test(input.quantity)
  )
    throw new Error("override");
  const id = crypto.randomUUID();
  const overrideId = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  const payload = {
    override_id: overrideId,
    event_id: input.eventId,
    scheduled_recipe_id: input.scheduledRecipeId,
    operation: "set",
    override_kind: "replace",
    target_line_key: input.targetLineKey,
    quantity: input.quantity,
  };
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      if (
        !(await activeScheduledRecipe(
          userId,
          organizationId,
          input.eventId,
          input.scheduledRecipeId,
        ))
      )
        throw new Error("override");
      await localDb.optimisticOverlays.put({
        userId,
        organizationId,
        entityType: "scheduled_ingredient_override",
        entityId: overrideId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: overrideId,
          organization_id: organizationId,
          ...payload,
          retired_at: null,
        },
        fieldClocks: {
          [`replace.${input.targetLineKey}`]: { mutationId: id, actionAt },
        },
        immutable: false,
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id,
        userId,
        organizationId,
        commandType: "scheduled_recipe.ingredient_override",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

export async function queueAddedOverride(
  userId: string,
  organizationId: string,
  input: {
    eventId: string;
    scheduledRecipeId: string;
    ingredientId: string;
    ingredientVersionId: string;
    quantity: string;
    includeInPortionWeight: boolean;
  },
) {
  if (
    ![
      input.eventId,
      input.scheduledRecipeId,
      input.ingredientId,
      input.ingredientVersionId,
    ].every((value) => uuid.test(value)) ||
    !quantity.test(input.quantity) ||
    typeof input.includeInPortionWeight !== "boolean"
  )
    throw new Error("override");
  const id = crypto.randomUUID();
  const overrideId = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  const payload = {
    override_id: overrideId,
    event_id: input.eventId,
    scheduled_recipe_id: input.scheduledRecipeId,
    operation: "set" as const,
    override_kind: "add" as const,
    ingredient_id: input.ingredientId,
    ingredient_version_id: input.ingredientVersionId,
    quantity: input.quantity,
    include_in_portion_weight: input.includeInPortionWeight,
    position_key: "z",
  };
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      if (
        !(await activeScheduledRecipe(
          userId,
          organizationId,
          input.eventId,
          input.scheduledRecipeId,
        )) ||
        !(await activeCatalogIngredient(
          userId,
          organizationId,
          input.ingredientId,
          input.ingredientVersionId,
        )) ||
        (await pinnedRecipeContainsIngredient(
          userId,
          organizationId,
          input.scheduledRecipeId,
          input.ingredientId,
        ))
      )
        throw new Error("override");
      await localDb.optimisticOverlays.put({
        userId,
        organizationId,
        entityType: "scheduled_ingredient_override",
        entityId: overrideId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: overrideId,
          organization_id: organizationId,
          ...payload,
          retired_at: null,
        },
        fieldClocks: {
          [`add.${overrideId}`]: { mutationId: id, actionAt },
        },
        immutable: false,
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id,
        userId,
        organizationId,
        commandType: "scheduled_recipe.ingredient_override",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

export async function queueClearReplacementOverride(
  userId: string,
  organizationId: string,
  input: { eventId: string; scheduledRecipeId: string; targetLineKey: string; overrideId: string },
) {
  if (![input.eventId, input.scheduledRecipeId, input.targetLineKey, input.overrideId].every((value) => uuid.test(value))) throw new Error("override");
  const id = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  const payload = {
    override_id: input.overrideId,
    event_id: input.eventId,
    scheduled_recipe_id: input.scheduledRecipeId,
    operation: "clear" as const,
    override_kind: "replace" as const,
    target_line_key: input.targetLineKey,
  };
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    if (!(await activeScheduledRecipe(userId, organizationId, input.eventId, input.scheduledRecipeId))) throw new Error("override");
    const lines = await readVisibleRecords(userId, organizationId, "recipe_ingredient_line");
    const scheduled = (await localDb.optimisticOverlays.get([userId, organizationId, "scheduled_recipe", input.scheduledRecipeId])) ?? await localDb.canonicalRecords.get([userId, organizationId, "scheduled_recipe", input.scheduledRecipeId]);
    if (!scheduled || !lines.some((line) => line.lifecycle === "active" && line.fields.organization_id === organizationId && line.fields.recipe_version_id === scheduled.fields.recipe_version_id && line.fields.line_key === input.targetLineKey)) throw new Error("override");
    const overrides = await readVisibleRecords(userId, organizationId, "scheduled_ingredient_override");
    const current = overrides.find((record) => record.entityId === input.overrideId && record.lifecycle === "active" && record.fields.organization_id === organizationId && record.fields.event_id === input.eventId && record.fields.scheduled_recipe_id === input.scheduledRecipeId && record.fields.override_kind === "replace" && record.fields.target_line_key === input.targetLineKey);
    if (!current) throw new Error("override");
    await localDb.optimisticOverlays.put({ ...current, lifecycle: "retired", fields: { ...current.fields, ...payload, retired_at: actionAt }, fieldClocks: { ...current.fieldClocks, [`replace.${input.targetLineKey}`]: { mutationId: id, actionAt } }, updatedAt: actionAt });
    await appendOutboxCommand({ id, userId, organizationId, commandType: "scheduled_recipe.ingredient_override", payload, actionAt, createdAt: actionAt, state: "pending" });
  });
}

export async function queueClearAddedOverride(
  userId: string,
  organizationId: string,
  input: { eventId: string; scheduledRecipeId: string; overrideId: string },
) {
  if (![input.eventId, input.scheduledRecipeId, input.overrideId].every((value) => uuid.test(value))) throw new Error("override");
  const id = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  const payload = { override_id: input.overrideId, event_id: input.eventId, scheduled_recipe_id: input.scheduledRecipeId, operation: "clear" as const, override_kind: "add" as const };
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    if (!(await activeScheduledRecipe(userId, organizationId, input.eventId, input.scheduledRecipeId))) throw new Error("override");
    const overrides = await readVisibleRecords(userId, organizationId, "scheduled_ingredient_override");
    const current = overrides.find((record) => record.entityId === input.overrideId && record.lifecycle === "active" && record.fields.organization_id === organizationId && record.fields.event_id === input.eventId && record.fields.scheduled_recipe_id === input.scheduledRecipeId && record.fields.override_kind === "add");
    if (!current) throw new Error("override");
    await localDb.optimisticOverlays.put({ ...current, lifecycle: "retired", fields: { ...current.fields, ...payload, retired_at: actionAt }, fieldClocks: { ...current.fieldClocks, [`add.${input.overrideId}`]: { mutationId: id, actionAt } }, updatedAt: actionAt });
    await appendOutboxCommand({ id, userId, organizationId, commandType: "scheduled_recipe.ingredient_override", payload, actionAt, createdAt: actionAt, state: "pending" });
  });
}

export async function replayScheduledIngredientOverride(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const p = command.payload;
  const clearReplacement = Object.keys(p).length === clearReplacementPayloadKeys.length && clearReplacementPayloadKeys.every((key) => key in p) && p.operation === "clear" && p.override_kind === "replace" && [p.override_id, p.event_id, p.scheduled_recipe_id, p.target_line_key].every((value) => typeof value === "string" && uuid.test(value));
  const clearAdded = Object.keys(p).length === clearAddedPayloadKeys.length && clearAddedPayloadKeys.every((key) => key in p) && p.operation === "clear" && p.override_kind === "add" && [p.override_id, p.event_id, p.scheduled_recipe_id].every((value) => typeof value === "string" && uuid.test(value));
  const replacement =
    Object.keys(p).length === replacementPayloadKeys.length &&
    replacementPayloadKeys.every((key) => key in p) &&
    p.operation === "set" &&
    p.override_kind === "replace" &&
    [p.override_id, p.event_id, p.scheduled_recipe_id, p.target_line_key].every(
      (value) => typeof value === "string" && uuid.test(value),
    ) &&
    typeof p.quantity === "string" &&
    quantity.test(p.quantity);
  const added =
    Object.keys(p).length === addedPayloadKeys.length &&
    addedPayloadKeys.every((key) => key in p) &&
    p.operation === "set" &&
    p.override_kind === "add" &&
    [
      p.override_id,
      p.event_id,
      p.scheduled_recipe_id,
      p.ingredient_id,
      p.ingredient_version_id,
    ].every((value) => typeof value === "string" && uuid.test(value)) &&
    typeof p.quantity === "string" &&
    quantity.test(p.quantity) &&
    typeof p.include_in_portion_weight === "boolean" &&
    typeof p.position_key === "string" &&
    /^[0-9A-Za-z]{1,255}$/.test(p.position_key);
  if (!replacement && !added && !clearReplacement && !clearAdded) return;
  if (!uuid.test(command.id) || timestampNanoseconds(command.actionAt) === undefined) return;
  if (
    !(await activeScheduledRecipe(
      userId,
      organizationId,
      p.event_id as string,
      p.scheduled_recipe_id as string,
    ))
  )
    return;
  if (clearReplacement && !(await pinnedRecipeContainsLine(userId, organizationId, p.scheduled_recipe_id as string, p.target_line_key as string))) return;
  if (
    added &&
    !(await activeCatalogIngredient(
      userId,
      organizationId,
      p.ingredient_id as string,
      p.ingredient_version_id as string,
    ))
  )
    return;
  const key = `${p.override_kind}.${replacement || clearReplacement ? p.target_line_key : p.override_id}`;
  const records = await localDb.canonicalRecords
    .where("[userId+organizationId+entityType]")
    .equals([userId, organizationId, "scheduled_ingredient_override"])
    .toArray();
  const overlays = await localDb.optimisticOverlays
    .where("[userId+organizationId+entityType]")
    .equals([userId, organizationId, "scheduled_ingredient_override"])
    .toArray();
  const existing =
    overlays.find((record) => record.fieldClocks[key] !== undefined) ??
    records.find((record) => record.fieldClocks[key] !== undefined);
  if (!wins(existing?.fieldClocks[key], command.id, command.actionAt)) return;
  if ((clearReplacement || clearAdded) && (!existing || existing.entityId !== p.override_id || existing.lifecycle !== "active" || existing.fields.override_kind !== p.override_kind || existing.fields.event_id !== p.event_id || existing.fields.scheduled_recipe_id !== p.scheduled_recipe_id)) return;
  const entityId = replacement && existing ? existing.entityId : (p.override_id as string);
  if (clearReplacement || clearAdded) {
    const current = existing;
    if (!current) return;
    await localDb.optimisticOverlays.put({ ...current, lifecycle: "retired", fields: { ...current.fields, ...p, retired_at: command.actionAt }, fieldClocks: { ...current.fieldClocks, [key]: { mutationId: command.id, actionAt: command.actionAt } }, updatedAt: command.actionAt });
    return;
  }
  await localDb.optimisticOverlays.put({
    userId,
    organizationId,
    entityType: "scheduled_ingredient_override",
    entityId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      ...existing?.fields,
      ...p,
      id: entityId,
      organization_id: organizationId,
      retired_at: null,
    },
    fieldClocks: {
      ...existing?.fieldClocks,
      [key]: {
        mutationId: command.id,
        actionAt: command.actionAt,
      },
    },
    immutable: false,
    updatedAt: command.actionAt,
  });
}
