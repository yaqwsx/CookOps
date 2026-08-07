import Dexie, { type EntityTable, type Table } from "dexie";

export type OutboxState = "pending" | "failed";
export type UploadState = "pending" | "failed";
export type SynchronizationActivity =
  | "caughtUp"
  | "syncing"
  | "retrying"
  | "blocked";

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

export interface OutboxCommand {
  id: string;
  userId: string;
  organizationId: string;
  commandType: string;
  payload: Record<string, unknown>;
  actionAt: string;
  createdAt: string;
  state: OutboxState;
  failureReason?: string;
}

export interface PendingUpload {
  id: string;
  userId: string;
  organizationId: string;
  attachmentId: string;
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
  }
}

export const localDb = new CookOpsDatabase();

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
