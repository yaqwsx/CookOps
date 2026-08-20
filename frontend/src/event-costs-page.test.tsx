import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "./i18n";
import { EventCostsPage } from "./event-costs-page";
import { appendOutboxCommand, localDb } from "./local-db";
import * as plannerProjections from "./planner-projections";
import * as archiveCache from "./archive-cache";
import * as syncBootstrap from "./sync-bootstrap";

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
      archived_at: lifecycle === "archived" ? "2026-08-14T00:00:00Z" : null,
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
    vi.restoreAllMocks();
    vi.spyOn(syncBootstrap, "pullOrganization").mockResolvedValue(false);
    vi.spyOn(archiveCache, "ensureArchivedEventCached").mockResolvedValue(true);
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

  it("hydrates an uncached archived costs route after pulling organization state", async () => {
    const archivedEvent = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const order: string[] = [];
    vi.mocked(syncBootstrap.pullOrganization).mockImplementation(async () => {
      order.push("pull");
      await record(archivedEvent, "30", "archived");
      const canonical = await localDb.canonicalRecords.get([userId, organizationId, "event", archivedEvent]);
      if (canonical) await localDb.canonicalRecords.put({ ...canonical, fields: { ...canonical.fields, current_archive_snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee" } });
      return false;
    });
    vi.mocked(archiveCache.ensureArchivedEventCached).mockImplementation(async () => {
      order.push("archive");
      return true;
    });

    render(<EventCostsPage eventId={archivedEvent} organizationId={organizationId} userId={userId} onUnauthenticated={vi.fn()} />);
    expect(await screen.findByText("Tato akce je archivovaná a plán je jen pro čtení.")).toBeInTheDocument();
    await vi.waitFor(() => expect(order).toEqual(["pull", "archive"]));
  });

  it("keeps active costs local without fetching an archive", async () => {
    render(<EventCostsPage eventId={eventA} organizationId={organizationId} userId={userId} onUnauthenticated={vi.fn()} />);
    await screen.findByRole("heading", { name: "Event A" });
    expect(archiveCache.ensureArchivedEventCached).not.toHaveBeenCalled();
  });

  it("calls unauthenticated callback when costs archive hydration returns 401", async () => {
    const archivedEvent = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    vi.mocked(syncBootstrap.pullOrganization).mockImplementation(async () => {
      await record(archivedEvent, "30", "archived");
      return false;
    });
    vi.mocked(archiveCache.ensureArchivedEventCached).mockRejectedValue(new syncBootstrap.SyncRequestError(401));
    const onUnauthenticated = vi.fn();
    render(<EventCostsPage eventId={archivedEvent} organizationId={organizationId} userId={userId} onUnauthenticated={onUnauthenticated} />);
    await vi.waitFor(() => expect(onUnauthenticated).toHaveBeenCalledOnce());
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
