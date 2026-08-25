import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { readVisibleEventSummaries } from "./event-projections";
import { EventSettingsPage } from "./event-settings-page";
import i18n, { defaultLocale } from "./i18n";
import { localDb } from "./local-db";
import * as archiveCache from "./archive-cache";
import * as syncBootstrap from "./sync-bootstrap";

const { queueEventAttendanceUpdate } = vi.hoisted(() => ({
  queueEventAttendanceUpdate: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("./event-attendance", () => ({ queueEventAttendanceUpdate }));

vi.mock("./event-lifecycle-form", () => ({
  EventLifecycle: () => <button type="button">lifecycle</button>,
}));

const userId = "user-a";
const organizationId = "organization-a";

async function addEvent(
  eventId: string,
  name: string,
  lifecycle: "active" | "archived" = "active",
  attendance = 3,
) {
  await localDb.canonicalRecords.put({
    userId,
    organizationId,
    entityType: "event",
    entityId: eventId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
    fields: {
      id: eventId,
      organization_id: organizationId,
      name,
      start_date: "2026-08-10",
      end_date: "2026-08-10",
      base_expected_attendance: attendance,
      budget_amount: "10",
      currency: "CZK",
      lifecycle,
      archived_at: lifecycle === "archived" ? "2026-08-08T00:00:00Z" : null,
    },
  });
}

async function grantEventLifecycleManagement(
  lifecycle: "active" | "retired" = "active",
) {
  await localDb.canonicalRecords.put({
    userId,
    organizationId,
    entityType: "organization_capabilities",
    entityId: organizationId,
    recordSchemaVersion: 1,
    lifecycle,
    fieldClocks: {},
    immutable: true,
    updatedAt: "2026-08-07T12:00:00.000Z",
    fields: { actor_user_id: userId, can_manage_organization: true },
  });
}

describe("EventSettingsPage", () => {
  beforeEach(async () => {
    vi.restoreAllMocks();
    vi.spyOn(syncBootstrap, "pullOrganization").mockResolvedValue(false);
    vi.spyOn(archiveCache, "ensureArchivedEventCached").mockResolvedValue(true);
    await i18n.changeLanguage(defaultLocale);
    await localDb.canonicalRecords.clear();
    await localDb.optimisticOverlays.clear();
  });

  it("hydrates an uncached archived route after pulling organization state", async () => {
    const archivedId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
    const order: string[] = [];
    vi.mocked(syncBootstrap.pullOrganization).mockImplementation(async () => {
      order.push("pull");
      await addEvent(archivedId, "Archived", "archived");
      const record = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "event",
        archivedId,
      ]);
      if (record)
        await localDb.canonicalRecords.put({
          ...record,
          fields: {
            ...record.fields,
            current_archive_snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
          },
        });
      return false;
    });
    vi.mocked(archiveCache.ensureArchivedEventCached).mockImplementation(
      async () => {
        order.push("archive");
        return true;
      },
    );

    render(
      <EventSettingsPage
        eventId={archivedId}
        organizationId={organizationId}
        userId={userId}
        onUnauthenticated={vi.fn()}
      />,
    );

    expect(
      await screen.findByText("Archivovaná akce je jen pro čtení."),
    ).toBeInTheDocument();
    await vi.waitFor(() => expect(order).toEqual(["pull", "archive"]));
    expect(archiveCache.ensureArchivedEventCached).toHaveBeenCalledWith(
      userId,
      organizationId,
      archivedId,
      expect.any(Function),
      expect.any(AbortSignal),
    );
  });

  it("calls unauthenticated callback when archive hydration returns 401", async () => {
    const archivedId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
    vi.mocked(syncBootstrap.pullOrganization).mockImplementation(async () => {
      await addEvent(archivedId, "Archived", "archived");
      return false;
    });
    vi.mocked(archiveCache.ensureArchivedEventCached).mockRejectedValue(
      new syncBootstrap.SyncRequestError(401),
    );
    const onUnauthenticated = vi.fn();
    render(
      <EventSettingsPage
        eventId={archivedId}
        organizationId={organizationId}
        userId={userId}
        onUnauthenticated={onUnauthenticated}
      />,
    );
    await vi.waitFor(() => expect(onUnauthenticated).toHaveBeenCalledOnce());
  });

  it("aborts an in-flight archive hydration when the route changes", async () => {
    const archivedId = "ffffffff-ffff-4fff-8fff-ffffffffffff";
    await addEvent("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "Event A");
    let signal: AbortSignal | undefined;
    vi.mocked(syncBootstrap.pullOrganization).mockImplementation(async () => {
      await addEvent(archivedId, "Archived", "archived");
      return false;
    });
    vi.mocked(archiveCache.ensureArchivedEventCached).mockImplementation(
      async (...args) => {
        signal = args[4];
        await new Promise<void>(() => undefined);
        return true;
      },
    );
    const view = render(
      <EventSettingsPage
        eventId={archivedId}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    await vi.waitFor(() => expect(signal).toBeDefined());
    view.rerender(
      <EventSettingsPage
        eventId="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        organizationId={organizationId}
        userId={userId}
      />,
    );
    expect(signal?.aborted).toBe(true);
    expect(await screen.findByDisplayValue("Event A")).toBeInTheDocument();
  });

  it("keeps projection identity scoped when route changes from event A to B", async () => {
    await addEvent("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "Event A");
    await addEvent(
      "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      "Event B",
      "active",
      7,
    );
    const { rerender } = render(
      <EventSettingsPage
        eventId="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        organizationId={organizationId}
        userId={userId}
      />,
    );
    await screen.findByDisplayValue("Event A");
    expect(
      screen.getByRole("link", { name: "Nastavení akce" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Náklady" })).toBeInTheDocument();
    await i18n.changeLanguage("en");
    expect(
      screen.getByRole("link", { name: "Event settings" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Costs" })).toBeInTheDocument();
    rerender(
      <EventSettingsPage
        eventId="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        organizationId={organizationId}
        userId={userId}
      />,
    );
    expect(await screen.findByDisplayValue("Event B")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Event A")).not.toBeInTheDocument();
    expect(
      screen.getByRole("spinbutton", { name: "Expected attendance" }),
    ).toHaveValue(7);
  });

  it("navigates to every event workspace section", async () => {
    await addEvent("99999999-9999-4999-8999-999999999999", "Event");
    render(
      <EventSettingsPage
        eventId="99999999-9999-4999-8999-999999999999"
        organizationId={organizationId}
        userId={userId}
      />,
    );
    expect(await screen.findByDisplayValue("Event")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Nakupování" })).toHaveAttribute(
      "href",
      `/organizations/${organizationId}/events/99999999-9999-4999-8999-999999999999/shopping`,
    );
    expect(screen.getByRole("link", { name: "Účtenky" })).toHaveAttribute(
      "href",
      `/organizations/${organizationId}/events/99999999-9999-4999-8999-999999999999/receipts`,
    );
  });

  it("queues active attendance and keeps archived attendance read-only", async () => {
    const activeId = "11111111-1111-4111-8111-111111111111";
    const archivedId = "22222222-2222-4222-8222-222222222222";
    await addEvent(activeId, "Active", "active", 7);
    await addEvent(archivedId, "Archived", "archived");
    const { rerender } = render(
      <EventSettingsPage
        eventId={activeId}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    const input = await screen.findByLabelText("Očekávaná účast");
    expect(input).toHaveValue(7);
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Uložit účast" }));
    expect(queueEventAttendanceUpdate).toHaveBeenCalledWith(
      userId,
      organizationId,
      activeId,
      "7",
    );
    rerender(
      <EventSettingsPage
        eventId={archivedId}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    expect(
      await screen.findByText("Archivovaná akce je jen pro čtení."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Očekávaná účast")).not.toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("does not revive archived events from stale overlays or expose invalid attendance", async () => {
    const eventId = "33333333-3333-4333-8333-333333333333";
    await addEvent(eventId, "Archived", "archived");
    const canonical = await localDb.canonicalRecords.get([
      userId,
      organizationId,
      "event",
      eventId,
    ]);
    if (!canonical) throw new Error("canonical event missing");
    await localDb.canonicalRecords.put({ ...canonical, lifecycle: "retired" });
    await localDb.optimisticOverlays.put({
      ...canonical,
      lifecycle: "active",
      fields: { ...canonical.fields, lifecycle: "active", name: "Revived" },
    });
    render(
      <EventSettingsPage
        eventId={eventId}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    expect(
      await screen.findByText("Archivovaná akce je jen pro čtení."),
    ).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Revived")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Očekávaná účast")).not.toBeInTheDocument();

    await localDb.canonicalRecords.put({
      ...canonical,
      entityId: "44444444-4444-4444-8444-444444444444",
      fields: {
        ...canonical.fields,
        id: "44444444-4444-4444-8444-444444444444",
        base_expected_attendance: -1,
      },
    });
    await expect(
      readVisibleEventSummaries(userId, organizationId),
    ).resolves.not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: "44444444-4444-4444-8444-444444444444" }),
      ]),
    );
  });

  it("gates lifecycle actions by organization capability and labels archived read-only state", async () => {
    await addEvent(
      "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      "Archived",
      "archived",
    );
    render(
      <EventSettingsPage
        eventId="cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        organizationId={organizationId}
        userId={userId}
      />,
    );
    expect(
      await screen.findByText("Archivovaná akce je jen pro čtení."),
    ).toHaveTextContent("Archivovaná akce je jen pro čtení.");
    expect(
      screen.queryByRole("button", { name: "lifecycle" }),
    ).not.toBeInTheDocument();
  });

  it("renders lifecycle action for an organization administrator", async () => {
    await addEvent("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "Managed");
    await grantEventLifecycleManagement();
    render(
      <EventSettingsPage
        eventId="dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        organizationId={organizationId}
        userId={userId}
      />,
    );
    expect(
      await screen.findByRole("button", { name: "lifecycle" }),
    ).toBeInTheDocument();
  });

  it("does not render lifecycle action for a retired capability", async () => {
    await addEvent("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "Revoked");
    await grantEventLifecycleManagement("retired");
    render(
      <EventSettingsPage
        eventId="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        organizationId={organizationId}
        userId={userId}
      />,
    );
    await screen.findByDisplayValue("Revoked");
    expect(
      screen.queryByRole("button", { name: "lifecycle" }),
    ).not.toBeInTheDocument();
  });
});
