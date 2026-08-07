import {
  localDb,
  type BootstrapStagingRecord,
  type CanonicalRecord,
  type OutboxCommand,
} from "./local-db";

const supportedEntityKinds = new Set([
  "organization",
  "organization_capabilities",
  "store_section",
  "organization_meal_role_preset",
  "recipe_tag",
  "dietary_tag",
  "unit_definition",
  "ingredient",
  "ingredient_version",
  "ingredient_price_estimate",
  "recipe",
  "recipe_version",
  "recipe_version_tag",
  "recipe_ingredient_line",
  "event",
  "event_day",
  "event_meal_role",
  "scheduled_recipe",
  "scheduled_ingredient_override",
  "event_ingredient_price",
  "event_ingredient_price_snapshot",
  "shopping_list",
  "shopping_generation_revision",
  "shopping_revision_source",
  "shopping_ingredient_row",
  "shopping_contribution",
  "shopping_contribution_snapshot",
  "ad_hoc_shopping_item",
  "receipt",
  "receipt_attachment",
]);

type BootstrapWireRecord = {
  organization_id: string;
  entity_id: string;
  entity_kind: string;
  operation: "upsert";
  payload: { record_schema_version: 1; record: Record<string, unknown> };
};

type BootstrapWireResponse = {
  sync_schema_version: 1;
  server_time: string;
  cursor: string;
  records: BootstrapWireRecord[];
};

export type BootstrapOptions = {
  fetch?: typeof fetch;
  /** Test-only interruption point; production publication is a single IDB transaction. */
  beforePublish?: () => Promise<void> | void;
};

export class SyncRequestError extends Error {
  constructor(readonly status: number) {
    super("Sync request failed.");
  }
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parseBootstrap(
  value: unknown,
  organizationId: string,
): BootstrapWireResponse {
  if (
    !object(value) ||
    value.sync_schema_version !== 1 ||
    typeof value.server_time !== "string" ||
    typeof value.cursor !== "string" ||
    value.cursor.length === 0 ||
    !Array.isArray(value.records)
  ) {
    throw new Error("Invalid bootstrap response.");
  }
  for (const record of value.records) {
    if (
      !object(record) ||
      record.organization_id !== organizationId ||
      typeof record.entity_id !== "string" ||
      typeof record.entity_kind !== "string" ||
      !supportedEntityKinds.has(record.entity_kind) ||
      record.operation !== "upsert" ||
      !object(record.payload) ||
      record.payload.record_schema_version !== 1 ||
      !object(record.payload.record)
    ) {
      throw new Error("Invalid bootstrap response.");
    }
  }
  return value as BootstrapWireResponse;
}

function canonical(
  userId: string,
  record: BootstrapWireRecord,
  updatedAt: string,
): CanonicalRecord {
  const fields = record.payload.record;
  return {
    userId,
    organizationId: record.organization_id,
    entityType: record.entity_kind,
    entityId: record.entity_id,
    recordSchemaVersion: record.payload.record_schema_version,
    lifecycle: fields.retired_at || fields.archived_at ? "retired" : "active",
    fields,
    fieldClocks: object(fields.field_clocks) ? fields.field_clocks : {},
    immutable: false,
    updatedAt,
  };
}

function pendingCommands(commands: OutboxCommand[]) {
  return commands
    .filter((command) => command.state === "pending")
    .sort(
      (left, right) =>
        left.createdAt.localeCompare(right.createdAt) ||
        left.id.localeCompare(right.id),
    );
}

/** Replay the current typed command set without changing durable command identities. */
async function replayOptimisticCommands(
  userId: string,
  organizationId: string,
  commands: OutboxCommand[],
) {
  async function current(entityType: string, entityId: string) {
    return (
      (await localDb.optimisticOverlays.get([
        userId,
        organizationId,
        entityType,
        entityId,
      ])) ??
      localDb.canonicalRecords.get([
        userId,
        organizationId,
        entityType,
        entityId,
      ])
    );
  }
  for (const command of pendingCommands(commands)) {
    const eventId = command.payload.event_id;
    if (command.commandType === "event.create" && typeof eventId === "string") {
      const existing = await current("event", eventId);
      await localDb.optimisticOverlays.put({
        userId,
        organizationId,
        entityType: "event",
        entityId: eventId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          ...existing?.fields,
          ...command.payload,
          id: eventId,
          lifecycle: "active",
        },
        fieldClocks: {
          ...existing?.fieldClocks,
          optimistic: { mutationId: command.id, actionAt: command.actionAt },
        },
        immutable: false,
        updatedAt: command.actionAt,
      });
    }
    if (
      command.commandType === "event.update_base_attendance" &&
      typeof eventId === "string" &&
      typeof command.payload.base_expected_attendance === "number"
    ) {
      const existing = await current("event", eventId);
      if (!existing) continue;
      await localDb.optimisticOverlays.put({
        ...existing,
        fields: {
          ...existing.fields,
          base_expected_attendance: command.payload.base_expected_attendance,
        },
        fieldClocks: {
          ...existing.fieldClocks,
          base_expected_attendance: {
            mutationId: command.id,
            actionAt: command.actionAt,
          },
        },
        updatedAt: command.actionAt,
      });
    }
    const entityId =
      command.commandType === "shopping_list.create"
        ? command.payload.shopping_list_id
        : command.commandType === "recipe.create"
          ? command.payload.recipe_id
          : undefined;
    if (typeof entityId === "string") {
      await localDb.optimisticOverlays.put({
        userId,
        organizationId,
        entityType:
          command.commandType === "shopping_list.create"
            ? "shopping_list"
            : "recipe",
        entityId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: { ...command.payload, id: entityId, lifecycle: "active" },
        fieldClocks: {
          optimistic: { mutationId: command.id, actionAt: command.actionAt },
        },
        immutable: false,
        updatedAt: command.actionAt,
      });
    }
  }
}

