import { beforeEach, expect, it } from "vitest";

import { localDb } from "./local-db";
import {
  queueReplacementOverride,
  replayReplacementOverride,
} from "./scheduled-ingredient-override";

const user = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organization = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const event = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const scheduled = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const line = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";

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
      fields: { id: scheduled, organization_id: organization, event_id: event },
      fieldClocks: {},
      immutable: false,
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
  await replayReplacementOverride(user, organization, {
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
  expect(await localDb.optimisticOverlays.count()).toBe(2);
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
  await replayReplacementOverride(user, organization, {
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
  await replayReplacementOverride(user, organization, {
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
    await replayReplacementOverride(user, organization, {
      id: crypto.randomUUID(),
      actionAt: new Date().toISOString(),
      payload: invalid,
    });
  expect(await localDb.optimisticOverlays.count()).toBe(0);
});
