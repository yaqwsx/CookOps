import {
  appendOutboxCommand,
  localDb,
  readVisibleCanonicalRecord,
} from "./local-db";
import { readVisibleRecords } from "./visible-records";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const amount = /^\d+(?:\.\d+)?$/;

export type CreateAdHocShoppingItemInput = {
  shoppingListId: string;
  name: string;
  targetAmount: string;
  unitId: string;
  storeSectionId: string;
  note?: string;
};

export type UpdateAdHocShoppingItemInput = CreateAdHocShoppingItemInput & {
  adHocShoppingItemId: string;
};

function checked(input: CreateAdHocShoppingItemInput) {
  const name = input.name.normalize("NFC").trim();
  const note = input.note?.trim() || null;
  if (
    !uuid.test(input.shoppingListId) ||
    !uuid.test(input.unitId) ||
    !uuid.test(input.storeSectionId) ||
    !name ||
    Array.from(name).length > 200 ||
    new TextEncoder().encode(JSON.stringify(name)).byteLength > 800 ||
    !amount.test(input.targetAmount) ||
    input.targetAmount.length > 100 ||
    (note !== null && note.length > 4000)
  )
    throw new Error("ad_hoc_shopping_item");
  return { name, note };
}

async function activeInputScope(
  userId: string,
  organizationId: string,
  input: CreateAdHocShoppingItemInput,
) {
  const list = await readVisibleCanonicalRecord(
    userId,
    organizationId,
    "shopping_list",
    input.shoppingListId,
  );
  const eventId = list?.fields.event_id;
  const [sections, units, event] = await Promise.all([
    readVisibleRecords(userId, organizationId, "store_section"),
    readVisibleRecords(userId, organizationId, "unit_definition"),
    typeof eventId === "string"
      ? localDb.canonicalRecords.get([userId, organizationId, "event", eventId])
      : undefined,
  ]);
  if (
    list?.lifecycle !== "active" ||
    typeof eventId !== "string" ||
    event?.lifecycle !== "active" ||
    !sections.some(
      (section) =>
        section.entityId === input.storeSectionId &&
        section.lifecycle === "active" &&
        section.fields.organization_id === organizationId,
    ) ||
    !units.some(
      (unit) =>
        unit.entityId === input.unitId &&
        unit.lifecycle === "active" &&
        unit.fields.allows_ingredient_quantity === true &&
        (unit.fields.organization_id === null ||
          unit.fields.organization_id === organizationId),
    )
  )
    throw new Error("ad_hoc_shopping_item");
  return eventId;
}

async function apply(
  userId: string,
  organizationId: string,
  command: {
    id: string;
    actionAt: string;
    payload: Record<string, unknown>;
  },
) {
  const payload = command.payload;
  if (
    typeof payload.shopping_list_id !== "string" ||
    typeof payload.ad_hoc_shopping_item_id !== "string" ||
    typeof payload.name !== "string" ||
    typeof payload.target_amount !== "string" ||
    typeof payload.unit_id !== "string" ||
    typeof payload.store_section_id !== "string"
  )
    return;
  const input: CreateAdHocShoppingItemInput = {
    shoppingListId: payload.shopping_list_id,
    name: payload.name,
    targetAmount: payload.target_amount,
    unitId: payload.unit_id,
    storeSectionId: payload.store_section_id,
    ...(typeof payload.note === "string" ? { note: payload.note } : {}),
  };
  const { name, note } = checked(input);
  const eventId = await activeInputScope(userId, organizationId, input);
  await localDb.optimisticOverlays.put({
    userId,
    organizationId,
    entityType: "ad_hoc_shopping_item",
    entityId: payload.ad_hoc_shopping_item_id,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      id: payload.ad_hoc_shopping_item_id,
      organization_id: organizationId,
      event_id: eventId,
      shopping_list_id: input.shoppingListId,
      name,
      target_amount: input.targetAmount,
      unit_id: input.unitId,
      store_section_id: input.storeSectionId,
      note,
      fulfilment_credit: "0",
      fulfilment_updated_at: null,
      fulfilment_updated_by_user_id: null,
      fulfilment_updated_by_installation_id: null,
      created_at: command.actionAt,
      created_by_user_id: userId,
      retired_at: null,
      retired_by_user_id: null,
    },
    fieldClocks: {
      optimistic: { mutationId: command.id, actionAt: command.actionAt },
    },
    immutable: false,
    updatedAt: command.actionAt,
  });
}

