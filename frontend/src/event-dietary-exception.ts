import {
  appendOutboxCommand,
  localDb,
  type CanonicalRecord,
  readVisibleCanonicalRecord,
  type OutboxCommand,
} from "./local-db";

export type DietaryExceptionInput = {
  name: string;
  note?: string | null;
  tagIds: string[];
};

const text = (value: unknown) =>
  typeof value === "string" ? value.normalize("NFC").trim() : "";
const utf8Bytes = (value: string) => {
  try {
    encodeURIComponent(value);
  } catch {
    return -1;
  }
  return new TextEncoder().encode(value).byteLength;
};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function clockRank(value: string): bigint {
  const match = value.match(/^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$/);
  if (!match) throw new Error("clock");
  const base = Date.parse(`${match[1]}${match[3]}`);
  if (Number.isNaN(base)) throw new Error("clock");
  const fraction = BigInt((match[2] ?? "").padEnd(6, "0").slice(0, 6));
  return BigInt(base) * 1000n + fraction;
}

function active(record: CanonicalRecord | undefined): boolean {
  return (
    record?.lifecycle === "active" &&
    record.fields.lifecycle !== "archived" &&
    record.fields.retired_at == null
  );
}

export async function queueEventDietaryExceptionCreate(
  userId: string,
  organizationId: string,
  eventId: string,
  input: DietaryExceptionInput,
  exceptionId = crypto.randomUUID(),
  mutationId = crypto.randomUUID(),
): Promise<string> {
  const name = text(input.name);
  const note = input.note == null ? null : text(input.note);
  const tags = [...new Set(input.tagIds)];
  if (
    !name ||
    name.length > 200 ||
    (note !== null && (utf8Bytes(note) < 0 || utf8Bytes(note) > 131072))
  )
    throw new Error("validation");
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
      if (canonicalEvent && !active(canonicalEvent)) throw new Error("event");
      const event = await readVisibleCanonicalRecord(
        userId,
        organizationId,
        "event",
        eventId,
      );
      if (!active(event)) throw new Error("event");
      for (const tagId of tags) {
        const canonicalTag = await localDb.canonicalRecords.get([
          userId,
          organizationId,
          "dietary_tag",
          tagId,
        ]);
        if (canonicalTag && !active(canonicalTag)) throw new Error("tag");
        const tag = await readVisibleCanonicalRecord(
          userId,
          organizationId,
          "dietary_tag",
          tagId,
        );
        if (!active(tag)) throw new Error("tag");
      }
      const now = new Date().toISOString();
      const exception: CanonicalRecord = {
        userId,
        organizationId,
        entityType: "event_dietary_exception",
        entityId: exceptionId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        immutable: false,
        updatedAt: now,
        fields: {
          id: exceptionId,
          event_id: eventId,
          name,
          note,
          tag_ids: tags,
          retired_at: null,
        },
        fieldClocks: {},
      };
      const command = {
        id: mutationId,
        userId,
        organizationId,
        commandType: "event_dietary_exception.create",
        payload: {
          exception_id: exceptionId,
          event_id: eventId,
          name,
          note,
          tag_ids: tags,
        },
        actionAt: now,
        createdAt: now,
        state: "pending" as const,
      };
      await localDb.optimisticOverlays.put(exception);
      await appendOutboxCommand(command);
    },
  );
  return exceptionId;
}

export async function queueEventDietaryExceptionUpdate(
  userId: string, organizationId: string, eventId: string, exceptionId: string,
  input: DietaryExceptionInput, mutationId = crypto.randomUUID(),
): Promise<void> {
  const name = text(input.name), note = input.note == null ? null : text(input.note);
  const tags = [...new Set(input.tagIds)];
  if (!uuid.test(exceptionId) || !uuid.test(eventId) || !name || name.length > 200 || (note !== null && utf8Bytes(note) > 131072) || tags.some((id) => !uuid.test(id)) || tags.length !== input.tagIds.length) throw new Error("validation");
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    const canonicalEvent = await localDb.canonicalRecords.get([userId, organizationId, "event", eventId]);
    if (canonicalEvent && !active(canonicalEvent)) throw new Error("event");
    if (!active(await readVisibleCanonicalRecord(userId, organizationId, "event", eventId))) throw new Error("event");
    const exception = await readVisibleCanonicalRecord(userId, organizationId, "event_dietary_exception", exceptionId);
    if (!exception || !active(exception) || exception.fields.event_id !== eventId) throw new Error("exception");
    for (const tagId of tags) if (!active(await readVisibleCanonicalRecord(userId, organizationId, "dietary_tag", tagId))) {
      const association = (await localDb.canonicalRecords.where("[userId+organizationId+entityType]").equals([userId, organizationId, "event_dietary_exception_tag"]).toArray()).find((x) => x.fields.exception_id === exceptionId && x.fields.dietary_tag_id === tagId && active(x));
      if (!association) throw new Error("tag");
    }
    const now = new Date().toISOString();
    await applyUpdateOverlay(exception, { name, note, tag_ids: tags }, mutationId, now);
    await appendOutboxCommand({ id: mutationId, userId, organizationId, commandType: "event_dietary_exception.update", payload: { exception_id: exceptionId, event_id: eventId, name, note, tag_ids: tags }, actionAt: now, createdAt: now, state: "pending" });
  });
}

