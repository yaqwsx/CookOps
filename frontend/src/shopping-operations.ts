import { appendOutboxCommand, localDb, type OutboxCommand } from "./local-db";
import { add, decimal, maxZeroSubtract, print } from "./shopping-projections";
import { timestampMicros } from "./shopping-list";
import { readVisibleRecords } from "./visible-records";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const quantity = /^\d+(?:\.\d+)?$/;
type RowInput = { shoppingListId: string; shoppingIngredientRowId: string };
type ContributionInput = RowInput & { shoppingContributionId: string };
let lastActionMilliseconds = 0;

function wins(
  record: { fieldClocks: Record<string, unknown> },
  field: string,
  id: string,
  actionAt: string,
) {
  const clock = record.fieldClocks[field];
  if (clock === undefined || clock === null) return true;
  if (typeof clock !== "object" || Array.isArray(clock)) return false;
  const value = clock as Record<string, unknown>;
  const at = value.actionAt ?? value.winning_client_wall_time;
  const mutation = value.mutationId ?? value.winning_mutation_id;
  const candidate = timestampMicros(actionAt);
  const current = typeof at === "string" ? timestampMicros(at) : undefined;
  return (
    typeof mutation === "string" &&
    candidate !== undefined &&
    current !== undefined &&
    (candidate > current || (candidate === current && id > mutation))
  );
}

async function nextActionAt(): Promise<string> {
  const previous = await localDb.outbox.orderBy("createdAt").last();
  const priorMilliseconds = previous ? Date.parse(previous.createdAt) : 0;
  lastActionMilliseconds = Math.max(
    Date.now(),
    priorMilliseconds + 1,
    lastActionMilliseconds + 1,
  );
  return new Date(lastActionMilliseconds).toISOString();
}

