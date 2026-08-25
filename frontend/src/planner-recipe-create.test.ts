import { beforeEach, describe, expect, it, vi } from "vitest";

const readEventPlanner = vi.hoisted(() => vi.fn());
vi.mock("./planner-projections", () => ({ readEventPlanner }));

import { localDb } from "./local-db";
import { assertPlannerTarget, queueRecipeCreate } from "./recipe-create";
import { queueRecipeSchedule } from "./scheduled-recipe";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "3ce17d2f-8365-4b1f-a80b-34d10425d51c";
const dayId = "4ce17d2f-8365-4b1f-a80b-34d10425d51c";
const roleId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";

describe("planner contextual recipe creation", () => {
  beforeEach(async () => {
    await Promise.all([
      localDb.canonicalRecords.clear(),
      localDb.optimisticOverlays.clear(),
      localDb.outbox.clear(),
    ]);
    readEventPlanner.mockReset();
    await localDb.canonicalRecords.put({
      userId,
      organizationId,
      entityType: "unit_definition",
      entityId: unitId,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: unitId,
        organization_id: null,
        code: "person",
        allows_recipe_scaling: true,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    });
  });

  it("queues create before schedule using the created version id", async () => {
    readEventPlanner.mockImplementation(async () => {
      const overlays = await localDb.optimisticOverlays.toArray();
      const recipe = overlays.find((item) => item.entityType === "recipe");
      const version = overlays.find(
        (item) => item.entityType === "recipe_version",
      );
      return {
        lifecycle: "active",
        recipes: recipe
          ? [{ id: recipe.entityId, versionId: version?.entityId }]
          : [],
        days: [{ id: dayId }],
        roles: [{ id: roleId }],
      };
    });
    const recipeId = await queueRecipeCreate(userId, organizationId, {
      name: "New",
      description: "",
      scalingUnitId: unitId,
      baseScalingAmount: "1",
    });
    await queueRecipeSchedule(userId, organizationId, {
      recipeId,
      eventId,
      eventDayId: dayId,
      eventMealRoleId: roleId,
    });
    const commands = (await localDb.outbox.toArray()).sort(
      (left, right) => (left.sequence ?? 0) - (right.sequence ?? 0),
    );
    expect(commands.map((command) => command.commandType)).toEqual([
      "recipe.create",
      "scheduled_recipe.schedule",
    ]);
    const schedule = commands[1];
    expect(schedule?.payload).toMatchObject({
      event_id: eventId,
      event_day_id: dayId,
      event_meal_role_id: roleId,
      recipe_id: recipeId,
    });
    expect(schedule?.payload.recipe_version_id).toBe(
      (await localDb.optimisticOverlays.toArray()).find(
        (item) => item.entityType === "recipe_version",
      )?.entityId,
    );
  });

  it("does not schedule when the created recipe is not visible in the active target", async () => {
    readEventPlanner
      .mockResolvedValueOnce({
        lifecycle: "active",
        recipes: [],
        days: [{ id: dayId }],
        roles: [{ id: roleId }],
      })
      .mockResolvedValue({
        lifecycle: "active",
        recipes: [],
        days: [{ id: dayId }],
        roles: [{ id: roleId }],
      });
    const recipeId = await queueRecipeCreate(userId, organizationId, {
      name: "New",
      description: "",
      scalingUnitId: unitId,
      baseScalingAmount: "1",
    });
    await expect(
      queueRecipeSchedule(userId, organizationId, {
        recipeId,
        eventId,
        eventDayId: dayId,
        eventMealRoleId: roleId,
      }),
    ).rejects.toThrow("selection");
    expect(
      (await localDb.outbox.toArray())
        .sort((left, right) => (left.sequence ?? 0) - (right.sequence ?? 0))
        .map((command) => command.commandType),
    ).toEqual(["recipe.create"]);
    const created = (await localDb.optimisticOverlays.toArray()).find(
      (item) => item.entityType === "recipe",
    );
    readEventPlanner.mockResolvedValue({
      lifecycle: "active",
      recipes: [
        {
          id: created?.entityId,
          versionId: (await localDb.optimisticOverlays.toArray()).find(
            (item) => item.entityType === "recipe_version",
          )?.entityId,
        },
      ],
      days: [{ id: dayId }],
      roles: [{ id: roleId }],
    });
    await queueRecipeSchedule(userId, organizationId, {
      recipeId: created?.entityId as string,
      eventId,
      eventDayId: dayId,
      eventMealRoleId: roleId,
    });
    expect(
      (await localDb.outbox.toArray())
        .sort((left, right) => (left.sequence ?? 0) - (right.sequence ?? 0))
        .map((command) => command.commandType),
    ).toEqual(["recipe.create", "scheduled_recipe.schedule"]);
  });

  it("rejects a stale target before creating anything", async () => {
    readEventPlanner.mockResolvedValue({
      lifecycle: "archived",
      recipes: [],
      days: [],
      roles: [],
    });
    await expect(
      assertPlannerTarget(userId, organizationId, eventId, dayId, roleId),
    ).rejects.toThrow("selection");
    expect(await localDb.outbox.count()).toBe(0);
  });
});
