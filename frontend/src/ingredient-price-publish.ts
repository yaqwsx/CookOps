import { appendOutboxCommand, localDb, type CanonicalRecord, type OutboxCommand } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

export type IngredientPricePublishInput = {
  ingredientId: string;
  amount: string;
  pricedQuantity: string;
  unitId: string;
  currency: string;
  logicalOperationId?: string | null;
};

function valid(input: IngredientPricePublishInput) {
  return uuid.test(input.ingredientId) && uuid.test(input.unitId) && input.amount.length <= 100 && input.pricedQuantity.length <= 100 && decimal.test(input.amount) && decimal.test(input.pricedQuantity) && Number(input.amount) >= 0 && Number(input.pricedQuantity) > 0 && /^[A-Z]{3}$/.test(input.currency) && (input.logicalOperationId == null || uuid.test(input.logicalOperationId));
}

async function available(userId: string, organizationId: string, payload: Record<string, unknown>) {
  if (!uuid.test(userId) || !uuid.test(organizationId) || Object.keys(payload).some((key) => !["ingredient_id", "ingredient_price_estimate_id", "amount", "priced_quantity", "unit_id", "currency", "logical_operation_id"].includes(key)) || typeof payload.ingredient_id !== "string" || typeof payload.ingredient_price_estimate_id !== "string" || !uuid.test(payload.ingredient_price_estimate_id) || typeof payload.amount !== "string" || typeof payload.priced_quantity !== "string" || typeof payload.unit_id !== "string" || typeof payload.currency !== "string") return false;
  const root = await localDb.canonicalRecords.get([userId, organizationId, "ingredient", payload.ingredient_id]);
  const organization = await localDb.canonicalRecords.get([userId, organizationId, "organization", organizationId]);
  const unit = await localDb.canonicalRecords.get([userId, organizationId, "unit_definition", payload.unit_id]);
  const version = root && typeof root.fields.current_version_id === "string" ? await localDb.canonicalRecords.get([userId, organizationId, "ingredient_version", root.fields.current_version_id]) : undefined;
  const canonical = version && typeof version.fields.canonical_unit_id === "string" ? await localDb.canonicalRecords.get([userId, organizationId, "unit_definition", version.fields.canonical_unit_id]) : undefined;
  return root?.lifecycle === "active" && organization?.fields.default_currency === payload.currency && version?.fields.ingredient_id === payload.ingredient_id && unit?.lifecycle === "active" && unit.fields.allows_ingredient_quantity === true && (unit.fields.organization_id === null || unit.fields.organization_id === organizationId) && canonical !== undefined && canonical.fields.dimension === unit.fields.dimension && (unit.fields.dimension !== "count" && unit.fields.dimension !== "custom" || unit.entityId === canonical.entityId) && valid({ ingredientId: payload.ingredient_id, amount: payload.amount, pricedQuantity: payload.priced_quantity, unitId: payload.unit_id, currency: payload.currency, logicalOperationId: payload.logical_operation_id as string | null });
}

function overlay(userId: string, organizationId: string, command: OutboxCommand, payload: Record<string, unknown>, root: CanonicalRecord): CanonicalRecord[] {
  return [{ userId, organizationId, entityType: "ingredient", entityId: payload.ingredient_id as string, recordSchemaVersion: 1, lifecycle: "active", fields: { ...root.fields, current_price_estimate_id: payload.ingredient_price_estimate_id }, fieldClocks: { ...root.fieldClocks, current_price_estimate_id: { winningClientWallTime: command.actionAt, winningMutationId: command.id } }, immutable: false, updatedAt: command.actionAt }, { userId, organizationId, entityType: "ingredient_price_estimate", entityId: payload.ingredient_price_estimate_id as string, recordSchemaVersion: 1, lifecycle: "active", fields: { id: payload.ingredient_price_estimate_id, ingredient_id: payload.ingredient_id, organization_id: organizationId, based_on_estimate_id: root.fields.current_price_estimate_id ?? null, state: "available", price_amount: payload.amount, priced_quantity: payload.priced_quantity, priced_unit_id: payload.unit_id, currency: payload.currency }, fieldClocks: {}, immutable: true, updatedAt: command.actionAt }];
}

export async function queueIngredientPricePublish(userId: string, organizationId: string, input: IngredientPricePublishInput) {
  const payload = { ingredient_id: input.ingredientId, ingredient_price_estimate_id: crypto.randomUUID(), amount: input.amount, priced_quantity: input.pricedQuantity, unit_id: input.unitId, currency: input.currency.toUpperCase(), ...(input.logicalOperationId ? { logical_operation_id: input.logicalOperationId } : {}) };
  const actionAt = new Date().toISOString();
  const command: OutboxCommand = { id: crypto.randomUUID(), userId, organizationId, commandType: "ingredient.publish_price_estimate", payload, actionAt, createdAt: actionAt, state: "pending" };
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => { if (!(await available(userId, organizationId, payload))) throw new Error("unavailable"); const root = await localDb.canonicalRecords.get([userId, organizationId, "ingredient", input.ingredientId]); if (!root) throw new Error("unavailable"); await localDb.optimisticOverlays.bulkPut(overlay(userId, organizationId, command, payload, root)); await appendOutboxCommand(command); });
  return payload.ingredient_price_estimate_id;
}

export async function replayIngredientPricePublish(userId: string, organizationId: string, command: Pick<OutboxCommand, "id" | "actionAt" | "payload">) {
  if (!uuid.test(command.id) || !Number.isFinite(Date.parse(command.actionAt)) || !command.payload || Array.isArray(command.payload) || typeof command.payload !== "object" || !(await available(userId, organizationId, command.payload))) return false;
  const root = await localDb.canonicalRecords.get([userId, organizationId, "ingredient", String(command.payload.ingredient_id)]);
  if (!root) return false;
  await localDb.optimisticOverlays.bulkPut(overlay(userId, organizationId, { ...command, userId, organizationId, commandType: "ingredient.publish_price_estimate", createdAt: command.actionAt, state: "pending" }, command.payload, root));
  return true;
}
