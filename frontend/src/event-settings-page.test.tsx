import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EventSettingsPage } from "./event-settings-page";
import i18n, { defaultLocale } from "./i18n";
import { localDb } from "./local-db";

vi.mock("./event-lifecycle-form", () => ({
  EventLifecycle: () => <button type="button">lifecycle</button>,
}));

const userId = "user-a";
const organizationId = "organization-a";

async function addEvent(
  eventId: string,
  name: string,
  lifecycle: "active" | "archived" = "active",
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
      base_expected_attendance: 3,
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
    await addEvent("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "Event B");
    const { rerender } = render(
      <EventSettingsPage
        eventId="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        onOpenCosts={vi.fn()}
        onOpenPlanner={vi.fn()}
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
        organizationId={organizationId}
        userId={userId}
      />,
    );
    expect(await screen.findByDisplayValue("Event B")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Event A")).not.toBeInTheDocument();
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
