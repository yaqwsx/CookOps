import type { EventSummary } from "./api/events";
import { parseCalendarDate } from "./event-create";
import { type CanonicalRecord, localDb } from "./local-db";
import { readVisibleRecords } from "./visible-records";

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
  const validStartDate = parseCalendarDate(startDate);
  const validEndDate = parseCalendarDate(endDate);
  const budgetAmount = text(fields.budget_amount);
  const currency = text(fields.currency);
  const location =
    fields.location === null || fields.location === undefined
      ? null
      : text(fields.location);
  const generalNote =
    fields.general_note === null || fields.general_note === undefined
      ? null
      : text(fields.general_note);
  const attendance = fields.base_expected_attendance;
  const lifecycle = fields.lifecycle;
  const archivedAt = fields.archived_at;
  const currentArchiveSnapshotId = fields.current_archive_snapshot_id;
  if (
    (fields.id !== undefined && fields.id !== record.entityId) ||
    (fields.organization_id !== undefined &&
      fields.organization_id !== organizationId) ||
    !name ||
    validStartDate === undefined ||
    validEndDate === undefined ||
    validEndDate < validStartDate ||
    (Date.parse(`${validEndDate}T00:00:00Z`) - Date.parse(`${validStartDate}T00:00:00Z`)) / 86400000 >= 366 ||
    !budgetAmount ||
    !currency ||
    (fields.location !== undefined && location === null) ||
    (fields.general_note !== undefined && generalNote === null) ||
    typeof attendance !== "number" ||
    !Number.isSafeInteger(attendance) ||
    attendance < 0 ||
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
    startDate: validStartDate,
    endDate: validEndDate,
    baseExpectedAttendance: attendance,
    budgetAmount,
    location,
    generalNote,
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
  return (await readVisibleRecords(userId, organizationId, "event", true))
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

export async function readEventCapabilities(
  userId: string,
  organizationId: string,
): Promise<{ canManage: boolean; canDuplicate: boolean }> {
  const capabilities = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "organization_capabilities",
    organizationId,
  ]);
  const trusted = (
    capabilities?.lifecycle === "active" &&
    capabilities.fields.actor_user_id === userId
  );
  return {
    canManage: trusted && capabilities.fields.can_manage_organization === true,
    canDuplicate: trusted && (capabilities.fields.role === "member" || capabilities.fields.role === "organization_admin" || capabilities.fields.role === "system_admin"),
  };
}

export async function canCreateEvents(
  userId: string,
  organizationId: string,
): Promise<boolean> {
  return (await readEventCapabilities(userId, organizationId)).canManage;
}

/** Event members may duplicate archived snapshots; lifecycle remains admin-only. */
export async function canDuplicateEvents(
  userId: string,
  organizationId: string,
): Promise<boolean> {
  return (await readEventCapabilities(userId, organizationId)).canDuplicate;
}
