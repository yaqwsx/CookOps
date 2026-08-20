import { beforeEach, describe, expect, it, vi } from "vitest";

import { localDb, readVisibleCanonicalRecord } from "./local-db";
import { readVisibleRecords } from "./visible-records";
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

function pullResponse(records: object[], nextCursor = "next-cursor") {
  return new Response(
    JSON.stringify({
      status: "ok",
      sync_schema_version: 1,
      server_time: "2026-08-07T12:00:00.000Z",
      next_cursor: nextCursor,
      transaction_groups: [{ records }],
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

function organizationRecord() {
  return {
    organization_id: organizationId,
    entity_id: organizationId,
    entity_kind: "organization",
    operation: "upsert",
    payload: {
      record_schema_version: 1,
      record: { id: organizationId, default_currency: "CZK" },
    },
  };
}

describe("bootstrapOrganization", () => {
  beforeEach(clearDatabase);

  it("accepts dietary exception and association records and replays pending create", async () => {
    const eventId = "00000000-0000-4000-8000-000000000041";
    const tagId = "00000000-0000-4000-8000-000000000042";
    const exceptionId = "00000000-0000-4000-8000-000000000043";
    await localDb.canonicalRecords.bulkPut([
      {
        userId,
        organizationId,
        entityType: "event",
        entityId: eventId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: { id: eventId, lifecycle: "active" },
        fieldClocks: {},
        immutable: false,
        updatedAt: "2026-08-07T10:00:00.000Z",
      },
      {
        userId,
        organizationId,
        entityType: "dietary_tag",
        entityId: tagId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: { id: tagId, name: "Vegan" },
        fieldClocks: {},
        immutable: false,
        updatedAt: "2026-08-07T10:00:00.000Z",
      },
    ]);
    await localDb.outbox.add({
      id: "00000000-0000-4000-8000-000000000044",
      userId,
      organizationId,
      commandType: "event_dietary_exception.create",
      payload: {
        event_id: eventId,
        exception_id: exceptionId,
        name: "Alex",
        note: null,
        tag_ids: [tagId],
      },
      actionAt: "2026-08-07T11:00:00.000Z",
      createdAt: "2026-08-07T11:00:00.000Z",
      state: "pending",
    });
    await localDb.outbox.add({
      id: "00000000-0000-4000-8000-000000000045",
      userId,
      organizationId,
      commandType: "event_dietary_exception.update",
      payload: { event_id: eventId, exception_id: exceptionId, name: "Updated", note: "Changed", tag_ids: [tagId] },
      actionAt: "2026-08-07T11:01:00.000Z",
      createdAt: "2026-08-07T11:01:00.000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          organizationRecord(),
          {
            organization_id: organizationId,
            entity_id: eventId,
            entity_kind: "event",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: { id: eventId, lifecycle: "active", archived_at: null },
            },
          },
          {
            organization_id: organizationId,
            entity_id: tagId,
            entity_kind: "dietary_tag",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: { id: tagId, name: "Vegan", retired_at: null },
            },
          },
          {
            organization_id: organizationId,
            entity_id: exceptionId,
            entity_kind: "event_dietary_exception",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: exceptionId,
                event_id: eventId,
                name: "Alex",
                note: null,
                retired_at: null,
              },
            },
          },
          {
            organization_id: organizationId,
            entity_id: `${exceptionId}:${tagId}`,
            entity_kind: "event_dietary_exception_tag",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: `${exceptionId}:${tagId}`,
                exception_id: exceptionId,
                dietary_tag_id: tagId,
                retired_at: null,
              },
            },
          },
        ]),
      ),
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "event_dietary_exception",
        exceptionId,
      ]),
    ).resolves.toMatchObject({ fields: { name: "Updated", note: "Changed", tag_ids: [tagId] } });
  });

  it("replays a pending recipe create as both root and immutable initial version", async () => {
    const recipeId = "3d8b2b21-c378-4574-9e46-9338c81305ef";
    const versionId = "4d8b2b21-c378-4574-9e46-9338c81305ef";
    const unitId = "5d8b2b21-c378-4574-9e46-9338c81305ef";
    await localDb.outbox.add({
      id: "recipe-create",
      userId,
      organizationId,
      commandType: "recipe.create",
      payload: {
        recipe_id: recipeId,
        recipe_version_id: versionId,
        name: "Pending soup",
        scaling_unit_id: unitId,
        base_scaling_amount: "4",
        ingredient_lines: [],
      },
      actionAt: "2026-08-07T11:00:00.000Z",
      createdAt: "2026-08-07T11:00:00.000Z",
      state: "pending",
    });

    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () => response([organizationRecord()])),
    });

    await expect(
      readVisibleCanonicalRecord(userId, organizationId, "recipe", recipeId),
    ).resolves.toMatchObject({
      fields: { current_version_id: versionId, name: "Pending soup" },
    });
    await expect(
      readVisibleCanonicalRecord(
        userId,
        organizationId,
        "recipe_version",
        versionId,
      ),
    ).resolves.toMatchObject({
      immutable: true,
      fields: { recipe_id: recipeId, name: "Pending soup" },
    });
  });

  it("replays a pending ingredient create as both root and immutable initial version", async () => {
    const ingredientId = "3d8b2b21-c378-4574-9e46-9338c81305ef";
    const versionId = "4d8b2b21-c378-4574-9e46-9338c81305ef";
    const unitId = "5d8b2b21-c378-4574-9e46-9338c81305ef";
    await localDb.outbox.add({
      id: "ingredient-create",
      userId,
      organizationId,
      commandType: "ingredient.create",
      payload: {
        ingredient_id: ingredientId,
        ingredient_version_id: versionId,
        name: "Pending tomatoes",
        canonical_unit_id: unitId,
        mass_per_canonical_quantity: "1",
        dietary_tag_ids: [],
      },
      actionAt: "2026-08-07T11:00:00.000Z",
      createdAt: "2026-08-07T11:00:00.000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          organizationRecord(),
          {
            organization_id: organizationId,
            entity_id: unitId,
            entity_kind: "unit_definition",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: unitId,
                organization_id: null,
                code: "g",
                dimension: "mass",
                base_unit_factor: "1",
                allows_ingredient_quantity: true,
              },
            },
          },
        ]),
      ),
    });
    await expect(
      readVisibleCanonicalRecord(
        userId,
        organizationId,
        "ingredient",
        ingredientId,
      ),
    ).resolves.toMatchObject({ fields: { current_version_id: versionId } });
    await expect(
      readVisibleCanonicalRecord(
        userId,
        organizationId,
        "ingredient_version",
        versionId,
      ),
    ).resolves.toMatchObject({
      immutable: true,
      fields: { ingredient_id: ingredientId, name: "Pending tomatoes" },
    });
  });

  it("does not replay an ingredient create without an active usable unit", async () => {
    await localDb.outbox.add({
      id: "ingredient-without-unit",
      userId,
      organizationId,
      commandType: "ingredient.create",
      payload: {
        ingredient_id: "3d8b2b21-c378-4574-9e46-9338c81305ef",
        ingredient_version_id: "4d8b2b21-c378-4574-9e46-9338c81305ef",
        name: "Pending tomatoes",
        canonical_unit_id: "5d8b2b21-c378-4574-9e46-9338c81305ef",
        mass_per_canonical_quantity: "1",
        dietary_tag_ids: [],
      },
      actionAt: "2026-08-07T11:00:00.000Z",
      createdAt: "2026-08-07T11:00:00.000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () => response([organizationRecord()])),
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("does not replay an invalid ingredient mass as a false optimistic projection", async () => {
    await localDb.outbox.add({
      id: "invalid-ingredient",
      userId,
      organizationId,
      commandType: "ingredient.create",
      payload: {
        ingredient_id: "3d8b2b21-c378-4574-9e46-9338c81305ef",
        ingredient_version_id: "4d8b2b21-c378-4574-9e46-9338c81305ef",
        name: "Invalid",
        canonical_unit_id: "5d8b2b21-c378-4574-9e46-9338c81305ef",
        mass_per_canonical_quantity: "0",
        dietary_tag_ids: [],
      },
      actionAt: "2026-08-07T11:00:00.000Z",
      createdAt: "2026-08-07T11:00:00.000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () => response([])),
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("replays same-millisecond dependent commands by durable sequence", async () => {
    await localDb.outbox.bulkAdd([
      {
        id: "z-create",
        userId,
        organizationId,
        commandType: "event.create",
        payload: {
          event_id: "event",
          name: "Created first",
          start_date: "2026-08-10",
          end_date: "2026-08-10",
          base_expected_attendance: 2,
          budget_amount: "0",
        },
        actionAt: "2026-08-07T11:00:00.000Z",
        createdAt: "2026-08-07T11:00:00.000Z",
        sequence: 1,
        state: "pending",
      },
      {
        id: "a-update",
        userId,
        organizationId,
        commandType: "event.update_base_attendance",
        payload: { event_id: "event", base_expected_attendance: 9 },
        actionAt: "2026-08-07T11:00:00.000Z",
        createdAt: "2026-08-07T11:00:00.000Z",
        sequence: 2,
        state: "pending",
      },
    ]);

    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () => response([organizationRecord()])),
    });

    await expect(
      readVisibleCanonicalRecord(userId, organizationId, "event", "event"),
    ).resolves.toMatchObject({ fields: { base_expected_attendance: 9 } });
  });

  it("keeps bootstrap event metadata clocks when replaying stale pending metadata", async () => {
    const eventId = "3d8b2b21-c378-4574-9e46-9338c81305ef";
    const mutationId = "4d8b2b21-c378-4574-9e46-9338c81305ef";
    await localDb.outbox.add({
      id: mutationId,
      userId,
      organizationId,
      commandType: "event.metadata",
      payload: {
        event_id: eventId,
        name: "Stale name",
        location: null,
        budget_amount: "10",
        general_note: null,
      },
      actionAt: "2026-08-07T11:00:00.000000Z",
      createdAt: "2026-08-07T11:00:00.000000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          organizationRecord(),
          {
            organization_id: organizationId,
            entity_id: eventId,
            entity_kind: "event",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: eventId,
                organization_id: organizationId,
                name: "Canonical name",
                start_date: "2026-08-10",
                end_date: "2026-08-10",
                location: null,
                general_note: null,
                base_expected_attendance: 2,
                budget_amount: "10",
                currency: "CZK",
                lifecycle: "active",
                archived_at: null,
                field_clocks: {
                  name: {
                    winning_client_wall_time: "2026-08-07T11:00:00.000001Z",
                    winning_mutation_id: "5d8b2b21-c378-4574-9e46-9338c81305ef",
                  },
                  location: {
                    winning_client_wall_time: "2026-08-07T11:00:00.000001Z",
                    winning_mutation_id: "5d8b2b21-c378-4574-9e46-9338c81305ef",
                  },
                  budget_amount: {
                    winning_client_wall_time: "2026-08-07T11:00:00.000001Z",
                    winning_mutation_id: "5d8b2b21-c378-4574-9e46-9338c81305ef",
                  },
                  general_note: {
                    winning_client_wall_time: "2026-08-07T11:00:00.000001Z",
                    winning_mutation_id: "5d8b2b21-c378-4574-9e46-9338c81305ef",
                  },
                },
              },
            },
          },
        ]),
      ),
    });
    await expect(
      readVisibleCanonicalRecord(userId, organizationId, "event", eventId),
    ).resolves.toMatchObject({ fields: { name: "Canonical name" } });
    await expect(
      localDb.optimisticOverlays.get([userId, organizationId, "event", eventId]),
    ).resolves.toBeUndefined();
  });

  it("replays a manual day after its pending event creator", async () => {
    const eventId = "3d8b2b21-c378-4574-9e46-9338c81305ef";
    const dayId = "4d8b2b21-c378-4574-9e46-9338c81305ef";
    await localDb.outbox.bulkAdd([
      {
        id: "5d8b2b21-c378-4574-9e46-9338c81305ef",
        userId,
        organizationId,
        commandType: "event.create",
        payload: { event_id: eventId, name: "Offline event" },
        actionAt: "2026-08-07T11:00:00.000Z",
        createdAt: "2026-08-07T11:00:00.000Z",
        sequence: 1,
        state: "pending",
      },
      {
        id: dayId,
        userId,
        organizationId,
        commandType: "event_day.create",
        payload: { event_day_id: dayId, event_id: eventId, calendar_date: "2026-08-11" },
        actionAt: "2026-08-07T11:00:00.000Z",
        createdAt: "2026-08-07T11:00:00.000Z",
        sequence: 2,
        state: "pending",
      },
    ]);

    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () => response([organizationRecord()])),
    });

    await expect(
      readVisibleCanonicalRecord(userId, organizationId, "event_day", dayId),
    ).resolves.toMatchObject({
      fields: { event_id: eventId, calendar_date: "2026-08-11" },
    });
  });

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
        response([organizationRecord(), record("server-event")]),
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
      fields: { name: "Offline event", currency: "CZK" },
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

  it("converges duplicate change pulls without duplicating canonical records or losing outbox work", async () => {
    await localDb.syncMetadata.add({
      userId,
      organizationId,
      cursor: "old-cursor",
      activity: "caughtUp",
    });
    const command = {
      id: "pending-command",
      userId,
      organizationId,
      commandType: "event.update_base_attendance" as const,
      payload: { event_id: "event", base_expected_attendance: 9 },
      actionAt: "2026-08-07T11:00:00.000Z",
      createdAt: "2026-08-07T11:00:00.000Z",
      state: "pending" as const,
    };
    await localDb.outbox.add(command);
    const send = vi.fn<typeof fetch>(async () =>
      pullResponse([{ ...record("event"), sequence: 1 }], "new-cursor"),
    );

    await pullOrganization(userId, organizationId, { fetch: send });
    await pullOrganization(userId, organizationId, { fetch: send });

    const records = (await localDb.canonicalRecords.toArray()).filter(
      (entry) =>
        entry.userId === userId &&
        entry.organizationId === organizationId &&
        entry.entityType === "event" &&
        entry.entityId === "event",
    );
    expect(records).toHaveLength(1);
    await expect(localDb.syncMetadata.get([userId, organizationId])).resolves.toMatchObject({
      cursor: "new-cursor",
      activity: "caughtUp",
    });
    await expect(localDb.outbox.get(command.id)).resolves.toEqual(command);
  });

  it("does not replay attendance over a canonical archived event", async () => {
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

    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          {
            ...record("event"),
            payload: {
              record_schema_version: 1,
              record: {
                id: "event",
                lifecycle: "archived",
                archived_at: "2026-08-07T12:00:00.000Z",
                base_expected_attendance: 3,
              },
            },
          },
        ]),
      ),
    });

    await expect(
      readVisibleCanonicalRecord(userId, organizationId, "event", "event"),
    ).resolves.toMatchObject({
      lifecycle: "retired",
      fields: { lifecycle: "archived", base_expected_attendance: 3 },
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("does not replay a schedule through a stale event overlay after archival", async () => {
    const ids = {
      event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
      day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
      role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
      recipe: "6d8b2b21-c378-4574-9e46-9338c81305ef",
      version: "7d8b2b21-c378-4574-9e46-9338c81305ef",
      scheduled: "8d8b2b21-c378-4574-9e46-9338c81305ef",
    };
    await localDb.optimisticOverlays.add({
      userId,
      organizationId,
      entityType: "event",
      entityId: ids.event,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: ids.event,
        lifecycle: "active",
        base_expected_attendance: 9,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T11:00:00.000Z",
    });
    await localDb.outbox.add({
      id: "schedule",
      userId,
      organizationId,
      commandType: "scheduled_recipe.schedule",
      payload: {
        scheduled_recipe_id: ids.scheduled,
        event_id: ids.event,
        event_day_id: ids.day,
        event_meal_role_id: ids.role,
        recipe_id: ids.recipe,
        recipe_version_id: ids.version,
      },
      actionAt: "2026-08-07T11:00:00.000Z",
      createdAt: "2026-08-07T11:00:00.000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          {
            organization_id: organizationId,
            entity_id: ids.event,
            entity_kind: "event",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: ids.event,
                lifecycle: "archived",
                archived_at: "now",
              },
            },
          },
        ]),
      ),
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "scheduled_recipe",
        ids.scheduled,
      ]),
    ).resolves.toBeUndefined();
  });

  it("replays a schedule after its pending attendance update", async () => {
    const ids = {
      event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
      day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
      role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
      recipe: "6d8b2b21-c378-4574-9e46-9338c81305ef",
      version: "7d8b2b21-c378-4574-9e46-9338c81305ef",
      scheduled: "8d8b2b21-c378-4574-9e46-9338c81305ef",
    };
    await localDb.outbox.bulkAdd([
      {
        id: "attendance",
        userId,
        organizationId,
        commandType: "event.update_base_attendance",
        payload: { event_id: ids.event, base_expected_attendance: 20 },
        actionAt: "2026-08-07T10:00:00.000Z",
        createdAt: "2026-08-07T10:00:00.000Z",
        state: "pending",
      },
      {
        id: "schedule",
        userId,
        organizationId,
        commandType: "scheduled_recipe.schedule",
        payload: {
          scheduled_recipe_id: ids.scheduled,
          event_id: ids.event,
          event_day_id: ids.day,
          event_meal_role_id: ids.role,
          recipe_id: ids.recipe,
          recipe_version_id: ids.version,
        },
        actionAt: "2026-08-07T10:01:00.000Z",
        createdAt: "2026-08-07T10:01:00.000Z",
        state: "pending",
      },
    ]);
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          {
            organization_id: organizationId,
            entity_id: ids.event,
            entity_kind: "event",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: ids.event,
                lifecycle: "active",
                base_expected_attendance: 12,
              },
            },
          },
        ]),
      ),
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "scheduled_recipe",
        ids.scheduled,
      ]),
    ).resolves.toMatchObject({ fields: { diner_count: 20 } });
  });

  it("replays a pending scheduled-recipe move over canonical placement clocks", async () => {
    const ids = {
      event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
      day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
      role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
      scheduled: "8d8b2b21-c378-4574-9e46-9338c81305ef",
    };
    await localDb.outbox.add({
      id: "move",
      userId,
      organizationId,
      commandType: "scheduled_recipe.move",
      payload: {
        scheduled_recipe_id: ids.scheduled,
        event_id: ids.event,
        event_day_id: ids.day,
        event_meal_role_id: ids.role,
        position_key: "z9",
      },
      actionAt: "2026-08-07T10:01:00.000Z",
      createdAt: "2026-08-07T10:01:00.000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          {
            organization_id: organizationId,
            entity_id: ids.event,
            entity_kind: "event",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: { id: ids.event, lifecycle: "active" },
            },
          },
          {
            organization_id: organizationId,
            entity_id: ids.day,
            entity_kind: "event_day",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: { id: ids.day, event_id: ids.event, retired_at: null },
            },
          },
          {
            organization_id: organizationId,
            entity_id: ids.role,
            entity_kind: "event_meal_role",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: { id: ids.role, event_id: ids.event, retired_at: null },
            },
          },
          {
            organization_id: organizationId,
            entity_id: ids.scheduled,
            entity_kind: "scheduled_recipe",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: ids.scheduled,
                event_id: ids.event,
                event_day_id: ids.day,
                event_meal_role_id: ids.role,
                position_key: "a",
                field_clocks: { placement: { winning_mutation_id: "server" } },
              },
            },
          },
        ]),
      ),
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "scheduled_recipe",
        ids.scheduled,
      ]),
    ).resolves.toMatchObject({
      fields: { position_key: "z9" },
      fieldClocks: { placement: { mutationId: "move" } },
    });
  });

  it("does not create an overlay for a relative scheduled-recipe move", async () => {
    const ids = { event: "3d8b2b21-c378-4574-9e46-9338c81305ef", day: "4d8b2b21-c378-4574-9e46-9338c81305ef", role: "5d8b2b21-c378-4574-9e46-9338c81305ef", scheduled: "8d8b2b21-c378-4574-9e46-9338c81305ef" };
    for (const [entityType, entityId, fields] of [
      ["event", ids.event, { id: ids.event, lifecycle: "active" }],
      ["event_day", ids.day, { id: ids.day, event_id: ids.event, lifecycle: "active" }],
      ["event_meal_role", ids.role, { id: ids.role, event_id: ids.event, lifecycle: "active" }],
      ["scheduled_recipe", ids.scheduled, { id: ids.scheduled, event_id: ids.event, event_day_id: ids.day, event_meal_role_id: ids.role, position_key: "a" }],
    ] as const) await localDb.canonicalRecords.put({ userId, organizationId, recordSchemaVersion: 1, entityType, entityId, fields, lifecycle: "active", fieldClocks: {}, immutable: false, updatedAt: "2026-08-07T12:00:00.000Z" });
    await localDb.outbox.put({ id: "relative", userId, organizationId, commandType: "scheduled_recipe.move", payload: { scheduled_recipe_id: ids.scheduled, event_id: ids.event, event_day_id: ids.day, event_meal_role_id: ids.role, placement: "start" }, actionAt: "2026-08-07T12:01:00.000Z", createdAt: "2026-08-07T12:01:00.000Z", state: "pending" });
    await bootstrapOrganization(userId, organizationId, { fetch: vi.fn<typeof fetch>(async () => response([])) });
    await expect(localDb.optimisticOverlays.get([userId, organizationId, "scheduled_recipe", ids.scheduled])).resolves.toBeUndefined();
  });

  it("does not replay a move whose bootstrap target is retired or from another event", async () => {
    const ids = {
      event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
      otherEvent: "9d8b2b21-c378-4574-9e46-9338c81305ef",
      day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
      role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
      scheduled: "8d8b2b21-c378-4574-9e46-9338c81305ef",
    };
    await localDb.outbox.add({
      id: "invalid-move",
      userId,
      organizationId,
      commandType: "scheduled_recipe.move",
      payload: {
        scheduled_recipe_id: ids.scheduled,
        event_id: ids.event,
        event_day_id: ids.day,
        event_meal_role_id: ids.role,
        position_key: "z9",
      },
      actionAt: "2026-08-07T10:01:00.000Z",
      createdAt: "2026-08-07T10:01:00.000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          {
            organization_id: organizationId,
            entity_id: ids.event,
            entity_kind: "event",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: { id: ids.event, lifecycle: "active" },
            },
          },
          {
            organization_id: organizationId,
            entity_id: ids.day,
            entity_kind: "event_day",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: ids.day,
                event_id: ids.otherEvent,
                retired_at: "now",
              },
            },
          },
          {
            organization_id: organizationId,
            entity_id: ids.role,
            entity_kind: "event_meal_role",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: { id: ids.role, event_id: ids.event, retired_at: null },
            },
          },
          {
            organization_id: organizationId,
            entity_id: ids.scheduled,
            entity_kind: "scheduled_recipe",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: ids.scheduled,
                event_id: ids.event,
                event_day_id: ids.day,
                event_meal_role_id: ids.role,
                position_key: "a",
              },
            },
          },
        ]),
      ),
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "scheduled_recipe",
        ids.scheduled,
      ]),
    ).resolves.toBeUndefined();
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

  it("keeps a canonical archived event authoritative over a pending reactivation", async () => {
    const eventId = "3d8b2b21-c378-4574-9e46-9338c81305ef";
    await localDb.outbox.add({
      id: "reactivate",
      userId,
      organizationId,
      commandType: "event.lifecycle",
      payload: { event_id: eventId, operation: "reactivate" },
      actionAt: "2026-08-07T10:00:00.000Z",
      createdAt: "2026-08-07T10:00:00.000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          {
            ...record(eventId),
            payload: {
              record_schema_version: 1,
              record: {
                id: eventId,
                lifecycle: "archived",
                archived_at: "2026-08-07T09:00:00.000Z",
              },
            },
          },
        ]),
      ),
    });
    await expect(
      readVisibleCanonicalRecord(userId, organizationId, "event", eventId),
    ).resolves.toMatchObject({
      lifecycle: "retired",
      fields: { lifecycle: "archived" },
    });
  });

  it("keeps a canonical tombstone authoritative over a stale pending overlay", async () => {
    const eventId = "3d8b2b21-c378-4574-9e46-9338c81305f0";
    await localDb.outbox.add({
      id: "stale-create",
      userId,
      organizationId,
      commandType: "event.create",
      payload: { event_id: eventId, name: "Stale event" },
      actionAt: "2026-08-07T10:00:00.000Z",
      createdAt: "2026-08-07T10:00:00.000Z",
      state: "pending",
    });
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () =>
        response([
          {
            ...record(eventId),
            payload: {
              record_schema_version: 1,
              record: { id: eventId, lifecycle: "tombstone" },
            },
          },
        ]),
      ),
    });
    await expect(
      localDb.canonicalRecords.get([userId, organizationId, "event", eventId]),
    ).resolves.toMatchObject({ lifecycle: "tombstone" });
    await expect(
      readVisibleRecords(userId, organizationId, "event"),
    ).resolves.toEqual([]);
  });

  it("retains override clocks when bootstrapping a catalog-update feed record", async () => {
    const overrideId = "44444444-4444-4444-8444-444444444444";
    await bootstrapOrganization(userId, organizationId, {
      fetch: vi.fn<typeof fetch>(async () => response([
        organizationRecord(),
        {
          organization_id: organizationId,
          entity_id: "11111111-1111-4111-8111-111111111111",
          entity_kind: "scheduled_recipe",
          operation: "upsert",
          payload: {
            record_schema_version: 1,
            record: {
              id: "11111111-1111-4111-8111-111111111111",
              event_id: "22222222-2222-4222-8222-222222222222",
              recipe_version_id: "33333333-3333-4333-8333-333333333333",
              selected_scale_amount: "34",
              scale_mode: "suggested",
              field_clocks: {
                placement: { winning_mutation_id: "placement" },
                recipe_version_id: { winning_mutation_id: "catalog" },
                selected_scale_amount: { winning_mutation_id: "catalog" },
                scale_mode: { winning_mutation_id: "catalog" },
              },
            },
          },
        },
        {
          organization_id: organizationId,
          entity_id: overrideId,
          entity_kind: "scheduled_ingredient_override",
          operation: "upsert",
          payload: {
            record_schema_version: 1,
            record: {
              id: overrideId,
              organization_id: organizationId,
              event_id: "22222222-2222-4222-8222-222222222222",
              scheduled_recipe_id: "11111111-1111-4111-8111-111111111111",
              override_kind: "add",
              ingredient_id: "55555555-5555-4555-8555-555555555555",
              ingredient_version_id: "66666666-6666-4666-8666-666666666666",
              quantity: "2",
              include_in_portion_weight: true,
              note: "local",
              position_key: "q",
              field_clocks: {
                "replace.line-catalog": { winning_mutation_id: "old" },
                catalog_update: { winning_mutation_id: "catalog" },
              },
            },
          },
        },
      ])),
    });
    await expect(readVisibleCanonicalRecord(userId, organizationId, "scheduled_ingredient_override", overrideId)).resolves.toMatchObject({
      fields: { override_kind: "add", quantity: "2" },
      fieldClocks: {
        "replace.line-catalog": { winning_mutation_id: "old" },
        catalog_update: { winning_mutation_id: "catalog" },
      },
    });
    await expect(readVisibleCanonicalRecord(userId, organizationId, "scheduled_recipe", "11111111-1111-4111-8111-111111111111")).resolves.toMatchObject({
      fields: { selected_scale_amount: "34", scale_mode: "suggested" },
      fieldClocks: {
        placement: { winning_mutation_id: "placement" },
        recipe_version_id: { winning_mutation_id: "catalog" },
        selected_scale_amount: { winning_mutation_id: "catalog" },
        scale_mode: { winning_mutation_id: "catalog" },
      },
    });
  });

  it("marks upgrade-required bootstrap responses without replacing local state", async () => {
    const existing = {
      userId,
      organizationId,
      entityType: "event",
      entityId: "existing-event",
      recordSchemaVersion: 1,
      lifecycle: "active" as const,
      fields: { id: "existing-event", name: "Local" },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T10:00:00.000Z",
    };
    await localDb.canonicalRecords.put(existing);
    await localDb.outbox.add({
      id: "pending-upgrade",
      userId,
      organizationId,
      commandType: "event.create",
      payload: {},
      actionAt: "2026-08-07T10:00:00.000Z",
      createdAt: "2026-08-07T10:00:00.000Z",
      state: "pending",
    });
    await localDb.syncMetadata.put({
      userId,
      organizationId,
      cursor: "old-cursor",
      activity: "caughtUp",
    });
    const unknownEntity = vi.fn<typeof fetch>(async () =>
      response([{ ...record("unknown"), entity_kind: "future_entity" }]),
    );
    await expect(bootstrapOrganization(userId, organizationId, { fetch: unknownEntity }))
      .rejects.toMatchObject({ name: "UpgradeRequiredError", reason: "entity_kind" });
    await expect(localDb.canonicalRecords.get([userId, organizationId, "event", "existing-event"])).resolves.toEqual(existing);
    await expect(localDb.outbox.get("pending-upgrade")).resolves.toMatchObject({ state: "pending" });
    await expect(localDb.syncMetadata.get([userId, organizationId])).resolves.toMatchObject({ cursor: "old-cursor", activity: "upgradeRequired" });

    const futureSchema = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify({ sync_schema_version: 2 }), { status: 200 }),
    );
    await expect(bootstrapOrganization(userId, organizationId, { fetch: futureSchema }))
      .rejects.toMatchObject({ name: "UpgradeRequiredError", reason: "sync_schema_version" });
    await expect(localDb.canonicalRecords.get([userId, organizationId, "event", "existing-event"])).resolves.toEqual(existing);
    await expect(localDb.outbox.get("pending-upgrade")).resolves.toMatchObject({ state: "pending" });
  });

  it("does not advance pull state for an unsupported record schema", async () => {
    const existing = {
      userId,
      organizationId,
      entityType: "event",
      entityId: "existing-event",
      recordSchemaVersion: 1,
      lifecycle: "active" as const,
      fields: { id: "existing-event", name: "Local" },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T10:00:00.000Z",
    };
    await localDb.canonicalRecords.put(existing);
    await localDb.syncMetadata.put({ userId, organizationId, cursor: "old-cursor", activity: "caughtUp" });
    const unsupported = {
      ...record("future-record"),
      payload: { record_schema_version: 2, record: { id: "future-record" } },
    };
    await expect(pullOrganization(userId, organizationId, { fetch: vi.fn<typeof fetch>(async () => pullResponse([unsupported])) }))
      .rejects.toMatchObject({ name: "UpgradeRequiredError", reason: "record_schema_version" });
    await expect(localDb.canonicalRecords.get([userId, organizationId, "event", "existing-event"])).resolves.toEqual(existing);
    await expect(localDb.syncMetadata.get([userId, organizationId])).resolves.toMatchObject({ cursor: "old-cursor", activity: "upgradeRequired" });
  });
});