type PullWireRecord = BootstrapWireRecord & {
  sequence: number;
  operation: string;
};
type PullWireResponse = {
  status: "ok" | "bootstrap_required";
  sync_schema_version: 1;
  server_time: string;
  next_cursor: string | null;
  transaction_groups: { records: PullWireRecord[] }[];
};

function parsePull(value: unknown, organizationId: string): PullWireResponse {
  if (
    !object(value) ||
    (value.status !== "ok" && value.status !== "bootstrap_required") ||
    value.sync_schema_version !== 1 ||
    typeof value.server_time !== "string" ||
    !Array.isArray(value.transaction_groups)
  )
    throw new Error("Invalid pull response.");
  if (value.status === "ok" && typeof value.next_cursor !== "string") {
    throw new Error("Invalid pull response.");
  }
  for (const group of value.transaction_groups) {
    if (!object(group) || !Array.isArray(group.records))
      throw new Error("Invalid pull response.");
    for (const record of group.records) {
      if (
        !object(record) ||
        record.organization_id !== organizationId ||
        typeof record.entity_id !== "string" ||
        typeof record.entity_kind !== "string" ||
        !supportedEntityKinds.has(record.entity_kind) ||
        typeof record.operation !== "string" ||
        !object(record.payload) ||
        record.payload.record_schema_version !== 1 ||
        !object(record.payload.record)
      )
        throw new Error("Invalid pull response.");
    }
  }
  return value as PullWireResponse;
}

/** Stage a complete server snapshot and atomically publish it for one user/org. */
export async function bootstrapOrganization(
  userId: string,
  organizationId: string,
  { fetch: send = fetch, beforePublish }: BootstrapOptions = {},
): Promise<void> {
  if (!userId || !organizationId)
    throw new Error("User and organization are required.");
  const response = await send("/api/v1/sync/bootstrap", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ organization_id: organizationId }),
  });
  if (!response.ok) throw new SyncRequestError(response.status);
  const body = parseBootstrap(await response.json(), organizationId);
  const attemptId = crypto.randomUUID();
  const staged: BootstrapStagingRecord[] = body.records.map((record) => ({
    ...canonical(userId, record, body.server_time),
    attemptId,
  }));

  await localDb.bootstrapStaging.bulkPut(staged);
  await beforePublish?.();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.bootstrapStaging,
    localDb.outbox,
    localDb.optimisticOverlays,
    localDb.syncMetadata,
    async () => {
      const records = await localDb.bootstrapStaging
        .where("[userId+organizationId+attemptId]")
        .equals([userId, organizationId, attemptId])
        .toArray();
      if (records.length !== staged.length)
        throw new Error("Incomplete bootstrap stage.");
      await localDb.canonicalRecords
        .where("[userId+organizationId]")
        .equals([userId, organizationId])
        .delete();
      await localDb.canonicalRecords.bulkPut(records);
      await localDb.optimisticOverlays
        .where("[userId+organizationId]")
        .equals([userId, organizationId])
        .delete();
      const pending = await localDb.outbox
        .where("[userId+organizationId+state]")
        .equals([userId, organizationId, "pending"])
        .toArray();
      await replayOptimisticCommands(userId, organizationId, pending);
      const previous = await localDb.syncMetadata.get([userId, organizationId]);
      await localDb.syncMetadata.put({
        ...previous,
        userId,
        organizationId,
        cursor: body.cursor,
        changeCursorHint: undefined,
        activity: "caughtUp",
        lastSuccessfulServerContact: body.server_time,
      });
      await localDb.bootstrapStaging
        .where("[userId+organizationId+attemptId]")
        .equals([userId, organizationId, attemptId])
        .delete();
    },
  );
}

/** Pull one complete page, or safely replace a stale replica before retrying it. */
export async function pullOrganization(
  userId: string,
  organizationId: string,
  { fetch: send = fetch }: BootstrapOptions = {},
): Promise<boolean> {
  const metadata = await localDb.syncMetadata.get([userId, organizationId]);
  if (!metadata?.cursor) {
    await bootstrapOrganization(userId, organizationId, { fetch: send });
    return true;
  }
  const response = await send("/api/v1/sync/pull", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      organization_id: organizationId,
      cursor: metadata.cursor,
    }),
  });
  if (!response.ok) throw new SyncRequestError(response.status);
  const body = parsePull(await response.json(), organizationId);
  if (body.status === "bootstrap_required") {
    await bootstrapOrganization(userId, organizationId, { fetch: send });
    return true;
  }
  for (const group of body.transaction_groups) {
    await localDb.transaction(
      "rw",
      localDb.canonicalRecords,
      localDb.outbox,
      localDb.optimisticOverlays,
      async () => {
        await localDb.canonicalRecords.bulkPut(
          group.records.map((record) =>
            canonical(userId, record, body.server_time),
          ),
        );
        await localDb.optimisticOverlays
          .where("[userId+organizationId]")
          .equals([userId, organizationId])
          .delete();
        const pending = await localDb.outbox
          .where("[userId+organizationId+state]")
          .equals([userId, organizationId, "pending"])
          .toArray();
        await replayOptimisticCommands(userId, organizationId, pending);
      },
    );
  }
  await localDb.syncMetadata.put({
    ...metadata,
    userId,
    organizationId,
    cursor: body.next_cursor ?? metadata.cursor,
    activity: "caughtUp",
    lastSuccessfulServerContact: body.server_time,
  });
  return body.transaction_groups.length > 0;
}
