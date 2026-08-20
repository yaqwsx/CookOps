import { appendOutboxCommand, localDb, type CanonicalRecord } from "./local-db";
import type { IngredientUnit } from "./ingredient-catalog";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

function positiveDecimal(value: unknown) {
  return typeof value === "string" && decimal.test(value) && Number(value) > 0;
}

function wellFormedName(value: string) {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) return false;
  }
  return !value.includes("\u0000");
}

function validName(value: unknown) {
  if (typeof value !== "string" || !wellFormedName(value)) return false;
  const name = value.normalize("NFC").trim();
  return name.length > 0 && name.length <= 200;
}

function canonicalName(value: unknown) {
  return (
    typeof value === "string" &&
    validName(value) &&
    value === value.normalize("NFC").trim()
  );
}

export type IngredientCreateInput = {
  name: string;
  canonicalUnitId: string;
  massPerCanonicalQuantity: string;
  dietaryTagIds: string[];
  defaultStoreSectionId?: string;
};
export type IngredientCreateValidationError = "name" | "unit" | "mass" | "tag" | "storeSection";
export type IngredientCreateResult = { ingredientId: string; ingredientVersionId: string };

export function validateIngredientCreate(
  input: IngredientCreateInput,
): IngredientCreateValidationError | undefined {
  if (!validName(input.name)) return "name";
  if (!uuid.test(input.canonicalUnitId)) return "unit";
  if (input.defaultStoreSectionId !== undefined && !uuid.test(input.defaultStoreSectionId)) return "storeSection";
  if (!positiveDecimal(input.massPerCanonicalQuantity)) return "mass";
  if (
    new Set(input.dietaryTagIds).size !== input.dietaryTagIds.length ||
    !input.dietaryTagIds.every((id) => uuid.test(id))
  )
    return "tag";
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
        default_store_section_id: payload.default_store_section_id ?? null,
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

async function availableDietaryTags(
  userId: string,
  organizationId: string,
  tagIds: string[],
): Promise<boolean> {
  if (
    new Set(tagIds).size !== tagIds.length ||
    !tagIds.every((id) => uuid.test(id))
  )
    return false;
  const tags = await Promise.all(
    tagIds.map((id) =>
      localDb.canonicalRecords.get([userId, organizationId, "dietary_tag", id]),
    ),
  );
  return tags.every(
    (tag) =>
      tag?.lifecycle === "active" && tag.fields.organization_id === organizationId,
  );
}
async function availableStoreSection(userId: string, organizationId: string, id: unknown) {
  if (typeof id !== "string" || !uuid.test(id)) return false;
  const section = await localDb.canonicalRecords.get([userId, organizationId, "store_section", id]);
  return section?.lifecycle === "active" && section.fields.organization_id === organizationId;
}

async function queueIngredientCreateResult(
  userId: string,
  organizationId: string,
  input: IngredientCreateInput,
): Promise<IngredientCreateResult> {
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
    dietary_tag_ids: input.dietaryTagIds,
    ...(input.defaultStoreSectionId !== undefined ? { default_store_section_id: input.defaultStoreSectionId } : {}),
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
      if (!(await availableDietaryTags(userId, organizationId, input.dietaryTagIds)))
        throw new Error("tag");
      if (input.defaultStoreSectionId !== undefined && !(await availableStoreSection(userId, organizationId, input.defaultStoreSectionId))) throw new Error("storeSection");
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
  return { ingredientId, ingredientVersionId };
}

export async function queueIngredientCreate(
  userId: string,
  organizationId: string,
  input: IngredientCreateInput,
): Promise<string> {
  return (await queueIngredientCreateResult(userId, organizationId, input)).ingredientId;
}

/** Queue one create while exposing both server-bound immutable identities to a caller selecting the new version. */
export async function queueIngredientCreateWithVersion(
  userId: string,
  organizationId: string,
  input: IngredientCreateInput,
): Promise<IngredientCreateResult> {
  return queueIngredientCreateResult(userId, organizationId, input);
}

/** Rebuild a pending typed create after replacing the canonical replica. */
export async function replayIngredientCreate(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const payload = command.payload;
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) return;
  const dietaryTagIds = payload.dietary_tag_ids;
  if (
    (Object.keys(payload).length !== 7 && Object.keys(payload).length !== 6) ||
    !Object.keys(payload).every((key) =>
      [
        "ingredient_id",
        "ingredient_version_id",
        "name",
        "canonical_unit_id",
        "mass_per_canonical_quantity",
        "dietary_tag_ids", "default_store_section_id",
      ].includes(key),
    ) ||
    typeof payload.ingredient_id !== "string" ||
    typeof payload.ingredient_version_id !== "string" ||
    !canonicalName(payload.name) ||
    typeof payload.canonical_unit_id !== "string" ||
    typeof payload.mass_per_canonical_quantity !== "string" ||
    ![
      payload.ingredient_id,
      payload.ingredient_version_id,
      payload.canonical_unit_id,
    ].every((id) => uuid.test(id)) ||
    !Array.isArray(dietaryTagIds) ||
    !dietaryTagIds.every((id) => typeof id === "string") ||
    !positiveDecimal(payload.mass_per_canonical_quantity) ||
    ("default_store_section_id" in payload && typeof payload.default_store_section_id !== "string")
  )
    return;
  const unit = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "unit_definition",
    payload.canonical_unit_id,
  ]);
  if (!unit || !availableUnit(unit, organizationId)) return;
  if (
    unit.fields.dimension === "mass" &&
    unit.fields.base_unit_factor !== payload.mass_per_canonical_quantity
  )
    return;
  if (!(await availableDietaryTags(userId, organizationId, dietaryTagIds))) return;
  if ("default_store_section_id" in payload && !(await availableStoreSection(userId, organizationId, payload.default_store_section_id))) return;
  await localDb.optimisticOverlays.bulkPut(
    overlays(userId, organizationId, command.id, command.actionAt, payload),
  );
}

export function defaultMassForUnit(unit: IngredientUnit | undefined): string {
  return unit?.dimension === "mass" && decimal.test(unit.baseUnitFactor ?? "")
    ? (unit.baseUnitFactor ?? "1")
    : "1";
}
