import { beforeEach, describe, expect, it } from "vitest";

import {
  localDb,
  readOrCreateBrowserInstallationId,
  readSynchronizationSummary,
} from "./local-db";

async function clearLocalDatabase() {
  await localDb.transaction(
    "rw",
    [
      localDb.browserInstallation,
      localDb.organizations,
      localDb.canonicalRecords,
      localDb.outbox,
      localDb.pendingUploads,
      localDb.syncMetadata,
    ],
    async () => {
      await Promise.all([
        localDb.organizations.clear(),
        localDb.browserInstallation.clear(),
        localDb.canonicalRecords.clear(),
        localDb.outbox.clear(),
        localDb.pendingUploads.clear(),
        localDb.syncMetadata.clear(),
      ]);
    },
  );
}

describe("local synchronization database", () => {
  beforeEach(clearLocalDatabase);

  it("persists separate browser installation identities for each user", async () => {
    const first = await readOrCreateBrowserInstallationId("user-a");

    await expect(readOrCreateBrowserInstallationId("user-a")).resolves.toBe(
      first,
    );
    expect(await readOrCreateBrowserInstallationId("user-b")).not.toBe(first);
    await expect(localDb.browserInstallation.get("user-a")).resolves.toEqual({
      id: "user-a",
      installationId: first,
    });
  });

  it("keeps command and photo upload state partitioned by organization", async () => {
    await localDb.outbox.bulkAdd([
      {
        id: "mutation-a",
        organizationId: "organization-a",
        commandType: "event.update",
        payload: { name: "Weekend cook" },
        actionAt: "2026-08-07T10:00:00.000Z",
        createdAt: "2026-08-07T10:00:00.000Z",
        state: "pending",
      },
      {
        id: "mutation-b",
        organizationId: "organization-b",
        commandType: "recipe.publish",
        payload: { name: "Soup" },
        actionAt: "2026-08-07T10:01:00.000Z",
        createdAt: "2026-08-07T10:01:00.000Z",
        state: "failed",
        failureReason: "validation_failed",
      },
    ]);
    await localDb.pendingUploads.add({
      id: "upload-a",
      organizationId: "organization-a",
      attachmentId: "receipt-a",
      blob: new Blob(["receipt"], { type: "image/jpeg" }),
      createdAt: "2026-08-07T10:02:00.000Z",
      state: "pending",
    });
    await localDb.syncMetadata.bulkAdd([
      {
        organizationId: "organization-a",
        activity: "syncing",
        cursor: "opaque-a",
      },
      {
        organizationId: "organization-b",
        activity: "blocked",
        cursor: "opaque-b",
      },
    ]);

    await expect(readSynchronizationSummary("organization-a")).resolves.toEqual(
      {
        activity: "syncing",
        pendingCommands: 1,
        pendingUploads: 1,
        failedCommands: 0,
        failedUploads: 0,
        lastSuccessfulServerContact: undefined,
        clockSkewWarning: undefined,
      },
    );
    await expect(readSynchronizationSummary("organization-b")).resolves.toEqual(
      {
        activity: "blocked",
        pendingCommands: 0,
        pendingUploads: 0,
        failedCommands: 1,
        failedUploads: 0,
        lastSuccessfulServerContact: undefined,
        clockSkewWarning: undefined,
      },
    );
  });

  it("uses the most actionable activity and preserves a clock-skew warning", async () => {
    await localDb.syncMetadata.bulkAdd([
      {
        organizationId: "organization-a",
        activity: "caughtUp",
        lastSuccessfulServerContact: "2026-08-07T09:00:00.000Z",
      },
      {
        organizationId: "organization-b",
        activity: "retrying",
        lastSuccessfulServerContact: "2026-08-07T10:00:00.000Z",
        clockSkewWarning: {
          approximateDifferenceSeconds: 400,
          serverTime: "2026-08-07T10:00:00.000Z",
        },
      },
    ]);

    await expect(readSynchronizationSummary()).resolves.toEqual({
      activity: "retrying",
      pendingCommands: 0,
      pendingUploads: 0,
      failedCommands: 0,
      failedUploads: 0,
      lastSuccessfulServerContact: "2026-08-07T10:00:00.000Z",
      clockSkewWarning: {
        approximateDifferenceSeconds: 400,
        serverTime: "2026-08-07T10:00:00.000Z",
      },
    });
  });
});
