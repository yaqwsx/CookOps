import { render, screen, waitFor } from "@testing-library/react";
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
  name: "Event A", startDate: "2026-08-15", endDate: "2026-08-15", attendance: 2,
  lifecycle: "active", days: [], hiddenDays: [], retiredDays: [], roles: [],
  retiredRoles: [], recipes: [], ingredients: [], scheduled: [],
};

function SummaryRoute({ eventId }: { eventId: string }) {
  const pendingSync = useEventPendingSync(userId, organizationId, eventId);
  return <EventSummary planner={{ ...planner, name: eventId === eventA ? "Event A" : "Event B" }} pendingSync={pendingSync} />;
}

describe("event summary", () => {
  beforeEach(async () => {
    await Promise.all([localDb.outbox.clear(), localDb.canonicalRecords.clear(), localDb.optimisticOverlays.clear(), localDb.archiveRecords.clear()]);
  });

  const record = (entityId: string, orgId = organizationId, fields = { event_id: eventA }) => ({
    userId, organizationId: orgId, entityType: "shopping_list", entityId, recordSchemaVersion: 1,
    lifecycle: "active" as const, fields, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString(),
  });

  it("counts a canonical shopping-list command for its event", async () => {
    await localDb.canonicalRecords.add(record("list-a"));
    await appendOutboxCommand({ id: "list-command", userId, organizationId, commandType: "shopping_list.rename", payload: { shopping_list_id: "list-a" }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
  });

  it("counts an optimistic-only shopping-list command before confirmation", async () => {
    await localDb.optimisticOverlays.add(record("list-a"));
    await appendOutboxCommand({ id: "list-command", userId, organizationId, commandType: "shopping_list.rename", payload: { shopping_list_id: "list-a" }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
  });

  it("counts an archived shopping-list command for its event", async () => {
    await localDb.archiveRecords.add({ ...record("archived-list"), eventId: eventA, snapshotId: "snapshot-a" });
    await appendOutboxCommand({ id: "archived-command", userId, organizationId, commandType: "shopping_list.rename", payload: { shopping_list_id: "archived-list" }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
  });

  it("does not count another organization or event list", async () => {
    await localDb.canonicalRecords.bulkAdd([record("other-org", "other-org"), record("other-event", organizationId, { event_id: eventB })]);
    await localDb.archiveRecords.add({ ...record("other-archive", "other-org"), eventId: eventA, snapshotId: "snapshot-other-org" });
    await localDb.archiveRecords.add({ ...record("other-archive-event"), eventId: eventB, snapshotId: "snapshot-other-event" });
    await appendOutboxCommand({ id: "other-org-command", userId, organizationId, commandType: "shopping_list.rename", payload: { shopping_list_id: "other-org" }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" });
    await appendOutboxCommand({ id: "other-event-command", userId, organizationId, commandType: "shopping_list.rename", payload: { shopping_list_id: "other-event" }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" });
    await appendOutboxCommand({ id: "other-archive-event-command", userId, organizationId, commandType: "shopping_list.rename", payload: { shopping_list_id: "other-archive-event" }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("0 čekajících změn")).toBeInTheDocument();
  });

  it("counts direct event commands", async () => {
    await appendOutboxCommand({ id: "event-command", userId, organizationId, commandType: "event.update", payload: { event_id: eventA }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
  });

  it("makes failed and pending outbox counts discoverable together", async () => {
    await appendOutboxCommand({ id: "pending", userId, organizationId, commandType: "event.update", payload: { event_id: eventA }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" });
    await appendOutboxCommand({ id: "failed", userId, organizationId, commandType: "event.update", payload: { event_id: eventA }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "failed" });
    render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 neúspěšných změn · 1 čekajících změn")).toBeInTheDocument();
  });

  it("clears the previous route status before showing the next event", async () => {
    await appendOutboxCommand({ id: "pending-a", userId, organizationId, commandType: "event.update", payload: { event_id: eventA }, actionAt: new Date().toISOString(), createdAt: new Date().toISOString(), state: "pending" });
    const view = render(<SummaryRoute eventId={eventA} />);
    expect(await screen.findByText("1 čekajících změn")).toBeInTheDocument();
    view.rerender(<SummaryRoute eventId={eventB} />);
    await waitFor(() => expect(screen.getByText("0 čekajících změn")).toBeInTheDocument());
    expect(screen.queryByText("1 čekajících změn")).toBeNull();
  });
});
