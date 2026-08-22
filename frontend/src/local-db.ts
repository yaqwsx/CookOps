import Dexie, { type EntityTable, type Table } from "dexie";
import type { EventSummary } from "./api/events";

export type OutboxState = "pending" | "failed";
export type UploadState = "pending" | "uploading" | "failed" | "synchronized";
export type SynchronizationActivity =
  | "caughtUp"
  | "syncing"
  | "retrying"
  | "blocked"
  | "upgradeRequired";

export interface CachedOrganization {
  id: string;
  name: string;
  lastAuthorizedAt: string;
}

export interface BrowserInstallation {
  id: string;
  installationId: string;
}

export interface CanonicalRecord {
  userId: string;
  organizationId: string;
  entityType: string;
  entityId: string;
  recordSchemaVersion: number;
  lifecycle: "active" | "retired" | "tombstone";
  fields: Record<string, unknown>;
  fieldClocks: Record<string, unknown>;
  immutable: boolean;
  updatedAt: string;
}

export interface ArchiveRecord extends CanonicalRecord {
  eventId: string;
  snapshotId: string;
}

export interface CachedArchivedEventSummary extends EventSummary {
  userId: string;
}

export interface OutboxCommand {
  id: string;
  userId: string;
  organizationId: string;
  commandType: string;
  payload: Record<string, unknown>;
  actionAt: string;
  createdAt: string;
  /** Durable local intent order within one user's organization replica. */
  sequence?: number;
  state: OutboxState;
  failureReason?: string;
}

export interface PendingUpload {
  id: string;
  userId: string;
  organizationId: string;
  attachmentId: string;
  receiptId?: string;
  positionKey?: string;
  createMutationId?: string;
  finalizeMutationId?: string;
  replaceAttachmentId?: string;
  serverCreated?: boolean;
  blob: Blob;
  createdAt: string;
  state: UploadState;
  failureReason?: string;
}

export interface OrganizationSyncMetadata {
  userId: string;
  organizationId: string;
  cursor?: string;
  changeCursorHint?: string;
  activity: SynchronizationActivity;
  lastSuccessfulServerContact?: string;
  lastAuthorizedAt?: string;
  clockSkewWarning?: {
    approximateDifferenceSeconds: number;
    serverTime: string;
  };
}

export interface BootstrapStagingRecord extends CanonicalRecord {
  attemptId: string;
}

export class CookOpsDatabase extends Dexie {
  readonly browserInstallation!: EntityTable<BrowserInstallation, "id">;
  readonly organizations!: EntityTable<CachedOrganization, "id">;
  readonly canonicalRecords!: Table<
    CanonicalRecord,
    [string, string, string, string]
  >;
  readonly archiveRecords!: Table<
    ArchiveRecord,
    [string, string, string, string, string, string]
  >;
  readonly archivedEventSummaries!: Table<
    CachedArchivedEventSummary,
    [string, string, string]
  >;
  readonly outbox!: EntityTable<OutboxCommand, "id">;
  readonly pendingUploads!: EntityTable<PendingUpload, "id">;
  readonly bootstrapStaging!: Table<
    BootstrapStagingRecord,
    [string, string, string, string, string]
  >;
  readonly optimisticOverlays!: Table<
    CanonicalRecord,
    [string, string, string, string]
  >;
  readonly syncMetadata!: Table<OrganizationSyncMetadata, [string, string]>;

