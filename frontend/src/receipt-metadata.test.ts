import { beforeEach, describe, expect, it } from "vitest";
import { liveQuery } from "dexie";

import {
  queueReceiptCreate,
  queueReceiptRetire,
  queueReceiptRestore,
  queueReceiptUpdate,
  replayReceiptCommand,
  validateReceiptInput,
} from "./receipt-metadata";
import { readEventReceipts } from "./receipt-projections";
import { compareOutboxCommands, localDb } from "./local-db";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const input = {
  title: "  Market  ",
  totalAmount: "12.50",
  receiptDate: "2026-08-07",
  note: "  bread\r\n  ",
};

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
}

async function addEvent() {
  await localDb.canonicalRecords.add({
    userId,
    organizationId,
    entityType: "event",
    entityId: eventId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      id: eventId,
      organization_id: organizationId,
      currency: "CZK",
      lifecycle: "active",
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

describe("offline receipt metadata", () => {
  beforeEach(clearDatabase);

  it("accepts only exact locally safe metadata", () => {
    expect(validateReceiptInput(input)).toBeUndefined();
    for (const candidate of [
      { ...input, title: " " },
      { ...input, title: "x".repeat(201) },
      { ...input, totalAmount: "01" },
      { ...input, totalAmount: "1e2" },
      { ...input, totalAmount: "-1" },
      { ...input, receiptDate: "07/08/2026" },
      { ...input, receiptDate: "2026-02-30" },
    ])
      expect(validateReceiptInput(candidate)).toBeDefined();
  });

  it("fuzzes malformed exact decimal and title input without queuing it", () => {
    for (let index = 0; index < 200; index += 1) {
      const fuzz = String.fromCharCode(index) + "e".repeat(index % 5);
      expect(
        validateReceiptInput({
          ...input,
          title: fuzz,
          totalAmount: `-${fuzz}`,
        }),
      ).toBeDefined();
    }
  });

  it("atomically creates, edits and retires an event-owned optimistic receipt", async () => {
    await addEvent();
    const receiptId = await queueReceiptCreate(
      userId,
      organizationId,
      eventId,
      input,
    );
    await expect(
      readEventReceipts(userId, organizationId, eventId),
    ).resolves.toEqual([
      {
        id: receiptId,
        title: "Market",
        totalAmount: "12.50",
        currency: "CZK",
        receiptDate: "2026-08-07",
        note: "  bread\n  ",
        retired: false,
      },
    ]);
    await queueReceiptUpdate(userId, organizationId, eventId, receiptId, {
      ...input,
      title: "Bakery",
      totalAmount: "0",
    });
    expect(
      (await readEventReceipts(userId, organizationId, eventId))[0],
    ).toMatchObject({ title: "Bakery", totalAmount: "0" });
    await queueReceiptRetire(userId, organizationId, eventId, receiptId);
    expect(
      (await readEventReceipts(userId, organizationId, eventId))[0],
    ).toMatchObject({
      id: receiptId,
      retired: true,
    });
    await queueReceiptRestore(userId, organizationId, eventId, receiptId);
    expect(
      (await readEventReceipts(userId, organizationId, eventId))[0],
    ).toMatchObject({
      id: receiptId,
      title: "Bakery",
    });
    expect(
      (await localDb.outbox.toArray())
        .sort(compareOutboxCommands)
        .map((command) => command.commandType),
    ).toEqual([
      "receipt.create",
      "receipt.update",
      "receipt.lifecycle",
      "receipt.lifecycle",
    ]);
  });

  it("notifies a live receipt projection after an optimistic create", async () => {
    await addEvent();
    let subscription: { unsubscribe: () => void } | undefined;
    const observed = new Promise<void>((resolve) => {
      subscription = liveQuery(() =>
        readEventReceipts(userId, organizationId, eventId),
      ).subscribe({
        next: (items) => {
          if (items.length === 1) resolve();
        },
      });
    });
    await queueReceiptCreate(userId, organizationId, eventId, input);
    await observed;
    subscription?.unsubscribe();
  });

  it("does not leave a spoofed event or receipt update in the outbox", async () => {
    await expect(
      queueReceiptCreate(userId, organizationId, eventId, input),
    ).rejects.toThrow("event");
    await addEvent();
    await expect(
      queueReceiptUpdate(
        userId,
        organizationId,
        eventId,
        crypto.randomUUID(),
        input,
      ),
    ).rejects.toThrow("event");
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("projects an explicit restore over a retired canonical receipt", async () => {
    await addEvent();
    const receiptId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    await localDb.canonicalRecords.add({
      userId,
      organizationId,
      entityType: "receipt",
      entityId: receiptId,
      recordSchemaVersion: 1,
      lifecycle: "retired",
      fields: {
        id: receiptId,
        organization_id: organizationId,
        event_id: eventId,
        title: "Market",
        total_amount: "12.50",
        currency: "CZK",
        receipt_date: null,
        note: null,
        retired_at: "2026-08-07T12:00:00.000Z",
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await queueReceiptRestore(userId, organizationId, eventId, receiptId);
    expect(
      (await readEventReceipts(userId, organizationId, eventId))[0],
    ).toMatchObject({
      id: receiptId,
      retired: false,
    });
    await localDb.optimisticOverlays.clear();
    await localDb.optimisticOverlays.add({
      userId,
      organizationId,
      entityType: "receipt",
      entityId: receiptId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: receiptId,
        organization_id: organizationId,
        event_id: eventId,
        title: "Spoofed restore",
        total_amount: "12.50",
        currency: "CZK",
        receipt_date: null,
        note: null,
        retired_at: null,
      },
      fieldClocks: {
        lifecycle: { mutationId: "not-a-uuid", actionAt: "later" },
      },
      immutable: false,
      updatedAt: "2026-08-07T12:01:00.000Z",
    });
    expect(
      (await readEventReceipts(userId, organizationId, eventId))[0],
    ).toMatchObject({ id: receiptId, retired: true, title: "Market" });
    await localDb.optimisticOverlays.clear();
    await replayReceiptCommand(userId, organizationId, {
      id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
      commandType: "receipt.lifecycle",
      actionAt: "2026-08-07T12:01:00.000Z",
      payload: {
        receipt_id: receiptId,
        event_id: eventId,
        operation: "restore",
      },
    });
    expect(
      (await readEventReceipts(userId, organizationId, eventId))[0],
    ).toMatchObject({
      id: receiptId,
      retired: false,
    });
  });

  it("replays an offline event and ordered receipt create-update-retire intent", async () => {
    await localDb.optimisticOverlays.add({
      userId,
      organizationId,
      entityType: "event",
      entityId: eventId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { id: eventId, organization_id: organizationId, currency: "CZK" },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    const receiptId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const base = { receipt_id: receiptId, event_id: eventId };
    await replayReceiptCommand(userId, organizationId, {
      id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
      commandType: "receipt.create",
      actionAt: "2026-08-07T12:00:00.000Z",
      payload: {
        ...base,
        title: "Market",
        total_amount: "12.50",
        receipt_date: null,
        note: null,
      },
    });
    await replayReceiptCommand(userId, organizationId, {
      id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
      commandType: "receipt.update",
      actionAt: "2026-08-07T12:01:00.000Z",
      payload: {
        ...base,
        title: "Bakery",
        total_amount: "0",
        receipt_date: null,
        note: null,
      },
    });
    await replayReceiptCommand(userId, organizationId, {
      id: "ace17d2f-8365-4b1f-a80b-34d10425d51c",
      commandType: "receipt.lifecycle",
      actionAt: "2026-08-07T12:02:00.000Z",
      payload: { ...base, operation: "retire" },
    });
    expect(
      (await readEventReceipts(userId, organizationId, eventId))[0],
    ).toMatchObject({
      id: receiptId,
      retired: true,
    });
    await expect(
      localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "receipt",
        receiptId,
      ]),
    ).resolves.toMatchObject({
      lifecycle: "retired",
      fields: { title: "Bakery", currency: "CZK" },
    });
  });
});
