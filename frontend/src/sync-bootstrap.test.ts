import { beforeEach, describe, expect, it, vi } from "vitest";

import { localDb, readVisibleCanonicalRecord } from "./local-db";
import { bootstrapOrganization, pullOrganization } from "./sync-bootstrap";

const userId = "user-a";
const organizationId = "organization-a";

async function clearDatabase() {
  await localDb.transaction(
    "rw",
    [
      localDb.canonicalRecords,
      localDb.bootstrapStaging,
      localDb.optimisticOverlays,
      localDb.outbox,
      localDb.pendingUploads,
      localDb.syncMetadata,
    ],
    async () => {
      await Promise.all([
        localDb.canonicalRecords.clear(),
        localDb.bootstrapStaging.clear(),
        localDb.optimisticOverlays.clear(),
        localDb.outbox.clear(),
        localDb.pendingUploads.clear(),
        localDb.syncMetadata.clear(),
      ]);
    },
  );
}

function response(records: object[], cursor = "new-cursor") {
  return new Response(
    JSON.stringify({
      sync_schema_version: 1,
      server_time: "2026-08-07T12:00:00.000Z",
      cursor,
      records,
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

function record(entityId: string, organization = organizationId) {
  return {
    organization_id: organization,
    entity_id: entityId,
    entity_kind: "event",
    operation: "upsert",
    payload: {
      record_schema_version: 1,
      record: { id: entityId, name: "Current event" },
    },
  };
}

describe("bootstrapOrganization", () => {
  beforeEach(clearDatabase);

  it("keeps the prior cache, user-owned work, and cursor when staging is interrupted", async () => {
    await localDb.canonicalRecords.add({
      userId,
      organizationId,
      entityType: "event",
      entityId: "old-event",
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { name: "Old event" },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T10:00:00.000Z",
    });
    await localDb.syncMetadata.add({
      userId,
      organizationId,
      cursor: "old-cursor",
      activity: "caughtUp",
    });
    await localDb.outbox.add({
      id: "pending",
      userId,
      organizationId,
      commandType: "event.create",
      payload: {},
      actionAt: "2026-08-07T10:00:00.000Z",
      createdAt: "2026-08-07T10:00:00.000Z",
      state: "pending",
    });
    await localDb.pendingUploads.add({
      id: "upload",
      userId,
      organizationId,
      attachmentId: "attachment",
      blob: new Blob(["photo"]),
      createdAt: "2026-08-07T10:00:00.000Z",
      state: "pending",
    });

    await expect(
      bootstrapOrganization(userId, organizationId, {
        fetch: vi.fn<typeof fetch>(async () => response([record("new-event")])),
        beforePublish: () => {
          throw new Error("interrupted");
        },
      }),
    ).rejects.toThrow("interrupted");

    await expect(
      localDb.canonicalRecords
        .where("[userId+organizationId]")
        .equals([userId, organizationId])
        .toArray(),
    ).resolves.toMatchObject([{ entityId: "old-event" }]);
    await expect(localDb.outbox.get("pending")).resolves.toMatchObject({
      state: "pending",
    });
    await expect(localDb.pendingUploads.get("upload")).resolves.toMatchObject({
      state: "pending",
    });
    await expect(
      localDb.syncMetadata.get([userId, organizationId]),
    ).resolves.toMatchObject({ cursor: "old-cursor" });
  });

  it("publishes only the requested user's organization and its durable cursor", async () => {
    await localDb.canonicalRecords.add({
      userId: "user-b",
      organizationId,
      entityType: "event",
      entityId: "private-event",
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {},
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T10:00:00.000Z",
    });
    const send = vi.fn<typeof fetch>(async () =>
      response([record("new-event")]),
    );

    await bootstrapOrganization(userId, organizationId, { fetch: send });

    expect(JSON.parse(String(send.mock.calls[0]?.[1]?.body))).toEqual({
      organization_id: organizationId,
    });
    await expect(
      localDb.canonicalRecords
        .where("[userId+organizationId]")
        .equals([userId, organizationId])
        .toArray(),
    ).resolves.toMatchObject([{ entityId: "new-event" }]);
    await expect(
      localDb.canonicalRecords
        .where("[userId+organizationId]")
        .equals(["user-b", organizationId])
        .toArray(),
    ).resolves.toMatchObject([{ entityId: "private-event" }]);
    await expect(
      localDb.syncMetadata.get([userId, organizationId]),
    ).resolves.toMatchObject({ cursor: "new-cursor" });
  });

  it("replays a pending optimistic event after replacing the canonical snapshot", async () => {
    const command = {
      id: "pending-event",
      userId,
      organizationId,
      commandType: "event.create",
      payload: {
        event_id: "local-event",
        name: "Offline event",
        start_date: "2026-08-08",
        end_date: "2026-08-08",
        base_expected_attendance: 5,
        budget_amount: "100",
      },
      actionAt: "2026-08-07T11:00:00.000Z",
      createdAt: "2026-08-07T11:00:00.000Z",
      state: "pending" as const,
    };
    await localDb.outbox.add(command);

    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([record("server-event")]),
      ),
    });

    await expect(localDb.outbox.get(command.id)).resolves.toEqual(command);
    await expect(
      readVisibleCanonicalRecord(
        userId,
        organizationId,
        "event",
        "local-event",
      ),
    ).resolves.toMatchObject({
      fields: { name: "Offline event" },
      fieldClocks: {
        optimistic: { mutationId: command.id, actionAt: command.actionAt },
      },
      updatedAt: command.actionAt,
    });
  });

  it("reapplies pending intent after a concurrent canonical pull", async () => {
    await localDb.syncMetadata.add({
      userId,
      organizationId,
      cursor: "old-cursor",
      activity: "caughtUp",
    });
    await localDb.outbox.add({
      id: "attendance",
      userId,
      organizationId,
      commandType: "event.update_base_attendance",
      payload: { event_id: "event", base_expected_attendance: 9 },
      actionAt: "2026-08-07T11:00:00.000Z",
      createdAt: "2026-08-07T11:00:00.000Z",
      state: "pending",
    });
    const serverRecord = {
      ...record("event"),
      payload: {
        record_schema_version: 1,
        record: {
          id: "event",
          name: "Current event",
          base_expected_attendance: 3,
        },
      },
    };
    await pullOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(
        async () =>
          new Response(
            JSON.stringify({
              status: "ok",
              sync_schema_version: 1,
              server_time: "2026-08-07T12:00:00.000Z",
              next_cursor: "new-cursor",
              transaction_groups: [
                { records: [{ ...serverRecord, sequence: 1 }] },
              ],
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
      ),
    });

    await expect(
      localDb.canonicalRecords.get([userId, organizationId, "event", "event"]),
    ).resolves.toMatchObject({ fields: { base_expected_attendance: 3 } });
    await expect(
      readVisibleCanonicalRecord(userId, organizationId, "event", "event"),
    ).resolves.toMatchObject({ fields: { base_expected_attendance: 9 } });
  });

  it("replays a dependent event update after its pending creator", async () => {
    await localDb.outbox.bulkAdd([
      {
        id: "create",
        userId,
        organizationId,
        commandType: "event.create",
        payload: {
          event_id: "created-offline",
          name: "Created offline",
          base_expected_attendance: 2,
        },
        actionAt: "2026-08-07T10:00:00.000Z",
        createdAt: "2026-08-07T10:00:00.000Z",
        state: "pending",
      },
      {
        id: "attendance",
        userId,
        organizationId,
        commandType: "event.update_base_attendance",
        payload: { event_id: "created-offline", base_expected_attendance: 7 },
        actionAt: "2026-08-07T10:01:00.000Z",
        createdAt: "2026-08-07T10:01:00.000Z",
        state: "pending",
      },
    ]);

    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () => response([])),
    });

    await expect(
      readVisibleCanonicalRecord(
        userId,
        organizationId,
        "event",
        "created-offline",
      ),
    ).resolves.toMatchObject({ fields: { base_expected_attendance: 7 } });
  });
});
