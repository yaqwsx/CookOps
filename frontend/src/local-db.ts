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
  organizationId: string;
  attachmentId: string;
  blob: Blob;
  createdAt: string;
  state: UploadState;
  failureReason?: string;
}

export interface OrganizationSyncMetadata {
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

export class CookOpsDatabase extends Dexie {
  readonly browserInstallation!: EntityTable<BrowserInstallation, "id">;
  readonly organizations!: EntityTable<CachedOrganization, "id">;
  readonly canonicalRecords!: Table<CanonicalRecord, [string, string, string]>;
  readonly outbox!: EntityTable<OutboxCommand, "id">;
  readonly pendingUploads!: EntityTable<PendingUpload, "id">;
  readonly syncMetadata!: EntityTable<
    OrganizationSyncMetadata,
    "organizationId"
  >;

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
  }
}

export const localDb = new CookOpsDatabase();

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
): Promise<SynchronizationSummary> {
  const inOrganization = <T extends { organizationId: string }>(entry: T) =>
    organizationId === undefined || entry.organizationId === organizationId;
  const [commands, uploads, metadata] = await Promise.all([
    localDb.outbox.toArray(),
    localDb.pendingUploads.toArray(),
    localDb.syncMetadata.toArray(),
  ]);
  const scopedCommands = commands.filter(inOrganization);
  const scopedUploads = uploads.filter(inOrganization);
  const scopedMetadata = metadata.filter(inOrganization);

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
