import { appendOutboxCommand, localDb, type CanonicalRecord } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const currency = /^[A-Z]{3}$/;

export type ReceiptInput = {
  title: string;
  totalAmount: string;
  receiptDate: string;
  note: string;
};
export type ReceiptInputError =
  | "title"
  | "totalAmount"
  | "receiptDate"
  | "event";

export function validateReceiptInput(
  input: ReceiptInput,
): ReceiptInputError | undefined {
  const title = input.title.normalize("NFC").trim();
  if (!title || title.length > 200 || title.includes("\0")) return "title";
  if (!decimal.test(input.totalAmount)) return "totalAmount";
  if (input.receiptDate) {
    const parsed = new Date(`${input.receiptDate}T00:00:00.000Z`);
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(input.receiptDate) ||
      Number.isNaN(parsed.valueOf()) ||
      parsed.toISOString().slice(0, 10) !== input.receiptDate
    )
      return "receiptDate";
  }
}

function payload(receiptId: string, eventId: string, input: ReceiptInput) {
  const note = input.note.normalize("NFC").replace(/\r\n?/g, "\n");
  return {
    receipt_id: receiptId,
    event_id: eventId,
    title: input.title.normalize("NFC").trim(),
    total_amount: input.totalAmount,
    receipt_date: input.receiptDate || null,
    note: note || null,
  };
}

function overlay(
  userId: string,
  organizationId: string,
  mutationId: string,
  actionAt: string,
  value: Record<string, unknown>,
  existing?: CanonicalRecord,
): CanonicalRecord {
  return {
    userId,
    organizationId,
    entityType: "receipt",
    entityId: value.receipt_id as string,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      ...existing?.fields,
      ...value,
      id: value.receipt_id,
      organization_id: organizationId,
      retired_at: null,
      retired_by_user_id: null,
    },
    fieldClocks: {
      ...existing?.fieldClocks,
      optimistic: { mutationId, actionAt },
    },
    immutable: false,
    updatedAt: actionAt,
  };
}

async function activeEvent(
  userId: string,
  organizationId: string,
  eventId: string,
) {
  const canonical = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "event",
    eventId,
  ]);
  if (canonical?.lifecycle === "retired") return undefined;
  const event =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "event",
      eventId,
    ])) ?? canonical;
  if (event?.lifecycle !== "active") return undefined;
  if (typeof event.fields.currency === "string") return event;
  const organization =
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "organization",
      organizationId,
    ])) ??
    (await localDb.canonicalRecords.get([
      userId,
      organizationId,
      "organization",
      organizationId,
    ]));
  const defaultCurrency = organization?.fields.default_currency;
  return typeof defaultCurrency === "string" && currency.test(defaultCurrency)
    ? { ...event, fields: { ...event.fields, currency: defaultCurrency } }
    : event;
}

async function currentReceipt(
  userId: string,
  organizationId: string,
  receiptId: string,
) {
  const canonical = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "receipt",
    receiptId,
  ]);
  if (canonical?.lifecycle === "retired") return canonical;
  return (
    (await localDb.optimisticOverlays.get([
      userId,
      organizationId,
      "receipt",
      receiptId,
    ])) ?? canonical
  );
}

