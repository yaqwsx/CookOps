import { beforeEach, expect, it } from "vitest";

import { localDb } from "./local-db";
import {
  queueAddedOverride,
  queueClearAddedOverride,
  queueClearReplacementOverride,
  queueReplacementOverride,
  replayScheduledIngredientOverride,
} from "./scheduled-ingredient-override";

const user = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organization = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const event = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const scheduled = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const line = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredient = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientVersion = "ace17d2f-8365-4b1f-a80b-34d10425d51c";
const recipeVersion = "bce17d2f-8365-4b1f-a80b-34d10425d51c";

beforeEach(async () => {
  await localDb.canonicalRecords.clear();
  await localDb.optimisticOverlays.clear();
  await localDb.outbox.clear();
});

async function activePlan(
  lifecycle: "active" | "retired" = "active",
  scheduledLifecycle: "active" | "retired" = "active",
) {
  await localDb.canonicalRecords.bulkPut([
    {
      userId: user,
      organizationId: organization,
      entityType: "event",
      entityId: event,
      recordSchemaVersion: 1,
      lifecycle,
      fields: { id: event, organization_id: organization, lifecycle: lifecycle === "active" ? "active" : "archived" },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    },
    {
      userId: user,
      organizationId: organization,
      entityType: "scheduled_recipe",
      entityId: scheduled,
      recordSchemaVersion: 1,
      lifecycle: scheduledLifecycle,
      fields: {
        id: scheduled,
        organization_id: organization,
        event_id: event,
        recipe_version_id: recipeVersion,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    },
  ]);
}

async function activeIngredient() {
  await localDb.canonicalRecords.bulkPut([
    {
      userId: user,
      organizationId: organization,
      entityType: "ingredient",
      entityId: ingredient,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: ingredient,
        organization_id: organization,
        current_version_id: ingredientVersion,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: new Date().toISOString(),
    },
    {
      userId: user,
      organizationId: organization,
      entityType: "ingredient_version",
      entityId: ingredientVersion,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: ingredientVersion,
        organization_id: organization,
        ingredient_id: ingredient,
      },
      fieldClocks: {},
      immutable: true,
      updatedAt: new Date().toISOString(),
    },
  ]);
}

it("queues and replays only valid replacement override intent", async () => {
  await activePlan();
  await queueReplacementOverride(user, organization, {
    eventId: event,
    scheduledRecipeId: scheduled,
    targetLineKey: line,
    quantity: "2.5",
  });
  await expect(localDb.outbox.toArray()).resolves.toEqual([
    expect.objectContaining({
      commandType: "scheduled_recipe.ingredient_override",
      payload: expect.objectContaining({ target_line_key: line }),
    }),
  ]);
  await replayScheduledIngredientOverride(user, organization, {
    id: crypto.randomUUID(),
    actionAt: new Date().toISOString(),
    payload: {
      override_id: crypto.randomUUID(),
      event_id: event,
      scheduled_recipe_id: scheduled,
      operation: "set",
      override_kind: "replace",
      target_line_key: line,
      quantity: "0",
    },
  });
  expect(await localDb.optimisticOverlays.count()).toBe(1);
});

it("queues an exact replacement clear and tombstones only that override", async () => {
  await activePlan();
  const overrideId = "cce17d2f-8365-4b1f-a80b-34d10425d51c";
  await localDb.canonicalRecords.put({ userId: user, organizationId: organization, entityType: "recipe_ingredient_line", entityId: line, recordSchemaVersion: 1, lifecycle: "active", fields: { id: line, organization_id: organization, recipe_version_id: recipeVersion, line_key: line }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() });
  await localDb.canonicalRecords.put({ userId: user, organizationId: organization, entityType: "scheduled_ingredient_override", entityId: overrideId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: overrideId, organization_id: organization, event_id: event, scheduled_recipe_id: scheduled, override_kind: "replace", target_line_key: line, quantity: "2" }, fieldClocks: { [`replace.${line}`]: { mutationId: "dce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-08T12:00:00.000000Z" } }, immutable: false, updatedAt: new Date().toISOString() });
  await queueClearReplacementOverride(user, organization, { eventId: event, scheduledRecipeId: scheduled, targetLineKey: line, overrideId });
  await expect(localDb.outbox.toArray()).resolves.toEqual([expect.objectContaining({ payload: { override_id: overrideId, event_id: event, scheduled_recipe_id: scheduled, operation: "clear", override_kind: "replace", target_line_key: line } })]);
  await expect(localDb.optimisticOverlays.get([user, organization, "scheduled_ingredient_override", overrideId])).resolves.toMatchObject({ lifecycle: "retired", fields: { retired_at: expect.any(String) } });
});