  constructor(name = "cookops") {
    super(name);
    this.version(1).stores({
      organizations: "id, lastAuthorizedAt",
      canonicalRecords:
        "[organizationId+entityType+entityId], organizationId, [organizationId+entityType], updatedAt",
      outbox: "id, organizationId, [organizationId+state], createdAt",
      pendingUploads: "id, organizationId, [organizationId+state], createdAt",
      syncMetadata: "organizationId",
    });
    this.version(2).stores({ browserInstallation: "id" });
    this.version(3)
      .stores({
        outbox:
          "id, userId, organizationId, [userId+state], [userId+organizationId+state], createdAt",
      })
      .upgrade((transaction) =>
        transaction.table("outbox").toCollection().modify({
          userId: "",
          state: "failed",
          failureReason: "owner_identity_required",
        }),
      );
    this.version(4)
      .stores({
        canonicalRecords:
          "[userId+organizationId+entityType+entityId], [userId+organizationId], organizationId, [userId+organizationId+entityType], updatedAt",
        pendingUploads:
          "id, userId, organizationId, [userId+organizationId+state], createdAt",
        syncMetadata: "[userId+organizationId], userId, organizationId",
      })
      .upgrade((transaction) => {
        transaction
          .table("canonicalRecords")
          .toCollection()
          .modify({ userId: "" });
        transaction
          .table("pendingUploads")
          .toCollection()
          .modify({ userId: "" });
        transaction.table("syncMetadata").toCollection().modify({ userId: "" });
      });
    this.version(5).stores({
      bootstrapStaging:
        "[userId+organizationId+attemptId+entityType+entityId], [userId+organizationId+attemptId]",
    });
    this.version(6).stores({
      optimisticOverlays:
        "[userId+organizationId+entityType+entityId], [userId+organizationId]",
    });
    this.version(7)
      .stores({
        outbox:
          "id, userId, organizationId, [userId+state], [userId+organizationId+state], [userId+organizationId+sequence], createdAt",
      })
      .upgrade(async (transaction) => {
        const outbox = transaction.table("outbox");
        const commands = (await outbox.toArray()) as OutboxCommand[];
        const sequences = new Map<string, number>();
        for (const command of [...commands].sort(
          (left, right) =>
            left.createdAt.localeCompare(right.createdAt) ||
            left.id.localeCompare(right.id),
        )) {
          const key = `${command.userId}:${command.organizationId}`;
          const sequence = (sequences.get(key) ?? 0) + 1;
          sequences.set(key, sequence);
          await outbox.update(command.id, { sequence });
        }
      });
    this.version(8).stores({
      archiveRecords:
        "[userId+organizationId+eventId+snapshotId+entityType+entityId], [userId+organizationId+eventId+snapshotId]",
    });
    this.version(9).stores({
      archivedEventSummaries:
        "[userId+organizationId+id], [userId+organizationId]",
    });
  }
}

export const localDb = new CookOpsDatabase();

export async function readCachedArchivedEventSummaries(
  userId: string,
  organizationId: string,
): Promise<EventSummary[]> {
  const cached = await localDb.archivedEventSummaries
    .where("[userId+organizationId]")
    .equals([userId, organizationId])
    .toArray();
  return cached
    .sort((left, right) => (right.archivedAt ?? "").localeCompare(left.archivedAt ?? "") || left.id.localeCompare(right.id))
    .map(({ userId: _userId, ...summary }) => summary);
}

