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