async function queue(
  userId: string,
  organizationId: string,
  eventId: string,
  receiptId: string,
  input: ReceiptInput,
  commandType: "receipt.create" | "receipt.update",
) {
  const error = validateReceiptInput(input);
  if (
    error ||
    !uuid.test(userId) ||
    !uuid.test(organizationId) ||
    !uuid.test(eventId)
  )
    throw new Error(error ?? "event");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  const value = payload(receiptId, eventId, input);
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      const event = await activeEvent(userId, organizationId, eventId);
      if (!event || typeof event.fields.currency !== "string")
        throw new Error("event");
      const current = await currentReceipt(userId, organizationId, receiptId);
      if (
        commandType === "receipt.update" &&
        (current?.lifecycle !== "active" || current.fields.event_id !== eventId)
      )
        throw new Error("event");
      const next = overlay(
        userId,
        organizationId,
        mutationId,
        actionAt,
        value,
        current,
      );
      await localDb.optimisticOverlays.put({
        ...next,
        fields: { ...next.fields, currency: event.fields.currency },
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType,
        payload: value,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
  return receiptId;
}

export function queueReceiptCreate(
  userId: string,
  organizationId: string,
  eventId: string,
  input: ReceiptInput,
) {
  return queue(
    userId,
    organizationId,
    eventId,
    crypto.randomUUID(),
    input,
    "receipt.create",
  );
}

export function queueReceiptUpdate(
  userId: string,
  organizationId: string,
  eventId: string,
  receiptId: string,
  input: ReceiptInput,
) {
  if (!uuid.test(receiptId)) return Promise.reject(new Error("event"));
  return queue(
    userId,
    organizationId,
    eventId,
    receiptId,
    input,
    "receipt.update",
  );
}

async function queueReceiptLifecycle(
  userId: string,
  organizationId: string,
  eventId: string,
  receiptId: string,
  operation: "retire" | "restore",
) {
  if (
    ![userId, organizationId, eventId, receiptId].every((id) => uuid.test(id))
  )
    throw new Error("event");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      if (!(await activeEvent(userId, organizationId, eventId)))
        throw new Error("event");
      const current = await currentReceipt(userId, organizationId, receiptId);
      if (
        !current ||
        current.fields.event_id !== eventId ||
        current.lifecycle !== (operation === "retire" ? "active" : "retired")
      )
        throw new Error("event");
      await localDb.optimisticOverlays.put({
        ...current,
        lifecycle: operation === "retire" ? "retired" : "active",
        fields:
          operation === "retire"
            ? { ...current.fields, retired_at: actionAt }
            : { ...current.fields, retired_at: null, retired_by_user_id: null },
        fieldClocks: {
          ...current.fieldClocks,
          lifecycle: { mutationId, actionAt },
        },
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "receipt.lifecycle",
        payload: {
          receipt_id: receiptId,
          event_id: eventId,
          operation,
        },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

export function queueReceiptRetire(
  userId: string,
  organizationId: string,
  eventId: string,
  receiptId: string,
) {
  return queueReceiptLifecycle(
    userId,
    organizationId,
    eventId,
    receiptId,
    "retire",
  );
}

export function queueReceiptRestore(
  userId: string,
  organizationId: string,
  eventId: string,
  receiptId: string,
) {
  return queueReceiptLifecycle(
    userId,
    organizationId,
    eventId,
    receiptId,
    "restore",
  );
}

/** Rebuild pending receipt intent after bootstrap replaces canonical records. */
export async function replayReceiptCommand(
  userId: string,
  organizationId: string,
  command: {
    id: string;
    commandType: string;
    actionAt: string;
    payload: Record<string, unknown>;
  },
) {
  const value = command.payload;
  if (
    typeof value.receipt_id !== "string" ||
    typeof value.event_id !== "string" ||
    !uuid.test(value.receipt_id) ||
    !uuid.test(value.event_id)
  )
    return;
  if (command.commandType === "receipt.lifecycle") {
    if (value.operation !== "retire" && value.operation !== "restore") return;
    const current = await currentReceipt(
      userId,
      organizationId,
      value.receipt_id,
    );
    if (
      !current ||
      current.fields.event_id !== value.event_id ||
      current.lifecycle !==
        (value.operation === "retire" ? "active" : "retired")
    )
      return;
    await localDb.optimisticOverlays.put({
      ...current,
      lifecycle: value.operation === "retire" ? "retired" : "active",
      fields:
        value.operation === "retire"
          ? { ...current.fields, retired_at: command.actionAt }
          : { ...current.fields, retired_at: null, retired_by_user_id: null },
      fieldClocks: {
        ...current.fieldClocks,
        lifecycle: { mutationId: command.id, actionAt: command.actionAt },
      },
      updatedAt: command.actionAt,
    });
    return;
  }
  if (
    (command.commandType !== "receipt.create" &&
      command.commandType !== "receipt.update") ||
    typeof value.title !== "string" ||
    typeof value.total_amount !== "string" ||
    !decimal.test(value.total_amount)
  )
    return;
  const event = await activeEvent(userId, organizationId, value.event_id);
  if (!event) return;
  const existing = await currentReceipt(
    userId,
    organizationId,
    value.receipt_id,
  );
  if (existing?.lifecycle === "retired") return;
  const next = overlay(
    userId,
    organizationId,
    command.id,
    command.actionAt,
    value,
    existing,
  );
  await localDb.optimisticOverlays.put({
    ...next,
    fields: { ...next.fields, currency: event.fields.currency },
  });
}
