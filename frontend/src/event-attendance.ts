import { isNonnegativeSafeInteger } from "./event-create";
import { appendOutboxCommand, localDb } from "./local-db";

export type EventAttendanceValidationError = "attendance" | "event";

export function validateEventAttendance(
  value: string,
): EventAttendanceValidationError | undefined {
  return isNonnegativeSafeInteger(value) ? undefined : "attendance";
}

/** Persist one attendance edit and its pending visible overlay together. */
export async function queueEventAttendanceUpdate(
  userId: string,
  organizationId: string,
  eventId: string,
  value: string,
): Promise<void> {
  const error = validateEventAttendance(value);
  if (error) throw new Error(error);

  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      const canonicalEvent = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "event",
        eventId,
      ]);
      if (canonicalEvent?.lifecycle === "retired") throw new Error("event");
      const event =
        (await localDb.optimisticOverlays.get([
          userId,
          organizationId,
          "event",
          eventId,
        ])) ?? canonicalEvent;
      if (event?.fields.lifecycle !== "active") throw new Error("event");
      await localDb.optimisticOverlays.put({
        ...event,
        fields: {
          ...event.fields,
          base_expected_attendance: Number(value),
        },
        fieldClocks: {
          ...event.fieldClocks,
          base_expected_attendance: { mutationId, actionAt },
        },
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "event.update_base_attendance",
        payload: { event_id: eventId, base_expected_attendance: Number(value) },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}
