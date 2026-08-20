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
  beforeEach(async () => localDb.outbox.clear());

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
