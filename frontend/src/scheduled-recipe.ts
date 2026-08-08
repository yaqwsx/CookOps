import { appendOutboxCommand, localDb } from "./local-db";
import { readEventPlanner } from "./planner-projections";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const decimal = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;

export type ScheduleRecipeInput = {
  eventId: string;
  eventDayId: string;
  eventMealRoleId: string;
  recipeId: string;
};

export type MoveScheduledRecipeInput = {
  scheduledRecipeId: string;
  eventId: string;
  eventDayId: string;
  eventMealRoleId: string;
  positionKey: string;
};

export async function queueScheduledRecipeAttendance(
  userId: string,
  organizationId: string,
  input: { scheduledRecipeId: string; eventId: string; dinerCount: number | null },
): Promise<void> {
  if (
    ![input.scheduledRecipeId, input.eventId].every((id) => uuid.test(id)) ||
    (input.dinerCount !== null && (!Number.isSafeInteger(input.dinerCount) || input.dinerCount < 0))
  )
    throw new Error("selection");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  const payload = {
    scheduled_recipe_id: input.scheduledRecipeId,
    event_id: input.eventId,
    operation: input.dinerCount === null ? "follow_event" : "set_manual",
    diner_count: input.dinerCount,
  };
  await localDb.transaction("rw", localDb.canonicalRecords, localDb.optimisticOverlays, localDb.outbox, async () => {
    const canonicalEvent = await localDb.canonicalRecords.get([userId, organizationId, "event", input.eventId]);
    const canonicalScheduled = await localDb.canonicalRecords.get([userId, organizationId, "scheduled_recipe", input.scheduledRecipeId]);
    if (canonicalEvent?.lifecycle === "retired" || canonicalScheduled?.lifecycle === "retired") throw new Error("selection");
    const event = (await localDb.optimisticOverlays.get([userId, organizationId, "event", input.eventId])) ?? canonicalEvent;
    const scheduled = (await localDb.optimisticOverlays.get([userId, organizationId, "scheduled_recipe", input.scheduledRecipeId])) ?? canonicalScheduled;
    if (event?.lifecycle !== "active" || event.fields.lifecycle !== "active" || scheduled?.lifecycle !== "active" || scheduled.fields.event_id !== input.eventId)
      throw new Error("selection");
    const dinerCount = input.dinerCount ?? event.fields.base_expected_attendance;
    if (!Number.isSafeInteger(dinerCount)) throw new Error("selection");
    await localDb.optimisticOverlays.put({ ...scheduled, fields: { ...scheduled.fields, diner_count: dinerCount, attendance_mode: input.dinerCount === null ? "follows_event" : "manual" }, fieldClocks: { ...scheduled.fieldClocks, attendance: { mutationId, actionAt } }, updatedAt: actionAt });
    await appendOutboxCommand({ id: mutationId, userId, organizationId, commandType: "scheduled_recipe.attendance", payload, actionAt, createdAt: actionAt, state: "pending" });
  });
}

