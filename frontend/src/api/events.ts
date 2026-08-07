export type EventSummary = {
  id: string;
  organizationId: string;
  name: string;
  startDate: string;
  endDate: string;
  baseExpectedAttendance: number;
  budgetAmount: string;
  currency: string;
  lifecycle: "active" | "archived";
  archivedAt: string | null;
};

export type EventPage = {
  events: EventSummary[];
  nextCursor: string | null;
};

export class EventRequestError extends Error {
  constructor(readonly status: number) {
    super("Event request failed.");
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function string(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function eventSummary(
  value: unknown,
  expectedOrganizationId: string,
): EventSummary | null {
  if (!isRecord(value)) return null;
  const id = string(value.id);
  const organizationId = string(value.organization_id);
  const name = string(value.name);
  const startDate = string(value.start_date);
  const endDate = string(value.end_date);
  const budgetAmount = string(value.budget_amount);
  const currency = string(value.currency);
  const attendance = value.base_expected_attendance;
  const lifecycle = value.lifecycle;
  const archivedAt = value.archived_at;
  if (
    !id ||
    organizationId !== expectedOrganizationId ||
    !name ||
    !startDate ||
    !endDate ||
    !budgetAmount ||
    !currency ||
    typeof attendance !== "number" ||
    !Number.isSafeInteger(attendance) ||
    (lifecycle !== "active" && lifecycle !== "archived") ||
    (archivedAt !== null && typeof archivedAt !== "string")
  ) {
    return null;
  }
  return {
    id,
    organizationId,
    name,
    startDate,
    endDate,
    baseExpectedAttendance: attendance,
    budgetAmount,
    currency,
    lifecycle,
    archivedAt,
  };
}

function parseEventPage(value: unknown, organizationId: string): EventPage {
  if (!isRecord(value) || !Array.isArray(value.events)) {
    throw new Error("Invalid event response.");
  }
  const events = value.events.map((event) =>
    eventSummary(event, organizationId),
  );
  const nextCursor = value.next_cursor;
  if (
    events.some((event) => event === null) ||
    (nextCursor !== null && typeof nextCursor !== "string")
  ) {
    throw new Error("Invalid event response.");
  }
  return { events: events as EventSummary[], nextCursor };
}

export async function getEventPage(
  organizationId: string,
  cursor?: string,
  refresh = false,
): Promise<EventPage> {
  const parameters = new URLSearchParams({ page_size: "25" });
  if (cursor) parameters.set("cursor", cursor);
  const response = await fetch(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/events?${parameters}`,
    { cache: refresh ? "no-store" : "default", credentials: "same-origin" },
  );
  if (!response.ok) throw new EventRequestError(response.status);
  return parseEventPage(await response.json(), organizationId);
}
