import {
  compareOutboxCommands,
  localDb,
  type BootstrapStagingRecord,
  type CanonicalRecord,
  type OutboxCommand,
} from "./local-db";
import { replayShoppingOperation } from "./shopping-operations";
import { replayRecipeCreate } from "./recipe-create";
import { replayRecipeLifecycle } from "./recipe-lifecycle";
import {
  replayScheduledRecipeAttendance,
  replayScheduledRecipeContext,
  replayScheduledRecipeLifecycle,
} from "./scheduled-recipe";
import { replayRecipeVersionPublish } from "./recipe-publish";
import { replayIngredientCreate } from "./ingredient-create";
import { replayScheduledIngredientOverride } from "./scheduled-ingredient-override";
import { replayReceiptCommand } from "./receipt-metadata";
import { replayCatalogConfiguration } from "./catalog-configuration";
import { replayEventDayCreate, replayEventDayLifecycle, replayEventDayNote, replayEventDayVisibility } from "./event-day";
import { replayEventMealRoleCreate, replayEventMealRoleLifecycle, replayEventMealRoleName, replayEventMealRolePosition } from "./event-meal-role";
import { replayEventMetadataUpdate } from "./event-metadata";
import {
  replayAdHocShoppingItem,
  replayAdHocShoppingItemFulfilment,
  replayAdHocShoppingItemLifecycle,
  replayAdHocShoppingItemUpdate,
} from "./ad-hoc-shopping-item";

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
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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
    .sort(compareOutboxCommands);
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
      const organization = await current("organization", organizationId);
      const defaultCurrency = organization?.fields.default_currency;
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
          organization_id: organizationId,
          ...(typeof defaultCurrency === "string"
            ? { currency: defaultCurrency }
            : {}),
          lifecycle: "active",
          archived_at: null,
        },
        fieldClocks: {
          ...existing?.fieldClocks,
          optimistic: { mutationId: command.id, actionAt: command.actionAt },
        },
        immutable: false,
        updatedAt: command.actionAt,
      });
    }
    if (command.commandType === "event.metadata")
      await replayEventMetadataUpdate(userId, organizationId, command);
    if (
      command.commandType === "event.update_base_attendance" &&
      typeof eventId === "string" &&
      typeof command.payload.base_expected_attendance === "number"
    ) {
      const canonicalEvent = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]);
      if (canonicalEvent?.lifecycle === "retired") continue;
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
    if (
      command.commandType === "shopping_list.set_available_supply" ||
      command.commandType === "shopping_list.set_manual_purchase_target" ||
      command.commandType === "shopping_list.set_contribution_fulfilment" ||
      command.commandType === "shopping_list.set_row_fulfilment"
    ) {
      try {
        await replayShoppingOperation(userId, organizationId, command);
      } catch {
        // A pending command targeting a now-archived or absent row stays recoverable.
      }
    }
    if (command.commandType === "recipe.create") {
      await replayRecipeCreate(userId, organizationId, command);
    }
    if (command.commandType === "recipe.publish_version") {
      await replayRecipeVersionPublish(userId, organizationId, command);
    }
    if (command.commandType === "recipe.lifecycle") {
      await replayRecipeLifecycle(userId, organizationId, command);
    }
    if (command.commandType === "ingredient.create") {
      await replayIngredientCreate(userId, organizationId, command);
    }
    if (command.commandType === "scheduled_recipe.attendance")
      await replayScheduledRecipeAttendance(userId, organizationId, command);
    if (command.commandType === "scheduled_recipe.context")
      await replayScheduledRecipeContext(userId, organizationId, command);
    if (command.commandType === "scheduled_recipe.lifecycle") {
      try { await replayScheduledRecipeLifecycle(userId, organizationId, command); } catch { /* remains recoverable */ }
    }
    if (command.commandType === "scheduled_recipe.ingredient_override")
      await replayScheduledIngredientOverride(userId, organizationId, command);
    if (command.commandType === "event_day.visibility")
      await replayEventDayVisibility(userId, organizationId, command);
    if (command.commandType === "event_day.note")
      await replayEventDayNote(userId, organizationId, command);
    if (command.commandType === "event_day.create")
      await replayEventDayCreate(userId, organizationId, command);
    if (command.commandType === "event_day.lifecycle")
      await replayEventDayLifecycle(userId, organizationId, command);
    if (command.commandType === "event_meal_role.create")
      await replayEventMealRoleCreate(userId, organizationId, command);
    if (command.commandType === "event_meal_role.position")
      await replayEventMealRolePosition(userId, organizationId, command);
    if (command.commandType === "event_meal_role.lifecycle")
      await replayEventMealRoleLifecycle(userId, organizationId, command);
    if (command.commandType === "event_meal_role.name")
      await replayEventMealRoleName(userId, organizationId, command);
    if (command.commandType.startsWith("receipt.")) {
      await replayReceiptCommand(userId, organizationId, command);
    }
    if (command.commandType === "catalog_configuration.mutate") {
      await replayCatalogConfiguration(userId, organizationId, command);
    }
    if (command.commandType === "shopping_list.create_ad_hoc_item") {
      try {
        await replayAdHocShoppingItem(userId, organizationId, command);
      } catch {
        // A pending item targeting a now-archived list remains recoverable.
      }
    }
    if (command.commandType === "shopping_list.set_ad_hoc_item_fulfilment") {
      try {
        await replayAdHocShoppingItemFulfilment(userId, organizationId, command);
      } catch {
        // A pending checkbox targeting a retired item stays recoverable.
      }
    }
    if (command.commandType === "shopping_list.ad_hoc_item_lifecycle") {
      try {
        await replayAdHocShoppingItemLifecycle(userId, organizationId, command);
      } catch {
        // A lifecycle intent targeting a changed item stays recoverable.
      }
    }
    if (command.commandType === "shopping_list.update_ad_hoc_item") {
      try {
        await replayAdHocShoppingItemUpdate(userId, organizationId, command);
      } catch {
        // A pending edit targeting a changed item stays recoverable.
      }
    }
    const entityId =
      command.commandType === "shopping_list.create"
        ? command.payload.shopping_list_id
        : undefined;
    if (typeof entityId === "string") {
      await localDb.optimisticOverlays.put({
        userId,
        organizationId,
        entityType: "shopping_list",
        entityId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          ...command.payload,
          id: entityId,
          organization_id: organizationId,
          lifecycle: "active",
        },
        fieldClocks: {
          optimistic: { mutationId: command.id, actionAt: command.actionAt },
        },
        immutable: false,
        updatedAt: command.actionAt,
      });
    }
    if (
      command.commandType === "scheduled_recipe.schedule" &&
      typeof command.payload.scheduled_recipe_id === "string" &&
      typeof command.payload.event_id === "string" &&
      typeof command.payload.event_day_id === "string" &&
      typeof command.payload.event_meal_role_id === "string" &&
      typeof command.payload.recipe_id === "string" &&
      typeof command.payload.recipe_version_id === "string" &&
      [
        command.payload.scheduled_recipe_id,
        command.payload.event_id,
        command.payload.event_day_id,
        command.payload.event_meal_role_id,
        command.payload.recipe_id,
        command.payload.recipe_version_id,
      ].every((id) => uuid.test(id))
    ) {
      const canonicalEvent = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "event",
        command.payload.event_id,
      ]);
      if (canonicalEvent?.lifecycle === "retired") continue;
      const event =
        (await localDb.optimisticOverlays.get([
          userId,
          organizationId,
          "event",
          command.payload.event_id,
        ])) ?? canonicalEvent;
      if (event?.fields.lifecycle !== "active") continue;
      await localDb.optimisticOverlays.put({
        userId,
        organizationId,
        entityType: "scheduled_recipe",
        entityId: command.payload.scheduled_recipe_id,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          ...command.payload,
          id: command.payload.scheduled_recipe_id,
          organization_id: organizationId,
          diner_count: event.fields.base_expected_attendance,
          attendance_mode: "follows_event",
          selected_scale_amount: "0",
          scale_mode: "suggested",
          note: null,
          retired_at: null,
        },
        fieldClocks: {
          optimistic: { mutationId: command.id, actionAt: command.actionAt },
        },
        immutable: false,
        updatedAt: command.actionAt,
      });
    }
    if (
      command.commandType === "scheduled_recipe.move" &&
      typeof command.payload.scheduled_recipe_id === "string" &&
      typeof command.payload.event_id === "string" &&
      typeof command.payload.event_day_id === "string" &&
      typeof command.payload.event_meal_role_id === "string" &&
      typeof command.payload.position_key === "string" &&
      [
        command.payload.scheduled_recipe_id,
        command.payload.event_id,
        command.payload.event_day_id,
        command.payload.event_meal_role_id,
      ].every((id) => uuid.test(id)) &&
      /^[0-9A-Za-z]{1,255}$/.test(command.payload.position_key)
    ) {
      const canonicalEvent = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "event",
        command.payload.event_id,
      ]);
      if (canonicalEvent?.lifecycle === "retired") continue;
      const scheduled = await current(
        "scheduled_recipe",
        command.payload.scheduled_recipe_id,
      );
      const [day, role] = await Promise.all([
        localDb.canonicalRecords.get([
          userId,
          organizationId,
          "event_day",
          command.payload.event_day_id,
        ]),
        localDb.canonicalRecords.get([
          userId,
          organizationId,
          "event_meal_role",
          command.payload.event_meal_role_id,
        ]),
      ]);
      if (
        scheduled?.lifecycle !== "active" ||
        scheduled.fields.event_id !== command.payload.event_id ||
        day?.lifecycle !== "active" ||
        day.fields.event_id !== command.payload.event_id ||
        role?.lifecycle !== "active" ||
        role.fields.event_id !== command.payload.event_id
      )
        continue;
      await localDb.optimisticOverlays.put({
        ...scheduled,
        fields: { ...scheduled.fields, ...command.payload },
        fieldClocks: {
          ...scheduled.fieldClocks,
          placement: { mutationId: command.id, actionAt: command.actionAt },
        },
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
