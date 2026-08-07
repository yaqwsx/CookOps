import {
  compareOutboxCommands,
  localDb,
  readOrCreateBrowserInstallationId,
  type OrganizationSyncMetadata,
  type OutboxCommand,
} from "./local-db";

export const MAX_COMMANDS_PER_PUSH = 100;
export const MAX_PUSH_BYTES = 1024 * 1024;

type SyncCommand = {
  mutation_id: string;
  command_kind: string;
  command_schema_version: 1;
  client_wall_time: string;
  payload: Record<string, unknown>;
};

type PushOutcome = {
  mutation_id: string;
  command_kind: string;
  status: "accepted" | "partially_superseded" | "rejected";
  error: { code: string } | null;
};

type PushResponse = {
  sync_schema_version: 1;
  server_time: string;
  change_cursor: string;
  clock_skew_warning: {
    difference_seconds: number;
    server_time: string;
  } | null;
  outcomes: PushOutcome[];
};

export type DispatchOutboxOptions = {
  userId?: string;
  clientInstallationId?: string;
  fetch?: typeof fetch;
  now?: () => Date;
};

const encoder = new TextEncoder();

function commandForPush(command: OutboxCommand): SyncCommand {
  return {
    mutation_id: command.id,
    command_kind: command.commandType,
    command_schema_version: 1,
    client_wall_time: command.actionAt,
    payload: command.payload,
  };
}

function bodyForPush(
  organizationId: string,
  clientInstallationId: string,
  requestSentAt: string,
  commands: OutboxCommand[],
) {
  return {
    organization_id: organizationId,
    client_installation_id: clientInstallationId,
    request_sent_at: requestSentAt,
    sync_schema_version: 1 as const,
    commands: commands.map(commandForPush),
  };
}

function byteLength(value: unknown): number {
  return encoder.encode(JSON.stringify(value)).byteLength;
}

function orderedCommands(commands: OutboxCommand[]): OutboxCommand[] {
  return [...commands].sort(compareOutboxCommands);
}

function batches(
  organizationId: string,
  clientInstallationId: string,
  requestSentAt: string,
  commands: OutboxCommand[],
): { batches: OutboxCommand[][]; oversized: OutboxCommand[] } {
  const result: OutboxCommand[][] = [];
  const oversized: OutboxCommand[] = [];
  let batch: OutboxCommand[] = [];

  for (const command of orderedCommands(commands)) {
    const candidate = [...batch, command];
    if (
      candidate.length <= MAX_COMMANDS_PER_PUSH &&
      byteLength(
        bodyForPush(
          organizationId,
          clientInstallationId,
          requestSentAt,
          candidate,
        ),
      ) <= MAX_PUSH_BYTES
    ) {
      batch = candidate;
      continue;
    }
    if (batch.length > 0) {
      result.push(batch);
      batch = [command];
    }
    if (
      byteLength(
        bodyForPush(organizationId, clientInstallationId, requestSentAt, [
          command,
        ]),
      ) > MAX_PUSH_BYTES
    ) {
      oversized.push(command);
      batch = [];
    }
  }
  if (batch.length > 0) result.push(batch);
  return { batches: result, oversized };
}

function parsePushResponse(
  value: unknown,
  commands: OutboxCommand[],
): PushResponse {
  if (!value || typeof value !== "object")
    throw new Error("Invalid sync response.");
  const response = value as Partial<PushResponse>;
  if (
    response.sync_schema_version !== 1 ||
    typeof response.server_time !== "string" ||
    typeof response.change_cursor !== "string" ||
    !Array.isArray(response.outcomes) ||
    response.outcomes.length !== commands.length
  ) {
    throw new Error("Invalid sync response.");
  }
  for (const [index, outcome] of response.outcomes.entries()) {
    const command = commands[index];
    if (
      !command ||
      outcome.mutation_id !== command.id ||
      outcome.command_kind !== command.commandType ||
      !["accepted", "partially_superseded", "rejected"].includes(
        outcome.status,
      ) ||
      (outcome.status === "rejected" && typeof outcome.error?.code !== "string")
    ) {
      throw new Error("Invalid sync response.");
    }
  }
  if (
    response.clock_skew_warning !== null &&
    (typeof response.clock_skew_warning !== "object" ||
      typeof response.clock_skew_warning.difference_seconds !== "number" ||
      typeof response.clock_skew_warning.server_time !== "string")
  ) {
    throw new Error("Invalid sync response.");
  }
  return response as PushResponse;
}

