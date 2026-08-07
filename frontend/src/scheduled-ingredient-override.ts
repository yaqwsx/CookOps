import { appendOutboxCommand, localDb } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const quantity = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const payloadKeys = [
  "override_id",
  "event_id",
  "scheduled_recipe_id",
  "operation",
  "override_kind",
  "target_line_key",
  "quantity",
];

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
    scheduled?.lifecycle === "active" &&
    scheduled.fields.event_id === eventId
  );
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

export async function replayReplacementOverride(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const p = command.payload;
  if (
    Object.keys(p).length !== payloadKeys.length ||
    !payloadKeys.every((key) => key in p) ||
    p.operation !== "set" ||
    p.override_kind !== "replace" ||
    ![
      p.override_id,
      p.event_id,
      p.scheduled_recipe_id,
      p.target_line_key,
    ].every((value) => typeof value === "string" && uuid.test(value)) ||
    typeof p.quantity !== "string" ||
    !quantity.test(p.quantity)
  )
    return;
  if (
    !(await activeScheduledRecipe(
      userId,
      organizationId,
      p.event_id as string,
      p.scheduled_recipe_id as string,
    ))
  )
    return;
  await localDb.optimisticOverlays.put({
    userId,
    organizationId,
    entityType: "scheduled_ingredient_override",
    entityId: p.override_id as string,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      id: p.override_id,
      organization_id: organizationId,
      ...p,
      retired_at: null,
    },
    fieldClocks: {
      [`replace.${p.target_line_key}`]: {
        mutationId: command.id,
        actionAt: command.actionAt,
      },
    },
    immutable: false,
    updatedAt: command.actionAt,
  });
}