export async function queueAdHocShoppingItem(
  userId: string,
  organizationId: string,
  input: CreateAdHocShoppingItemInput,
) {
  const { name, note } = checked(input);
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  const itemId = crypto.randomUUID();
  const payload = {
    shopping_list_id: input.shoppingListId,
    ad_hoc_shopping_item_id: itemId,
    name,
    target_amount: input.targetAmount,
    unit_id: input.unitId,
    store_section_id: input.storeSectionId,
    note,
  };
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      await apply(userId, organizationId, {
        id: mutationId,
        actionAt,
        payload,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "shopping_list.create_ad_hoc_item",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

export async function replayAdHocShoppingItem(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  await apply(userId, organizationId, command);
}

async function applyUpdate(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const payload = command.payload;
  if (
    Object.keys(payload).length !== 7 ||
    ![
      "shopping_list_id",
      "ad_hoc_shopping_item_id",
      "name",
      "target_amount",
      "unit_id",
      "store_section_id",
      "note",
    ].every((key) => key in payload) ||
    typeof payload.shopping_list_id !== "string" ||
    typeof payload.ad_hoc_shopping_item_id !== "string" ||
    typeof payload.name !== "string" ||
    typeof payload.target_amount !== "string" ||
    typeof payload.unit_id !== "string" ||
    typeof payload.store_section_id !== "string" ||
    (payload.note !== null && typeof payload.note !== "string") ||
    !uuid.test(command.id) ||
    !Number.isFinite(Date.parse(command.actionAt))
  )
    return;
  const input: UpdateAdHocShoppingItemInput = {
    shoppingListId: payload.shopping_list_id,
    adHocShoppingItemId: payload.ad_hoc_shopping_item_id,
    name: payload.name,
    targetAmount: payload.target_amount,
    unitId: payload.unit_id,
    storeSectionId: payload.store_section_id,
    ...(typeof payload.note === "string" ? { note: payload.note } : {}),
  };
  const { name, note } = checked(input);
  const [canonicalItem, canonicalList] = await Promise.all([
    localDb.canonicalRecords.get([userId, organizationId, "ad_hoc_shopping_item", input.adHocShoppingItemId]),
    localDb.canonicalRecords.get([userId, organizationId, "shopping_list", input.shoppingListId]),
  ]);
  const item = await readVisibleCanonicalRecord(
    userId,
    organizationId,
    "ad_hoc_shopping_item",
    input.adHocShoppingItemId,
  );
  const list = await readVisibleCanonicalRecord(userId, organizationId, "shopping_list", input.shoppingListId);
  const eventId = item?.fields.event_id;
  const [canonicalEvent, visibleEvent] = typeof eventId === "string"
    ? await Promise.all([
        localDb.canonicalRecords.get([userId, organizationId, "event", eventId]),
        readVisibleCanonicalRecord(userId, organizationId, "event", eventId),
      ])
    : [undefined, undefined];
  if (
    canonicalItem?.lifecycle === "retired" ||
    canonicalList?.lifecycle === "retired" ||
    canonicalEvent?.lifecycle === "retired" ||
    item?.lifecycle !== "active" ||
    list?.lifecycle !== "active" ||
    visibleEvent?.lifecycle !== "active" ||
    item.fields.shopping_list_id !== input.shoppingListId
  )
    throw new Error("ad_hoc_shopping_item");
  await activeInputScope(userId, organizationId, input);
  const fields = {
    name,
    target_amount: input.targetAmount,
    unit_id: input.unitId,
    store_section_id: input.storeSectionId,
    note,
  };
  const winning = Object.fromEntries(
    Object.entries(fields).filter(([field]) => {
      const clock = item.fieldClocks[field];
      if (clock === undefined || clock === null) return true;
      if (typeof clock !== "object" || Array.isArray(clock)) return false;
      const value = clock as Record<string, unknown>;
      const currentAt = typeof value.actionAt === "string" ? value.actionAt : value.winning_client_wall_time;
      const currentId = typeof value.mutationId === "string" ? value.mutationId : value.winning_mutation_id;
      const currentTime = typeof currentAt === "string" ? Date.parse(currentAt) : NaN;
      return (
        typeof currentId === "string" &&
        Number.isFinite(currentTime) &&
        (Date.parse(command.actionAt) > currentTime ||
          (Date.parse(command.actionAt) === currentTime && command.id > currentId))
      );
    }),
  );
  if (!Object.keys(winning).length) return;
  await localDb.optimisticOverlays.put({
    ...item,
    fields: { ...item.fields, ...winning },
    fieldClocks: {
      ...item.fieldClocks,
      ...Object.fromEntries(Object.keys(winning).map((field) => [field, { mutationId: command.id, actionAt: command.actionAt }])),
    },
    updatedAt: command.actionAt,
  });
}

export async function queueAdHocShoppingItemUpdate(
  userId: string,
  organizationId: string,
  input: UpdateAdHocShoppingItemInput,
) {
  const { name, note } = checked(input);
  if (!uuid.test(input.adHocShoppingItemId)) throw new Error("ad_hoc_shopping_item");
  const id = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  const payload = {
    shopping_list_id: input.shoppingListId,
    ad_hoc_shopping_item_id: input.adHocShoppingItemId,
    name,
    target_amount: input.targetAmount,
    unit_id: input.unitId,
    store_section_id: input.storeSectionId,
    note,
  };
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    await applyUpdate(userId, organizationId, { id, actionAt, payload });
    await appendOutboxCommand({
      id, userId, organizationId, commandType: "shopping_list.update_ad_hoc_item", payload,
      actionAt, createdAt: actionAt, state: "pending",
    });
  });
}

export async function replayAdHocShoppingItemUpdate(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  await applyUpdate(userId, organizationId, command);
}

async function applyFulfilment(
  userId: string,
  organizationId: string,
  shoppingListId: string,
  itemId: string,
  fulfilled: boolean,
  mutationId: string,
  actionAt: string,
) {
  const [canonicalItem, canonicalList] = await Promise.all([
    localDb.canonicalRecords.get([
      userId,
      organizationId,
      "ad_hoc_shopping_item",
      itemId,
    ]),
    localDb.canonicalRecords.get([
      userId,
      organizationId,
      "shopping_list",
      shoppingListId,
    ]),
  ]);
  if (canonicalItem?.lifecycle === "retired" || canonicalList?.lifecycle === "retired")
    throw new Error("ad_hoc_shopping_item");
  const list =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "shopping_list",
      shoppingListId,
    ])) ?? canonicalList;
  const item = await readVisibleCanonicalRecord(userId, organizationId, "ad_hoc_shopping_item", itemId);
  const eventId = item?.fields.event_id;
  const canonicalEvent =
    typeof eventId === "string"
      ? await localDb.canonicalRecords.get([userId, organizationId, "event", eventId])
      : undefined;
  if (canonicalEvent?.lifecycle === "retired") throw new Error("ad_hoc_shopping_item");
  const event =
    typeof eventId === "string"
      ? (await localDb.optimisticOverlays.get([userId, organizationId, "event", eventId])) ?? canonicalEvent
      : undefined;
  if (
    !uuid.test(shoppingListId) ||
    !uuid.test(itemId) ||
    item?.lifecycle !== "active" ||
    list?.lifecycle !== "active" ||
    item.fields.shopping_list_id !== shoppingListId ||
    list.fields.event_id !== eventId ||
    event?.lifecycle !== "active" ||
    event.fields.lifecycle !== "active"
  )
    throw new Error("ad_hoc_shopping_item");
  const clock = item.fieldClocks.fulfilment_credit;
  if (clock !== undefined && clock !== null) {
    if (typeof clock !== "object" || Array.isArray(clock))
      throw new Error("ad_hoc_shopping_item");
    const value = clock as Record<string, unknown>;
    const currentAt = typeof value.actionAt === "string" ? value.actionAt : value.winning_client_wall_time;
    const currentId = typeof value.mutationId === "string" ? value.mutationId : value.winning_mutation_id;
    const candidateTime = Date.parse(actionAt);
    const currentTime = typeof currentAt === "string" ? Date.parse(currentAt) : NaN;
    if (
      typeof currentId !== "string" ||
      !Number.isFinite(candidateTime) ||
      !Number.isFinite(currentTime) ||
      candidateTime < currentTime ||
      (candidateTime === currentTime && mutationId <= currentId)
    )
      return;
  }
  await localDb.optimisticOverlays.put({
    ...item,
    fields: {
      ...item.fields,
      fulfilment_credit: fulfilled ? item.fields.target_amount : "0",
      fulfilment_updated_at: actionAt,
      fulfilment_updated_by_user_id: userId,
    },
    fieldClocks: {
      ...item.fieldClocks,
      fulfilment_credit: { mutationId, actionAt },
    },
    updatedAt: actionAt,
  });
}