export async function queueScheduledRecipeContext(
  userId: string,
  organizationId: string,
  input: {
    scheduledRecipeId: string;
    eventId: string;
    consumptionPercentage: string;
    selectedScaleAmount: string | null;
  },
): Promise<void> {
  if (
    ![input.scheduledRecipeId, input.eventId].every((id) => uuid.test(id)) ||
    !decimal.test(input.consumptionPercentage) ||
    (input.selectedScaleAmount !== null &&
      !decimal.test(input.selectedScaleAmount))
  )
    throw new Error("selection");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  const payload = {
    scheduled_recipe_id: input.scheduledRecipeId,
    event_id: input.eventId,
    consumption_percentage: input.consumptionPercentage,
    operation:
      input.selectedScaleAmount === null ? "use_suggestion" : "set_manual",
    selected_scale_amount: input.selectedScaleAmount,
  };
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
        input.eventId,
      ]);
      const canonical = await localDb.canonicalRecords.get([
        userId,
        organizationId,
        "scheduled_recipe",
        input.scheduledRecipeId,
      ]);
      if (
        canonicalEvent?.lifecycle === "retired" ||
        canonical?.lifecycle === "retired"
      )
        throw new Error("selection");
      const event =
        (await localDb.optimisticOverlays.get([
          userId,
          organizationId,
          "event",
          input.eventId,
        ])) ?? canonicalEvent;
      const scheduled =
        (await localDb.optimisticOverlays.get([
          userId,
          organizationId,
          "scheduled_recipe",
          input.scheduledRecipeId,
        ])) ?? canonical;
      if (
        event?.lifecycle !== "active" ||
        event.fields.lifecycle !== "active" ||
        scheduled?.lifecycle !== "active" ||
        scheduled.fields.event_id !== input.eventId
      )
        throw new Error("selection");
      const scale =
        input.selectedScaleAmount ?? scheduled.fields.selected_scale_amount;
      await localDb.optimisticOverlays.put({
        ...scheduled,
        fields: {
          ...scheduled.fields,
          consumption_percentage: input.consumptionPercentage,
          selected_scale_amount: scale,
          scale_mode:
            input.selectedScaleAmount === null ? "suggested" : "manual",
        },
        fieldClocks: {
          ...scheduled.fieldClocks,
          context: { mutationId, actionAt },
        },
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "scheduled_recipe.context",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

export async function replayScheduledRecipeContext(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const p = command.payload;
  if (
    Object.keys(p).length !== 5 ||
    ![
      "scheduled_recipe_id",
      "event_id",
      "consumption_percentage",
      "operation",
      "selected_scale_amount",
    ].every((key) => key in p) ||
    typeof p.scheduled_recipe_id !== "string" ||
    typeof p.event_id !== "string" ||
    typeof p.consumption_percentage !== "string" ||
    !uuid.test(p.scheduled_recipe_id) ||
    !uuid.test(p.event_id) ||
    !decimal.test(p.consumption_percentage) ||
    !(
      (p.operation === "use_suggestion" && p.selected_scale_amount === null) ||
      (p.operation === "set_manual" &&
        typeof p.selected_scale_amount === "string" &&
        decimal.test(p.selected_scale_amount))
    )
  )
    return;
  const event = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "event",
    p.event_id,
  ]);
  const canonical = await localDb.canonicalRecords.get([
    userId,
    organizationId,
    "scheduled_recipe",
    p.scheduled_recipe_id,
  ]);
  if (
    event?.lifecycle !== "active" ||
    event.fields.lifecycle !== "active" ||
    canonical?.lifecycle !== "active" ||
    canonical.fields.event_id !== p.event_id
  )
    return;
  const clock = canonical.fieldClocks.context;
  if (clock !== undefined) {
    if (clock === null || typeof clock !== "object" || Array.isArray(clock))
      return;
    const contextClock = clock as Record<string, unknown>;
    const at =
      typeof contextClock.actionAt === "string"
        ? contextClock.actionAt
        : contextClock.winning_client_wall_time;
    const id =
      typeof contextClock.mutationId === "string"
        ? contextClock.mutationId
        : contextClock.winning_mutation_id;
    const candidateTime = Date.parse(command.actionAt);
    const currentTime = typeof at === "string" ? Date.parse(at) : NaN;
    if (
      typeof id !== "string" ||
      !Number.isFinite(candidateTime) ||
      !Number.isFinite(currentTime) ||
      candidateTime < currentTime ||
      (candidateTime === currentTime && command.id <= id)
    )
      return;
  }
  await localDb.optimisticOverlays.put({
    ...canonical,
    fields: {
      ...canonical.fields,
      consumption_percentage: p.consumption_percentage,
      selected_scale_amount:
        p.selected_scale_amount ?? canonical.fields.selected_scale_amount,
      scale_mode: p.operation === "set_manual" ? "manual" : "suggested",
    },
    fieldClocks: {
      ...canonical.fieldClocks,
      context: { mutationId: command.id, actionAt: command.actionAt },
    },
    updatedAt: command.actionAt,
  });
}

