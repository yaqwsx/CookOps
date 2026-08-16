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
    utf8Bytes(name) < 0 ||
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