it("replays a replacement clear only for the active pinned override", async () => {
  await activePlan();
  const overrideId = "cce17d2f-8365-4b1f-a80b-34d10425d51c";
  await localDb.canonicalRecords.put({ userId: user, organizationId: organization, entityType: "recipe_ingredient_line", entityId: line, recordSchemaVersion: 1, lifecycle: "active", fields: { id: line, organization_id: organization, recipe_version_id: recipeVersion, line_key: line }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() });
  await localDb.optimisticOverlays.put({ userId: user, organizationId: organization, entityType: "scheduled_ingredient_override", entityId: overrideId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: overrideId, organization_id: organization, event_id: event, scheduled_recipe_id: scheduled, override_kind: "replace", target_line_key: line, quantity: "2" }, fieldClocks: { [`replace.${line}`]: { mutationId: "dce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-08T12:00:00.000000Z" } }, immutable: false, updatedAt: new Date().toISOString() });
  await replayScheduledIngredientOverride(user, organization, { id: "ece17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-08T12:00:00.000001Z", payload: { override_id: overrideId, event_id: event, scheduled_recipe_id: scheduled, operation: "clear", override_kind: "replace", target_line_key: line } });
  await expect(localDb.optimisticOverlays.get([user, organization, "scheduled_ingredient_override", overrideId])).resolves.toMatchObject({ lifecycle: "retired", fields: { operation: "clear" } });
});

it("does not queue a clear for a retired or foreign pinned line", async () => {
  await activePlan();
  const overrideId = "cce17d2f-8365-4b1f-a80b-34d10425d51c";
  await localDb.canonicalRecords.put({ userId: user, organizationId: organization, entityType: "scheduled_ingredient_override", entityId: overrideId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: overrideId, organization_id: organization, event_id: event, scheduled_recipe_id: scheduled, override_kind: "replace", target_line_key: line, quantity: "2" }, fieldClocks: {}, immutable: false, updatedAt: new Date().toISOString() });
  for (const fields of [{ id: line, organization_id: organization, recipe_version_id: recipeVersion, line_key: line }, { id: line, organization_id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c", recipe_version_id: recipeVersion, line_key: line }]) {
    await localDb.canonicalRecords.put({ userId: user, organizationId: organization, entityType: "recipe_ingredient_line", entityId: line, recordSchemaVersion: 1, lifecycle: fields.organization_id === organization ? "retired" : "active", fields, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() });
    await expect(queueClearReplacementOverride(user, organization, { eventId: event, scheduledRecipeId: scheduled, targetLineKey: line, overrideId })).rejects.toThrow("override");
    await localDb.canonicalRecords.delete([user, organization, "recipe_ingredient_line", line]);
  }
});

it("keeps a canonical microsecond winner and ignores malformed clear intent", async () => {
  await activePlan();
  const overrideId = "cce17d2f-8365-4b1f-a80b-34d10425d51c";
  await localDb.canonicalRecords.bulkPut([
    { userId: user, organizationId: organization, entityType: "recipe_ingredient_line", entityId: line, recordSchemaVersion: 1, lifecycle: "active", fields: { id: line, organization_id: organization, recipe_version_id: recipeVersion, line_key: line }, fieldClocks: {}, immutable: true, updatedAt: new Date().toISOString() },
    { userId: user, organizationId: organization, entityType: "scheduled_ingredient_override", entityId: overrideId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: overrideId, organization_id: organization, event_id: event, scheduled_recipe_id: scheduled, override_kind: "replace", target_line_key: line, quantity: "2" }, fieldClocks: { [`replace.${line}`]: { winning_mutation_id: "00000000-0000-4000-8000-000000000001", winning_client_wall_time: "2026-08-08T12:00:00.000001+00:00" } }, immutable: false, updatedAt: new Date().toISOString() },
  ]);
  await replayScheduledIngredientOverride(user, organization, { id: "ffffffff-ffff-4fff-8fff-ffffffffffff", actionAt: "2026-08-08T12:00:00.000000Z", payload: { override_id: overrideId, event_id: event, scheduled_recipe_id: scheduled, operation: "clear", override_kind: "replace", target_line_key: line } });
  await replayScheduledIngredientOverride(user, organization, { id: crypto.randomUUID(), actionAt: "2026-08-08T12:00:00.000002Z", payload: { override_id: overrideId, event_id: event, scheduled_recipe_id: scheduled, operation: "clear", override_kind: "replace", target_line_key: line, extra: true } });
  await expect(localDb.optimisticOverlays.get([user, organization, "scheduled_ingredient_override", overrideId])).resolves.toBeUndefined();
});

