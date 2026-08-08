import { beforeEach, describe, expect, it } from "vitest";

import { localDb } from "./local-db";
import { readEventPlanner } from "./planner-projections";
import { queueAddedOverride } from "./scheduled-ingredient-override";
import {
  queueRecipeSchedule,
  queueScheduledRecipeMove,
} from "./scheduled-recipe";

const ids = {
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
  role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
  recipe: "6d8b2b21-c378-4574-9e46-9338c81305ef",
  version: "7d8b2b21-c378-4574-9e46-9338c81305ef",
};

async function seed() {
  const base = {
    userId: ids.user,
    organizationId: ids.organization,
    recordSchemaVersion: 1,
    lifecycle: "active" as const,
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  };
  await localDb.canonicalRecords.bulkPut([
    {
      ...base,
      entityType: "event",
      entityId: ids.event,
      fields: {
        id: ids.event,
        organization_id: ids.organization,
        name: "Cookout",
        start_date: "2026-08-10",
        end_date: "2026-08-10",
        base_expected_attendance: 12,
        lifecycle: "active",
      },
    },
    {
      ...base,
      entityType: "event_day",
      entityId: ids.day,
      fields: {
        id: ids.day,
        event_id: ids.event,
        calendar_date: "2026-08-10",
        note: "Prep early",
      },
    },
    {
      ...base,
      entityType: "event_meal_role",
      entityId: ids.role,
      fields: {
        id: ids.role,
        event_id: ids.event,
        custom_name: "Dinner",
        position_key: "a",
      },
    },
    {
      ...base,
      entityType: "recipe",
      entityId: ids.recipe,
      fields: { id: ids.recipe, current_version_id: ids.version },
    },
    {
      ...base,
      entityType: "recipe_version",
      entityId: ids.version,
      fields: { id: ids.version, recipe_id: ids.recipe, name: "Chili" },
    },
  ]);
}

