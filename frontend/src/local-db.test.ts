import { beforeEach, describe, expect, it } from "vitest";

import {
  appendOutboxCommand,
  compareOutboxCommands,
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

  it("assigns durable creation order per user and organization while preserving legacy fallback", async () => {
    const command = {
      userId: "user-a",
      organizationId: "organization-a",
      commandType: "recipe.create",
      payload: {},
      actionAt: "2026-08-07T10:00:00.000Z",
      createdAt: "2026-08-07T10:00:00.000Z",
      state: "pending" as const,
    };
    await appendOutboxCommand({ ...command, id: "z-first" });
    await appendOutboxCommand({ ...command, id: "a-second" });
    await appendOutboxCommand({
      ...command,
      id: "other-organization",
      organizationId: "organization-b",
    });
    const commands = await localDb.outbox.toArray();
    expect(commands.find((item) => item.id === "z-first")?.sequence).toBe(1);
    expect(commands.find((item) => item.id === "a-second")?.sequence).toBe(2);
    expect(
      commands.find((item) => item.id === "other-organization")?.sequence,
    ).toBe(1);
    expect(
      [...commands]
        .filter((item) => item.organizationId === "organization-a")
        .sort(compareOutboxCommands),
    ).toMatchObject([{ id: "z-first" }, { id: "a-second" }]);
  });

  it("keeps command and photo upload state partitioned by organization", async () => {
    await localDb.outbox.bulkAdd([
      {
        id: "mutation-a",
        userId: "user-a",
        organizationId: "organization-a",
        commandType: "event.update",
        payload: { name: "Weekend cook" },
        actionAt: "2026-08-07T10:00:00.000Z",
        createdAt: "2026-08-07T10:00:00.000Z",
        state: "pending",
      },
      {
        id: "mutation-b",
        userId: "user-a",
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
      userId: "user-a",
      organizationId: "organization-a",
      attachmentId: "receipt-a",
      blob: new Blob(["receipt"], { type: "image/jpeg" }),
      createdAt: "2026-08-07T10:02:00.000Z",
      state: "pending",
    });
    await localDb.syncMetadata.bulkAdd([
      {
        userId: "user-a",
        organizationId: "organization-a",
        activity: "syncing",
        cursor: "opaque-a",
      },
      {
        userId: "user-a",
        organizationId: "organization-b",
        activity: "blocked",
        cursor: "opaque-b",
      },
    ]);

    await expect(
      readSynchronizationSummary("organization-a", "user-a"),
    ).resolves.toEqual({
      activity: "syncing",
      pendingCommands: 1,
      pendingUploads: 1,
      failedCommands: 0,
      failedUploads: 0,
      lastSuccessfulServerContact: undefined,
      clockSkewWarning: undefined,
    });
    await expect(
      readSynchronizationSummary("organization-b", "user-a"),
    ).resolves.toEqual({
      activity: "blocked",
      pendingCommands: 0,
      pendingUploads: 0,
      failedCommands: 1,
      failedUploads: 0,
      lastSuccessfulServerContact: undefined,
      clockSkewWarning: undefined,
    });
  });

  it("uses the most actionable activity and preserves a clock-skew warning", async () => {
    await localDb.syncMetadata.bulkAdd([
      {
        userId: "user-a",
        organizationId: "organization-a",
        activity: "caughtUp",
        lastSuccessfulServerContact: "2026-08-07T09:00:00.000Z",
      },
      {
        userId: "user-a",
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