export async function queueAdHocShoppingItemFulfilment(
  userId: string,
  organizationId: string,
  input: { shoppingListId: string; adHocShoppingItemId: string; fulfilled: boolean },
) {
  if (typeof input.fulfilled !== "boolean") throw new Error("ad_hoc_shopping_item");
  const mutationId = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    await applyFulfilment(userId, organizationId, input.shoppingListId, input.adHocShoppingItemId, input.fulfilled, mutationId, actionAt);
    await appendOutboxCommand({
      id: mutationId, userId, organizationId,
      commandType: "shopping_list.set_ad_hoc_item_fulfilment",
      payload: { shopping_list_id: input.shoppingListId, ad_hoc_shopping_item_id: input.adHocShoppingItemId, fulfilled: input.fulfilled },
      actionAt, createdAt: actionAt, state: "pending",
    });
  });
}

export async function replayAdHocShoppingItemFulfilment(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const { shopping_list_id: listId, ad_hoc_shopping_item_id: itemId, fulfilled } = command.payload;
  if (
    Object.keys(command.payload).length !== 3 ||
    !["shopping_list_id", "ad_hoc_shopping_item_id", "fulfilled"].every((key) => key in command.payload) ||
    typeof listId !== "string" ||
    typeof itemId !== "string" ||
    typeof fulfilled !== "boolean" ||
    !uuid.test(command.id) ||
    !Number.isFinite(Date.parse(command.actionAt))
  )
    return;
  await applyFulfilment(userId, organizationId, listId, itemId, fulfilled, command.id, command.actionAt);
}

