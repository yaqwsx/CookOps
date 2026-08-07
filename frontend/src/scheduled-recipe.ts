import { appendOutboxCommand, localDb } from "./local-db";
import { readEventPlanner } from "./planner-projections";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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
