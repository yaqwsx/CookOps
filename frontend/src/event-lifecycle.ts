import { appendOutboxCommand, localDb } from "./local-db";

export type EventLifecycleOperation = "archive" | "reactivate";

/** Queue a guarded online lifecycle intent without locally applying server history. */
export async function queueEventLifecycle(
  userId: string,
  organizationId: string,
  eventId: string,
  operation: EventLifecycleOperation,
): Promise<void> {
  if (!navigator.onLine) throw new Error("online");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.outbox,
    async () => {
      const canonical = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]);
      const event = canonical;
      const expected = operation === "archive" ? "active" : "archived";
      if (!event || event.fields.lifecycle !== expected)
        throw new Error("event");
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "event.lifecycle",
        payload: { event_id: eventId, operation },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}
