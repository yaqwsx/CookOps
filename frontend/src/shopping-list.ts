import {
  appendOutboxCommand,
  localDb,
  readVisibleCanonicalRecord,
} from "./local-db";
import { readEventPlanner } from "./planner-projections";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type CreateShoppingListInput = {
  eventId: string;
  name: string;
  scheduledRecipeIds: string[];
};

export type RefreshShoppingListInput = {
  shoppingListId: string;
  parentGenerationRevisionId: string;
  scheduledRecipeIds: string[];
};

/** Store the materialization request and its immediately visible list together. */
export async function queueShoppingList(
  userId: string,
  organizationId: string,
  input: CreateShoppingListInput,
): Promise<void> {
  const name = input.name.trim();
  if (
    !uuid.test(input.eventId) ||
    !name ||
    name.length > 200 ||
    new TextEncoder().encode(JSON.stringify(name)).byteLength > 800 ||
    input.scheduledRecipeIds.some((id) => !uuid.test(id)) ||
    new Set(input.scheduledRecipeIds).size !== input.scheduledRecipeIds.length
  )
    throw new Error("shopping_list");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  const shoppingListId = crypto.randomUUID();
  const generationRevisionId = crypto.randomUUID();
  const payload = {
    shopping_list_id: shoppingListId,
    generation_revision_id: generationRevisionId,
    event_id: input.eventId,
    name,
    scheduled_recipe_ids: input.scheduledRecipeIds,
  };
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      const planner = await readEventPlanner(
        userId,
        organizationId,
        input.eventId,
      );
      if (
        planner?.lifecycle !== "active" ||
        input.scheduledRecipeIds.some(
          (id) => !planner.scheduled.some((recipe) => recipe.id === id),
        )
      )
        throw new Error("shopping_list");
      await localDb.optimisticOverlays.put({
        userId,
        organizationId,
        entityType: "shopping_list",
        entityId: shoppingListId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: shoppingListId,
          organization_id: organizationId,
          event_id: input.eventId,
          name,
          current_generation_revision_id: generationRevisionId,
          scheduled_recipe_ids: input.scheduledRecipeIds,
          created_at: actionAt,
          created_by_user_id: userId,
        },
        fieldClocks: { optimistic: { mutationId, actionAt } },
        immutable: false,
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "shopping_list.create",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

/** Persist a refresh intent; the server alone advances the generated revision pointer. */
export async function queueShoppingListRefresh(
  userId: string,
  organizationId: string,
  input: RefreshShoppingListInput,
): Promise<boolean> {
  if (
    !uuid.test(input.shoppingListId) ||
    !uuid.test(input.parentGenerationRevisionId) ||
    input.scheduledRecipeIds.some((id) => !uuid.test(id)) ||
    new Set(input.scheduledRecipeIds).size !== input.scheduledRecipeIds.length
  )
    throw new Error("shopping_list");
  const actionAt = new Date().toISOString();
  return localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      const list = await readVisibleCanonicalRecord(
        userId,
        organizationId,
        "shopping_list",
        input.shoppingListId,
      );
      const eventId = list?.fields.event_id;
      const parent = list?.fields.current_generation_revision_id;
      if (
        list?.lifecycle !== "active" ||
        typeof eventId !== "string" ||
        parent !== input.parentGenerationRevisionId
      )
        throw new Error("shopping_list");
      if (
        await hasQueuedShoppingListRefresh(
          userId,
          organizationId,
          input.shoppingListId,
        )
      )
        return false;
      const planner = await readEventPlanner(userId, organizationId, eventId);
      if (
        planner?.lifecycle !== "active" ||
        input.scheduledRecipeIds.some(
          (id) => !planner.scheduled.some((recipe) => recipe.id === id),
        )
      )
        throw new Error("shopping_list");
      await appendOutboxCommand({
        id: crypto.randomUUID(),
        userId,
        organizationId,
        commandType: "shopping_list.refresh",
        payload: {
          generation_revision_id: crypto.randomUUID(),
          shopping_list_id: input.shoppingListId,
          parent_generation_revision_id: input.parentGenerationRevisionId,
          scheduled_recipe_ids: input.scheduledRecipeIds,
        },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
      return true;
    },
  );
}

export async function hasQueuedShoppingListRefresh(
  userId: string,
  organizationId: string,
  shoppingListId: string,
): Promise<boolean> {
  return (
    await localDb.outbox
      .where("[userId+organizationId+state]")
      .equals([userId, organizationId, "pending"])
      .toArray()
  ).some(
    (command) =>
      command.commandType === "shopping_list.refresh" &&
      command.payload.shopping_list_id === shoppingListId,
  );
}