describe("offline recipe scheduling", () => {
  beforeEach(async () => {
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.optimisticOverlays.clear(),
      localDb.outbox.clear(),
    ]);
  });

  it("does not project a hidden event day", async () => {
    await seed();
    await localDb.canonicalRecords.update(
      [ids.user, ids.organization, "event_day", ids.day],
      { fields: { id: ids.day, event_id: ids.event, calendar_date: "2026-08-10", note: null, is_visible: false } },
    );
    await expect(readEventPlanner(ids.user, ids.organization, ids.event)).resolves.toMatchObject({
      days: [],
      hiddenDays: [expect.objectContaining({ id: ids.day, visible: false })],
    });
  });

  it("writes one visible scheduled recipe and typed outbox command atomically", async () => {
    await seed();
    await queueRecipeSchedule(ids.user, ids.organization, {
      eventId: ids.event,
      eventDayId: ids.day,
      eventMealRoleId: ids.role,
      recipeId: ids.recipe,
    });
    await expect(
      readEventPlanner(ids.user, ids.organization, ids.event),
    ).resolves.toMatchObject({
      scheduled: [
        expect.objectContaining({
          name: "Chili",
          dinerCount: 12,
          dayId: ids.day,
          roleId: ids.role,
        }),
      ],
    });
    await expect(localDb.outbox.toArray()).resolves.toEqual([
      expect.objectContaining({
        commandType: "scheduled_recipe.schedule",
        state: "pending",
        payload: expect.objectContaining({
          event_id: ids.event,
          event_day_id: ids.day,
          event_meal_role_id: ids.role,
          recipe_id: ids.recipe,
          recipe_version_id: ids.version,
        }),
      }),
    ]);
  });

  it("rejects fuzzed identifiers and cross-event selections without partial work", async () => {
    await seed();
    for (const eventId of [
      "",
      "not-a-uuid",
      "00000000-0000-0000-0000-000000000000",
    ]) {
      await expect(
        queueRecipeSchedule(ids.user, ids.organization, {
          eventId,
          eventDayId: ids.day,
          eventMealRoleId: ids.role,
          recipeId: ids.recipe,
        }),
      ).rejects.toThrow("selection");
    }
    await expect(
      queueRecipeSchedule(ids.user, ids.organization, {
        eventId: ids.event,
        eventDayId: ids.day,
        eventMealRoleId: ids.role,
        recipeId: "8d8b2b21-c378-4574-9e46-9338c81305ef",
      }),
    ).rejects.toThrow("selection");
    await expect(localDb.outbox.count()).resolves.toBe(0);
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("moves a stored scheduled recipe with one scoped overlay and outbox command", async () => {
    await seed();
    await queueRecipeSchedule(ids.user, ids.organization, {
      eventId: ids.event,
      eventDayId: ids.day,
      eventMealRoleId: ids.role,
      recipeId: ids.recipe,
    });
    const schedule = await localDb.outbox.toCollection().first();
    const scheduledRecipeId = schedule?.payload.scheduled_recipe_id;
    expect(typeof scheduledRecipeId).toBe("string");
    await queueScheduledRecipeMove(ids.user, ids.organization, {
      scheduledRecipeId: scheduledRecipeId as string,
      eventId: ids.event,
      eventDayId: ids.day,
      eventMealRoleId: ids.role,
      positionKey: "z9",
    });
    await expect(localDb.outbox.toArray()).resolves.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ commandType: "scheduled_recipe.schedule" }),
        expect.objectContaining({
          commandType: "scheduled_recipe.move",
          payload: expect.objectContaining({
            scheduled_recipe_id: scheduledRecipeId,
            position_key: "z9",
          }),
        }),
      ]),
    );
    await expect(
      localDb.optimisticOverlays.get([
        ids.user,
        ids.organization,
        "scheduled_recipe",
        scheduledRecipeId as string,
      ]),
    ).resolves.toMatchObject({ fields: { position_key: "z9" } });
  });

  it("keeps a canonical archived event read-only despite a stale active overlay", async () => {
    await seed();
    await localDb.canonicalRecords.update(
      [ids.user, ids.organization, "event", ids.event],
      {
        lifecycle: "retired",
        fields: {
          id: ids.event,
          organization_id: ids.organization,
          name: "Cookout",
          start_date: "2026-08-10",
          end_date: "2026-08-10",
          base_expected_attendance: 12,
          lifecycle: "archived",
        },
      },
    );
    await localDb.optimisticOverlays.add({
      userId: ids.user,
      organizationId: ids.organization,
      entityType: "event",
      entityId: ids.event,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: ids.event,
        organization_id: ids.organization,
        name: "Stale",
        start_date: "2026-08-10",
        end_date: "2026-08-10",
        base_expected_attendance: 12,
        lifecycle: "active",
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:01:00.000Z",
    });
    await expect(
      readEventPlanner(ids.user, ids.organization, ids.event),
    ).resolves.toMatchObject({ lifecycle: "archived", name: "Cookout" });
  });

  it("projects an optimistic local added ingredient immediately", async () => {
    await seed();
    const scheduledId = "8d8b2b21-c378-4574-9e46-9338c81305ef";
    const ingredientId = "9d8b2b21-c378-4574-9e46-9338c81305ef";
    const ingredientVersionId = "ad8b2b21-c378-4574-9e46-9338c81305ef";
    const base = {
      userId: ids.user,
      organizationId: ids.organization,
      recordSchemaVersion: 1,
      lifecycle: "active" as const,
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    };
    await localDb.canonicalRecords.bulkPut([
      {
        ...base,
        entityType: "scheduled_recipe",
        entityId: scheduledId,
        fields: {
          id: scheduledId,
          organization_id: ids.organization,
          event_id: ids.event,
          event_day_id: ids.day,
          event_meal_role_id: ids.role,
          recipe_id: ids.recipe,
          recipe_version_id: ids.version,
          diner_count: 12,
          consumption_percentage: "100",
          selected_scale_amount: "1",
          position_key: "a",
        },
      },
      {
        ...base,
        entityType: "ingredient",
        entityId: ingredientId,
        fields: {
          id: ingredientId,
          organization_id: ids.organization,
          current_version_id: ingredientVersionId,
        },
      },
      {
        ...base,
        entityType: "ingredient_version",
        entityId: ingredientVersionId,
        fields: {
          id: ingredientVersionId,
          organization_id: ids.organization,
          ingredient_id: ingredientId,
          name: "Paprika",
        },
      },
    ]);
    await queueAddedOverride(ids.user, ids.organization, {
      eventId: ids.event,
      scheduledRecipeId: scheduledId,
      ingredientId,
      ingredientVersionId,
      quantity: "2.5",
      includeInPortionWeight: true,
    });
    const planner = await readEventPlanner(ids.user, ids.organization, ids.event);
    expect(planner?.scheduled[0]?.localAddedIngredients).toEqual([
      expect.objectContaining({ name: "Paprika", quantity: "2.5" }),
    ]);
  });
});
