import { appendOutboxCommand, localDb, type CanonicalRecord } from "./local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type Operation = "retire" | "restore";
type Command = {
  id: string;
  actionAt: string;
  payload: Record<string, unknown>;
};

function timestampMicros(value: string): bigint | undefined {
  const match = /^(.*?)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$/.exec(value);
  if (!match) return undefined;
  const milliseconds = Date.parse(`${match[1]}${match[3]}`);
  if (!Number.isFinite(milliseconds)) return undefined;
  return BigInt(milliseconds) * 1_000n + BigInt((match[2] ?? "").slice(0, 6).padEnd(6, "0"));
}

function wins(record: CanonicalRecord, id: string, actionAt: string): boolean {
  const clock = record.fieldClocks.lifecycle;
  if (clock === undefined || clock === null) return true;
  if (typeof clock !== "object" || Array.isArray(clock)) return false;
  const value = clock as Record<string, unknown>;
  const clockAt = value.actionAt ?? value.winning_client_wall_time;
  const clockId = value.mutationId ?? value.winning_mutation_id;
  if (typeof clockAt !== "string" || typeof clockId !== "string") return false;
  const left = timestampMicros(actionAt);
  const right = timestampMicros(clockAt);
  return left !== undefined && right !== undefined && (left > right || (left === right && id > clockId));
}

async function applyLifecycle(
  userId: string,
  organizationId: string,
  recipeId: string,
  operation: Operation,
  mutationId: string,
  actionAt: string,
): Promise<void> {
  const canonical = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "recipe",
    recipeId,
  ]);
  const overlay = await localDb.optimisticOverlays.get([
    userId,
    organizationId,
    "recipe",
    recipeId,
  ]);
  const expected = operation === "retire" ? "active" : "retired";
  const current = overlay ?? canonical;
  if (
    !current ||
    (canonical !== undefined && canonical.lifecycle !== expected) ||
    current.fields.organization_id !== organizationId ||
    typeof current.fields.current_version_id !== "string" ||
    !uuid.test(current.fields.current_version_id) ||
    current.lifecycle !== expected
  )
    return;
  if (!wins(current, mutationId, actionAt)) return;
  await localDb.optimisticOverlays.put({
    ...current,
    lifecycle: operation === "retire" ? "retired" : "active",
    fields: {
      ...current.fields,
      lifecycle: operation === "retire" ? "retired" : "active",
      retired_at: operation === "retire" ? actionAt : null,
      retired_by_user_id: operation === "retire" ? userId : null,
    },
    fieldClocks: { ...current.fieldClocks, lifecycle: { mutationId, actionAt } },
    updatedAt: actionAt,
  });
}

export async function queueRecipeLifecycle(
  userId: string,
  organizationId: string,
  input: { recipeId: string; operation: Operation },
): Promise<void> {
  if (!uuid.test(userId) || !uuid.test(organizationId) || !uuid.test(input.recipeId))
    throw new Error("selection");
  const mutationId = crypto.randomUUID();
  const actionAt = new Date().toISOString();
  await localDb.transaction(
    "rw",
    localDb.canonicalRecords,
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      const canonical = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "recipe",
        input.recipeId,
      ]);
      const expected = input.operation === "retire" ? "active" : "retired";
      const overlay = await localDb.optimisticOverlays.get([
        userId,
        organizationId,
        "recipe",
        input.recipeId,
      ]);
      const current = overlay ?? canonical;
      if (
        !current ||
        (canonical !== undefined && canonical.lifecycle !== expected) ||
        current.lifecycle !== expected ||
        current.fields.organization_id !== organizationId ||
        typeof current.fields.current_version_id !== "string" ||
        !uuid.test(current.fields.current_version_id)
      )
        throw new Error("selection");
      await applyLifecycle(userId, organizationId, input.recipeId, input.operation, mutationId, actionAt);
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "recipe.lifecycle",
        payload: { recipe_id: input.recipeId, operation: input.operation },
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

export async function replayRecipeLifecycle(
  userId: string,
  organizationId: string,
  command: Command,
): Promise<void> {
  const payload = command.payload;
  if (
    Object.keys(payload).length !== 2 ||
    typeof payload.recipe_id !== "string" ||
    !uuid.test(payload.recipe_id) ||
    (payload.operation !== "retire" && payload.operation !== "restore") ||
    !uuid.test(command.id) ||
    timestampMicros(command.actionAt) === undefined
  )
    return;
  await applyLifecycle(
    userId,
    organizationId,
    payload.recipe_id,
    payload.operation,
    command.id,
    command.actionAt,
  );
}
