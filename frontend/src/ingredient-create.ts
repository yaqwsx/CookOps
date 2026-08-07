import { appendOutboxCommand, localDb, type CanonicalRecord } from "./local-db";
import type { IngredientUnit } from "./ingredient-catalog";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

function positiveDecimal(value: unknown) {
  return typeof value === "string" && decimal.test(value) && Number(value) > 0;
}

export type IngredientCreateInput = {
  name: string;
  canonicalUnitId: string;
  massPerCanonicalQuantity: string;
};
export type IngredientCreateValidationError = "name" | "unit" | "mass";

export function validateIngredientCreate(
  input: IngredientCreateInput,
): IngredientCreateValidationError | undefined {
  const name = input.name.normalize("NFC").trim();
  if (!name || name.length > 200) return "name";
  if (!uuid.test(input.canonicalUnitId)) return "unit";
  if (!positiveDecimal(input.massPerCanonicalQuantity)) return "mass";
}

function overlays(
  userId: string,
  organizationId: string,
  mutationId: string,
  actionAt: string,
  payload: Record<string, unknown>,
): CanonicalRecord[] {
  const fields = { ...payload, organization_id: organizationId };
  const optimistic = { optimistic: { mutationId, actionAt } };
  return [
    {
      userId,
      organizationId,
      entityType: "ingredient",
      entityId: payload.ingredient_id as string,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: payload.ingredient_id,
        current_version_id: payload.ingredient_version_id,
        current_price_estimate_id: null,
        retired_at: null,
        ...fields,
      },
      fieldClocks: optimistic,
      immutable: false,
      updatedAt: actionAt,
    },
    {
      userId,
      organizationId,
      entityType: "ingredient_version",
      entityId: payload.ingredient_version_id as string,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: payload.ingredient_version_id,
        ingredient_id: payload.ingredient_id,
        dietary_tag_ids: [],
        default_store_section_id: null,
        immutable: true,
        ...fields,
      },
      fieldClocks: optimistic,
      immutable: true,
      updatedAt: actionAt,
    },
  ];
}

function availableUnit(
  unit: CanonicalRecord | undefined,
  organizationId: string,
) {
  const owner = unit?.fields.organization_id;
  if (
    !(
      unit?.lifecycle === "active" &&
      (owner === null || owner === organizationId) &&
      unit.fields.allows_ingredient_quantity === true
    )
  )
    return false;
  return true;
}

export async function queueIngredientCreate(
  userId: string,
  organizationId: string,
  input: IngredientCreateInput,
): Promise<string> {
  const error = validateIngredientCreate(input);
  if (error || !uuid.test(userId) || !uuid.test(organizationId))
    throw new Error(error ?? "unit");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  const ingredientId = crypto.randomUUID();
  const ingredientVersionId = crypto.randomUUID();
  const payload = {
    ingredient_id: ingredientId,
    ingredient_version_id: ingredientVersionId,
    name: input.name.normalize("NFC").trim(),
    canonical_unit_id: input.canonicalUnitId,
    mass_per_canonical_quantity: input.massPerCanonicalQuantity,
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
        input.canonicalUnitId,
      ]);
      if (!availableUnit(unit, organizationId)) throw new Error("unit");
      if (
        unit?.fields.dimension === "mass" &&
        unit.fields.base_unit_factor !== input.massPerCanonicalQuantity
      )
        throw new Error("mass");
      await localDb.optimisticOverlays.bulkPut(
        overlays(userId, organizationId, mutationId, actionAt, payload),
      );
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "ingredient.create",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
  return ingredientId;
}

/** Rebuild a pending typed create after replacing the canonical replica. */
export async function replayIngredientCreate(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const payload = command.payload;
  if (
    typeof payload.ingredient_id !== "string" ||
    typeof payload.ingredient_version_id !== "string" ||
    typeof payload.name !== "string" ||
    typeof payload.canonical_unit_id !== "string" ||
    typeof payload.mass_per_canonical_quantity !== "string" ||
    ![
      payload.ingredient_id,
      payload.ingredient_version_id,
      payload.canonical_unit_id,
    ].every((id) => uuid.test(id)) ||
    !positiveDecimal(payload.mass_per_canonical_quantity)
  )
    return;
  await localDb.optimisticOverlays.bulkPut(
    overlays(userId, organizationId, command.id, command.actionAt, payload),
  );
}

export function defaultMassForUnit(unit: IngredientUnit | undefined): string {
  return unit?.dimension === "mass" && decimal.test(unit.baseUnitFactor ?? "")
    ? (unit.baseUnitFactor ?? "1")
    : "1";
}