async function applyUpdateOverlay(exception: CanonicalRecord, values: { name: string; note: string | null; tag_ids: string[] }, mutationId: string, actionAt: string) {
  const fields = { ...exception.fields }, fieldClocks = { ...exception.fieldClocks };
  for (const [field, value] of Object.entries(values)) {
    const clock = exception.fieldClocks[field] as Record<string, unknown> | undefined;
    if (clock) {
      const winnerTime = typeof clock.winning_client_wall_time === "string" ? clock.winning_client_wall_time : typeof clock.actionAt === "string" ? clock.actionAt : null;
      const winnerId = typeof clock.winning_mutation_id === "string" ? clock.winning_mutation_id : typeof clock.mutationId === "string" ? clock.mutationId : null;
      if (!winnerTime || !winnerId || (clockRank(winnerTime) > clockRank(actionAt) || clockRank(winnerTime) === clockRank(actionAt) && winnerId >= mutationId)) continue;
    }
    fields[field] = value; fieldClocks[field] = { mutationId, actionAt };
  }
  await localDb.optimisticOverlays.put({ ...exception, updatedAt: actionAt, fields, fieldClocks });
}

export async function replayEventDietaryExceptionUpdate(userId: string, organizationId: string, command: OutboxCommand) {
  if (typeof command.id !== "string" || !uuid.test(command.id) || typeof command.actionAt !== "string" || command.payload === null || typeof command.payload !== "object" || Array.isArray(command.payload) || Object.keys(command.payload).length !== 5 || Object.keys(command.payload).some((key) => !["exception_id", "event_id", "name", "note", "tag_ids"].includes(key))) throw new Error("validation");
  const { event_id: eventId, exception_id: exceptionId, name, note, tag_ids: tagIds } = command.payload;
  if (typeof eventId !== "string" || typeof exceptionId !== "string" || !uuid.test(eventId) || !uuid.test(exceptionId) || typeof name !== "string" || !name.normalize("NFC").trim() || name.normalize("NFC").trim().length > 200 || typeof note !== "string" && note !== null || note !== null && utf8Bytes(note.normalize("NFC").trim()) > 131072 || !Array.isArray(tagIds) || tagIds.some((id) => typeof id !== "string" || !uuid.test(id)) || new Set(tagIds).size !== tagIds.length) throw new Error("validation");
  const canonicalEvent = await localDb.canonicalRecords.get([userId, organizationId, "event", eventId]);
  if (canonicalEvent && !active(canonicalEvent)) throw new Error("event");
  if (!active(await readVisibleCanonicalRecord(userId, organizationId, "event", eventId))) throw new Error("event");
  const canonical = await readVisibleCanonicalRecord(userId, organizationId, "event_dietary_exception", exceptionId);
  if (!canonical || !active(canonical) || canonical.fields.event_id !== eventId) throw new Error("exception");
  for (const tagId of tagIds) if (!active(await readVisibleCanonicalRecord(userId, organizationId, "dietary_tag", tagId))) {
    const association = (await localDb.canonicalRecords.where("[userId+organizationId+entityType]").equals([userId, organizationId, "event_dietary_exception_tag"]).toArray()).find((x) => x.fields.exception_id === exceptionId && x.fields.dietary_tag_id === tagId && active(x));
    if (!association) throw new Error("tag");
  }
  const fields = { ...canonical.fields }, fieldClocks = { ...canonical.fieldClocks };
  for (const [field, value] of [["name", name], ["note", note], ["tag_ids", tagIds]] as const) {
    const clock = canonical.fieldClocks[field];
    if (clock && typeof clock === "object") {
      const wireClock = clock as Record<string, unknown>;
      const winnerTime = typeof wireClock.winning_client_wall_time === "string" ? wireClock.winning_client_wall_time : typeof wireClock.actionAt === "string" ? wireClock.actionAt : null;
      const winnerId = typeof wireClock.winning_mutation_id === "string" ? wireClock.winning_mutation_id : typeof wireClock.mutationId === "string" ? wireClock.mutationId : null;
      if (!winnerTime || !winnerId || !uuid.test(winnerId)) throw new Error("clock");
      const winner = clockRank(winnerTime), candidate = clockRank(command.actionAt);
      if (winner > candidate || (winner === candidate && winnerId >= command.id)) continue;
    }
    fields[field] = value;
    fieldClocks[field] = { mutationId: command.id, actionAt: command.actionAt };
  }
  await applyUpdateOverlay(canonical, { name: fields.name as string, note: fields.note as string | null, tag_ids: fields.tag_ids as string[] }, command.id, command.actionAt);
}