export async function replayScheduledRecipeAttendance(
  userId: string,
  organizationId: string,
  command: { id: string; actionAt: string; payload: Record<string, unknown> },
) {
  const payload = command.payload;
  if (
    Object.keys(payload).length !== 4 ||
    !["scheduled_recipe_id", "event_id", "operation", "diner_count"].every((key) => key in payload) ||
    typeof payload.scheduled_recipe_id !== "string" ||
    typeof payload.event_id !== "string" ||
    !uuid.test(payload.scheduled_recipe_id) ||
    !uuid.test(payload.event_id) ||
    !((payload.operation === "set_manual" && Number.isSafeInteger(payload.diner_count) && (payload.diner_count as number) >= 0) || (payload.operation === "follow_event" && payload.diner_count === null))
  ) return;
  const canonicalEvent = await localDb.canonicalRecords.get([userId, organizationId, "event", payload.event_id]);
  const canonical = await localDb.canonicalRecords.get([userId, organizationId, "scheduled_recipe", payload.scheduled_recipe_id]);
  if (canonicalEvent?.lifecycle === "retired" || canonical?.lifecycle === "retired") return;
  const event = (await localDb.optimisticOverlays.get([userId, organizationId, "event", payload.event_id])) ?? canonicalEvent;
  const scheduled = (await localDb.optimisticOverlays.get([userId, organizationId, "scheduled_recipe", payload.scheduled_recipe_id])) ?? canonical;
  if (event?.lifecycle !== "active" || event.fields.lifecycle !== "active" || scheduled?.lifecycle !== "active" || scheduled.fields.event_id !== payload.event_id) return;
  const clock = scheduled.fieldClocks.attendance;
  if (clock !== undefined) {
    if (clock === null || typeof clock !== "object" || Array.isArray(clock)) return;
    const attendanceClock = clock as Record<string, unknown>;
    const currentAt = typeof attendanceClock.actionAt === "string"
      ? attendanceClock.actionAt
      : attendanceClock.winning_client_wall_time;
    const currentId = typeof attendanceClock.mutationId === "string"
      ? attendanceClock.mutationId
      : attendanceClock.winning_mutation_id;
    const candidateTime = Date.parse(command.actionAt);
    const currentTime = typeof currentAt === "string" ? Date.parse(currentAt) : NaN;
    if (
      typeof currentId !== "string" || !Number.isFinite(candidateTime) ||
      !Number.isFinite(currentTime) ||
      candidateTime < currentTime || (candidateTime === currentTime && command.id <= currentId)
    ) return;
  }
  const dinerCount = payload.operation === "follow_event" ? event.fields.base_expected_attendance : payload.diner_count;
  if (!Number.isSafeInteger(dinerCount)) return;
  await localDb.optimisticOverlays.put({ ...scheduled, fields: { ...scheduled.fields, diner_count: dinerCount, attendance_mode: payload.operation === "follow_event" ? "follows_event" : "manual" }, fieldClocks: { ...scheduled.fieldClocks, attendance: { mutationId: command.id, actionAt: command.actionAt } }, updatedAt: command.actionAt });
}

