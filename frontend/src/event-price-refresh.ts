import { appendOutboxCommand, localDb } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Queue the server-current refresh only; local catalog values never masquerade as refreshed prices. */
export async function queueEventPriceRefresh(
  userId: string,
  organizationId: string,
  eventId: string,
): Promise<boolean> {
  if (![userId, organizationId, eventId].every((value) => uuid.test(value)))
    throw new Error("event");
  const actionAt = new Date().toISOString();
  return localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.outbox,
    async () => {
      const event = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]);
      if (event?.lifecycle !== "active" || event.fields?.lifecycle !== "active")
        throw new Error("event");
      const exists = (
        await localDb.outbox
          .where("[userId+organizationId+state]")
          .equals([userId, organizationId, "pending"])
          .toArray()
      ).some(
        (command) =>
          command.commandType === "event.update_price_estimates" &&
          command.payload.event_id === eventId,
      );
      if (exists) return false;
      await appendOutboxCommand({
        id: crypto.randomUUID(),
        userId,
        organizationId,
        commandType: "event.update_price_estimates",
        payload: { event_id: eventId },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
      return true;
    },
  );
}

export async function eventPriceRefreshPending(
  userId: string,
  organizationId: string,
  eventId: string,
): Promise<boolean> {
  return (
    await localDb.outbox
      .where("[userId+organizationId+state]")
      .equals([userId, organizationId, "pending"])
      .toArray()
  ).some(
    (command) =>
      command.commandType === "event.update_price_estimates" &&
      command.payload.event_id === eventId,
  );
}
