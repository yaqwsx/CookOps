import { localDb, type CanonicalRecord } from "./local-db";

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
    if (result.get(overlay.entityId)?.lifecycle !== "retired")
      result.set(overlay.entityId, overlay);
  }
  return [...result.values()].filter(
    (record) => includeRetired || record.lifecycle === "active",
  );
}
