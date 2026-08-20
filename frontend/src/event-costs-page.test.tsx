import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "./i18n";
import { EventCostsPage } from "./event-costs-page";
import { appendOutboxCommand, localDb } from "./local-db";
import * as plannerProjections from "./planner-projections";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventA = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventB = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventMissing = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";

async function record(eventId: string, budget: string, lifecycle: "active" | "archived" = "active") {
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
      name: eventId === eventA ? "Event A" : "Event B",
      start_date: "2026-08-15",
      end_date: "2026-08-15",
      base_expected_attendance: 2,
      lifecycle,
      currency: "CZK",
      budget_amount: budget,
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-15T12:00:00.000Z",
  });
}

describe("event costs route", () => {
  beforeEach(async () => {
    await localDb.canonicalRecords.clear();
    await localDb.outbox.clear();
    await record(eventA, "10");
    await record(eventB, "20");
    await appendOutboxCommand({
      id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
      userId,
      organizationId,
      commandType: "event.update_price_estimates",
      payload: { event_id: eventA },
      actionAt: "2026-08-15T12:00:00.000Z",
      createdAt: "2026-08-15T12:00:00.000Z",
      state: "pending",
    });
  });

  it("does not retain event A costs while the route changes to event B", async () => {
    const props = {
      organizationId,
      userId,
    };
    const view = render(<EventCostsPage {...props} eventId={eventA} />);
    expect((await screen.findAllByText("10.00 CZK")).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "Event A" })).toBeInTheDocument();
    expect(screen.getByText("Očekávaná účast")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", {
        name: "Aktualizace odhadů čeká na synchronizaci",
      }),
    ).toBeDisabled();

    view.rerender(<EventCostsPage {...props} eventId={eventB} />);
    await waitFor(() => expect(screen.queryAllByText("10.00 CZK")).toHaveLength(0));
    expect((await screen.findAllByText("20.00 CZK")).length).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: "Aktualizovat odhady cen" }),
    ).toBeEnabled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("clears an unavailable event state before rendering the next event", async () => {
    const props = {
      organizationId,
      userId,
    };
    const readPlanner = vi
      .spyOn(plannerProjections, "readEventPlanner")
      .mockRejectedValueOnce(new Error("cached projection unavailable"));
    const view = render(<EventCostsPage {...props} eventId={eventMissing} />);
    expect(
      await screen.findByText("Náklady akce nejsou v místní projekci k dispozici."),
    ).toBeInTheDocument();
    readPlanner.mockRestore();

    view.rerender(<EventCostsPage {...props} eventId={eventB} />);
    await waitFor(() =>
      expect(
        screen.queryByText("Náklady akce nejsou v místní projekci k dispozici."),
      ).toBeNull(),
    );
    expect((await screen.findAllByText("20.00 CZK")).length).toBeGreaterThan(0);
  });

  it("shows archived costs as read-only while retaining values", async () => {
    const archivedEvent = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
    await record(archivedEvent, "30", "archived");

    render(
      <EventCostsPage
        eventId={archivedEvent}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    expect(await screen.findByText("Tato akce je archivovaná a plán je jen pro čtení.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Event B" })).toBeInTheDocument();
    expect((await screen.findAllByText("30.00 CZK")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Aktualizovat odhady cen" })).toBeNull();
  });
});