async function updateMetadata(
  userId: string,
  organizationId: string,
  update: Partial<Omit<OrganizationSyncMetadata, "organizationId">>,
) {
  const existing = await localDb.syncMetadata.get([userId, organizationId]);
  await localDb.syncMetadata.put({
    ...existing,
    userId,
    organizationId,
    activity: existing?.activity ?? "caughtUp",
    ...update,
  });
}

async function rejectCommands(
  commands: OutboxCommand[],
  failureReason: string,
) {
  await localDb.transaction("rw", localDb.outbox, async () => {
    await Promise.all(
      commands.map((command) =>
        localDb.outbox.update(command.id, { state: "failed", failureReason }),
      ),
    );
  });
}

/** Deliver an organization's pending commands without discarding unknown outcomes. */
export async function dispatchOutbox(
  organizationId: string,
  {
    userId,
    clientInstallationId: suppliedInstallationId,
    fetch: send = fetch,
    now = () => new Date(),
  }: DispatchOutboxOptions,
): Promise<void> {
  if (!userId) {
    throw new Error("Authenticated user ID is required for synchronization.");
  }
  const pending = await localDb.outbox
    .where("[userId+organizationId+state]")
    .equals([userId, organizationId, "pending"])
    .toArray();
  if (pending.length === 0) return;

  let clientInstallationId = suppliedInstallationId;
  if (!clientInstallationId) {
    clientInstallationId = await readOrCreateBrowserInstallationId(userId);
  }
  const sizingSentAt = now().toISOString();
  const partition = batches(
    organizationId,
    clientInstallationId,
    sizingSentAt,
    pending,
  );
  await rejectCommands(partition.oversized, "command_too_large");
  await updateMetadata(userId, organizationId, { activity: "syncing" });

  try {
    for (const commands of partition.batches) {
      const requestSentAt = now().toISOString();
      const response = await send("/api/v1/sync/push", {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(
          bodyForPush(
            organizationId,
            clientInstallationId,
            requestSentAt,
            commands,
          ),
        ),
      });
      if (!response.ok) throw new Error("Sync push failed.");
      const result = parsePushResponse(await response.json(), commands);
      await localDb.transaction(
        "rw",
        localDb.outbox,
        localDb.syncMetadata,
        async () => {
          for (const outcome of result.outcomes) {
            if (outcome.status === "rejected") {
              await localDb.outbox.update(outcome.mutation_id, {
                state: "failed",
                failureReason: outcome.error?.code,
              });
            } else {
              await localDb.outbox.delete(outcome.mutation_id);
            }
          }
          const existing = await localDb.syncMetadata.get([
            userId,
            organizationId,
          ]);
          await localDb.syncMetadata.put({
            ...existing,
            userId,
            organizationId,
            activity: "syncing",
            lastSuccessfulServerContact: result.server_time,
            changeCursorHint: result.change_cursor,
            clockSkewWarning: result.clock_skew_warning
              ? {
                  approximateDifferenceSeconds:
                    result.clock_skew_warning.difference_seconds,
                  serverTime: result.clock_skew_warning.server_time,
                }
              : undefined,
          });
        },
      );
    }
    const failed = await localDb.outbox
      .where("[userId+organizationId+state]")
      .equals([userId, organizationId, "failed"])
      .count();
    await updateMetadata(userId, organizationId, {
      activity: failed ? "blocked" : "caughtUp",
    });
  } catch (error) {
    await updateMetadata(userId, organizationId, { activity: "retrying" });
    throw error;
  }
}
