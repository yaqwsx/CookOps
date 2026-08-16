import { appendOutboxCommand, localDb, type CanonicalRecord, type OutboxCommand } from "./local-db";
import { validateIngredientCreate } from "./ingredient-create";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type IngredientVersionPublishInput = {
  ingredientId: string;
  basedOnVersionId: string;
  name: string;
  canonicalUnitId: string;
  massPerCanonicalQuantity: string;
  dietaryTagIds: string[];
  defaultStoreSectionId?: string | null;
  logicalOperationId?: string | null;
};

function valid(input: IngredientVersionPublishInput) {
  return uuid.test(input.ingredientId) && uuid.test(input.basedOnVersionId) &&
    uuid.test(input.canonicalUnitId) && (input.defaultStoreSectionId == null || uuid.test(input.defaultStoreSectionId)) && validateIngredientCreate({
      name: input.name,
      canonicalUnitId: input.canonicalUnitId,
      massPerCanonicalQuantity: input.massPerCanonicalQuantity,
      dietaryTagIds: input.dietaryTagIds,
    }) === undefined;
}

async function available(
  userId: string,
  organizationId: string,
  payload: Record<string, unknown>,
) {
  const keys = ["ingredient_id", "based_on_version_id", "ingredient_version_id", "name", "canonical_unit_id", "mass_per_canonical_quantity", "dietary_tag_ids", "default_store_section_id", "logical_operation_id"];
  const required = keys.slice(0, 8);
  if (Object.keys(payload).some((key) => !keys.includes(key)) || required.some((key) => !(key in payload)) ||
    ![payload.ingredient_id, payload.based_on_version_id, payload.ingredient_version_id, payload.name, payload.canonical_unit_id, payload.mass_per_canonical_quantity].every((value) => typeof value === "string") ||
    !uuid.test(payload.ingredient_version_id as string) || (payload.logical_operation_id !== undefined && (typeof payload.logical_operation_id !== "string" || !uuid.test(payload.logical_operation_id))) || (payload.default_store_section_id !== null && typeof payload.default_store_section_id !== "string") || !Array.isArray(payload.dietary_tag_ids) || !payload.dietary_tag_ids.every((id) => typeof id === "string" && uuid.test(id)) ||
    !valid({ ingredientId: String(payload.ingredient_id), basedOnVersionId: String(payload.based_on_version_id), name: payload.name as string, canonicalUnitId: payload.canonical_unit_id as string, massPerCanonicalQuantity: payload.mass_per_canonical_quantity as string, dietaryTagIds: payload.dietary_tag_ids as string[], defaultStoreSectionId: payload.default_store_section_id as string | null })) return false;
  const ingredient = await localDb.canonicalRecords.get([userId, organizationId, "ingredient", String(payload.ingredient_id)]);
  const base = await localDb.canonicalRecords.get([userId, organizationId, "ingredient_version", String(payload.based_on_version_id)]);
  const unit = await localDb.canonicalRecords.get([userId, organizationId, "unit_definition", String(payload.canonical_unit_id)]);
  const baseUnitId = typeof base?.fields.canonical_unit_id === "string" ? base.fields.canonical_unit_id : undefined;
  const baseUnit = baseUnitId ? await localDb.canonicalRecords.get([userId, organizationId, "unit_definition", baseUnitId]) : undefined;
  if (ingredient?.lifecycle !== "active" || !base || base.fields.ingredient_id !== payload.ingredient_id || !unit || !baseUnit || unit.lifecycle !== "active" || baseUnit.lifecycle !== "active" || unit.fields.organization_id !== null && unit.fields.organization_id !== organizationId || baseUnit.fields.organization_id !== null && baseUnit.fields.organization_id !== organizationId || unit.fields.allows_ingredient_quantity !== true || baseUnit.fields.allows_ingredient_quantity !== true || unit.fields.dimension !== baseUnit.fields.dimension) return false;
  if (unit.fields.dimension === "mass" && unit.fields.base_unit_factor !== payload.mass_per_canonical_quantity) return false;
  const tags = payload.dietary_tag_ids as string[];
  const old = new Set(Array.isArray(base.fields.dietary_tag_ids) ? base.fields.dietary_tag_ids : []);
  for (const id of tags) {
    const tag = await localDb.canonicalRecords.get([userId, organizationId, "dietary_tag", id]);
    if (!old.has(id) && (tag?.lifecycle !== "active" || tag.fields.organization_id !== organizationId)) return false;
  }
  return true;
}

