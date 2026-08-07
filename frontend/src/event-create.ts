import { localDb } from "./local-db";

export type EventCreateInput = {
  name: string;
  startDate: string;
  endDate: string;
  baseExpectedAttendance: string;
  budgetAmount: string;
  location: string;
  generalNote: string;
};

export type EventCreateValidationError =
  | "name"
  | "startDate"
  | "endDate"
  | "dateRange"
  | "attendance"
  | "budget"
  | "location";

const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const integer = /^(?:0|[1-9]\d*)$/;
const calendarDate = /^\d{4}-\d{2}-\d{2}$/;

function dateMilliseconds(value: string): number | undefined {
  if (!calendarDate.test(value)) return undefined;
  const [year, month, day] = value.split("-").map(Number);
  const timestamp = Date.UTC(year, month - 1, day);
  const date = new Date(timestamp);
  return date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
    ? timestamp
    : undefined;
}

export function validateEventCreate(
  input: EventCreateInput,
): EventCreateValidationError | undefined {
  if (!input.name.trim() || input.name.trim().length > 200) return "name";
  const start = dateMilliseconds(input.startDate);
  if (start === undefined) return "startDate";
  const end = dateMilliseconds(input.endDate);
  if (end === undefined) return "endDate";
  if (end < start || end - start >= 366 * 24 * 60 * 60 * 1000)
    return "dateRange";
  if (
    !integer.test(input.baseExpectedAttendance) ||
    !Number.isSafeInteger(Number(input.baseExpectedAttendance))
  )
    return "attendance";
  if (!decimal.test(input.budgetAmount)) return "budget";
  if (input.location.trim().length > 300) return "location";
}

/** Persist one event create intent and its visible projection together. */
export async function queueEventCreate(
  userId: string,
  organizationId: string,
  input: EventCreateInput,
): Promise<string> {
  const error = validateEventCreate(input);
  if (error) throw new Error(error);

  const now = new Date().toISOString();
  const eventId = crypto.randomUUID();
  const mutationId = crypto.randomUUID();
  const location = input.location.trim() || undefined;
  const generalNote = input.generalNote || undefined;
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      const organization = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "organization",
        organizationId,
      ]);
      const currency = organization?.fields.default_currency;
      const defaultCurrency =
        typeof currency === "string" && /^[A-Z]{3}$/.test(currency)
          ? currency
          : undefined;
      if (!defaultCurrency) throw new Error("organizationCurrency");
      const payload = {
        event_id: eventId,
        name: input.name.trim(),
        start_date: input.startDate,
        end_date: input.endDate,
        base_expected_attendance: Number(input.baseExpectedAttendance),
        budget_amount: input.budgetAmount,
        ...(location ? { location } : {}),
        ...(generalNote ? { general_note: generalNote } : {}),
      };
      await localDb.optimisticOverlays.put({
        userId,
        organizationId,
        entityType: "event",
        entityId: eventId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          ...payload,
          id: eventId,
          organization_id: organizationId,
          currency: defaultCurrency,
          lifecycle: "active",
          archived_at: null,
        },
        fieldClocks: { optimistic: { mutationId, actionAt: now } },
        immutable: false,
        updatedAt: now,
      });
      await localDb.outbox.add({
        id: mutationId,
        userId,
        organizationId,
        commandType: "event.create",
        payload,
        actionAt: now,
        createdAt: now,
        state: "pending",
      });
    },
  );
  return eventId;
}
