import { appendOutboxCommand, localDb, readOfflineAuthorization, type CanonicalRecord } from "./local-db";
import { timestampNanoseconds } from "./timestamp";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const fields = ["name", "description", "default_currency"] as const;
type Metadata = { name: string; description: string | null; default_currency: string };

const supportedCurrencies = (() => {
  try {
    return new Set(Intl.supportedValuesOf("currency"));
  } catch {
    return undefined;
  }
})();

function object(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value); }
function validText(value: unknown, max: number): value is string { return typeof value === "string" && value.normalize("NFC").trim() === value && value.length > 0 && value.length <= max && !value.includes("\0"); }
function validCurrency(value: unknown): value is string { return typeof value === "string" && /^[A-Z]{3}$/.test(value) && supportedCurrencies?.has(value) === true; }
function wins(actionAt: string, mutationId: string, current: unknown): boolean {
  if (current === undefined || current === null) return true;
  if (!object(current)) return false;
  const currentAt = typeof current.actionAt === "string" ? current.actionAt : current.winning_client_wall_time;
  const currentId = typeof current.mutationId === "string" ? current.mutationId : current.winning_mutation_id;
  if (typeof currentAt !== "string" || typeof currentId !== "string") return false;
  const next = timestampNanoseconds(actionAt), old = timestampNanoseconds(currentAt);
  return next !== undefined && old !== undefined && (next > old || (next === old && mutationId > currentId));
}
function payload(value: unknown): value is Record<string, unknown> {
  if (!object(value) || Object.keys(value).sort().join("\0") !== ["default_currency", "description", "name", "organization_id"].join("\0")) return false;
  return uuid.test(String(value.organization_id)) && validText(value.name, 200) && (value.description === null || validText(value.description, 10000)) && validCurrency(value.default_currency);
}
function metadata(record: CanonicalRecord): Metadata | undefined {
  const fields = record.fields;
  return validText(fields.name, 200) && (fields.description === null || fields.description === undefined || validText(fields.description, 10000)) && typeof fields.default_currency === "string" ? { name: fields.name, description: fields.description === undefined ? null : fields.description, default_currency: fields.default_currency } : undefined;
}

export async function queueOrganizationMetadata(userId: string, organizationId: string, input: Metadata): Promise<string | undefined> {
  if (!uuid.test(userId) || !uuid.test(organizationId) || !validText(input.name, 200) || (input.description !== null && !validText(input.description, 10000)) || !validCurrency(input.default_currency) || (!navigator.onLine && !(await readOfflineAuthorization(userId, organizationId))) ) return;
  const id = crypto.randomUUID(), actionAt = new Date().toISOString();
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    const canonical = await localDb.canonicalRecords.get([userId, organizationId, "organization", organizationId]);
    if (canonical?.lifecycle !== "active") return;
    const current = await localDb.optimisticOverlays.get([userId, organizationId, "organization", organizationId]) ?? canonical;
    if (current.lifecycle !== "active") return;
    await localDb.optimisticOverlays.put({ ...current, fields: { ...current.fields, ...input }, fieldClocks: { ...current.fieldClocks, ...Object.fromEntries(fields.map((field) => [field, { mutationId: id, actionAt }])) }, updatedAt: actionAt });
    await appendOutboxCommand({ id, userId, organizationId, commandType: "organization.update", payload: { organization_id: organizationId, name: input.name, description: input.description, default_currency: input.default_currency }, actionAt, createdAt: actionAt, state: "pending" });
  });
  return id;
}

export async function replayOrganizationMetadata(userId: string, organizationId: string, command: { id: string; actionAt: string; payload: Record<string, unknown> }): Promise<void> {
  if (!uuid.test(userId) || !uuid.test(organizationId) || !uuid.test(command.id) || timestampNanoseconds(command.actionAt) === undefined || !payload(command.payload) || command.payload.organization_id !== organizationId) return;
  const canonical = await localDb.canonicalRecords.get([userId, organizationId, "organization", organizationId]);
  if (canonical?.lifecycle !== "active") return;
  const current = await localDb.optimisticOverlays.get([userId, organizationId, "organization", organizationId]) ?? canonical;
  const nextFields = { ...current.fields }, nextClocks = { ...current.fieldClocks };
  for (const field of fields) if (wins(command.actionAt, command.id, current.fieldClocks[field])) { nextFields[field] = command.payload[field]; nextClocks[field] = { mutationId: command.id, actionAt: command.actionAt }; }
  await localDb.optimisticOverlays.put({ ...current, fields: nextFields, fieldClocks: nextClocks, updatedAt: command.actionAt });
}

export function readOrganizationMetadata(record: CanonicalRecord | undefined): Metadata | undefined { return record && metadata(record); }
