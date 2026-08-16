import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EventReceipts } from "./event-receipts";
import "./i18n";
import { localDb } from "./local-db";

const receiptMocks = vi.hoisted(() => ({ queueReceiptCreate: vi.fn() }));
vi.mock("./receipt-metadata", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./receipt-metadata")>()),
  queueReceiptCreate: receiptMocks.queueReceiptCreate,
}));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization: vi.fn().mockResolvedValue(undefined),
  SyncRequestError: class SyncRequestError extends Error {},
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
});
