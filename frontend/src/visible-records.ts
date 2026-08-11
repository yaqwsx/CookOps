import { localDb, type CanonicalRecord } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isExplicitLifecycleRestore(record: CanonicalRecord): boolean {
  const lifecycle = record.fieldClocks.lifecycle;
  if (
    record.lifecycle !== "active" ||
    record.fields.retired_at !== null ||
    lifecycle === null ||
    typeof lifecycle !== "object" ||
    Array.isArray(lifecycle)
  )
    return false;
  const { mutationId, actionAt } = lifecycle as Record<string, unknown>;
  return (
    typeof mutationId === "string" &&
    uuid.test(mutationId) &&
    typeof actionAt === "string" &&
    /^\d{4}-\d{2}-\d{2}T/.test(actionAt) &&
    Number.isFinite(Date.parse(actionAt))
  );
}

/** Merge canonical records with pending local intent, without reviving a retired server record. */
export async function readVisibleRecords(
  userId: string,
  organizationId: string,
  entityType: string,
  includeRetired = false,
): Promise<CanonicalRecord[]> {
  const key = [userId, organizationId, entityType] as const;
  const records = await localDb.canonicalRecords
    .where("[userId+organizationId+entityType]")
    .equals(key)
    .toArray();
  const overlays = await localDb.optimisticOverlays
    .where("[userId+organizationId+entityType]")
    .equals(key)
    .toArray();
  const result = new Map(records.map((record) => [record.entityId, record]));
  for (const overlay of overlays) {
    const canonical = result.get(overlay.entityId);
    const explicitLifecycleRestore =
      (entityType === "receipt" || entityType === "ad_hoc_shopping_item" || entityType === "event_day" || entityType === "event_meal_role" || entityType === "recipe") &&
      isExplicitLifecycleRestore(overlay);
    if (canonical?.lifecycle !== "retired" || explicitLifecycleRestore)
      result.set(overlay.entityId, overlay);
  }
  return [...result.values()].filter(
    (record) => includeRetired || record.lifecycle === "active",
  );
}