async function applyLifecycle(
  userId: string,
  organizationId: string,
  shoppingListId: string,
  itemId: string,
  operation: "retire" | "restore",
  mutationId: string,
  actionAt: string,
) {
  const [canonicalItem, canonicalList] = await Promise.all([
    localDb.canonicalRecords.get([userId, organizationId, "ad_hoc_shopping_item", itemId]),
    localDb.canonicalRecords.get([userId, organizationId, "shopping_list", shoppingListId]),
  ]);
  const current =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "ad_hoc_shopping_item",
      itemId,
    ])) ?? canonicalItem;
  const eventId = current?.fields.event_id;
  const canonicalEvent =
    typeof eventId === "string"
      ? await localDb.canonicalRecords.get([userId, organizationId, "event", eventId])
      : undefined;
  const event =
    typeof eventId === "string"
      ? (await localDb.optimisticOverlays.get([userId, organizationId, "event", eventId])) ?? canonicalEvent
      : undefined;
  if (
    !uuid.test(shoppingListId) ||
    !uuid.test(itemId) ||
    canonicalList?.lifecycle === "retired" ||
    canonicalEvent?.lifecycle === "retired" ||
    current?.fields.shopping_list_id !== shoppingListId ||
    current.lifecycle !== (operation === "retire" ? "active" : "retired") ||
    canonicalList?.fields.event_id !== eventId ||
    event?.lifecycle !== "active" ||
    event.fields.lifecycle !== "active"
  )
    throw new Error("ad_hoc_shopping_item");
  const clock = current.fieldClocks.lifecycle;
  if (clock !== undefined && clock !== null) {
    if (typeof clock !== "object" || Array.isArray(clock))
      throw new Error("ad_hoc_shopping_item");
    const value = clock as Record<string, unknown>;
    const currentAt = typeof value.actionAt === "string" ? value.actionAt : value.winning_client_wall_time;
    const currentId = typeof value.mutationId === "string" ? value.mutationId : value.winning_mutation_id;
    const candidateTime = Date.parse(actionAt);
    const currentTime = typeof currentAt === "string" ? Date.parse(currentAt) : NaN;
    if (
      typeof currentId !== "string" ||
      !Number.isFinite(candidateTime) ||
      !Number.isFinite(currentTime) ||
      candidateTime < currentTime ||
      (candidateTime === currentTime && mutationId <= currentId)
    )
      return;
  }
  await localDb.optimisticOverlays.put({
    ...current,
    lifecycle: operation === "retire" ? "retired" : "active",
    fields:
      operation === "retire"
        ? { ...current.fields, retired_at: actionAt, retired_by_user_id: userId }
        : { ...current.fields, retired_at: null, retired_by_user_id: null },
    fieldClocks: { ...current.fieldClocks, lifecycle: { mutationId, actionAt } },
    updatedAt: actionAt,
  });
}

