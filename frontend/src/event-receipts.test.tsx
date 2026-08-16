import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EventReceipts } from "./event-receipts";
import "./i18n";
import { localDb } from "./local-db";

const receiptMocks = vi.hoisted(() => ({ queueReceiptCreate: vi.fn() }));
const mediaMocks = vi.hoisted(() => ({
  prepareReceiptImage: vi.fn(),
  queueReceiptAttachment: vi.fn(),
}));
vi.mock("./receipt-metadata", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./receipt-metadata")>()),
  queueReceiptCreate: receiptMocks.queueReceiptCreate,
}));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization: vi.fn().mockResolvedValue(undefined),
  SyncRequestError: class SyncRequestError extends Error {},
}));
vi.mock("./receipt-media", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./receipt-media")>()),
  prepareReceiptImage: mediaMocks.prepareReceiptImage,
  queueReceiptAttachment: mediaMocks.queueReceiptAttachment,
}));

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.archiveRecords.clear(),
    localDb.optimisticOverlays.clear(),
  ]);
}

describe("event receipt metadata screen", () => {
  beforeEach(async () => {
    await clearDatabase();
    receiptMocks.queueReceiptCreate
      .mockReset()
      .mockResolvedValue(crypto.randomUUID());
    mediaMocks.prepareReceiptImage.mockReset();
    mediaMocks.queueReceiptAttachment.mockReset();
  });

  it("uses accessible exact-decimal metadata controls and a camera-capable photo picker", async () => {
    const user = userEvent.setup();
    render(
      <EventReceipts
        eventId={eventId}
        onBack={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    await screen.findByRole("heading", { name: "Účtenky" });
    await user.type(
      screen.getByLabelText("Obchod nebo stručný název"),
      "Bakery",
    );
    await user.clear(screen.getByLabelText("Celková částka"));
    await user.type(screen.getByLabelText("Celková částka"), "12.50");
    await user.click(screen.getByRole("button", { name: "Uložit účtenku" }));
    expect(receiptMocks.queueReceiptCreate).toHaveBeenCalledWith(
      userId,
      organizationId,
      eventId,
      expect.objectContaining({ title: "Bakery", totalAmount: "12.50" }),
    );
  });

  it("hides receipt mutations when refresh archives the event", async () => {
    const snapshotId = crypto.randomUUID();
    const receiptId = crypto.randomUUID();
    await localDb.canonicalRecords.put({ userId, organizationId, entityType: "event", entityId: eventId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: eventId, organization_id: organizationId, lifecycle: "active", current_archive_snapshot_id: null }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() });
    await localDb.canonicalRecords.put({ userId, organizationId, entityType: "receipt", entityId: receiptId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: receiptId, organization_id: organizationId, event_id: eventId, title: "Bakery", total_amount: "12.50", currency: "CZK", receipt_date: null, note: null }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() });
    const sync = vi.mocked((await import("./sync-bootstrap")).pullOrganization);
    sync.mockImplementationOnce(async () => { await localDb.canonicalRecords.update([userId, organizationId, "event", eventId], { fields: { id: eventId, organization_id: organizationId, lifecycle: "archived", current_archive_snapshot_id: snapshotId }, lifecycle: "retired" }); await localDb.archiveRecords.put({ userId, organizationId, eventId, snapshotId, entityType: "receipt", entityId: receiptId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: receiptId, organization_id: organizationId, event_id: eventId, title: "Bakery", total_amount: "12.50", currency: "CZK", receipt_date: null, note: null }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() }); return true; });
    render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    await screen.findByText("Bakery");
    expect(screen.queryByRole("button", { name: "Uložit účtenku" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upravit" })).not.toBeInTheDocument();
  });

  it("queues selected receipt photos in picker order and keeps earlier photos after a later failure", async () => {
    const receiptId = crypto.randomUUID();
    await localDb.canonicalRecords.put({
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
        title: "Bakery",
        total_amount: "12.50",
        currency: "CZK",
        receipt_date: null,
        note: null,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    });
    const first = new Blob(["first"], { type: "image/jpeg" });
    const firstPending = {
      id: "pending-first",
      userId,
      organizationId,
      receiptId,
      attachmentId: "attachment-first",
      blob: first,
      createdAt: new Date().toISOString(),
      state: "pending" as const,
    };
    mediaMocks.prepareReceiptImage
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(new Blob(["second"], { type: "image/jpeg" }));
    mediaMocks.queueReceiptAttachment
      .mockResolvedValueOnce(firstPending)
      .mockRejectedValueOnce(new Error("image"));
    const user = userEvent.setup();
    render(
      <EventReceipts
        eventId={eventId}
        onBack={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    await screen.findByRole("heading", { name: "Účtenky" });
    const files = [
      new File(["first-source"], "first.jpg", { type: "image/jpeg" }),
      new File(["second-source"], "second.jpg", { type: "image/jpeg" }),
    ];
    const picker = screen.getByLabelText("Přidat fotografii účtenky");
    await user.upload(picker, files);
    expect(mediaMocks.prepareReceiptImage).toHaveBeenNthCalledWith(1, files[0]);
    expect(mediaMocks.prepareReceiptImage).toHaveBeenNthCalledWith(2, files[1]);
    expect(mediaMocks.queueReceiptAttachment).toHaveBeenCalledTimes(2);
    expect(mediaMocks.queueReceiptAttachment).toHaveBeenCalledWith(
      userId,
      organizationId,
      receiptId,
      first,
    );
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect((picker as HTMLInputElement).value).toBe("");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Fotografie čeká na nahrání",
    );
  });

  it("queues every selected receipt photo in picker order", async () => {
    const receiptId = crypto.randomUUID();
    await localDb.canonicalRecords.put({
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
        title: "Bakery",
        total_amount: "12.50",
        currency: "CZK",
        receipt_date: null,
        note: null,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    });
    const prepared = [
      new Blob(["first"], { type: "image/jpeg" }),
      new Blob(["second"], { type: "image/jpeg" }),
    ];
    mediaMocks.prepareReceiptImage
      .mockResolvedValueOnce(prepared[0])
      .mockResolvedValueOnce(prepared[1])
      .mockResolvedValue(prepared[0]);
    mediaMocks.queueReceiptAttachment.mockResolvedValue({});
    const files = [
      new File(["first-source"], "first.jpg", { type: "image/jpeg" }),
      new File(["second-source"], "second.jpg", { type: "image/jpeg" }),
    ];
    const user = userEvent.setup();
    render(
      <EventReceipts
        eventId={eventId}
        onBack={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    await screen.findByRole("heading", { name: "Účtenky" });
    const picker = screen.getByLabelText("Přidat fotografii účtenky");
    await user.upload(picker, files);
    expect(mediaMocks.queueReceiptAttachment).toHaveBeenNthCalledWith(
      1,
      userId,
      organizationId,
      receiptId,
      prepared[0],
    );
    expect(mediaMocks.queueReceiptAttachment).toHaveBeenNthCalledWith(
      2,
      userId,
      organizationId,
      receiptId,
      prepared[1],
    );
    expect((picker as HTMLInputElement).value).toBe("");
    await user.upload(picker, files[0]);
    expect(mediaMocks.queueReceiptAttachment).toHaveBeenCalledTimes(3);
  });

  it("serializes a rapid second picker change behind the active batch", async () => {
    const receiptId = crypto.randomUUID();
    await localDb.canonicalRecords.put({
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
        title: "Bakery",
        total_amount: "12.50",
        currency: "CZK",
        receipt_date: null,
        note: null,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    });
    let release!: (blob: Blob) => void;
    const gate = new Promise<Blob>((resolve) => {
      release = resolve;
    });
    const prepared = new Blob(["prepared"], { type: "image/jpeg" });
    mediaMocks.prepareReceiptImage.mockReturnValue(gate);
    mediaMocks.queueReceiptAttachment.mockResolvedValue({});
    render(
      <EventReceipts
        eventId={eventId}
        onBack={vi.fn()}
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    await screen.findByRole("heading", { name: "Účtenky" });
    const picker = screen.getByLabelText("Přidat fotografii účtenky");
    const file = new File(["first"], "first.jpg", { type: "image/jpeg" });
    const fileList = (files: File[]): FileList =>
      Object.assign(files, { item: (index: number) => files[index] ?? null }) as
        unknown as FileList;
    fireEvent.change(picker, { target: { files: fileList([file]) } });
    fireEvent.change(picker, {
      target: {
        files: fileList([
          new File(["second"], "second.jpg", { type: "image/jpeg" }),
        ]),
      },
    });
    expect(mediaMocks.prepareReceiptImage).toHaveBeenCalledTimes(1);
    release(prepared);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mediaMocks.queueReceiptAttachment).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
