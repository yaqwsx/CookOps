import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EventOverview } from "./events-overview";
import i18n, { defaultLocale } from "./i18n";
import { localDb } from "./local-db";

const { pullOrganization, SyncRequestError } = vi.hoisted(() => ({
  pullOrganization: vi.fn(async () => false),
  SyncRequestError: class SyncRequestError extends Error {
    constructor(readonly status: number) {
      super("Sync request failed.");
    }
  },
}));
vi.mock("./sync-bootstrap", () => ({ pullOrganization, SyncRequestError }));

const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const eventId = "3d8b2b21-c378-4574-9e46-9338c81305ef";

function setOnline(value: boolean) {
  Object.defineProperty(navigator, "onLine", { configurable: true, value });
}

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.syncMetadata.clear(),
  ]);
}

async function addEvent(
  fields: Record<string, unknown>,
  table: "canonical" | "overlay" = "canonical",
) {
  const record = {
    userId,
    organizationId,
    entityType: "event",
    entityId: eventId,
    recordSchemaVersion: 1,
    lifecycle: "active" as const,
    fields: {
      id: eventId,
      organization_id: organizationId,
      name: "Letní vaření",
      start_date: "2026-08-10",
      end_date: "2026-08-12",
      base_expected_attendance: 24,
      budget_amount: "1200.50",
      currency: "CZK",
      lifecycle: "active",
      archived_at: null,
      ...fields,
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  };
  await (table === "canonical"
    ? localDb.canonicalRecords
    : localDb.optimisticOverlays
  ).put(record);
}

describe("EventOverview", () => {
  beforeEach(async () => {
    await clearDatabase();
    pullOrganization.mockReset();
    pullOrganization.mockResolvedValue(false);
    setOnline(true);
    await i18n.changeLanguage(defaultLocale);
  });

  afterEach(() => vi.unstubAllGlobals());

  it("renders the canonical projection with its pending overlay instead of a REST page", async () => {
    await addEvent({ base_expected_attendance: 24 });
    await addEvent({ base_expected_attendance: 31 }, "overlay");
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    expect(
      await screen.findByRole("heading", { name: "Letní vaření" }),
    ).toBeInTheDocument();
    expect(screen.getByText("31")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Přehled čte uložené projekce akcí a čekající místní změny.",
      ),
    ).toBeInTheDocument();
    expect(pullOrganization).toHaveBeenCalledWith(userId, organizationId);
  });

  it("keeps cached events readable offline without attempting synchronization", async () => {
    await addEvent({});
    setOnline(false);
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    expect(
      await screen.findByRole("heading", { name: "Letní vaření" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Zobrazujeme uložené akce bez připojení.",
    );
    expect(pullOrganization).not.toHaveBeenCalled();
  });

  it("keeps cache errors recoverable and retries the existing synchronization path", async () => {
    pullOrganization.mockRejectedValueOnce(new Error("temporary"));
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Akce se nepodařilo načíst. Zkuste to znovu.",
    );
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Zkusit znovu" }));
    await waitFor(() => expect(pullOrganization).toHaveBeenCalledTimes(2));
  });

  it("returns to authentication when synchronization rejects the expired session", async () => {
    pullOrganization.mockRejectedValueOnce(new SyncRequestError(401));
    const onUnauthenticated = vi.fn();
    render(
      <EventOverview
        onUnauthenticated={onUnauthenticated}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    await waitFor(() => expect(onUnauthenticated).toHaveBeenCalledOnce());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