it("keeps override intent recoverable without reviving it after an archive", async () => {
  await activePlan("retired");
  await expect(
    queueReplacementOverride(user, organization, {
      eventId: event,
      scheduledRecipeId: scheduled,
      targetLineKey: line,
      quantity: "2.5",
    }),
  ).rejects.toThrow("override");
  await replayScheduledIngredientOverride(user, organization, {
    id: crypto.randomUUID(),
    actionAt: new Date().toISOString(),
    payload: {
      override_id: crypto.randomUUID(),
      event_id: event,
      scheduled_recipe_id: scheduled,
      operation: "set",
      override_kind: "replace",
      target_line_key: line,
      quantity: "2.5",
    },
  });
  expect(await localDb.optimisticOverlays.count()).toBe(0);
});

it("does not replay an override for a retired scheduled recipe", async () => {
  await activePlan("active", "retired");
  await replayScheduledIngredientOverride(user, organization, {
    id: crypto.randomUUID(),
    actionAt: new Date().toISOString(),
    payload: {
      override_id: crypto.randomUUID(),
      event_id: event,
      scheduled_recipe_id: scheduled,
      operation: "set",
      override_kind: "replace",
      target_line_key: line,
      quantity: "2.5",
    },
  });
  expect(await localDb.optimisticOverlays.count()).toBe(0);
});

it("queues and replays an added override only for its active catalog version", async () => {
  await activePlan();
  await activeIngredient();
  await queueAddedOverride(user, organization, {
    eventId: event,
    scheduledRecipeId: scheduled,
    ingredientId: ingredient,
    ingredientVersionId: ingredientVersion,
    quantity: "2.5",
    includeInPortionWeight: true,
  });
  expect(await localDb.outbox.toArray()).toEqual([
    expect.objectContaining({
      commandType: "scheduled_recipe.ingredient_override",
      payload: expect.objectContaining({
        override_kind: "add",
        ingredient_id: ingredient,
        ingredient_version_id: ingredientVersion,
      }),
    }),
  ]);
  await replayScheduledIngredientOverride(user, organization, {
    id: crypto.randomUUID(),
    actionAt: new Date().toISOString(),
    payload: {
      override_id: crypto.randomUUID(),
      event_id: event,
      scheduled_recipe_id: scheduled,
      operation: "set",
      override_kind: "add",
      ingredient_id: ingredient,
      ingredient_version_id: ingredientVersion,
      quantity: "0",
      include_in_portion_weight: false,
      position_key: "z",
    },
  });
  expect(await localDb.optimisticOverlays.count()).toBe(2);
});

it("fails closed for an added override with a stale catalog version", async () => {
  await activePlan();
  await activeIngredient();
  await expect(
    queueAddedOverride(user, organization, {
      eventId: event,
      scheduledRecipeId: scheduled,
      ingredientId: ingredient,
      ingredientVersionId: crypto.randomUUID(),
      quantity: "2.5",
      includeInPortionWeight: true,
    }),
  ).rejects.toThrow("override");
});

it("does not queue an added override for an ingredient pinned in the recipe", async () => {
  await activePlan();
  await activeIngredient();
  await localDb.optimisticOverlays.put({
    userId: user,
    organizationId: organization,
    entityType: "recipe_ingredient_line",
    entityId: line,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: {
      id: line,
      organization_id: organization,
      recipe_version_id: recipeVersion,
      ingredient_version_id: ingredientVersion,
    },
    fieldClocks: {},
    immutable: true,
    updatedAt: new Date().toISOString(),
  });
  await expect(
    queueAddedOverride(user, organization, {
      eventId: event,
      scheduledRecipeId: scheduled,
      ingredientId: ingredient,
      ingredientVersionId: ingredientVersion,
      quantity: "2.5",
      includeInPortionWeight: true,
    }),
  ).rejects.toThrow("override");
});

