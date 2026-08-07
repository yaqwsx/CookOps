import type { EventSummary } from "./api/events";
import { localDb, type CanonicalRecord } from "./local-db";

function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function eventSummary(
  record: CanonicalRecord,
  organizationId: string,
): EventSummary | null {
  const fields = record.fields;
  const name = text(fields.name);
  const startDate = text(fields.start_date);
  const endDate = text(fields.end_date);
  const budgetAmount = text(fields.budget_amount);
  const currency = text(fields.currency);
  const attendance = fields.base_expected_attendance;
  const lifecycle = fields.lifecycle;
  const archivedAt = fields.archived_at;
  const currentArchiveSnapshotId = fields.current_archive_snapshot_id;
  if (
    (fields.id !== undefined && fields.id !== record.entityId) ||
    (fields.organization_id !== undefined &&
      fields.organization_id !== organizationId) ||
    !name ||
    !startDate ||
    !endDate ||
    !budgetAmount ||
    !currency ||
    typeof attendance !== "number" ||
    !Number.isSafeInteger(attendance) ||
    (lifecycle !== "active" && lifecycle !== "archived") ||
    (archivedAt !== null && typeof archivedAt !== "string") ||
    (currentArchiveSnapshotId !== undefined &&
      currentArchiveSnapshotId !== null &&
      typeof currentArchiveSnapshotId !== "string")
  ) {
    return null;
  }
  return {
    id: record.entityId,
    organizationId,
    name,
    startDate,
    endDate,
    baseExpectedAttendance: attendance,
    budgetAmount,
    currency,
    lifecycle,
    archivedAt,
    currentArchiveSnapshotId,
  };
}

/** Read the event projection users see: canonical records plus pending overlays. */
export async function readVisibleEventSummaries(
  userId: string,
  organizationId: string,
): Promise<EventSummary[]> {
  const key = [userId, organizationId, "event"] as const;
  const [canonical, overlays] = await Promise.all([
    localDb.canonicalRecords
      .where("[userId+organizationId+entityType]")
      .equals(key)
      .toArray(),
    localDb.optimisticOverlays
      .where("[userId+organizationId+entityType]")
      .equals(key)
      .toArray(),
  ]);
  const visible = new Map(canonical.map((record) => [record.entityId, record]));
  for (const overlay of overlays) visible.set(overlay.entityId, overlay);
  return [...visible.values()]
    .map((record) => eventSummary(record, organizationId))
    .filter((event): event is EventSummary => event !== null)
    .sort(
      (left, right) =>
        Number(left.lifecycle === "archived") -
          Number(right.lifecycle === "archived") ||
        right.startDate.localeCompare(left.startDate) ||
        left.id.localeCompare(right.id),
    );
}

export async function canCreateEvents(
  userId: string,
  organizationId: string,
): Promise<boolean> {
  const capabilities = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "organization_capabilities",
    organizationId,
  ]);
  return (
    capabilities?.fields.actor_user_id === userId &&
    capabilities.fields.can_manage_organization === true
  );
}
