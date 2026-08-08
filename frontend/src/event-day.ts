import { appendOutboxCommand, localDb } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type VisibilityInput = { eventDayId: string; eventId: string; isVisible: boolean };

function wins(clock: unknown, mutationId: string, actionAt: string): boolean {
  if (clock === undefined || clock === null) return true;
  if (typeof clock !== "object" || Array.isArray(clock)) return false;
  const value = clock as Record<string, unknown>;
  const at = typeof value.actionAt === "string" ? value.actionAt : value.winning_client_wall_time;
  const id = typeof value.mutationId === "string" ? value.mutationId : value.winning_mutation_id;
  const candidate = Date.parse(actionAt);
  const current = typeof at === "string" ? Date.parse(at) : NaN;
  return typeof id === "string" && Number.isFinite(candidate) && Number.isFinite(current) && (candidate > current || (candidate === current && mutationId > id));
}

async function apply(userId: string, organizationId: string, input: VisibilityInput, mutationId: string, actionAt: string): Promise<boolean> {
  const [canonicalEvent, canonicalDay] = await Promise.all([
    localDb.canonicalRecords.get([userId, organizationId, "event", input.eventId]),
    localDb.canonicalRecords.get([userId, organizationId, "event_day", input.eventDayId]),
  ]);
  if (canonicalEvent?.lifecycle === "retired" || canonicalDay?.lifecycle === "retired") return false;
  const [event, day] = await Promise.all([
    localDb.optimisticOverlays.get([userId, organizationId, "event", input.eventId]),
    localDb.optimisticOverlays.get([userId, organizationId, "event_day", input.eventDayId]),
  ]);
  const visibleEvent = event ?? canonicalEvent;
  const visibleDay = day ?? canonicalDay;
  if (visibleEvent?.lifecycle !== "active" || visibleEvent.fields.lifecycle !== "active" || visibleDay?.lifecycle !== "active" || visibleDay.fields.event_id !== input.eventId || !wins(visibleDay.fieldClocks.is_visible, mutationId, actionAt)) return false;
  await localDb.optimisticOverlays.put({
    ...visibleDay,
    fields: { ...visibleDay.fields, is_visible: input.isVisible },
    fieldClocks: { ...visibleDay.fieldClocks, is_visible: { mutationId, actionAt } },
    updatedAt: actionAt,
  });
  return true;
}

export async function queueEventDayVisibility(userId: string, organizationId: string, input: VisibilityInput): Promise<void> {
  if (![input.eventDayId, input.eventId].every((value) => uuid.test(value))) throw new Error("selection");
  const id = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    if (!(await apply(userId, organizationId, input, id, actionAt))) throw new Error("selection");
    await appendOutboxCommand({ id, userId, organizationId, commandType: "event_day.visibility", payload: { event_day_id: input.eventDayId, event_id: input.eventId, is_visible: input.isVisible }, actionAt, createdAt: actionAt, state: "pending" });
  });
}

export async function replayEventDayVisibility(userId: string, organizationId: string, command: { id: string; actionAt: string; payload: Record<string, unknown> }): Promise<void> {
  const payload = command.payload;
  if (Object.keys(payload).length !== 3 || typeof payload.event_day_id !== "string" || typeof payload.event_id !== "string" || typeof payload.is_visible !== "boolean" || ![command.id, payload.event_day_id, payload.event_id].every((value) => uuid.test(value)) || !Number.isFinite(Date.parse(command.actionAt))) return;
  await apply(userId, organizationId, { eventDayId: payload.event_day_id, eventId: payload.event_id, isVisible: payload.is_visible }, command.id, command.actionAt);
}
