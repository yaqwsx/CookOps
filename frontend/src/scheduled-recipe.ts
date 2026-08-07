import { localDb } from "./local-db";
import { readEventPlanner } from "./planner-projections";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type ScheduleRecipeInput = {
  eventId: string;
  eventDayId: string;
  eventMealRoleId: string;
  recipeId: string;
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
      await localDb.outbox.add({
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
