import { beforeEach, describe, expect, it } from "vitest";

import { localDb } from "./local-db";
import { queueShoppingList } from "./shopping-list";
import {
  queueShoppingAvailableSupply,
  queueShoppingContributionFulfilment,
  queueShoppingManualPurchaseTarget,
  queueShoppingRowFulfilment,
  replayShoppingOperation,
} from "./shopping-operations";
import { readShoppingList, readShoppingLists } from "./shopping-projections";

const ids = {
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  recipe: "6d8b2b21-c378-4574-9e46-9338c81305ef",
  version: "7d8b2b21-c378-4574-9e46-9338c81305ef",
  day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
  role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
  scheduled: "8d8b2b21-c378-4574-9e46-9338c81305ef",
  list: "9d8b2b21-c378-4574-9e46-9338c81305ef",
  revision: "0e8b2b21-c378-4574-9e46-9338c81305ef",
  row: "1e8b2b21-c378-4574-9e46-9338c81305ef",
  contribution: "2e8b2b21-c378-4574-9e46-9338c81305ef",
  retiredContribution: "6e8b2b21-c378-4574-9e46-9338c81305ef",
  snapshot: "3e8b2b21-c378-4574-9e46-9338c81305ef",
  unit: "4e8b2b21-c378-4574-9e46-9338c81305ef",
  section: "5e8b2b21-c378-4574-9e46-9338c81305ef",
};

async function clearDatabase() {
  await Promise.all([
    localDb.canonicalRecords.clear(),
    localDb.optimisticOverlays.clear(),
    localDb.outbox.clear(),
  ]);
}