export async function replayEventDietaryExceptionCreate(
  userId: string,
  organizationId: string,
  command: OutboxCommand,
) {
  const eventId = command.payload.event_id;
  const exceptionId = command.payload.exception_id;
  const name = command.payload.name;
  const note = command.payload.note;
  const tagIds = command.payload.tag_ids;
  if (
    typeof eventId !== "string" ||
    typeof exceptionId !== "string" ||
    typeof name !== "string" ||
    (note !== null && typeof note !== "string") ||
    !Array.isArray(tagIds) ||
    tagIds.some((id) => typeof id !== "string")
  )
    throw new Error("validation");
  const canonicalEvent = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "event",
    eventId,
  ]);
  if (canonicalEvent && !active(canonicalEvent)) throw new Error("event");
  const event = await readVisibleCanonicalRecord(
    userId,
    organizationId,
    "event",
    eventId,
  );
  if (!active(event)) throw new Error("event");
  for (const tagId of tagIds) {
    const canonicalTag = await localDb.canonicalRecords.get([
      userId,
      organizationId,
      "dietary_tag",
      tagId,
    ]);
    if (canonicalTag && !active(canonicalTag)) throw new Error("tag");
    const tag = await readVisibleCanonicalRecord(
      userId,
      organizationId,
      "dietary_tag",
      tagId,
    );
    if (!active(tag)) throw new Error("tag");
  }
  const now = command.actionAt;
  await localDb.optimisticOverlays.put({
    userId,
    organizationId,
    entityType: "event_dietary_exception",
    entityId: exceptionId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    immutable: false,
    updatedAt: now,
    fields: {
      id: exceptionId,
      event_id: eventId,
      name,
      note,
      tag_ids: tagIds,
      retired_at: null,
    },
    fieldClocks: {},
  });
}

export async function readVisibleEventDietaryExceptions(
  userId: string,
  organizationId: string,
  eventId: string,
) {
  const records = await localDb.canonicalRecords
    .where("[userId+organizationId+entityType]")
    .equals([userId, organizationId, "event_dietary_exception"])
    .toArray();
  const overlays = await localDb.optimisticOverlays
    .where("[userId+organizationId]")
    .equals([userId, organizationId])
    .toArray();
  const merged = new Map(records.map((record) => [record.entityId, record]));
  for (const record of overlays.filter(
    (item) => item.entityType === "event_dietary_exception",
  ))
    merged.set(record.entityId, record);
  const associations = await localDb.canonicalRecords
    .where("[userId+organizationId+entityType]")
    .equals([userId, organizationId, "event_dietary_exception_tag"])
    .toArray();
  const tags = await localDb.canonicalRecords
    .where("[userId+organizationId+entityType]")
    .equals([userId, organizationId, "dietary_tag"])
    .toArray();
  const tagNames = new Map(
    tags.map((tag) => [
      tag.entityId,
      String(tag.fields.name ?? tag.fields.seed_key ?? tag.entityId),
    ]),
  );
  return [...merged.values()]
    .filter((record) => record.fields.event_id === eventId && active(record))
    .map((record) => {
      const ids = Array.isArray(record.fields.tag_ids)
        ? record.fields.tag_ids.filter(
            (id): id is string => typeof id === "string",
          )
        : associations
            .filter(
              (association) =>
                association.fields.exception_id === record.entityId &&
                active(association),
            )
            .map((association) => String(association.fields.dietary_tag_id));
      return {
        ...record,
        fields: {
          ...record.fields,
          selected_tag_ids: ids,
          selected_tag_names: ids.map((id) => tagNames.get(id) ?? id),
        },
      };
    })
    .sort((a, b) =>
      String((a.fields as Record<string, unknown>).name).localeCompare(
        String((b.fields as Record<string, unknown>).name),
      ),
    );
}
