import { appendOutboxCommand, localDb } from "./local-db";

/** Queue a snapshot-guarded copy. The server supplies the copied graph atomically. */
export async function queueEventDuplicate(
  userId: string,
  organizationId: string,
  sourceEventId: string,
  sourceArchiveSnapshotId: string,
  name: string,
): Promise<void> {
  const actionAt = new Date().toISOString();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.outbox,
    async () => {
      const source = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "event",
        sourceEventId,
      ]);
      if (
        source?.fields.lifecycle !== "archived" ||
        source?.fields.current_archive_snapshot_id !== sourceArchiveSnapshotId
      )
        throw new Error("event");
      await appendOutboxCommand({
        id: crypto.randomUUID(),
        userId,
        organizationId,
        commandType: "event.duplicate",
        payload: {
          event_id: crypto.randomUUID(),
          source_event_id: sourceEventId,
          source_archive_snapshot_id: sourceArchiveSnapshotId,
          name,
        },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}