async function seedPlanner() {
  const record = (
    entityType: string,
    entityId: string,
    fields: Record<string, unknown>,
  ) => ({
    userId: ids.user,
    organizationId: ids.organization,
    entityType,
    entityId,
    recordSchemaVersion: 1,
    lifecycle: "active" as const,
    fields,
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
  await localDb.canonicalRecords.bulkAdd([
    record("event", ids.event, {
      id: ids.event,
      organization_id: ids.organization,
      name: "Weekend cook",
      start_date: "2026-08-10",
      end_date: "2026-08-10",
      base_expected_attendance: 4,
      lifecycle: "active",
    }),
    record("event_day", ids.day, {
      id: ids.day,
      event_id: ids.event,
      calendar_date: "2026-08-10",
      note: null,
    }),
    record("event_meal_role", ids.role, {
      id: ids.role,
      event_id: ids.event,
      custom_name: "Dinner",
      position_key: "a",
    }),
    record("recipe", ids.recipe, {
      id: ids.recipe,
      current_version_id: ids.version,
    }),
    record("recipe_version", ids.version, {
      id: ids.version,
      recipe_id: ids.recipe,
      name: "Chili",
    }),
    record("scheduled_recipe", ids.scheduled, {
      id: ids.scheduled,
      organization_id: ids.organization,
      event_id: ids.event,
      event_day_id: ids.day,
      event_meal_role_id: ids.role,
      recipe_id: ids.recipe,
      diner_count: 4,
      position_key: "a",
    }),
  ]);
}

describe("offline shopping-list creation", () => {
  beforeEach(clearDatabase);

  it("writes a scoped visible list and typed materialization intent atomically", async () => {
    await seedPlanner();

    await queueShoppingList(ids.user, ids.organization, {
      eventId: ids.event,
      name: "  Saturday shopping  ",
      scheduledRecipeIds: [ids.scheduled],
    });

    await expect(
      readShoppingLists(ids.user, ids.organization, ids.event),
    ).resolves.toEqual([
      expect.objectContaining({ name: "Saturday shopping", sourceCount: 1 }),
    ]);
    await expect(localDb.outbox.toArray()).resolves.toEqual([
      expect.objectContaining({
        userId: ids.user,
        organizationId: ids.organization,
        commandType: "shopping_list.create",
        state: "pending",
        payload: expect.objectContaining({
          event_id: ids.event,
          name: "Saturday shopping",
          scheduled_recipe_ids: [ids.scheduled],
        }),
      }),
    ]);
  });

  it("rejects malformed, duplicate, and out-of-event source selections without partial state", async () => {
    await seedPlanner();
    const invalidInputs = [
      { name: " ", scheduledRecipeIds: [] },
      { name: "x".repeat(201), scheduledRecipeIds: [] },
      { name: "List", scheduledRecipeIds: [ids.scheduled, ids.scheduled] },
      { name: "List", scheduledRecipeIds: ["not-a-uuid"] },
      {
        name: "List",
        scheduledRecipeIds: ["9d8b2b21-c378-4574-9e46-9338c81305ef"],
      },
    ];
    for (const input of invalidInputs) {
      await expect(
        queueShoppingList(ids.user, ids.organization, {
          eventId: ids.event,
          ...input,
        }),
      ).rejects.toThrow("shopping_list");
    }
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("fuzzes untrusted identifiers before any local write", async () => {
    await seedPlanner();
    for (const suffix of Array.from(
      { length: 32 },
      (_, index) => `${index}-bad`,
    )) {
      await expect(
        queueShoppingList(ids.user, ids.organization, {
          eventId: suffix,
          name: "List",
          scheduledRecipeIds: [],
        }),
      ).rejects.toThrow("shopping_list");
    }
    await expect(localDb.outbox.count()).resolves.toBe(0);
  });

  it("derives remaining from active generation and all contribution credit, including retired sources", async () => {
    const record = (
      entityType: string,
      entityId: string,
      fields: Record<string, unknown>,
    ) => ({
      userId: ids.user,
      organizationId: ids.organization,
      entityType,
      entityId,
      recordSchemaVersion: 1,
      lifecycle: "active" as const,
      fields,
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await localDb.canonicalRecords.bulkAdd([
      record("shopping_list", ids.list, {
        id: ids.list,
        organization_id: ids.organization,
        event_id: ids.event,
        name: "Saturday",
        current_generation_revision_id: ids.revision,
        created_at: "2026-08-07T12:00:00.000Z",
      }),
      record("shopping_ingredient_row", ids.row, {
        id: ids.row,
        organization_id: ids.organization,
        event_id: ids.event,
        shopping_list_id: ids.list,
        ingredient_name: "Tomatoes",
        calculation_unit_id: ids.unit,
        available_supply_quantity: "2",
        manual_purchase_target: null,
        aggregate_fulfilment_credit: "0",
        default_store_section_name: "Vegetables",
        store_section_override_id: ids.section,
      }),
      record("shopping_contribution", ids.contribution, {
        id: ids.contribution,
        organization_id: ids.organization,
        event_id: ids.event,
        shopping_list_id: ids.list,
        shopping_ingredient_row_id: ids.row,
        fulfilment_credit: "6",
      }),
      {
        ...record("shopping_contribution", ids.retiredContribution, {
          id: ids.retiredContribution,
          organization_id: ids.organization,
          event_id: ids.event,
          shopping_list_id: ids.list,
          shopping_ingredient_row_id: ids.row,
          fulfilment_credit: "1",
          retired_at: "2026-08-07T12:00:00.000Z",
        }),
        lifecycle: "retired" as const,
      },
      record("shopping_contribution_snapshot", ids.snapshot, {
        id: ids.snapshot,
        organization_id: ids.organization,
        event_id: ids.event,
        shopping_list_id: ids.list,
        generation_revision_id: ids.revision,
        shopping_contribution_id: ids.contribution,
        active_in_revision: true,
        generated_quantity: "10",
      }),
      record("unit_definition", ids.unit, { id: ids.unit, code: "kg" }),
      record("store_section", ids.section, {
        id: ids.section,
        organization_id: ids.organization,
        name: "Cold storage",
      }),
    ]);

    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toEqual(
      expect.objectContaining({
        rows: [
          expect.objectContaining({
            id: ids.row,
            ingredientName: "Tomatoes",
            sectionName: "Cold storage",
            availableSupply: "2",
            target: "8",
            remaining: "1",
            unit: "kg",
          }),
        ],
      }),
    );
  });

  it("queues scoped supply, target, and whole-row fulfilment atomically without float coercion", async () => {
    const record = (
      entityType: string,
      entityId: string,
      fields: Record<string, unknown>,
    ) => ({
      userId: ids.user,
      organizationId: ids.organization,
      entityType,
      entityId,
      recordSchemaVersion: 1,
      lifecycle: "active" as const,
      fields,
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await localDb.canonicalRecords.bulkAdd([
      record("event", ids.event, { id: ids.event, lifecycle: "active" }),
      record("shopping_list", ids.list, {
        id: ids.list,
        organization_id: ids.organization,
        event_id: ids.event,
        current_generation_revision_id: ids.revision,
      }),
      record("shopping_ingredient_row", ids.row, {
        id: ids.row,
        organization_id: ids.organization,
        shopping_list_id: ids.list,
        available_supply_quantity: "0",
        manual_purchase_target: null,
        aggregate_fulfilment_credit: "0",
      }),
      record("shopping_contribution", ids.contribution, {
        id: ids.contribution,
        organization_id: ids.organization,
        shopping_list_id: ids.list,
        shopping_ingredient_row_id: ids.row,
        fulfilment_credit: "0",
      }),
      record("shopping_contribution_snapshot", ids.snapshot, {
        id: ids.snapshot,
        shopping_list_id: ids.list,
        generation_revision_id: ids.revision,
        shopping_contribution_id: ids.contribution,
        active_in_revision: true,
        generated_quantity: "1.25",
      }),
    ]);
    const input = {
      shoppingListId: ids.list,
      shoppingIngredientRowId: ids.row,
    };
    await queueShoppingAvailableSupply(ids.user, ids.organization, {
      ...input,
      quantity: "0.25",
    });
    await queueShoppingManualPurchaseTarget(ids.user, ids.organization, {
      ...input,
      quantity: "2",
    });
    await queueShoppingRowFulfilment(ids.user, ids.organization, {
      ...input,
      fulfilled: true,
    });
    await queueShoppingContributionFulfilment(ids.user, ids.organization, {
      ...input,
      shoppingContributionId: ids.contribution,
      fulfilled: true,
    });
    const row = await localDb.optimisticOverlays.get([
      ids.user,
      ids.organization,
      "shopping_ingredient_row",
      ids.row,
    ]);
    expect(row?.fields).toMatchObject({
      available_supply_quantity: "0.25",
      manual_purchase_target: "2",
      aggregate_fulfilment_credit: "0.75",
    });
    await expect(localDb.outbox.toArray()).resolves.toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          commandType: "shopping_list.set_available_supply",
          payload: expect.objectContaining({ quantity: "0.25" }),
        }),
        expect.objectContaining({
          commandType: "shopping_list.set_manual_purchase_target",
          payload: expect.objectContaining({ quantity: "2" }),
        }),
        expect.objectContaining({
          commandType: "shopping_list.set_row_fulfilment",
          payload: expect.objectContaining({ fulfilled: true }),
        }),
        expect.objectContaining({
          commandType: "shopping_list.set_contribution_fulfilment",
          payload: expect.objectContaining({
            shopping_contribution_id: ids.contribution,
            fulfilled: true,
          }),
        }),
      ]),
    );
    const pending = (await localDb.outbox.toArray()).sort(
      (left, right) =>
        left.createdAt.localeCompare(right.createdAt) ||
        left.id.localeCompare(right.id),
    );
    await localDb.optimisticOverlays.clear();
    for (const command of pending)
      await replayShoppingOperation(ids.user, ids.organization, command);
    await expect(
      localDb.optimisticOverlays.get([
        ids.user,
        ids.organization,
        "shopping_ingredient_row",
        ids.row,
      ]),
    ).resolves.toMatchObject({
      fields: {
        available_supply_quantity: "0.25",
        manual_purchase_target: "2",
        aggregate_fulfilment_credit: "0.75",
      },
    });
    await expect(
      localDb.optimisticOverlays.get([
        ids.user,
        ids.organization,
        "shopping_contribution",
        ids.contribution,
      ]),
    ).resolves.toMatchObject({ fields: { fulfilment_credit: "1.25" } });
    const before = await localDb.outbox.count();
    for (const invalid of ["-1", "1e3", "NaN", "", "x".repeat(101)])
      await expect(
        queueShoppingAvailableSupply(ids.user, ids.organization, {
          ...input,
          quantity: invalid,
        }),
      ).rejects.toThrow("shopping_operation");
    await expect(localDb.outbox.count()).resolves.toBe(before);
  });
});
