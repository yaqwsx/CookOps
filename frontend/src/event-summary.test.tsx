import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import "./i18n";
import { appendOutboxCommand, localDb } from "./local-db";
import { EventSummary, useEventPendingSync } from "./event-summary";
import type { EventPlannerProjection } from "./planner-projections";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventA = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventB = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";

const planner: EventPlannerProjection = {
  name: "Event A",
  startDate: "2026-08-15",
  endDate: "2026-08-15",
  attendance: 2,
  lifecycle: "active",
  days: [],
  hiddenDays: [],
  retiredDays: [],
  roles: [],
  retiredRoles: [],
  recipes: [],
  ingredients: [],
  scheduled: [],
};

function SummaryRoute({
  eventId,
  lifecycle = "active",
}: {
  eventId: string;
  lifecycle?: "active" | "archived";
}) {
  const pendingSync = useEventPendingSync(userId, organizationId, eventId);
  return (
    <EventSummary
      eventId={eventId}
      organizationId={organizationId}
      userId={userId}
      planner={{
        ...planner,
        lifecycle,
        name: eventId === eventA ? "Event A" : "Event B",
      }}
      pendingSync={pendingSync}
    />
  );
}

describe("event summary", () => {
  beforeEach(async () => {
    await Promise.all([
      localDb.outbox.clear(),
      localDb.canonicalRecords.clear(),
      localDb.optimisticOverlays.clear(),
      localDb.archiveRecords.clear(),
    ]);
  });

  const record = (
    entityId: string,
    orgId = organizationId,
    fields = { event_id: eventA },
  ) => ({
    userId,
    organizationId: orgId,
    entityType: "shopping_list",
    entityId,
    recordSchemaVersion: 1,
    lifecycle: "active" as const,
    fields,
    fieldClocks: {},
    immutable: false,
    updatedAt: new Date().toISOString(),
  });

  const eventRecord = (
    lifecycle: "active" | "archived",
    snapshot: string | null = null,
  ) => ({
    userId,
    organizationId,
    entityType: "event",
    entityId: eventA,
    recordSchemaVersion: 1,
    lifecycle: "active" as const,
    fields: {
      id: eventA,
      organization_id: organizationId,
      name: "Event A",
      start_date: "2026-08-15",
      end_date: "2026-08-15",
      base_expected_attendance: 2,
      budget_amount: "10",
      currency: "CZK",
      lifecycle,
      archived_at: lifecycle === "archived" ? "2026-08-16" : null,
      current_archive_snapshot_id: snapshot,
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: new Date().toISOString(),
  });

  const capability = (
    role: "member" | "organization_admin" = "organization_admin",
  ) => ({
    userId,
    organizationId,
    entityType: "organization_capabilities",
    entityId: organizationId,
    recordSchemaVersion: 1,
    lifecycle: "active" as const,
    fields: {
      actor_user_id: userId,
      role,
      can_manage_organization: role === "organization_admin",
    },
    fieldClocks: {},
    immutable: false,
    updatedAt: new Date().toISOString(),
  });

  it("counts a canonical shopping-list command for its event", async () => {
    await localDb.canonicalRecords.add(record("list-a"));
    await appendOutboxCommand({
      id: "list-command",
      userId,
      organizationId,
      commandType: "shopping_list.rename",
      payload: { shopping_list_id: "list-a" },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "pending",
    });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
  });

  it("counts an optimistic-only shopping-list command before confirmation", async () => {
    await localDb.optimisticOverlays.add(record("list-a"));
    await appendOutboxCommand({
      id: "list-command",
      userId,
      organizationId,
      commandType: "shopping_list.rename",
      payload: { shopping_list_id: "list-a" },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "pending",
    });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
  });

  it("counts an archived shopping-list command for its event", async () => {
    await localDb.archiveRecords.add({
      ...record("archived-list"),
      eventId: eventA,
      snapshotId: "snapshot-a",
    });
    await appendOutboxCommand({
      id: "archived-command",
      userId,
      organizationId,
      commandType: "shopping_list.rename",
      payload: { shopping_list_id: "archived-list" },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "pending",
    });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
  });

  it("does not count another organization or event list", async () => {
    await localDb.canonicalRecords.bulkAdd([
      record("other-org", "other-org"),
      record("other-event", organizationId, { event_id: eventB }),
    ]);
    await localDb.archiveRecords.add({
      ...record("other-archive", "other-org"),
      eventId: eventA,
      snapshotId: "snapshot-other-org",
    });
    await localDb.archiveRecords.add({
      ...record("other-archive-event"),
      eventId: eventB,
      snapshotId: "snapshot-other-event",
    });
    await appendOutboxCommand({
      id: "other-org-command",
      userId,
      organizationId,
      commandType: "shopping_list.rename",
      payload: { shopping_list_id: "other-org" },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "pending",
    });
    await appendOutboxCommand({
      id: "other-event-command",
      userId,
      organizationId,
      commandType: "shopping_list.rename",
      payload: { shopping_list_id: "other-event" },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "pending",
    });
    await appendOutboxCommand({
      id: "other-archive-event-command",
      userId,
      organizationId,
      commandType: "shopping_list.rename",
      payload: { shopping_list_id: "other-archive-event" },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "pending",
    });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("0 čekajících změn")).toBeInTheDocument();
  });

  it("counts direct event commands", async () => {
    await appendOutboxCommand({
      id: "event-command",
      userId,
      organizationId,
      commandType: "event.update",
      payload: { event_id: eventA },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "pending",
    });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
  });

  it("makes failed and pending outbox counts discoverable together", async () => {
    await appendOutboxCommand({
      id: "pending",
      userId,
      organizationId,
      commandType: "event.update",
      payload: { event_id: eventA },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "pending",
    });
    await appendOutboxCommand({
      id: "failed",
      userId,
      organizationId,
      commandType: "event.update",
      payload: { event_id: eventA },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "failed",
    });
    render(<SummaryRoute eventId={eventA} />);
    expect(
      await screen.findByText("1 neúspěšných změn · 1 čekajících změn"),
    ).toBeInTheDocument();
  });

  it("clears the previous route status before showing the next event", async () => {
    await appendOutboxCommand({
      id: "pending-a",
      userId,
      organizationId,
      commandType: "event.update",
      payload: { event_id: eventA },
      actionAt: new Date().toISOString(),
      createdAt: new Date().toISOString(),
      state: "pending",
    });
    const view = render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
    view.rerender(<SummaryRoute eventId={eventB} />);
    await waitFor(() =>
      expect(screen.getByText("0 čekajících změn")).toBeInTheDocument(),
    );
    expect(screen.queryByText("1 čekajících změn")).toBeNull();
  });

  it("shows archive only for an authorized active event and queues the stable event id", async () => {
    await localDb.canonicalRecords.bulkAdd([
      eventRecord("active"),
      capability(),
    ]);
    const user = userEvent.setup();
    render(<SummaryRoute eventId={eventA} />);
    const archive = await screen.findByRole("button", {
      name: "Archivovat akci",
    });
    await user.click(archive);
    await user.click(
      screen.getByRole("button", { name: "Potvrdit archivaci" }),
    );
    await waitFor(async () =>
      expect(
        (await localDb.outbox.toArray()).filter(
          (command) => command.commandType === "event.lifecycle",
        ),
      ).toHaveLength(1),
    );
    expect(
      (await localDb.outbox.toArray()).find(
        (command) => command.commandType === "event.lifecycle",
      )?.payload,
    ).toMatchObject({ event_id: eventA, operation: "archive" });
  });

  it("shows archived read-only state and authorized reactivation/duplication actions", async () => {
    const snapshotId = "snapshot-a";
    await localDb.canonicalRecords.bulkAdd([
      eventRecord("archived", snapshotId),
      capability(),
    ]);
    const user = userEvent.setup();
    render(<SummaryRoute eventId={eventA} lifecycle="archived" />);
    expect(
      await screen.findByText("Archivovaná akce je jen pro čtení."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Znovu aktivovat akci" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Duplikovat plán" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Duplikovat plán" }));
    await waitFor(async () =>
      expect(
        (await localDb.outbox.toArray()).filter(
          (command) => command.commandType === "event.duplicate",
        ),
      ).toHaveLength(1),
    );
    expect(
      (await localDb.outbox.toArray()).find(
        (command) => command.commandType === "event.duplicate",
      )?.payload,
    ).toMatchObject({
      source_event_id: eventA,
      source_archive_snapshot_id: snapshotId,
    });
  });

  it("lets ordinary members duplicate archived events without lifecycle controls", async () => {
    await localDb.canonicalRecords.bulkAdd([
      eventRecord("archived", "snapshot-member"),
      capability("member"),
    ]);
    const user = userEvent.setup();
    render(<SummaryRoute eventId={eventA} lifecycle="archived" />);
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Archivovat akci" }),
      ).toBeNull(),
    );
    await user.click(
      await screen.findByRole("button", { name: "Duplikovat plán" }),
    );
    await waitFor(async () =>
      expect(
        (await localDb.outbox.toArray()).filter(
          (command) => command.commandType === "event.duplicate",
        ),
      ).toHaveLength(1),
    );
    expect(
      (await localDb.outbox.toArray()).find(
        (command) => command.commandType === "event.duplicate",
      )?.payload,
    ).toMatchObject({
      source_event_id: eventA,
      source_archive_snapshot_id: "snapshot-member",
    });
  });
});