it("queues an exact clear for an active added override and tombstones it", async () => {
  await activePlan();
  const overrideId = "cce17d2f-8365-4b1f-a80b-34d10425d51c";
  await localDb.canonicalRecords.put({ userId: user, organizationId: organization, entityType: "scheduled_ingredient_override", entityId: overrideId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: overrideId, organization_id: organization, event_id: event, scheduled_recipe_id: scheduled, override_kind: "add", ingredient_id: ingredient, ingredient_version_id: ingredientVersion, quantity: "2" }, fieldClocks: { [`add.${overrideId}`]: { mutationId: "dce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-08T12:00:00.000000Z" } }, immutable: false, updatedAt: new Date().toISOString() });
  await queueClearAddedOverride(user, organization, { eventId: event, scheduledRecipeId: scheduled, overrideId });
  await expect(localDb.outbox.toArray()).resolves.toEqual([expect.objectContaining({ payload: { override_id: overrideId, event_id: event, scheduled_recipe_id: scheduled, operation: "clear", override_kind: "add" } })]);
  await expect(localDb.optimisticOverlays.get([user, organization, "scheduled_ingredient_override", overrideId])).resolves.toMatchObject({ lifecycle: "retired", fields: { retired_at: expect.any(String), override_kind: "add" } });
});

it("replays only a newer clear for an existing active added override", async () => {
  await activePlan();
  const overrideId = "cce17d2f-8365-4b1f-a80b-34d10425d51c";
  await localDb.canonicalRecords.put({ userId: user, organizationId: organization, entityType: "scheduled_ingredient_override", entityId: overrideId, recordSchemaVersion: 1, lifecycle: "active", fields: { id: overrideId, organization_id: organization, event_id: event, scheduled_recipe_id: scheduled, override_kind: "add", ingredient_id: ingredient, ingredient_version_id: ingredientVersion, quantity: "2" }, fieldClocks: { [`add.${overrideId}`]: { mutationId: "dce17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-08T12:00:00.000001Z" } }, immutable: false, updatedAt: new Date().toISOString() });
  const payload = { override_id: overrideId, event_id: event, scheduled_recipe_id: scheduled, operation: "clear", override_kind: "add" };
  await replayScheduledIngredientOverride(user, organization, { id: "ece17d2f-8365-4b1f-a80b-34d10425d51c", actionAt: "2026-08-08T12:00:00.000000Z", payload });
  expect(await localDb.optimisticOverlays.get([user, organization, "scheduled_ingredient_override", overrideId])).toBeUndefined();
  await replayScheduledIngredientOverride(user, organization, { id: "ffffffff-ffff-4fff-8fff-ffffffffffff", actionAt: "2026-08-08T12:00:00.000002Z", payload });
  await expect(localDb.optimisticOverlays.get([user, organization, "scheduled_ingredient_override", overrideId])).resolves.toMatchObject({ lifecycle: "retired", fields: { operation: "clear" } });
});

it("keeps the LWW-winning replacement overlay when replay order is stale", async () => {
  await activePlan();
  const newer = "fce17d2f-8365-4b1f-a80b-34d10425d51c";
  await replayScheduledIngredientOverride(user, organization, {
    id: newer,
    actionAt: "2026-08-08T12:00:01.000Z",
    payload: {
      override_id: crypto.randomUUID(),
      event_id: event,
      scheduled_recipe_id: scheduled,
      operation: "set",
      override_kind: "replace",
      target_line_key: line,
      quantity: "2",
    },
  });
  await replayScheduledIngredientOverride(user, organization, {
    id: "dce17d2f-8365-4b1f-a80b-34d10425d51c",
    actionAt: "2026-08-08T12:00:00.000Z",
    payload: {
      override_id: crypto.randomUUID(),
      event_id: event,
      scheduled_recipe_id: scheduled,
      operation: "set",
      override_kind: "replace",
      target_line_key: line,
      quantity: "1",
    },
  });
  const override = await localDb.optimisticOverlays
    .where("[userId+organizationId+entityType]")
    .equals([user, organization, "scheduled_ingredient_override"])
    .first();
  expect(override?.fields.quantity).toBe("2");
  expect(override?.fieldClocks[`replace.${line}`]).toEqual({
    mutationId: newer,
    actionAt: "2026-08-08T12:00:01.000Z",
  });
});

it("fails closed for stale or expanded override payloads", async () => {
  await activePlan();
  const payload = {
    override_id: crypto.randomUUID(),
    event_id: event,
    scheduled_recipe_id: scheduled,
    override_kind: "replace",
    target_line_key: line,
    quantity: "2.5",
  };
  for (const invalid of [
    { ...payload, operation: "clear" },
    { ...payload, operation: "set", unexpected: true },
  ])
    await replayScheduledIngredientOverride(user, organization, {
      id: crypto.randomUUID(),
      actionAt: new Date().toISOString(),
      payload: invalid,
    });
  expect(await localDb.optimisticOverlays.count()).toBe(0);
});