function overlays(userId: string, organizationId: string, command: OutboxCommand, payload: Record<string, unknown>, root: CanonicalRecord | undefined): CanonicalRecord[] {
  const optimistic = { optimistic: { mutationId: command.id, actionAt: command.actionAt } };
  return [
    { userId, organizationId, entityType: "ingredient", entityId: payload.ingredient_id as string, recordSchemaVersion: 1, lifecycle: "active", fields: { ...root?.fields, current_version_id: payload.ingredient_version_id }, fieldClocks: { ...root?.fieldClocks, ...optimistic }, immutable: false, updatedAt: command.actionAt },
    { userId, organizationId, entityType: "ingredient_version", entityId: payload.ingredient_version_id as string, recordSchemaVersion: 1, lifecycle: "active", fields: { id: payload.ingredient_version_id, ingredient_id: payload.ingredient_id, organization_id: organizationId, based_on_version_id: payload.based_on_version_id, name: payload.name, normalized_name: String(payload.name).toLowerCase(), canonical_unit_id: payload.canonical_unit_id, mass_per_canonical_quantity: payload.mass_per_canonical_quantity, dietary_tag_ids: payload.dietary_tag_ids, default_store_section_id: payload.default_store_section_id }, fieldClocks: optimistic, immutable: true, updatedAt: command.actionAt },
  ];
}

export async function queueIngredientVersionPublish(userId: string, organizationId: string, input: IngredientVersionPublishInput) {
  const normalizedInput = { ...input, name: input.name.normalize("NFC").trim() };
  if (!uuid.test(userId) || !uuid.test(organizationId) || !valid(normalizedInput) || (normalizedInput.logicalOperationId != null && !uuid.test(normalizedInput.logicalOperationId))) throw new Error("invalid");
  const actionAt = new Date().toISOString();
  const payload = { ingredient_id: normalizedInput.ingredientId, based_on_version_id: normalizedInput.basedOnVersionId, ingredient_version_id: crypto.randomUUID(), name: normalizedInput.name, canonical_unit_id: normalizedInput.canonicalUnitId, mass_per_canonical_quantity: normalizedInput.massPerCanonicalQuantity, dietary_tag_ids: normalizedInput.dietaryTagIds, default_store_section_id: normalizedInput.defaultStoreSectionId ?? null, ...(normalizedInput.logicalOperationId ? { logical_operation_id: normalizedInput.logicalOperationId } : {}) };
  const command: OutboxCommand = { id: crypto.randomUUID(), userId, organizationId, commandType: "ingredient.publish_version", payload, actionAt, createdAt: actionAt, state: "pending" };
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    if (!(await available(userId, organizationId, command.payload))) throw new Error("unavailable");
    const root = await localDb.canonicalRecords.get([userId, organizationId, "ingredient", normalizedInput.ingredientId]);
    await localDb.optimisticOverlays.bulkPut(overlays(userId, organizationId, command, command.payload, root));
    await appendOutboxCommand(command);
  });
  return command.payload.ingredient_version_id as string;
}

export async function replayIngredientVersionPublish(userId: string, organizationId: string, command: Pick<OutboxCommand, "id" | "actionAt" | "payload">) {
  if (!uuid.test(command.id) || typeof command.actionAt !== "string" || !Number.isFinite(Date.parse(command.actionAt)) || command.payload === null || Array.isArray(command.payload) || typeof command.payload !== "object") return false;
  if (!(await available(userId, organizationId, command.payload))) return false;
  const root = await localDb.canonicalRecords.get([userId, organizationId, "ingredient", String(command.payload.ingredient_id)]);
  await localDb.optimisticOverlays.bulkPut(overlays(userId, organizationId, { ...command, userId, organizationId, commandType: "ingredient.publish_version", createdAt: command.actionAt, state: "pending" }, command.payload, root));
  return true;
}
