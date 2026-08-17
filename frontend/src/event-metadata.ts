import { appendOutboxCommand, localDb } from "./local-db";
import { parseCalendarDate } from "./event-create";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const fields = ["name", "location", "budget_amount", "general_note", "start_date", "end_date"] as const;
type MetadataField = (typeof fields)[number];

function timestampMicros(value: unknown): bigint | undefined {
  if (typeof value !== "string") return undefined;
  const match = /^(.*?)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$/.exec(value);
  if (!match) return undefined;
  const milliseconds = Date.parse(`${match[1]}${match[3]}`);
  if (!Number.isFinite(milliseconds)) return undefined;
  return BigInt(milliseconds) * 1_000n + BigInt((match[2] ?? "").slice(0, 6).padEnd(6, "0"));
}

function wins(clock: unknown, mutationId: string, actionAt: string): boolean {
  if (clock === undefined || clock === null) return true;
  if (typeof clock !== "object" || Array.isArray(clock)) return false;
  const value = clock as Record<string, unknown>;
  const currentAt = value.actionAt ?? value.winning_client_wall_time;
  const currentId = value.mutationId ?? value.winning_mutation_id;
  const candidate = timestampMicros(actionAt);
  const current = timestampMicros(currentAt);
  return typeof currentId === "string" && uuid.test(currentId) && candidate !== undefined && current !== undefined && (candidate > current || (candidate === current && mutationId > currentId));
}

function name(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const normalized = value.normalize("NFC").trim();
  return normalized && normalized.length <= 200 && !normalized.includes("\0") && !/[\uD800-\uDFFF]/.test(normalized) ? normalized : undefined;
}

function location(value: unknown): string | null | undefined {
  if (value === null) return null;
  if (typeof value !== "string") return undefined;
  const normalized = value.normalize("NFC").trim();
  return normalized.length <= 300 && !normalized.includes("\0") && !/[\uD800-\uDFFF]/.test(normalized) ? normalized || null : undefined;
}

function note(value: unknown): string | null | undefined {
  if (value === null) return null;
  if (typeof value !== "string") return undefined;
  const normalized = value.normalize("NFC").replace(/\r\n?/g, "\n");
  return normalized.length <= 4000 && !normalized.includes("\0") && !/[\uD800-\uDFFF]/.test(normalized) ? normalized || null : undefined;
}

function budget(value: unknown): string | undefined {
  return typeof value === "string" && value.length <= 100 && decimal.test(value) ? value : undefined;
}

async function currentEvent(userId: string, organizationId: string, eventId: string) {
  const canonical = await localDb.canonicalRecords.get([userId, organizationId, "event", eventId]);
  if (canonical?.lifecycle === "retired") return undefined;
  const event = await localDb.optimisticOverlays.get([userId, organizationId, "event", eventId]) ?? canonical;
  return event?.lifecycle === "active" && event.fields.lifecycle === "active" && event.fields.id === eventId && event.fields.organization_id === organizationId ? event : undefined;
}

async function apply(
  userId: string,
  organizationId: string,
  eventId: string,
  values: Partial<Record<MetadataField, unknown>>,
  mutationId: string,
  actionAt: string,
  selectedFields: readonly MetadataField[] = fields,
): Promise<boolean> {
  const event = await currentEvent(userId, organizationId, eventId);
  if (!event) return false;
  const nextFields = { ...event.fields };
  const nextClocks = { ...event.fieldClocks };
  let changed = false;
  for (const field of selectedFields) {
    if (!wins(nextClocks[field], mutationId, actionAt)) continue;
    nextFields[field] = values[field];
    nextClocks[field] = { mutationId, actionAt };
    changed = true;
  }
  if (!changed) return false;
  await localDb.optimisticOverlays.put({ ...event, fields: nextFields, fieldClocks: nextClocks, updatedAt: actionAt });
  return true;
}

export type EventMetadataInput = {
  eventId: string;
  name: string;
  location: string;
  budgetAmount: string;
  generalNote: string;
  startDate: string;
  endDate: string;
};

function values(input: EventMetadataInput, includeDates = true): Partial<Record<MetadataField, unknown>> | undefined {
  const normalizedName = name(input.name);
  const normalizedLocation = location(input.location);
  const normalizedNote = note(input.generalNote);
  const normalizedStart = parseCalendarDate(input.startDate);
  const normalizedEnd = parseCalendarDate(input.endDate);
  if (!normalizedName || normalizedLocation === undefined || normalizedNote === undefined || !budget(input.budgetAmount)) return undefined;
  if (includeDates && (!normalizedStart || !normalizedEnd || normalizedEnd < normalizedStart || (Date.parse(`${normalizedEnd}T00:00:00Z`) - Date.parse(`${normalizedStart}T00:00:00Z`)) / 86400000 >= 366)) return undefined;
  return { name: normalizedName, location: normalizedLocation, budget_amount: input.budgetAmount, general_note: normalizedNote, ...(includeDates ? { start_date: normalizedStart, end_date: normalizedEnd } : {}) };
}

export async function queueEventMetadataUpdate(userId: string, organizationId: string, input: EventMetadataInput): Promise<void> {
  const normalized = values(input);
  if (!uuid.test(input.eventId) || !normalized) throw new Error("metadata");
  const id = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    if (!(await apply(userId, organizationId, input.eventId, normalized, id, actionAt))) throw new Error("event");
    await appendOutboxCommand({ id, userId, organizationId, commandType: "event.metadata", payload: { event_id: input.eventId, ...normalized }, actionAt, createdAt: actionAt, state: "pending" });
  });
}

export async function replayEventMetadataUpdate(userId: string, organizationId: string, command: { id: string; actionAt: string; payload: Record<string, unknown> }): Promise<void> {
  const payload = command.payload;
  if (!uuid.test(command.id) || timestampMicros(command.actionAt) === undefined || !uuid.test(String(payload.event_id))) return;
  const keys = Object.keys(payload);
  const legacy = keys.length === 5 && keys.includes("event_id") && keys.every((key) => ["event_id", "name", "location", "budget_amount", "general_note"].includes(key));
  const modern = keys.length === 7 && keys.includes("start_date") && keys.includes("end_date") && keys.every((key) => ["event_id", "name", "location", "budget_amount", "general_note", "start_date", "end_date"].includes(key));
  if (!legacy && !modern) return;
  const normalized = values({ eventId: String(payload.event_id), name: String(payload.name ?? ""), location: payload.location === null ? "" : String(payload.location ?? "\u0000"), budgetAmount: String(payload.budget_amount ?? ""), generalNote: payload.general_note === null ? "" : String(payload.general_note ?? "\u0000"), startDate: String(payload.start_date ?? ""), endDate: String(payload.end_date ?? "") }, modern);
  if (!normalized || payload.location !== null && location(payload.location) !== payload.location || payload.general_note !== null && note(payload.general_note) !== payload.general_note || payload.name !== normalized.name || payload.budget_amount !== normalized.budget_amount || modern && (payload.start_date !== normalized.start_date || payload.end_date !== normalized.end_date)) return;
  await apply(userId, organizationId, String(payload.event_id), normalized, command.id, command.actionAt, modern ? fields : fields.slice(0, 4));
}
