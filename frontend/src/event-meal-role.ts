import { appendOutboxCommand, localDb } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function validName(name: unknown): name is string {
  return typeof name === "string" && name.length > 0 && Array.from(name).length <= 200 && !name.includes("\0") && !/[\uD800-\uDFFF]/.test(name);
}

function timestampNanoseconds(value: string): bigint | undefined {
  const match = /^(.*?)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$/.exec(value);
  if (!match) return undefined;
  const seconds = Date.parse(`${match[1]}${match[3]}`);
  if (!Number.isFinite(seconds)) return undefined;
  return BigInt(seconds) * 1_000_000n + BigInt((match[2] ?? "").slice(0, 6).padEnd(6, "0"));
}

async function apply(userId: string, organizationId: string, eventId: string, roleId: string, name: string, mutationId: string, actionAt: string): Promise<boolean> {
  const canonicalEvent = await localDb.canonicalRecords.get([userId, organizationId, "event", eventId]);
  if (canonicalEvent?.lifecycle === "retired") return false;
  const event = (await localDb.optimisticOverlays.get([userId, organizationId, "event", eventId])) ?? canonicalEvent;
  if (event?.lifecycle !== "active" || event.fields.lifecycle !== "active") return false;
  const roles = await Promise.all([
    localDb.canonicalRecords.where("[userId+organizationId+entityType]").equals([userId, organizationId, "event_meal_role"]).toArray(),
    localDb.optimisticOverlays.where("[userId+organizationId+entityType]").equals([userId, organizationId, "event_meal_role"]).toArray(),
  ]);
  if (roles.flat().some((role) => role.lifecycle === "active" && role.fields.event_id === eventId && role.fields.normalized_custom_name === name.toLowerCase())) return false;
  await localDb.optimisticOverlays.put({ userId, organizationId, entityType: "event_meal_role", entityId: roleId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: roleId, event_id: eventId, source_preset_id: null, built_in_translation_key: null, custom_name: name, normalized_custom_name: name.toLowerCase(), position_key: `z${roleId.replaceAll("-", "")}`, retired_at: null }, fieldClocks: { position_key: { mutationId, actionAt } }, immutable: false, updatedAt: actionAt });
  return true;
}

export async function queueEventMealRoleCreate(userId: string, organizationId: string, input: { eventId: string; customName: string }): Promise<void> {
  const name = typeof input.customName === "string" ? input.customName.normalize("NFC").trim() : input.customName;
  if (!uuid.test(input.eventId) || !validName(name)) throw new Error("selection");
  const id = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    if (!(await apply(userId, organizationId, input.eventId, id, name, id, actionAt))) throw new Error("selection");
    await appendOutboxCommand({ id, userId, organizationId, commandType: "event_meal_role.create", payload: { event_meal_role_id: id, event_id: input.eventId, custom_name: name }, actionAt, createdAt: actionAt, state: "pending" });
  });
}

export async function replayEventMealRoleCreate(userId: string, organizationId: string, command: { id: string; actionAt: string; payload: Record<string, unknown> }): Promise<void> {
  const payload = command.payload;
  if (Object.keys(payload).length !== 3 || typeof payload.event_meal_role_id !== "string" || typeof payload.event_id !== "string" || ![command.id, payload.event_meal_role_id, payload.event_id].every((value) => uuid.test(value)) || !Number.isFinite(Date.parse(command.actionAt)) || !validName(payload.custom_name) || payload.custom_name.normalize("NFC").trim() !== payload.custom_name) return;
  await apply(userId, organizationId, payload.event_id, payload.event_meal_role_id, payload.custom_name, command.id, command.actionAt);
}

async function applyPosition(userId: string, organizationId: string, eventId: string, roleId: string, positionKey: string, mutationId: string, actionAt: string): Promise<boolean> {
  const canonicalEvent = await localDb.canonicalRecords.get([userId, organizationId, "event", eventId]);
  const event = (await localDb.optimisticOverlays.get([userId, organizationId, "event", eventId])) ?? canonicalEvent;
  const canonicalRole = await localDb.canonicalRecords.get([userId, organizationId, "event_meal_role", roleId]);
  const role = (await localDb.optimisticOverlays.get([userId, organizationId, "event_meal_role", roleId])) ?? canonicalRole;
  if (canonicalEvent?.lifecycle === "retired" || event?.fields.lifecycle !== "active" || role?.lifecycle !== "active" || role.fields.event_id !== eventId) return false;
  const clock = role.fieldClocks.position_key as Record<string, unknown> | null | undefined;
  if (clock && typeof clock === "object") {
    const currentAt = typeof clock.actionAt === "string" ? clock.actionAt : clock.winning_client_wall_time;
    const currentId = typeof clock.mutationId === "string" ? clock.mutationId : clock.winning_mutation_id;
    const candidate = timestampNanoseconds(actionAt);
    const current = typeof currentAt === "string" ? timestampNanoseconds(currentAt) : undefined;
    if (typeof currentId !== "string" || candidate === undefined || current === undefined || !uuid.test(currentId) || candidate < current || (candidate === current && mutationId <= currentId)) return false;
  }
  await localDb.optimisticOverlays.put({ ...role, fields: { ...role.fields, position_key: positionKey }, fieldClocks: { ...role.fieldClocks, position_key: { mutationId, actionAt } }, updatedAt: actionAt });
  return true;
}

export async function queueEventMealRolePosition(userId: string, organizationId: string, input: { eventId: string; eventMealRoleId: string; positionKey: string }): Promise<void> {
  if (![input.eventId, input.eventMealRoleId].every((value) => uuid.test(value)) || !/^[0-9A-Za-z]{1,255}$/.test(input.positionKey)) throw new Error("selection");
  const id = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    if (!(await applyPosition(userId, organizationId, input.eventId, input.eventMealRoleId, input.positionKey, id, actionAt))) throw new Error("selection");
    await appendOutboxCommand({ id, userId, organizationId, commandType: "event_meal_role.position", payload: { event_meal_role_id: input.eventMealRoleId, event_id: input.eventId, position_key: input.positionKey }, actionAt, createdAt: actionAt, state: "pending" });
  });
}

export async function replayEventMealRolePosition(userId: string, organizationId: string, command: { id: string; actionAt: string; payload: Record<string, unknown> }): Promise<void> {
  const payload = command.payload;
  if (Object.keys(payload).length !== 3 || typeof payload.event_meal_role_id !== "string" || typeof payload.event_id !== "string" || typeof payload.position_key !== "string" || ![command.id, payload.event_meal_role_id, payload.event_id].every((value) => uuid.test(value)) || !Number.isFinite(Date.parse(command.actionAt)) || !/^[0-9A-Za-z]{1,255}$/.test(payload.position_key)) return;
  await applyPosition(userId, organizationId, payload.event_id, payload.event_meal_role_id, payload.position_key, command.id, command.actionAt);
}
