import type { CanonicalRecord } from "./local-db";
import { readVisibleRecords } from "./visible-records";

export type CatalogIngredient = {
  id: string;
  versionId: string;
  name: string;
  canonicalUnitName: string;
  massPerCanonicalQuantity: string;
  retired?: boolean;
};
export type IngredientUnit = {
  id: string;
  name: string;
  dimension: string;
  baseUnitFactor: string | undefined;
};
export type IngredientCatalogProjection = {
  ingredients: CatalogIngredient[];
  units: IngredientUnit[];
  dietaryTags: { id: string; name: string }[];
};

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

function text(record: CanonicalRecord, key: string) {
  const value = record.fields[key];
  return typeof value === "string" ? value : undefined;
}

/** Return only active, organization-owned catalog roots with their matching current version. */
export async function readIngredientCatalog(
  userId: string,
  organizationId: string,
  includeRetired = false,
): Promise<IngredientCatalogProjection> {
  if (!uuid.test(userId) || !uuid.test(organizationId))
    return { ingredients: [], units: [], dietaryTags: [] };
  const [roots, versions, records, tags] = await Promise.all([
    readVisibleRecords(userId, organizationId, "ingredient", includeRetired),
    readVisibleRecords(userId, organizationId, "ingredient_version"),
    readVisibleRecords(userId, organizationId, "unit_definition"),
    readVisibleRecords(userId, organizationId, "dietary_tag"),
  ]);
  const units = records
    .filter((record) => {
      const owner = record.fields.organization_id;
      return (
        uuid.test(record.entityId) &&
        text(record, "id") === record.entityId &&
        (owner === null || owner === organizationId) &&
        record.fields.allows_ingredient_quantity === true
      );
    })
    .map((record) => ({
      id: record.entityId,
      name: text(record, "custom_name") ?? text(record, "code"),
      dimension: text(record, "dimension"),
      baseUnitFactor: text(record, "base_unit_factor"),
    }))
    .filter(
      (unit): unit is IngredientUnit =>
        Boolean(unit.name && unit.dimension) &&
        (unit.dimension === "count" ||
          unit.dimension === "custom" ||
          decimal.test(unit.baseUnitFactor ?? "")),
    )
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) || left.id.localeCompare(right.id),
    );
  const unitNames = new Map(units.map((unit) => [unit.id, unit.name]));
  const versionById = new Map(
    versions
      .filter(
        (record) =>
          uuid.test(record.entityId) &&
          text(record, "id") === record.entityId &&
          text(record, "organization_id") === organizationId,
      )
      .map((record) => [record.entityId, record]),
  );
  const ingredients = roots
    .filter(
      (record) =>
        uuid.test(record.entityId) &&
        text(record, "id") === record.entityId &&
        text(record, "organization_id") === organizationId,
    )
    .map((root) => {
      const versionId = text(root, "current_version_id");
      const candidate = versionId ? versionById.get(versionId) : undefined;
      const version =
        candidate && text(candidate, "ingredient_id") === root.entityId
          ? candidate
          : undefined;
      return {
        id: root.entityId,
        versionId,
        name: version && text(version, "name"),
        canonicalUnitId: version && text(version, "canonical_unit_id"),
        massPerCanonicalQuantity:
          version && text(version, "mass_per_canonical_quantity"),
        ...(includeRetired ? { retired: root.lifecycle === "retired" } : {}),
      };
    })
    .filter(
      (
        item,
      ): item is {
        id: string;
        versionId: string;
        name: string;
        canonicalUnitId: string;
        massPerCanonicalQuantity: string;
      } =>
        Boolean(
          item.name &&
            item.versionId &&
            uuid.test(item.versionId) &&
            item.canonicalUnitId &&
            item.massPerCanonicalQuantity &&
            unitNames.has(item.canonicalUnitId) &&
            decimal.test(item.massPerCanonicalQuantity),
        ),
    )
    .map(({ canonicalUnitId, ...item }) => ({
      ...item,
      canonicalUnitName: unitNames.get(canonicalUnitId) ?? canonicalUnitId,
    }))
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) || left.id.localeCompare(right.id),
    );
  const dietaryTags = tags
    .filter(
      (record) =>
        uuid.test(record.entityId) &&
        text(record, "id") === record.entityId &&
        text(record, "organization_id") === organizationId,
    )
    .map((record) => ({ id: record.entityId, name: text(record, "name") }))
    .filter((tag): tag is { id: string; name: string } => Boolean(tag.name));
  return { ingredients, units, dietaryTags };
}
