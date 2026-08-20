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

type ShoppingCommand = { id: string; actionAt: string; payload: Record<string, unknown> };

export function timestampMicros(value: string): bigint | undefined {
  const match = /^(.*?)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$/.exec(value);
  if (!match) return undefined;
  const milliseconds = Date.parse(`${match[1]}${match[3]}`);
  if (!Number.isFinite(milliseconds)) return undefined;
  return BigInt(milliseconds) * 1_000n + BigInt((match[2] ?? "").slice(0, 6).padEnd(6, "0"));
}

function wins(record: { fieldClocks: Record<string, unknown> }, id: string, actionAt: string): boolean {
  const clock = record.fieldClocks.name;
  if (clock === undefined || clock === null) return true;
  if (typeof clock !== "object" || Array.isArray(clock)) return false;
  const value = clock as Record<string, unknown>;
  const clockAt = value.actionAt ?? value.winning_client_wall_time;
  const clockId = value.mutationId ?? value.winning_mutation_id;
  const left = typeof clockAt === "string" ? timestampMicros(actionAt) : undefined;
  const right = typeof clockAt === "string" ? timestampMicros(clockAt) : undefined;
  return typeof clockId === "string" && left !== undefined && right !== undefined &&
    (left > right || (left === right && id > clockId));
}

function canonicalName(value: string): string {
  const name = value.normalize("NFC").trim();
  if (!name || name.length > 200 || new TextEncoder().encode(JSON.stringify(name)).byteLength > 800)
    throw new Error("shopping_list");
  return name;
}

async function applyRename(userId: string, organizationId: string, listId: string, name: string, mutationId: string, actionAt: string): Promise<void> {
  const canonical = await localDb.canonicalRecords.get([userId, organizationId, "shopping_list", listId]);
  const overlay = await localDb.optimisticOverlays.get([userId, organizationId, "shopping_list", listId]);
  const current = overlay ?? canonical;
  if (current?.lifecycle !== "active" || current.fields.organization_id !== organizationId || typeof current.fields.event_id !== "string" || (canonical !== undefined && canonical.lifecycle !== "active")) return;
  const [canonicalEvent, visibleEvent] = await Promise.all([
    localDb.canonicalRecords.get([userId, organizationId, "event", current.fields.event_id]),
    readVisibleCanonicalRecord(userId, organizationId, "event", current.fields.event_id),
  ]);
  if (canonicalEvent?.lifecycle !== "active" || visibleEvent?.lifecycle !== "active" || !wins(current, mutationId, actionAt)) return;
  await localDb.optimisticOverlays.put({
    ...current,
    fields: { ...current.fields, name },
    fieldClocks: { ...current.fieldClocks, name: { mutationId, actionAt } },
    updatedAt: actionAt,
  });
}

export async function queueShoppingListRename(userId: string, organizationId: string, input: { shoppingListId: string; name: string }): Promise<void> {
  if (!uuid.test(userId) || !uuid.test(organizationId) || !uuid.test(input.shoppingListId)) throw new Error("shopping_list");
  const name = canonicalName(input.name);
  const mutationId = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    const canonical = await localDb.canonicalRecords.get([userId, organizationId, "shopping_list", input.shoppingListId]);
    const overlay = await localDb.optimisticOverlays.get([userId, organizationId, "shopping_list", input.shoppingListId]);
    const current = overlay ?? canonical;
    if (current?.lifecycle !== "active" || current.fields.organization_id !== organizationId || typeof current.fields.event_id !== "string" || (canonical !== undefined && canonical.lifecycle !== "active")) throw new Error("shopping_list");
    const [canonicalEvent, visibleEvent] = await Promise.all([
      localDb.canonicalRecords.get([userId, organizationId, "event", current.fields.event_id]),
      readVisibleCanonicalRecord(userId, organizationId, "event", current.fields.event_id),
    ]);
    if (canonicalEvent?.lifecycle !== "active" || visibleEvent?.lifecycle !== "active") throw new Error("shopping_list");
    await applyRename(userId, organizationId, input.shoppingListId, name, mutationId, actionAt);
    await appendOutboxCommand({ id: mutationId, userId, organizationId, commandType: "shopping_list.rename", payload: { shopping_list_id: input.shoppingListId, name }, actionAt, createdAt: actionAt, state: "pending" });
  });
}

export async function replayShoppingListRename(userId: string, organizationId: string, command: ShoppingCommand): Promise<void> {
  const payload = command.payload;
  if (Object.keys(payload).length !== 2 || typeof payload.shopping_list_id !== "string" || !uuid.test(payload.shopping_list_id) || typeof payload.name !== "string" || !uuid.test(command.id) || typeof command.actionAt !== "string" || timestampMicros(command.actionAt) === undefined) return;
  let name: string;
  try { name = canonicalName(payload.name); } catch { return; }
  await applyRename(userId, organizationId, payload.shopping_list_id, name, command.id, command.actionAt);
}

/** Store the materialization request and its immediately visible list together. */
export async function queueShoppingList(
  userId: string,
  organizationId: string,
  input: CreateShoppingListInput,
): Promise<string> {
  const name = canonicalName(input.name);
  if (
    !uuid.test(input.eventId) ||
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
  return localDb.transaction(
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
      const days = planner?.days ?? [];
      const roles = planner?.roles ?? [];
      if (
        planner?.lifecycle !== "active" ||
        input.scheduledRecipeIds.some(
          (id) => !(planner.scheduled ?? []).some((recipe) => recipe.id === id && !recipe.retired && days.some((day) => day.id === recipe.dayId) && roles.some((role) => role.id === recipe.roleId)),
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
      return shoppingListId;
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