export async function queueAdHocShoppingItemLifecycle(
  userId: string,
  organizationId: string,
  input: {
    shoppingListId: string;
    adHocShoppingItemId: string;
    operation: "retire" | "restore";
  },
) {
  if (!uuid.test(input.shoppingListId) || !uuid.test(input.adHocShoppingItemId))
    throw new Error("ad_hoc_shopping_item");
  const id = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      await applyLifecycle(
        userId,
        organizationId,
        input.shoppingListId,
        input.adHocShoppingItemId,
        input.operation,
        id,
        actionAt,
      );
      await appendOutboxCommand({
        id,
        userId,
        organizationId,
        commandType: "shopping_list.ad_hoc_item_lifecycle",
        payload: {
          shopping_list_id: input.shoppingListId,
          ad_hoc_shopping_item_id: input.adHocShoppingItemId,
          operation: input.operation,
        },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

export async function replayAdHocShoppingItemLifecycle(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const payload = command.payload;
  if (
    Object.keys(payload).length !== 3 ||
    !["shopping_list_id", "ad_hoc_shopping_item_id", "operation"].every((key) => key in payload) ||
    typeof payload.shopping_list_id !== "string" ||
    typeof payload.ad_hoc_shopping_item_id !== "string" ||
    !uuid.test(command.id) ||
    !Number.isFinite(Date.parse(command.actionAt)) ||
    (payload.operation !== "retire" && payload.operation !== "restore")
  )
    return;
  await applyLifecycle(
    userId,
    organizationId,
    payload.shopping_list_id,
    payload.ad_hoc_shopping_item_id,
    payload.operation,
    command.id,
    command.actionAt,
  );
}
