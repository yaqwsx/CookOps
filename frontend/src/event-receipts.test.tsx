import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EventReceipts } from "./event-receipts";
import i18n from "./i18n";
import { localDb } from "./local-db";
import * as plannerProjections from "./planner-projections";
import * as costProjections from "./event-cost-projections";

const receiptMocks = vi.hoisted(() => ({ queueReceiptCreate: vi.fn() }));
const mediaMocks = vi.hoisted(() => ({
  prepareReceiptImage: vi.fn(),
  queueReceiptAttachment: vi.fn(),
}));
const archiveMocks = vi.hoisted(() => ({
  ensureArchivedEventCached: vi.fn().mockResolvedValue(true),
}));
vi.mock("./receipt-metadata", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./receipt-metadata")>()),
  queueReceiptCreate: receiptMocks.queueReceiptCreate,
}));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization: vi.fn().mockResolvedValue(undefined),
  SyncRequestError: class SyncRequestError extends Error {
    status: number;
    constructor(status: number) {
      super(`sync-${status}`);
      this.status = status;
    }
  },
}));
vi.mock("./receipt-media", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./receipt-media")>()),
  prepareReceiptImage: mediaMocks.prepareReceiptImage,
  queueReceiptAttachment: mediaMocks.queueReceiptAttachment,
}));
vi.mock("./archive-cache", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./archive-cache")>()),
  ensureArchivedEventCached: archiveMocks.ensureArchivedEventCached,
}));

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.archiveRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.pendingUploads.clear(),
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
    archiveMocks.ensureArchivedEventCached.mockReset().mockResolvedValue(true);
    vi.mocked((await import("./sync-bootstrap")).pullOrganization)
      .mockReset()
      .mockResolvedValue(false);
  });

  it("does not refresh or hydrate a known active event", async () => {
    await localDb.canonicalRecords.put({ userId, organizationId, entityType: "event", entityId: eventId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: eventId, organization_id: organizationId, lifecycle: "active", current_archive_snapshot_id: null }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() });
    const sync = vi.mocked((await import("./sync-bootstrap")).pullOrganization);
    render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    await screen.findByRole("heading", { name: "Účtenky" });
    expect(sync).not.toHaveBeenCalled();
    expect(archiveMocks.ensureArchivedEventCached).not.toHaveBeenCalled();
  });

  it("reports archive hydration authorization failures", async () => {
    const onUnauthenticated = vi.fn();
    await localDb.canonicalRecords.put({ userId, organizationId, entityType: "event", entityId: eventId, recordSchemaVersion: 1, lifecycle: "retired", fields: { id: eventId, organization_id: organizationId, lifecycle: "archived", current_archive_snapshot_id: crypto.randomUUID() }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() });
    const { SyncRequestError, pullOrganization } = await import("./sync-bootstrap");
    vi.mocked(pullOrganization).mockResolvedValueOnce(false);
    archiveMocks.ensureArchivedEventCached.mockRejectedValueOnce(new SyncRequestError(401));
    render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={onUnauthenticated} organizationId={organizationId} userId={userId} />);
    await waitFor(() => expect(onUnauthenticated).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("aborts archive hydration when the route changes", async () => {
    const eventB = crypto.randomUUID();
    let release!: () => void;
    const hydration = new Promise<boolean>((resolve) => { release = () => resolve(true); });
    archiveMocks.ensureArchivedEventCached.mockImplementationOnce(async (...args) => {
      const signal = args[4];
      await hydration;
      expect(signal?.aborted).toBe(true);
      return true;
    });
    await localDb.canonicalRecords.put({ userId, organizationId, entityType: "event", entityId: eventId, recordSchemaVersion: 1, lifecycle: "retired", fields: { id: eventId, organization_id: organizationId, lifecycle: "archived", current_archive_snapshot_id: crypto.randomUUID() }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() });
    const view = render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    await waitFor(() => expect(archiveMocks.ensureArchivedEventCached).toHaveBeenCalled());
    view.rerender(<EventReceipts eventId={eventB} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    release();
    await screen.findByRole("heading", { name: "Účtenky" });
    expect(screen.queryByText("Načítáme účtenky…")).not.toBeInTheDocument();
  });

  it("does not report authorization after an aborted archive hydration", async () => {
    const eventB = crypto.randomUUID();
    let rejectHydration!: (reason: unknown) => void;
    const hydration = new Promise<boolean>((_resolve, reject) => { rejectHydration = reject; });
    archiveMocks.ensureArchivedEventCached.mockImplementationOnce(async () => hydration);
    await localDb.canonicalRecords.put({ userId, organizationId, entityType: "event", entityId: eventId, recordSchemaVersion: 1, lifecycle: "retired", fields: { id: eventId, organization_id: organizationId, lifecycle: "archived", current_archive_snapshot_id: crypto.randomUUID() }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() });
    const onUnauthenticated = vi.fn();
    const view = render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={onUnauthenticated} organizationId={organizationId} userId={userId} />);
    await waitFor(() => expect(archiveMocks.ensureArchivedEventCached).toHaveBeenCalled());
    view.rerender(<EventReceipts eventId={eventB} onBack={vi.fn()} onUnauthenticated={onUnauthenticated} organizationId={organizationId} userId={userId} />);
    rejectHydration(new (await import("./sync-bootstrap")).SyncRequestError(401));
    await screen.findByRole("heading", { name: "Účtenky" });
    expect(onUnauthenticated).not.toHaveBeenCalled();
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
    await localDb.canonicalRecords.put({ userId, organizationId, entityType: "receipt", entityId: receiptId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: receiptId, organization_id: organizationId, event_id: eventId, title: "Bakery", total_amount: "12.50", currency: "CZK", receipt_date: null, note: null }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() });
    const sync = vi.mocked((await import("./sync-bootstrap")).pullOrganization);
    sync.mockImplementationOnce(async () => { await localDb.canonicalRecords.put({ userId, organizationId, entityType: "event", entityId: eventId, recordSchemaVersion: 1, lifecycle: "retired", fields: { id: eventId, organization_id: organizationId, lifecycle: "archived", current_archive_snapshot_id: snapshotId }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() }); await localDb.archiveRecords.put({ userId, organizationId, eventId, snapshotId, entityType: "receipt", entityId: receiptId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: receiptId, organization_id: organizationId, event_id: eventId, title: "Bakery", total_amount: "12.50", currency: "CZK", receipt_date: null, note: null }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() }); return true; });
    render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    await screen.findByText("Bakery");
    expect(screen.queryByRole("button", { name: "Uložit účtenku" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upravit" })).not.toBeInTheDocument();
  });

  it("renders the shared summary from the scoped planner and cost projections", async () => {
    const planner = vi.spyOn(plannerProjections, "readEventPlanner").mockResolvedValue({ name: "Receipt Event", startDate: "2026-08-15", endDate: "2026-08-15", attendance: 3, lifecycle: "archived", days: [], hiddenDays: [], retiredDays: [], roles: [], retiredRoles: [], recipes: [], ingredients: [], scheduled: [] });
    const costs = vi.spyOn(costProjections, "readEventCosts").mockResolvedValue({ budget: "30", total: "20", actual: "10", remaining: "20", currency: "CZK", expectedShopping: "20", missingIngredients: [], scheduled: new Map() });
    try {
      render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
      expect(await screen.findByRole("heading", { name: "Receipt Event" })).toBeInTheDocument();
      expect(screen.getByText("Očekávaná účast")).toBeInTheDocument();
      expect(screen.getByText("30 CZK")).toBeInTheDocument();
    } finally {
      planner.mockRestore();
      costs.mockRestore();
    }
  });

  it("formats receipt details when the application locale changes", async () => {
    await localDb.canonicalRecords.bulkPut([
      { userId, organizationId, entityType: "event", entityId: eventId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: eventId, organization_id: organizationId, lifecycle: "active", current_archive_snapshot_id: null }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() },
      { userId, organizationId, entityType: "receipt", entityId: "receipt-locale", recordSchemaVersion: 1, lifecycle: "active", fields: { id: "receipt-locale", organization_id: organizationId, event_id: eventId, title: "Bakery", total_amount: "12.50", currency: "CZK", receipt_date: "2026-08-07", note: null }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() },
    ]);
    await i18n.changeLanguage("cs");
    const view = render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    expect(await screen.findByText((_, element) => element?.textContent === "12,50 Kč")).toBeInTheDocument();
    expect(screen.getByText(/srpna/)).toBeInTheDocument();
    await i18n.changeLanguage("en");
    expect(await screen.findByText((_, element) => element?.textContent === "CZK 12.50")).toBeInTheDocument();
    expect(screen.getByText(/August/)).toBeInTheDocument();
    view.unmount();
    await i18n.changeLanguage("cs");
  });

  it("does not render the previous summary while switching event identity", async () => {
    const eventB = crypto.randomUUID();
    const planner = vi.spyOn(plannerProjections, "readEventPlanner").mockImplementation(async (_userId, _organizationId, requestedEventId) => ({ name: requestedEventId === eventId ? "Event A" : "Event B", startDate: "2026-08-15", endDate: "2026-08-15", attendance: 3, lifecycle: "active", days: [], hiddenDays: [], retiredDays: [], roles: [], retiredRoles: [], recipes: [], ingredients: [], scheduled: [] }));
    const costs = vi.spyOn(costProjections, "readEventCosts").mockResolvedValue({ budget: "30", total: "20", actual: "10", remaining: "20", currency: "CZK", expectedShopping: "20", missingIngredients: [], scheduled: new Map() });
    try {
      const view = render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
      expect(await screen.findByRole("heading", { name: "Event A" })).toBeInTheDocument();
      view.rerender(<EventReceipts eventId={eventB} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
      expect(screen.queryByRole("heading", { name: "Event A" })).not.toBeInTheDocument();
      expect(await screen.findByRole("heading", { name: "Event B" })).toBeInTheDocument();
    } finally {
      planner.mockRestore();
      costs.mockRestore();
    }
  });

  it("clears receipts and read-only state while switching event identity", async () => {
    const eventB = crypto.randomUUID();
    const receiptId = crypto.randomUUID();
    await localDb.canonicalRecords.bulkPut([
      { userId, organizationId, entityType: "receipt", entityId: receiptId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: receiptId, organization_id: organizationId, event_id: eventId, title: "Old receipt", total_amount: "1", currency: "CZK", receipt_date: null, note: null }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() },
      { userId, organizationId, entityType: "event", entityId: eventB, recordSchemaVersion: 1, lifecycle: "retired", fields: { id: eventB, organization_id: organizationId, lifecycle: "archived", current_archive_snapshot_id: crypto.randomUUID() }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() },
    ]);
    const view = render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    expect(await screen.findByText("Old receipt")).toBeInTheDocument();
    view.rerender(<EventReceipts eventId={eventB} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    expect(screen.queryByText("Old receipt")).not.toBeInTheDocument();
    expect(screen.queryByText("Událost je archivována")).not.toBeInTheDocument();
  });

  it("previews persisted pending photos across remounts without marking them synchronized", async () => {
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
    const otherReceiptId = crypto.randomUUID();
    await localDb.canonicalRecords.put({
      userId,
      organizationId,
      entityType: "receipt",
      entityId: otherReceiptId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: otherReceiptId,
        organization_id: organizationId,
        event_id: eventId,
        title: "Cafe",
        total_amount: "3.50",
        currency: "CZK",
        receipt_date: null,
        note: null,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    });
    const objectUrls = vi.fn((blob: Blob) => `blob:${blob.size}`);
    const revokeObjectUrl = vi.fn();
    vi.stubGlobal("URL", {
      createObjectURL: objectUrls,
      revokeObjectURL: revokeObjectUrl,
    });
    for (const state of ["pending", "uploading", "failed"] as const)
      await localDb.pendingUploads.put({
        id: `upload-${state}`,
        userId,
        organizationId,
        receiptId,
        attachmentId: crypto.randomUUID(),
        blob: new Blob([state], { type: "image/jpeg" }),
        createdAt: new Date().toISOString(),
        state,
      });
    try {
      const first = render(
        <EventReceipts
          eventId={eventId}
          onBack={vi.fn()}
          onUnauthenticated={vi.fn()}
          organizationId={organizationId}
          userId={userId}
        />,
      );
      expect(
        await screen.findAllByRole("img", { name: "Fotografie účtenky" }),
      ).toHaveLength(3);
      expect(screen.queryByText("Fotografie nahrána")).not.toBeInTheDocument();
      await localDb.pendingUploads.put({
        id: "upload-other",
        userId,
        organizationId,
        receiptId: otherReceiptId,
        attachmentId: crypto.randomUUID(),
        blob: new Blob(["other"], { type: "image/jpeg" }),
        createdAt: new Date().toISOString(),
        state: "pending",
      });
      await waitFor(() =>
        expect(
          screen.getAllByRole("img", { name: "Fotografie účtenky" }),
        ).toHaveLength(4),
      );
      expect(objectUrls).toHaveBeenCalledTimes(4);
      expect(revokeObjectUrl).not.toHaveBeenCalled();
      await localDb.pendingUploads.update("upload-pending", {
        state: "uploading",
      });
      expect(await screen.findByText("Fotografie se nahrává")).toBeInTheDocument();
      expect(objectUrls).toHaveBeenCalledTimes(4);
      expect(revokeObjectUrl).not.toHaveBeenCalled();
      await localDb.pendingUploads.update("upload-pending", {
        state: "synchronized",
      });
      await waitFor(() =>
        expect(
          screen.getAllByRole("img", { name: "Fotografie účtenky" }),
        ).toHaveLength(3),
      );
      expect(revokeObjectUrl).toHaveBeenCalledWith("blob:7");
      first.unmount();
      expect(revokeObjectUrl.mock.calls.length).toBeGreaterThanOrEqual(3);
      render(
        <EventReceipts
          eventId={eventId}
          onBack={vi.fn()}
          onUnauthenticated={vi.fn()}
          organizationId={organizationId}
          userId={userId}
        />,
      );
      expect(
        await screen.findAllByRole("img", { name: "Fotografie účtenky" }),
      ).toHaveLength(3);
    } finally {
      vi.unstubAllGlobals();
    }
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

  it("asks for a legible retake when compression cannot preserve readability", async () => {
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
    mediaMocks.prepareReceiptImage.mockRejectedValue({
      code: "receipt_image_readability",
    });
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
    await user.upload(
      screen.getByLabelText("Přidat fotografii účtenky"),
      new File(["source"], "receipt.jpg", { type: "image/jpeg" }),
    );
    expect(
      await screen.findByText(
        "Účtenku se nepodařilo zkomprimovat čitelně. Vyfoťte ji znovu, nebo ji rozdělte do více fotografií.",
      ),
    ).toBeInTheDocument();
    expect(mediaMocks.queueReceiptAttachment).not.toHaveBeenCalled();
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

  it("queues a replacement for the canonical attachment without hiding the old photo", async () => {
    const receiptId = crypto.randomUUID();
    const attachmentId = crypto.randomUUID();
    await localDb.canonicalRecords.bulkPut([
      {
        userId,
        organizationId,
        entityType: "receipt",
        entityId: receiptId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: { id: receiptId, organization_id: organizationId, event_id: eventId, title: "Bakery", total_amount: "12.50", currency: "CZK", receipt_date: null, note: null },
        fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString(),
      },
      {
        userId,
        organizationId,
        entityType: "receipt_attachment",
        entityId: attachmentId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: { id: attachmentId, organization_id: organizationId, receipt_id: receiptId, storage_state: "ready", media_type: "image/jpeg" },
        fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString(),
      },
    ]);
    const prepared = new Blob(["replacement"], { type: "image/jpeg" });
    mediaMocks.prepareReceiptImage.mockResolvedValue(prepared);
    mediaMocks.queueReceiptAttachment.mockResolvedValue({ id: "pending-replacement" });
    const user = userEvent.setup();
    render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    await screen.findByRole("heading", { name: "Účtenky" });
    const oldPhoto = await screen.findByRole("img", { name: "Fotografie účtenky" });
    await user.upload(screen.getByLabelText("Nahradit fotografii"), new File(["new"], "new.jpg", { type: "image/jpeg" }));
    expect(mediaMocks.queueReceiptAttachment).toHaveBeenCalledWith(userId, organizationId, receiptId, prepared, attachmentId);
    expect(oldPhoto).toHaveAttribute("src", expect.stringContaining(`/media/receipt-attachments/${attachmentId}`));
  });

  it("does not offer replacement for retired attachments or an in-flight replacement", async () => {
    const receiptId = crypto.randomUUID();
    const retiredId = crypto.randomUUID();
    const busyId = crypto.randomUUID();
    await localDb.canonicalRecords.bulkPut([
      { userId, organizationId, entityType: "receipt", entityId: receiptId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: receiptId, organization_id: organizationId, event_id: eventId, title: "Bakery", total_amount: "12.50", currency: "CZK", receipt_date: null, note: null }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() },
      ...[retiredId, busyId].map((id, index) => ({ userId, organizationId, entityType: "receipt_attachment", entityId: id, recordSchemaVersion: 1, lifecycle: index ? "active" as const : "retired" as const, fields: { id, organization_id: organizationId, receipt_id: receiptId, storage_state: "ready", media_type: "image/jpeg" }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() })),
    ]);
    await localDb.pendingUploads.add({ id: "busy", userId, organizationId, receiptId, attachmentId: crypto.randomUUID(), replaceAttachmentId: busyId, blob: new Blob(["old"], { type: "image/jpeg" }), createdAt: new Date().toISOString(), state: "pending" });
    render(<EventReceipts eventId={eventId} onBack={vi.fn()} onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    await screen.findByRole("heading", { name: "Účtenky" });
    expect(screen.queryAllByLabelText("Nahradit fotografii")).toHaveLength(0);
  });
});
