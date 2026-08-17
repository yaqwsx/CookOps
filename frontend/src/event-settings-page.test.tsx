import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { readVisibleEventSummaries } from "./event-projections";
import { EventSettingsPage } from "./event-settings-page";
import i18n, { defaultLocale } from "./i18n";
import { localDb } from "./local-db";

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
    await i18n.changeLanguage(defaultLocale);
    await localDb.canonicalRecords.clear();
    await localDb.optimisticOverlays.clear();
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
        onOpenCosts={vi.fn()}
        onOpenPlanner={vi.fn()}
        onOpenReceipts={vi.fn()}
        onOpenShopping={vi.fn()}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    await screen.findByDisplayValue("Event A");
    expect(
      screen.getByRole("button", { name: "Zpět na plán" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Náklady" })).toBeInTheDocument();
    await i18n.changeLanguage("en");
    expect(
      screen.getByRole("button", { name: "Back to planner" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Costs" })).toBeInTheDocument();
    rerender(
      <EventSettingsPage
        eventId="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        onOpenCosts={vi.fn()}
        onOpenPlanner={vi.fn()}
        onOpenReceipts={vi.fn()}
        onOpenShopping={vi.fn()}
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
    const onOpenPlanner = vi.fn();
    const onOpenCosts = vi.fn();
    const onOpenShopping = vi.fn();
    const onOpenReceipts = vi.fn();
    await addEvent("99999999-9999-4999-8999-999999999999", "Event");
    render(
      <EventSettingsPage
        eventId="99999999-9999-4999-8999-999999999999"
        onOpenCosts={onOpenCosts}
        onOpenPlanner={onOpenPlanner}
        onOpenReceipts={onOpenReceipts}
        onOpenShopping={onOpenShopping}
        organizationId={organizationId}
        userId={userId}
      />,
    );
    expect(await screen.findByDisplayValue("Event")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Nákupy" }));
    await user.click(screen.getByRole("button", { name: "Účtenky" }));
    expect(
      screen.getByRole("button", { name: "Zpět na plán" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Náklady" })).toBeInTheDocument();
    expect(onOpenShopping).toHaveBeenCalledOnce();
    expect(onOpenReceipts).toHaveBeenCalledOnce();
  });

  it("queues active attendance and keeps archived attendance read-only", async () => {
    const activeId = "11111111-1111-4111-8111-111111111111";
    const archivedId = "22222222-2222-4222-8222-222222222222";
    await addEvent(activeId, "Active", "active", 7);
    await addEvent(archivedId, "Archived", "archived");
    const { rerender } = render(
      <EventSettingsPage
        eventId={activeId}
        onOpenCosts={vi.fn()}
        onOpenPlanner={vi.fn()}
        onOpenReceipts={vi.fn()}
        onOpenShopping={vi.fn()}
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
        onOpenCosts={vi.fn()}
        onOpenPlanner={vi.fn()}
        onOpenReceipts={vi.fn()}
        onOpenShopping={vi.fn()}
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
        onOpenCosts={vi.fn()}
        onOpenPlanner={vi.fn()}
        onOpenReceipts={vi.fn()}
        onOpenShopping={vi.fn()}
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
        onOpenCosts={vi.fn()}
        onOpenPlanner={vi.fn()}
        onOpenReceipts={vi.fn()}
        onOpenShopping={vi.fn()}
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
        onOpenCosts={vi.fn()}
        onOpenPlanner={vi.fn()}
        onOpenReceipts={vi.fn()}
        onOpenShopping={vi.fn()}
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
        onOpenCosts={vi.fn()}
        onOpenPlanner={vi.fn()}
        onOpenReceipts={vi.fn()}
        onOpenShopping={vi.fn()}
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