/** Persist an add-to-plan intent with the immediately visible scheduled card. */
export async function queueRecipeSchedule(
  userId: string,
  organizationId: string,
  input: ScheduleRecipeInput,
): Promise<void> {
  if (Object.values(input).some((id) => !uuid.test(id)))
    throw new Error("selection");
  const planner = await readEventPlanner(userId, organizationId, input.eventId);
  const recipe = planner?.recipes.find((item) => item.id === input.recipeId);
  if (
    planner?.lifecycle !== "active" ||
    !planner.days.some((item) => item.id === input.eventDayId) ||
    !planner.roles.some((item) => item.id === input.eventMealRoleId) ||
    !recipe
  )
    throw new Error("selection");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  const scheduledRecipeId = crypto.randomUUID();
  const payload = {
    scheduled_recipe_id: scheduledRecipeId,
    event_id: input.eventId,
    event_day_id: input.eventDayId,
    event_meal_role_id: input.eventMealRoleId,
    recipe_id: recipe.id,
    recipe_version_id: recipe.versionId,
    consumption_percentage: "100",
    position_key: "a",
  };
  await localDb.transaction(
    "rw",
    localDb.optimisticOverlays,
    localDb.outbox,
    async () => {
      await localDb.optimisticOverlays.put({
        userId,
        organizationId,
        entityType: "scheduled_recipe",
        entityId: scheduledRecipeId,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          ...payload,
          id: scheduledRecipeId,
          organization_id: organizationId,
          diner_count: planner.attendance,
          attendance_mode: "follows_event",
          selected_scale_amount: "0",
          scale_mode: "suggested",
          note: null,
          retired_at: null,
        },
        fieldClocks: { optimistic: { mutationId, actionAt } },
        immutable: false,
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "scheduled_recipe.schedule",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}

/** Persist one accessible planner move and its visible placement together. */
export async function queueScheduledRecipeMove(
  userId: string,
  organizationId: string,
  input: MoveScheduledRecipeInput,
): Promise<void> {
  if (
    !Object.entries(input)
      .filter(([key]) => key !== "positionKey")
      .every(([, value]) => uuid.test(value)) ||
    !/^[0-9A-Za-z]{1,255}$/.test(input.positionKey)
  )
    throw new Error("selection");
  const actionAt = new Date().toISOString();
  const mutationId = crypto.randomUUID();
  const payload = {
    scheduled_recipe_id: input.scheduledRecipeId,
    event_id: input.eventId,
    event_day_id: input.eventDayId,
    event_meal_role_id: input.eventMealRoleId,
    position_key: input.positionKey,
  };
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
        input.eventId,
      ]);
      if (canonicalEvent?.lifecycle === "retired") throw new Error("selection");
      const event =
        (await localDb.optimisticOverlays.get([
          userId,
          organizationId,
          "event",
          input.eventId,
        ])) ?? canonicalEvent;
      const scheduled =
        (await localDb.optimisticOverlays.get([
          userId,
          organizationId,
          "scheduled_recipe",
          input.scheduledRecipeId,
        ])) ??
        (await localDb.canonicalRecords.get([
          userId,
          organizationId,
          "scheduled_recipe",
          input.scheduledRecipeId,
        ]));
      const [day, role] = await Promise.all([
        localDb.canonicalRecords.get([
          userId,
          organizationId,
          "event_day",
          input.eventDayId,
        ]),
        localDb.canonicalRecords.get([
          userId,
          organizationId,
          "event_meal_role",
          input.eventMealRoleId,
        ]),
      ]);
      if (
        event?.fields.lifecycle !== "active" ||
        !scheduled ||
        scheduled.lifecycle !== "active" ||
        scheduled.fields.event_id !== input.eventId ||
        day?.lifecycle !== "active" ||
        day.fields.event_id !== input.eventId ||
        role?.lifecycle !== "active" ||
        role.fields.event_id !== input.eventId
      )
        throw new Error("selection");
      await localDb.optimisticOverlays.put({
        ...scheduled,
        fields: { ...scheduled.fields, ...payload },
        fieldClocks: {
          ...scheduled.fieldClocks,
          placement: { mutationId, actionAt },
        },
        updatedAt: actionAt,
      });
      await appendOutboxCommand({
        id: mutationId,
        userId,
        organizationId,
        commandType: "scheduled_recipe.move",
        payload,
        actionAt,
        createdAt: actionAt,
        state: "pending",
      });
    },
  );
}
