import type { CanonicalRecord } from "./local-db";
import { localDb } from "./local-db";
import type { IngredientUnit } from "./ingredient-catalog";
import { readVisibleRecords } from "./visible-records";

export type IngredientCopyCatalog = {
  units: IngredientUnit[];
  sections: { id: string; name: string }[];
  dietaryTags: { id: string; name: string; seedKey: string | null }[];
};

export type IngredientCopyCatalogMode = "source" | "destination";

function text(fields: Record<string, unknown>, key: string): string | undefined {
  return typeof fields[key] === "string" ? fields[key] : undefined;
}

export async function readIngredientCopyCatalog(
  userId: string,
  organizationId: string,
  mode: IngredientCopyCatalogMode = "destination",
): Promise<IngredientCopyCatalog> {
  const read = (entityType: string): Promise<CanonicalRecord[]> =>
    mode === "source"
      ? readVisibleRecords(userId, organizationId, entityType, true)
      : localDb.canonicalRecords
          .where("[userId+organizationId+entityType]")
          .equals([userId, organizationId, entityType])
          .filter((record) => record.lifecycle === "active")
          .toArray();
  const [units, sections, tags] = await Promise.all([
    read("unit_definition"),
    read("store_section"),
    read("dietary_tag"),
  ]);
  const ownedBy = (record: (typeof units)[number], allowGlobal: boolean) => {
    const owner = record.fields.organization_id;
    return owner === organizationId || (allowGlobal && owner === null);
  };
  return {
    units: units
      .filter((record) => {
        return (
          ownedBy(record, true) &&
          text(record.fields, "id") === record.entityId &&
          record.fields.allows_ingredient_quantity === true
        );
      })
      .map((record) => ({
        id: record.entityId,
        name: text(record.fields, "custom_name") ?? text(record.fields, "code") ?? "",
        dimension: text(record.fields, "dimension") ?? "",
        baseUnitFactor: text(record.fields, "base_unit_factor"),
      }))
      .filter((unit) => unit.name && unit.dimension),
    sections: sections
      .filter((record) => ownedBy(record, false))
      .map((record) => ({ id: record.entityId, name: text(record.fields, "name") ?? "" }))
      .filter((section) => section.name),
    dietaryTags: tags
      .filter((record) => ownedBy(record, false))
      .map((record) => ({
        id: record.entityId,
        name: text(record.fields, "name") ?? "",
        seedKey: text(record.fields, "seed_key") ?? null,
      }))
      .filter((tag) => tag.name),
  };
}
