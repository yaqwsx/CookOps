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

  it("uses accessible exact-decimal metadata controls without offering photo upload", async () => {
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
    expect(
      screen.queryByText(/Fotografie účtenek zatím nejsou/i),
    ).not.toBeInTheDocument();
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
});