function checkedInput(input: RowInput) {
  if (
    !uuid.test(input.shoppingListId) ||
    !uuid.test(input.shoppingIngredientRowId)
  )
    throw new Error("shopping_operation");
}
function canonicalRowNote(value: unknown): string | null {
  if (value !== null && typeof value !== "string")
    throw new Error("shopping_operation");
  if (value === null) return null;
  const note = value.normalize("NFC").replace(/\r\n?/g, "\n").trim();
  if (
    [...note].length > 4000 ||
    note.includes("\0") ||
    [...note].some((char) => {
      const code = char.charCodeAt(0);
      return code >= 0xd800 && code <= 0xdfff;
    })
  )
    throw new Error("shopping_operation");
  return note || null;
}
async function activeRow(
  userId: string,
  organizationId: string,
  input: RowInput,
) {
  const [lists, rows, events] = await Promise.all([
    readVisibleRecords(userId, organizationId, "shopping_list", true),
    readVisibleRecords(userId, organizationId, "shopping_ingredient_row", true),
    readVisibleRecords(userId, organizationId, "event", true),
  ]);
  const list = lists.find(
    (record) =>
      record.entityId === input.shoppingListId &&
      record.lifecycle === "active" &&
      record.fields.organization_id === organizationId &&
      typeof record.fields.event_id === "string",
  );
  const row = rows.find(
    (record) =>
      record.entityId === input.shoppingIngredientRowId &&
      record.lifecycle === "active" &&
      record.fields.organization_id === organizationId &&
      record.fields.shopping_list_id === input.shoppingListId,
  );
  const event =
    typeof list?.fields.event_id === "string"
      ? events.find((record) => record.entityId === list.fields.event_id)
      : undefined;
  if (
    !list ||
    !row ||
    event?.lifecycle !== "active" ||
    event.fields.lifecycle !== "active"
  )
    throw new Error("shopping_operation");
  return { row, list };
}
async function queueRow(
  userId: string,
  organizationId: string,
  input: RowInput,
  commandType:
    | "shopping_list.set_available_supply"
    | "shopping_list.set_manual_purchase_target"
    | "shopping_list.set_store_section_override",
  quantityValue: string | null,
) {
  checkedInput(input);
  if (
    commandType !== "shopping_list.set_store_section_override" &&
    quantityValue !== null &&
    (!quantity.test(quantityValue) || quantityValue.length > 100)
  )
    throw new Error("shopping_operation");
  let actionAt = "";
  const mutationId = crypto.randomUUID();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      actionAt = await nextActionAt();
      const { row } = await activeRow(userId, organizationId, input);
      const field =
        commandType === "shopping_list.set_available_supply"
          ? "available_supply_quantity"
          : commandType === "shopping_list.set_manual_purchase_target"
            ? "manual_purchase_target"
            : "store_section_override_id";
      if (!wins(row, field, mutationId, actionAt))
        throw new Error("shopping_operation");
      if (
        commandType === "shopping_list.set_store_section_override" &&
        quantityValue !== null
      ) {
        const section = (
          await readVisibleRecords(
            userId,
            organizationId,
            "store_section",
            true,
          )
        ).find(
          (record) =>
            record.entityId === quantityValue &&
            record.lifecycle === "active" &&
            record.fields.organization_id === organizationId,
        );
        if (!section) throw new Error("shopping_operation");
      }
      await localDb.optimisticOverlays.put({
        ...row,
        fields: { ...row.fields, [field]: quantityValue },
        fieldClocks: { ...row.fieldClocks, [field]: { mutationId, actionAt } },
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType,
        payload:
          commandType === "shopping_list.set_store_section_override"
            ? {
                shopping_list_id: input.shoppingListId,
                shopping_ingredient_row_id: input.shoppingIngredientRowId,
                store_section_id: quantityValue,
              }
            : {
                shopping_list_id: input.shoppingListId,
                shopping_ingredient_row_id: input.shoppingIngredientRowId,
                quantity: quantityValue,
              },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}
export function queueShoppingAvailableSupply(
  userId: string,
  organizationId: string,
  input: RowInput & { quantity: string },
) {
  return queueRow(
    userId,
    organizationId,
    input,
    "shopping_list.set_available_supply",
    input.quantity,
  );
}
export function queueShoppingManualPurchaseTarget(
  userId: string,
  organizationId: string,
  input: RowInput & { quantity: string | null },
) {
  return queueRow(
    userId,
    organizationId,
    input,
    "shopping_list.set_manual_purchase_target",
    input.quantity,
  );
}

export function queueShoppingStoreSectionOverride(
  userId: string,
  organizationId: string,
  input: RowInput & { storeSectionId: string | null },
) {
  if (input.storeSectionId !== null && !uuid.test(input.storeSectionId))
    throw new Error("shopping_operation");
  return queueRow(
    userId,
    organizationId,
    input,
    "shopping_list.set_store_section_override",
    input.storeSectionId,
  );
}

export async function queueShoppingRowNote(
  userId: string,
  organizationId: string,
  input: RowInput & { note: string | null },
) {
  checkedInput(input);
  const note = canonicalRowNote(input.note);
  const mutationId = crypto.randomUUID();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      const actionAt = await nextActionAt();
      const { row } = await activeRow(userId, organizationId, input);
      if (!wins(row, "note", mutationId, actionAt))
        throw new Error("shopping_operation");
      await localDb.optimisticOverlays.put({
        ...row,
        fields: { ...row.fields, note },
        fieldClocks: { ...row.fieldClocks, note: { mutationId, actionAt } },
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "shopping_list.set_row_note",
        payload: {
          shopping_list_id: input.shoppingListId,
          shopping_ingredient_row_id: input.shoppingIngredientRowId,
          note,
        },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

async function applyContributionFulfilment(
  userId: string,
  organizationId: string,
  input: ContributionInput,
  fulfilled: boolean,
  mutationId: string,
  actionAt: string,
) {
  const { list } = await activeRow(userId, organizationId, input);
  const revisionId = list.fields.current_generation_revision_id;
  if (typeof revisionId !== "string" || !uuid.test(revisionId))
    throw new Error("shopping_operation");
  const [contributions, snapshots] = await Promise.all([
    readVisibleRecords(userId, organizationId, "shopping_contribution", true),
    readVisibleRecords(
      userId,
      organizationId,
      "shopping_contribution_snapshot",
      true,
    ),
  ]);
  const contribution = contributions.find(
    (record) =>
      record.entityId === input.shoppingContributionId &&
      record.fields.shopping_list_id === input.shoppingListId &&
      record.fields.shopping_ingredient_row_id ===
        input.shoppingIngredientRowId &&
      record.lifecycle !== "tombstone",
  );
  if (!contribution) throw new Error("shopping_operation");
  const snapshot = snapshots.find(
    (record) =>
      record.fields.generation_revision_id === revisionId &&
      record.fields.shopping_contribution_id === input.shoppingContributionId,
  );
  const generated = decimal(snapshot?.fields.generated_quantity) ?? add([]);
  await localDb.optimisticOverlays.put({
    ...contribution,
    fields: {
      ...contribution.fields,
      fulfilment_credit: fulfilled ? print(generated) : "0",
      fulfilment_updated_at: actionAt,
      fulfilment_updated_by_user_id: userId,
    },
    fieldClocks: {
      ...contribution.fieldClocks,
      fulfilment_credit: { mutationId, actionAt },
    },
    updatedAt: actionAt,
  });
}

export async function queueShoppingContributionFulfilment(
  userId: string,
  organizationId: string,
  input: ContributionInput & { fulfilled: boolean },
) {
  checkedInput(input);
  if (
    !uuid.test(input.shoppingContributionId) ||
    typeof input.fulfilled !== "boolean"
  )
    throw new Error("shopping_operation");
  let actionAt = "";
  const mutationId = crypto.randomUUID();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      actionAt = await nextActionAt();
      await applyContributionFulfilment(
        userId,
        organizationId,
        input,
        input.fulfilled,
        mutationId,
        actionAt,
      );
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "shopping_list.set_contribution_fulfilment",
        payload: {
          shopping_list_id: input.shoppingListId,
          shopping_contribution_id: input.shoppingContributionId,
          fulfilled: input.fulfilled,
        },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}
export async function queueShoppingRowFulfilment(
  userId: string,
  organizationId: string,
  input: RowInput & { fulfilled: boolean },
) {
  checkedInput(input);
  if (typeof input.fulfilled !== "boolean")
    throw new Error("shopping_operation");
  let actionAt = "";
  const mutationId = crypto.randomUUID();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      actionAt = await nextActionAt();
      const { row, list } = await activeRow(userId, organizationId, input);
      const revisionId = list.fields.current_generation_revision_id;
      if (typeof revisionId !== "string" || !uuid.test(revisionId))
        throw new Error("shopping_operation");
      const [contributions, snapshots] = await Promise.all([
        readVisibleRecords(
          userId,
          organizationId,
          "shopping_contribution",
          true,
        ),
        readVisibleRecords(
          userId,
          organizationId,
          "shopping_contribution_snapshot",
          true,
        ),
      ]);
      const rowContributions = contributions.filter(
        (record) =>
          record.fields.shopping_list_id === input.shoppingListId &&
          record.fields.shopping_ingredient_row_id ===
            input.shoppingIngredientRowId &&
          record.lifecycle !== "tombstone",
      );
      const activeGenerated = new Map(
        snapshots
          .filter(
            (record) =>
              record.fields.shopping_list_id === input.shoppingListId &&
              record.fields.generation_revision_id === revisionId &&
              record.fields.active_in_revision === true &&
              typeof record.fields.shopping_contribution_id === "string",
          )
          .map((record) => [
            record.fields.shopping_contribution_id as string,
            decimal(record.fields.generated_quantity) ?? add([]),
          ]),
      );
      for (const contribution of rowContributions) {
        const generated = activeGenerated.get(contribution.entityId);
        const credit = input.fulfilled
          ? generated
            ? print(generated)
            : contribution.fields.fulfilment_credit
          : "0";
        await localDb.optimisticOverlays.put({
          ...contribution,
          fields: {
            ...contribution.fields,
            fulfilment_credit: credit,
            fulfilment_updated_at: actionAt,
            fulfilment_updated_by_user_id: userId,
          },
          fieldClocks: {
            ...contribution.fieldClocks,
            fulfilment_credit: { mutationId, actionAt },
          },
          updatedAt: actionAt,
        });
      }
      const credits = add(
        rowContributions.map((contribution) => {
          const generated = activeGenerated.get(contribution.entityId);
          return input.fulfilled && generated
            ? generated
            : input.fulfilled
              ? (decimal(contribution.fields.fulfilment_credit) ?? add([]))
              : add([]);
        }),
      );
      const target =
        decimal(row.fields.manual_purchase_target) ??
        maxZeroSubtract(
          add([...activeGenerated.values()]),
          decimal(row.fields.available_supply_quantity) ?? add([]),
        );
      await localDb.optimisticOverlays.put({
        ...row,
        fields: {
          ...row.fields,
          aggregate_fulfilment_credit: input.fulfilled
            ? print(maxZeroSubtract(target, credits))
            : "0",
          fulfilment_updated_at: actionAt,
          fulfilment_updated_by_user_id: userId,
        },
        fieldClocks: {
          ...row.fieldClocks,
          aggregate_fulfilment_credit: { mutationId, actionAt },
        },
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "shopping_list.set_row_fulfilment",
        payload: {
          shopping_list_id: input.shoppingListId,
          shopping_ingredient_row_id: input.shoppingIngredientRowId,
          fulfilled: input.fulfilled,
        },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

/** Reapply pending shopping intent after canonical replacement without creating a new command. */
export async function replayShoppingOperation(
  userId: string,
  organizationId: string,
  command: OutboxCommand,
): Promise<void> {
  if (
    !uuid.test(command.id) ||
    typeof command.actionAt !== "string" ||
    timestampMicros(command.actionAt) === undefined
  )
    return;
  const payload = command.payload;
  const listId = payload.shopping_list_id;
  const rowId = payload.shopping_ingredient_row_id;
  if (typeof listId !== "string" || !uuid.test(listId)) return;
  if (
    command.commandType === "shopping_list.set_contribution_fulfilment" &&
    typeof payload.shopping_contribution_id === "string" &&
    typeof payload.fulfilled === "boolean"
  ) {
    const contribution = (
      await readVisibleRecords(
        userId,
        organizationId,
        "shopping_contribution",
        true,
      )
    ).find(
      (record) =>
        record.entityId === payload.shopping_contribution_id &&
        record.fields.shopping_list_id === listId &&
        typeof record.fields.shopping_ingredient_row_id === "string",
    );
    if (!contribution) return;
    await applyContributionFulfilment(
      userId,
      organizationId,
      {
        shoppingListId: listId,
        shoppingIngredientRowId: contribution.fields
          .shopping_ingredient_row_id as string,
        shoppingContributionId: payload.shopping_contribution_id,
      },
      payload.fulfilled,
      command.id,
      command.actionAt,
    );
    return;
  }
  if (typeof rowId !== "string" || !uuid.test(rowId)) return;
  const input = { shoppingListId: listId, shoppingIngredientRowId: rowId };
  const sectionOverride =
    command.commandType === "shopping_list.set_store_section_override" &&
    Object.keys(payload).length === 3 &&
    (payload.store_section_id === null ||
      (typeof payload.store_section_id === "string" &&
        uuid.test(payload.store_section_id)));
  const quantityOperation =
    (command.commandType === "shopping_list.set_available_supply" ||
      command.commandType === "shopping_list.set_manual_purchase_target") &&
    (typeof payload.quantity === "string" || payload.quantity === null) &&
    (payload.quantity === null || quantity.test(payload.quantity));
  const noteOperation =
    command.commandType === "shopping_list.set_row_note" &&
    Object.keys(payload).length === 3 &&
    (typeof payload.note === "string" || payload.note === null);
  if (noteOperation) {
    const note = canonicalRowNote(payload.note);
    const { row } = await activeRow(userId, organizationId, input);
    if (!wins(row, "note", command.id, command.actionAt)) return;
    await localDb.optimisticOverlays.put({
      ...row,
      fields: { ...row.fields, note },
      fieldClocks: {
        ...row.fieldClocks,
        note: { mutationId: command.id, actionAt: command.actionAt },
      },
      updatedAt: command.actionAt,
    });
    return;
  }
  if (sectionOverride && payload.store_section_id !== null) {
    const section = (
      await readVisibleRecords(userId, organizationId, "store_section", true)
    ).find(
      (record) =>
        record.entityId === payload.store_section_id &&
        record.lifecycle === "active" &&
        record.fields.organization_id === organizationId,
    );
    if (!section) return;
  }
  if (quantityOperation || sectionOverride) {
    const { row } = await activeRow(userId, organizationId, input);
    const field =
      command.commandType === "shopping_list.set_available_supply"
        ? "available_supply_quantity"
        : command.commandType === "shopping_list.set_manual_purchase_target"
          ? "manual_purchase_target"
          : "store_section_override_id";
    if (!wins(row, field, command.id, command.actionAt)) return;
    await localDb.optimisticOverlays.put({
      ...row,
      fields: {
        ...row.fields,
        [field]: sectionOverride ? payload.store_section_id : payload.quantity,
      },
      fieldClocks: {
        ...row.fieldClocks,
        [field]: { mutationId: command.id, actionAt: command.actionAt },
      },
      updatedAt: command.actionAt,
    });
  }
  if (
    command.commandType === "shopping_list.set_row_fulfilment" &&
    typeof payload.fulfilled === "boolean"
  ) {
    const { row, list } = await activeRow(userId, organizationId, input);
    const revisionId = list.fields.current_generation_revision_id;
    if (typeof revisionId !== "string" || !uuid.test(revisionId)) return;
    const [contributions, snapshots] = await Promise.all([
      readVisibleRecords(userId, organizationId, "shopping_contribution", true),
      readVisibleRecords(
        userId,
        organizationId,
        "shopping_contribution_snapshot",
        true,
      ),
    ]);
    const entries = contributions.filter(
      (record) =>
        record.fields.shopping_list_id === listId &&
        record.fields.shopping_ingredient_row_id === rowId &&
        record.lifecycle !== "tombstone",
    );
    const generated = new Map(
      snapshots
        .filter(
          (record) =>
            record.fields.shopping_list_id === listId &&
            record.fields.generation_revision_id === revisionId &&
            record.fields.active_in_revision === true &&
            typeof record.fields.shopping_contribution_id === "string",
        )
        .map((record) => [
          record.fields.shopping_contribution_id as string,
          decimal(record.fields.generated_quantity) ?? add([]),
        ]),
    );
    for (const entry of entries) {
      const amount = generated.get(entry.entityId);
      await localDb.optimisticOverlays.put({
        ...entry,
        fields: {
          ...entry.fields,
          fulfilment_credit:
            payload.fulfilled && amount
              ? print(amount)
              : payload.fulfilled
                ? entry.fields.fulfilment_credit
                : "0",
          fulfilment_updated_at: command.actionAt,
          fulfilment_updated_by_user_id: userId,
        },
        fieldClocks: {
          ...entry.fieldClocks,
          fulfilment_credit: {
            mutationId: command.id,
            actionAt: command.actionAt,
          },
        },
        updatedAt: command.actionAt,
      });
    }
    const credits = add(
      entries.map((entry) =>
        payload.fulfilled
          ? (generated.get(entry.entityId) ??
            decimal(entry.fields.fulfilment_credit) ??
            add([]))
          : add([]),
      ),
    );
    const target =
      decimal(row.fields.manual_purchase_target) ??
      maxZeroSubtract(
        add([...generated.values()]),
        decimal(row.fields.available_supply_quantity) ?? add([]),
      );
    await localDb.optimisticOverlays.put({
      ...row,
      fields: {
        ...row.fields,
        aggregate_fulfilment_credit: payload.fulfilled
          ? print(maxZeroSubtract(target, credits))
          : "0",
        fulfilment_updated_at: command.actionAt,
        fulfilment_updated_by_user_id: userId,
      },
      fieldClocks: {
        ...row.fieldClocks,
        aggregate_fulfilment_credit: {
          mutationId: command.id,
          actionAt: command.actionAt,
        },
      },
      updatedAt: command.actionAt,
    });
  }
}