function isCanonicalTimestamp(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|\+00:00)$/.exec(value);
  if (!match) return false;
  const milliseconds = (match[2] ?? "").slice(0, 3).padEnd(3, "0");
  const timestamp = Date.parse(`${match[1]}.${milliseconds}Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString() === `${match[1]}.${milliseconds}Z`;
}

export async function readHydratedArchivedEventIds(
  userId: string,
  organizationId: string,
  summaries: Pick<EventSummary, "id" | "currentArchiveSnapshotId">[],
): Promise<Set<string>> {
  const hydrated = new Set<string>();
  for (const summary of summaries) {
    if (!summary.currentArchiveSnapshotId) continue;
    const marker = await localDb.archiveRecords.get([
      userId,
      organizationId,
      summary.id,
      summary.currentArchiveSnapshotId,
      "event_archive_snapshot",
      `archive:${summary.currentArchiveSnapshotId}`,
    ]);
    if (marker?.lifecycle === "active" && marker.fields.snapshot_id === summary.currentArchiveSnapshotId) hydrated.add(summary.id);
  }
  return hydrated;
}

export async function cacheArchivedEventSummaries(
  userId: string,
  organizationId: string,
  summaries: EventSummary[],
): Promise<void> {
  if (summaries.some((summary) =>
    summary.organizationId !== organizationId ||
    !summary.id ||
    (summary.lifecycle === "archived" && !isCanonicalTimestamp(summary.archivedAt)) ||
    (summary.lifecycle !== "archived" && summary.lifecycle !== "active")
  )) return;
  await localDb.transaction("rw", localDb.archivedEventSummaries, async () => {
    await localDb.archivedEventSummaries.bulkDelete(
      summaries.filter((summary) => summary.lifecycle === "active").map((summary) => [userId, organizationId, summary.id] as [string, string, string]),
    );
    await localDb.archivedEventSummaries.bulkPut(summaries.filter((summary) => summary.lifecycle === "archived").map((summary) => ({ ...summary, userId, organizationId })));
  });
}

export const OFFLINE_AUTHORIZATION_LEASE_MS = 7 * 24 * 60 * 60 * 1000;

export function hasValidOfflineAuthorization(lastAuthorizedAt: unknown, now = new Date()): boolean {
  if (typeof lastAuthorizedAt !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(lastAuthorizedAt)) return false;
  const authorized = Date.parse(lastAuthorizedAt);
  return Number.isFinite(authorized) && new Date(authorized).toISOString() === lastAuthorizedAt && Number.isFinite(now.getTime()) && now.getTime() >= authorized && now.getTime() - authorized <= OFFLINE_AUTHORIZATION_LEASE_MS;
}

export async function readOfflineAuthorization(userId: string, organizationId: string, now = new Date()): Promise<boolean> {
  const metadata = await localDb.syncMetadata.get([userId, organizationId]);
  return hasValidOfflineAuthorization(metadata?.lastAuthorizedAt, now);
}

export async function readCachedOrganizations(userId: string): Promise<{ id: string; name: string }[]> {
  const [canonical, overlays] = await Promise.all([
    localDb.canonicalRecords
      .where("[userId+organizationId]")
      .between([userId, Dexie.minKey], [userId, Dexie.maxKey])
      .toArray(),
    localDb.optimisticOverlays
      .where("[userId+organizationId]")
      .between([userId, Dexie.minKey], [userId, Dexie.maxKey])
      .toArray(),
  ]);
  const visible = new Map(
    canonical
      .filter((record) => record.entityType === "organization")
      .map((record) => [record.organizationId, record] as const),
  );
  for (const record of overlays) {
    if (record.entityType === "organization") visible.set(record.organizationId, record);
  }
  return [...visible.values()]
    .filter((record) => typeof record.fields.name === "string")
    .map((record) => ({ id: record.organizationId, name: record.fields.name as string }));
}

/** Append while holding the outbox transaction, preserving dependency order across equal timestamps. */
export async function appendOutboxCommand(
  command: Omit<OutboxCommand, "sequence">,
): Promise<void> {
  const existing = await localDb.outbox
    .where("[userId+organizationId+sequence]")
    .between(
      [command.userId, command.organizationId, Dexie.minKey],
      [command.userId, command.organizationId, Dexie.maxKey],
    )
    .toArray();
  const sequence =
    existing.reduce(
      (latest, item) =>
        typeof item.sequence === "number"
          ? Math.max(latest, item.sequence)
          : latest,
      0,
    ) + 1;
  await localDb.outbox.add({ ...command, sequence });
}

export async function readFailedOutboxCommands(
  userId: string,
  organizationId: string,
): Promise<OutboxCommand[]> {
  return localDb.outbox
    .where("[userId+organizationId+state]")
    .equals([userId, organizationId, "failed"])
    .toArray();
}

export async function discardFailedOutboxCommand(
  userId: string,
  organizationId: string,
  commandId: string,
): Promise<boolean> {
  return localDb.transaction("rw", localDb.outbox, async () => {
    const command = await localDb.outbox.get(commandId);
    if (
      command?.state !== "failed" ||
      command.userId !== userId ||
      command.organizationId !== organizationId
    )
      return false;
    await localDb.outbox.delete(commandId);
    return true;
  });
}

export interface RecoverableIntent {
  schema: "cookops.recoverable-intent";
  version: 1;
  commandId: string;
  commandType: string;
  actionAt: string;
  failureCode: string;
  payload: Record<string, unknown>;
}

export function toRecoverableIntent(command: OutboxCommand): RecoverableIntent {
  const payload = JSON.parse(JSON.stringify(command.payload)) as unknown;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("Invalid outbox payload.");
  return {
    schema: "cookops.recoverable-intent",
    version: 1,
    commandId: command.id,
    commandType: command.commandType,
    actionAt: command.actionAt,
    failureCode: command.failureReason ?? "unknown",
    payload: payload as Record<string, unknown>,
  };
}

/** Legacy records retain their pre-v7 timestamp order until Dexie upgrades them. */
export function compareOutboxCommands(
  left: OutboxCommand,
  right: OutboxCommand,
): number {
  if (typeof left.sequence === "number" && typeof right.sequence === "number")
    return left.sequence - right.sequence;
  return (
    left.createdAt.localeCompare(right.createdAt) ||
    left.id.localeCompare(right.id)
  );
}

/** Read the projection users see: authoritative records plus their pending overlay. */
export async function readVisibleCanonicalRecord(
  userId: string,
  organizationId: string,
  entityType: string,
  entityId: string,
): Promise<CanonicalRecord | undefined> {
  return (
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      entityType,
      entityId,
    ])) ??
    localDb.canonicalRecords.get([userId, organizationId, entityType, entityId])
  );
}

export async function readOrCreateBrowserInstallationId(
  userId: string,
): Promise<string> {
  const current = await localDb.browserInstallation.get(userId);
  if (current) return current.installationId;
  const installationId = crypto.randomUUID();
  try {
    await localDb.browserInstallation.add({ id: userId, installationId });
    return installationId;
  } catch {
    const concurrent = await localDb.browserInstallation.get(userId);
    if (concurrent) return concurrent.installationId;
    throw new Error("Unable to persist browser installation.");
  }
}

export interface SynchronizationSummary {
  activity: SynchronizationActivity;
  pendingCommands: number;
  pendingUploads: number;
  failedCommands: number;
  failedUploads: number;
  lastSuccessfulServerContact?: string;
  clockSkewWarning?: OrganizationSyncMetadata["clockSkewWarning"];
}

function chooseActivity(
  activities: SynchronizationActivity[],
): SynchronizationActivity {
  if (activities.includes("upgradeRequired")) return "upgradeRequired";
  if (activities.includes("blocked")) return "blocked";
  if (activities.includes("retrying")) return "retrying";
  if (activities.includes("syncing")) return "syncing";
  return "caughtUp";
}

function latestContact(
  metadata: OrganizationSyncMetadata[],
): string | undefined {
  return metadata.reduce<string | undefined>((latest, entry) => {
    if (!entry.lastSuccessfulServerContact) return latest;
    if (!latest || entry.lastSuccessfulServerContact > latest) {
      return entry.lastSuccessfulServerContact;
    }
    return latest;
  }, undefined);
}

export async function readSynchronizationSummary(
  organizationId?: string,
  userId?: string,
): Promise<SynchronizationSummary> {
  const inOrganization = <T extends { organizationId: string }>(entry: T) =>
    organizationId === undefined || entry.organizationId === organizationId;
  const [commands, uploads, metadata] = await Promise.all([
    localDb.outbox.toArray(),
    localDb.pendingUploads.toArray(),
    localDb.syncMetadata.toArray(),
  ]);
  const scopedCommands = commands.filter(
    (command) =>
      inOrganization(command) &&
      (userId === undefined || command.userId === userId),
  );
  const scopedUploads = uploads.filter(
    (upload) =>
      inOrganization(upload) &&
      (userId === undefined || upload.userId === userId),
  );
  const scopedMetadata = metadata.filter(
    (entry) =>
      inOrganization(entry) &&
      (userId === undefined || entry.userId === userId),
  );

  return {
    activity: chooseActivity(scopedMetadata.map((entry) => entry.activity)),
    pendingCommands: scopedCommands.filter((entry) => entry.state === "pending")
      .length,
    pendingUploads: scopedUploads.filter((entry) => entry.state === "pending")
      .length,
    failedCommands: scopedCommands.filter((entry) => entry.state === "failed")
      .length,
    failedUploads: scopedUploads.filter((entry) => entry.state === "failed")
      .length,
    lastSuccessfulServerContact: latestContact(scopedMetadata),
    clockSkewWarning: scopedMetadata.find((entry) => entry.clockSkewWarning)
      ?.clockSkewWarning,
  };
}
