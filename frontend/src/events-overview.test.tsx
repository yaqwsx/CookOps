import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EventOverview } from "./events-overview";
import type { EventSummary } from "./api/events";
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
const { getEventPage } = vi.hoisted(() => ({
  getEventPage: vi.fn(),
}));
vi.mock("./api/events", async () => {
  const actual = await vi.importActual<typeof import("./api/events")>("./api/events");
  return { ...actual, getEventPage };
});

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
    localDb.outbox.clear(),
  ]);
}

async function addEvent(
  fields: Record<string, unknown>,
  table: "canonical" | "overlay" = "canonical",
  id = eventId,
) {
  const record = {
    userId,
    organizationId,
    entityType: "event",
    entityId: id,
    recordSchemaVersion: 1,
    lifecycle: "active" as const,
    fields: {
      id,
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

async function addOrganization() {
  await localDb.canonicalRecords.bulkPut([
    {
      userId,
      organizationId,
      entityType: "organization",
      entityId: organizationId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: { id: organizationId, default_currency: "CZK" },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    },
    {
      userId,
      organizationId,
      entityType: "organization_capabilities",
      entityId: organizationId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        actor_user_id: userId,
        can_manage_organization: true,
        organization_id: organizationId,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    },
  ]);
}

describe("EventOverview", () => {
  beforeEach(async () => {
    await clearDatabase();
    pullOrganization.mockReset();
    pullOrganization.mockResolvedValue(false);
    getEventPage.mockReset();
    getEventPage.mockResolvedValue({ events: [], nextCursor: null });
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

  it("links to the organization recipe and ingredient catalogs", async () => {
    await addEvent({ base_expected_attendance: 24 });
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    await screen.findByRole("heading", { name: "Letní vaření" });
    expect(screen.getByRole("link", { name: "Recepty" })).toHaveAttribute(
      "href",
      `/organizations/${organizationId}/recipes`,
    );
    expect(screen.getByRole("link", { name: "Suroviny" })).toHaveAttribute(
      "href",
      `/organizations/${organizationId}/ingredients`,
    );
  });

  it("loads archive pages without replacing local records and resets them on organization change", async () => {
    const archived = (id: string, name: string): EventSummary => ({
      id,
      organizationId,
      name,
      startDate: "2026-08-10",
      endDate: "2026-08-12",
      baseExpectedAttendance: 4,
      budgetAmount: "20.00",
      currency: "CZK",
      lifecycle: "archived",
      archivedAt: "2026-08-13",
    });
    getEventPage
      .mockResolvedValueOnce({ events: [archived(eventId, "Server duplicate")], nextCursor: "next" })
      .mockResolvedValueOnce({ events: [archived("11111111-1111-4111-8111-111111111111", "Second archive")], nextCursor: null });
    await addEvent({ name: "Local active" });
    const user = userEvent.setup();
    const view = render(<EventOverview onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    expect(await screen.findByRole("heading", { name: "Local active" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Server duplicate" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Načíst další archivované akce" }));
    expect(await screen.findByRole("heading", { name: "Second archive" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { name: "Local active" })).toHaveLength(1);

    view.rerender(<EventOverview onUnauthenticated={vi.fn()} organizationId="different-org" userId={userId} />);
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Second archive" })).not.toBeInTheDocument());
  });

  it("ignores an old organization archive response after switching organizations", async () => {
    const oldOrganizationId = organizationId;
    const newOrganizationId = "6de17d2f-8365-4b1f-a80b-34d10425d51c";
    let resolveOldPage!: (page: { events: EventSummary[]; nextCursor: string | null }) => void;
    const oldPage = new Promise<{ events: EventSummary[]; nextCursor: string | null }>((resolve) => {
      resolveOldPage = resolve;
    });
    const summary = (id: string, name: string, org: string): EventSummary => ({
      id,
      organizationId: org,
      name,
      startDate: "2026-08-10",
      endDate: "2026-08-12",
      baseExpectedAttendance: 4,
      budgetAmount: "20.00",
      currency: "CZK",
      lifecycle: "archived",
      archivedAt: "2026-08-13",
    });
    getEventPage
      .mockImplementationOnce(() => oldPage)
      .mockResolvedValueOnce({ events: [summary("22222222-2222-4222-8222-222222222222", "Current archive", newOrganizationId)], nextCursor: null });
    const view = render(<EventOverview onUnauthenticated={vi.fn()} organizationId={oldOrganizationId} userId={userId} />);
    await waitFor(() => expect(getEventPage).toHaveBeenCalledWith(oldOrganizationId, undefined));
    view.rerender(<EventOverview onUnauthenticated={vi.fn()} organizationId={newOrganizationId} userId={userId} />);
    resolveOldPage({ events: [summary("11111111-1111-4111-8111-111111111111", "Old archive", oldOrganizationId)], nextCursor: null });
    expect(screen.queryByRole("heading", { name: "Old archive" })).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Current archive" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Old archive" })).not.toBeInTheDocument();
  });

  it("does not start a stale archive request after an old pull finishes", async () => {
    const newOrganizationId = "7ee17d2f-8365-4b1f-a80b-34d10425d51c";
    let resolveOldPull!: (value: boolean) => void;
    const oldPull = new Promise<boolean>((resolve) => {
      resolveOldPull = resolve;
    });
    pullOrganization
      .mockImplementationOnce(() => oldPull)
      .mockResolvedValueOnce(false);
    getEventPage.mockResolvedValue({
      events: [{
        id: "33333333-3333-4333-8333-333333333333",
        organizationId: newOrganizationId,
        name: "Current archive",
        startDate: "2026-08-10",
        endDate: "2026-08-12",
        baseExpectedAttendance: 4,
        budgetAmount: "20.00",
        currency: "CZK",
        lifecycle: "archived",
        archivedAt: "2026-08-13",
      }],
      nextCursor: "next",
    });
    const view = render(<EventOverview onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />);
    await waitFor(() => expect(pullOrganization).toHaveBeenCalledWith(userId, organizationId));
    view.rerender(<EventOverview onUnauthenticated={vi.fn()} organizationId={newOrganizationId} userId={userId} />);
    resolveOldPull(false);
    await waitFor(() => expect(getEventPage).toHaveBeenCalledWith(newOrganizationId, undefined));
    expect(getEventPage).not.toHaveBeenCalledWith(organizationId, undefined);
    expect(screen.getByRole("button", { name: "Načíst další archivované akce" })).not.toBeDisabled();
  });

  it("filters archived summaries by trimmed case-insensitive name or id while keeping active events visible", async () => {
    await addEvent({ name: "Active event", lifecycle: "active" });
    await addEvent(
      { name: "Čokoládový ples", lifecycle: "archived", archived_at: "2026-08-13" },
      "canonical",
      "11111111-1111-4111-8111-111111111111",
    );
    await addEvent(
      { name: "Other archive", lifecycle: "archived", archived_at: "2026-08-14" },
      "canonical",
      "22222222-2222-4222-8222-222222222222",
    );
    const user = userEvent.setup();
    render(
      <EventOverview onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />,
    );

    expect(await screen.findByRole("heading", { name: "Active event" })).toBeInTheDocument();
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
    await user.type(screen.getByRole("searchbox"), "  ČOKOLÁDOVÝ  ");
    expect(screen.getByRole("heading", { name: "Active event" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Čokoládový ples" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Other archive" })).not.toBeInTheDocument();

    await user.clear(screen.getByRole("searchbox"));
    await user.type(screen.getByRole("searchbox"), "22222222-2222-4222-8222-222222222222");
    expect(screen.getByRole("heading", { name: "Active event" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Other archive" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Čokoládový ples" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Vymazat hledání archivovaných akcí" }));
    expect(screen.getByRole("heading", { name: "Čokoládový ples" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Other archive" })).toBeInTheDocument();
  });

  it("shows an accessible empty state and no search when archived events are absent", async () => {
    await addEvent({ name: "Only active", lifecycle: "active" });
    const user = userEvent.setup();
    render(
      <EventOverview onUnauthenticated={vi.fn()} organizationId={organizationId} userId={userId} />,
    );
    expect(await screen.findByRole("heading", { name: "Only active" })).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();

    await addEvent({ name: "Archived later", lifecycle: "archived", archived_at: "2026-08-13" }, "canonical", "33333333-3333-4333-8333-333333333333");
    await waitFor(() => expect(screen.getByRole("searchbox")).toBeInTheDocument());
    await user.type(screen.getByRole("searchbox"), "missing");
    expect(screen.getByRole("status")).toHaveTextContent("Archivované akci neodpovídají hledání.");
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

  it("does not offer event creation without the cached administrator capability", async () => {
    await addEvent({});
    setOnline(false);
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    await screen.findByRole("heading", { name: "Letní vaření" });
    expect(
      screen.queryByRole("heading", { name: "Nová akce" }),
    ).not.toBeInTheDocument();
  });

  it("edits cached active attendance through the offline outbox", async () => {
    await addEvent({});
    setOnline(false);
    const user = userEvent.setup();
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    const card = await screen.findByRole("article");
    const attendance = within(card).getByLabelText("Očekávaná účast");
    await waitFor(() => expect(attendance).toHaveValue(24));
    await user.clear(attendance);
    await user.type(attendance, "9");
    await user.click(
      within(card).getByRole("button", { name: "Uložit účast" }),
    );

    expect(await within(card).findByRole("status")).toHaveTextContent(
      "Účast je uložena místně a bude synchronizována.",
    );
    await expect(localDb.outbox.toArray()).resolves.toContainEqual(
      expect.objectContaining({
        commandType: "event.update_base_attendance",
        payload: { event_id: eventId, base_expected_attendance: 9 },
      }),
    );
  });

  it("offers an administrator an explicit online archive confirmation", async () => {
    await addOrganization();
    await addEvent({});
    const user = userEvent.setup();
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    const card = await screen.findByRole("article");
    await user.click(
      within(card).getByRole("button", { name: "Archivovat akci" }),
    );
    expect(
      within(card).getByText(
        "Archivace vytvoří neměnný historický záznam. Aktivní plán už nepůjde upravovat.",
      ),
    ).toBeInTheDocument();
    await user.click(
      within(card).getByRole("button", { name: "Potvrdit archivaci" }),
    );
    await expect(localDb.outbox.toArray()).resolves.toContainEqual(
      expect.objectContaining({
        commandType: "event.lifecycle",
        payload: { event_id: eventId, operation: "archive" },
      }),
    );
  });

  it("creates a valid event locally while offline and exposes it as pending work", async () => {
    await addOrganization();
    setOnline(false);
    const user = userEvent.setup();
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    await user.type(await screen.findByLabelText("Název"), "Offline picnic");
    await user.type(screen.getByLabelText("Začátek"), "2026-08-10");
    await user.type(screen.getByLabelText("Konec"), "2026-08-10");
    await user.clear(screen.getByLabelText("Očekávaná účast"));
    await user.type(screen.getByLabelText("Očekávaná účast"), "8");
    await user.clear(screen.getByLabelText("Rozpočet"));
    await user.type(screen.getByLabelText("Rozpočet"), "50.25");
    await user.click(screen.getByRole("button", { name: "Uložit akci" }));

    expect(
      await screen.findByRole("heading", { name: "Offline picnic" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Akce je uložena místně a bude synchronizována."),
    ).toBeInTheDocument();
    await expect(localDb.outbox.count()).resolves.toBe(1);
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

  it("keeps local event creation available after a transient pull failure", async () => {
    await addOrganization();
    pullOrganization.mockRejectedValueOnce(new Error("temporary"));
    const user = userEvent.setup();
    render(
      <EventOverview
        onUnauthenticated={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await user.type(
      await screen.findByLabelText("Název"),
      "Cached organization",
    );
    await user.type(screen.getByLabelText("Začátek"), "2026-08-10");
    await user.type(screen.getByLabelText("Konec"), "2026-08-10");
    await user.click(screen.getByRole("button", { name: "Uložit akci" }));

    expect(
      await screen.findByRole("heading", { name: "Cached organization" }),
    ).toBeInTheDocument();
    await expect(localDb.outbox.toArray()).resolves.toContainEqual(
      expect.objectContaining({
        commandType: "event.create",
        payload: expect.objectContaining({ name: "Cached organization" }),
      }),
    );
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
