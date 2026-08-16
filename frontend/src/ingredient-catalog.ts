import type { CanonicalRecord } from "./local-db";
import { readVisibleRecords } from "./visible-records";

export type CatalogIngredient = {
  id: string;
  versionId: string;
  name: string;
  canonicalUnitName: string;
  massPerCanonicalQuantity: string;
  canonicalUnitId?: string;
  dietaryTagIds?: string[];
  defaultStoreSectionId?: string | null;
  retired?: boolean;
  historical?: boolean;
  versions?: { id: string; name: string; canonicalUnitName: string; mass: string; basedOnVersionId?: string; canonicalUnitId: string; dietaryTagIds: string[]; defaultStoreSectionId: string | null }[];
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
    readVisibleRecords(userId, organizationId, "dietary_tag", true),
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
      const history = [...versionById.values()]
        .filter((version) => text(version, "ingredient_id") === root.entityId)
        .map((version) => ({ id: version.entityId, name: text(version, "name"), canonicalUnitName: unitNames.get(text(version, "canonical_unit_id") ?? "") ?? "—", mass: text(version, "mass_per_canonical_quantity") ?? "", basedOnVersionId: text(version, "based_on_version_id"), canonicalUnitId: text(version, "canonical_unit_id") ?? "", dietaryTagIds: Array.isArray(version.fields.dietary_tag_ids) ? version.fields.dietary_tag_ids.filter((id): id is string => typeof id === "string") : [], defaultStoreSectionId: text(version, "default_store_section_id") ?? null }))
        .filter((version): version is { id: string; name: string; canonicalUnitName: string; mass: string; basedOnVersionId: string | undefined; canonicalUnitId: string; dietaryTagIds: string[]; defaultStoreSectionId: string | null } => Boolean(version.name && version.mass))
        .sort((left, right) => left.id.localeCompare(right.id));
      const currentTags = version && Array.isArray(version.fields.dietary_tag_ids) ? version.fields.dietary_tag_ids.filter((id): id is string => typeof id === "string") : [];
      return {
        id: root.entityId,
        versionId,
        name: version && text(version, "name"),
        canonicalUnitId: version && text(version, "canonical_unit_id"),
        massPerCanonicalQuantity:
          version && text(version, "mass_per_canonical_quantity"),
        ...(currentTags.length ? { dietaryTagIds: currentTags } : {}),
        ...(version && text(version, "default_store_section_id") ? { defaultStoreSectionId: text(version, "default_store_section_id") } : {}),
        ...(includeRetired ? { retired: root.lifecycle === "retired" } : {}),
        ...(history.length > 1 ? { versions: history } : {}),
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
        dietaryTagIds: string[];
        defaultStoreSectionId?: string;
        versions?: { id: string; name: string; canonicalUnitName: string; mass: string; basedOnVersionId: string | undefined; canonicalUnitId: string; dietaryTagIds: string[]; defaultStoreSectionId: string | null }[];
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
