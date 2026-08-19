import { beforeEach, describe, expect, it } from "vitest";

import { compareOutboxCommands, localDb } from "./local-db";
import { queueShoppingList, queueShoppingListRefresh } from "./shopping-list";
import {
  queueShoppingAvailableSupply,
  queueShoppingContributionFulfilment,
  queueShoppingManualPurchaseTarget,
  queueShoppingRowFulfilment,
  queueShoppingStoreSectionOverride,
  queueShoppingRowNote,
  replayShoppingOperation,
} from "./shopping-operations";
import { readShoppingList, readShoppingLists } from "./shopping-projections";
import {
  queueAdHocShoppingItem,
  queueAdHocShoppingItemFulfilment,
  queueAdHocShoppingItemLifecycle,
  queueAdHocShoppingItemUpdate,
  replayAdHocShoppingItemFulfilment,
  replayAdHocShoppingItemLifecycle,
  replayAdHocShoppingItemUpdate,
} from "./ad-hoc-shopping-item";

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
      recipe_version_id: ids.version,
      diner_count: 4,
      consumption_percentage: "100",
      selected_scale_amount: "4",
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

  it("projects and queues a distinct ad-hoc shopping item atomically", async () => {
    await seedPlanner();
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
      record("unit_definition", ids.unit, {
        id: ids.unit,
        organization_id: null,
        code: "g",
        allows_ingredient_quantity: true,
      }),
      record("store_section", ids.section, {
        id: ids.section,
        organization_id: ids.organization,
        name: "Produce",
      }),
    ]);
    await queueAdHocShoppingItem(ids.user, ids.organization, {
      shoppingListId: ids.list,
      name: "  Lemons ",
      targetAmount: "3.5",
      unitId: ids.unit,
      storeSectionId: ids.section,
    });
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      adHocItems: [
        { name: "Lemons", target: "3.5", unit: "g", sectionName: "Produce" },
      ],
    });
    await expect(localDb.outbox.toArray()).resolves.toEqual([
      expect.objectContaining({
        commandType: "shopping_list.create_ad_hoc_item",
        payload: expect.objectContaining({
          shopping_list_id: ids.list,
          name: "Lemons",
          target_amount: "3.5",
        }),
      }),
    ]);
    await queueAdHocShoppingItem(ids.user, ids.organization, {
      shoppingListId: ids.list,
      name: "Cafe\u0301",
      targetAmount: "1",
      unitId: ids.unit,
      storeSectionId: ids.section,
    });
    await expect(localDb.outbox.orderBy("createdAt").last()).resolves.toEqual(
      expect.objectContaining({
        payload: expect.objectContaining({ name: "Café" }),
      }),
    );
    await expect(
      queueAdHocShoppingItem(ids.user, ids.organization, {
        shoppingListId: ids.list,
        name: "😀".repeat(200),
        targetAmount: "1",
        unitId: ids.unit,
        storeSectionId: ids.section,
      }),
    ).rejects.toThrow("ad_hoc_shopping_item");
    await expect(localDb.outbox.count()).resolves.toBe(2);
  });

  it("projects contribution context and exact snapshot cost, but rejects malformed price data", async () => {
    await seedPlanner();
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
        ingredient_id: ids.recipe,
        ingredient_name: "Tomatoes",
        calculation_unit_id: ids.unit,
        available_supply_quantity: "0",
        manual_purchase_target: "10",
        aggregate_fulfilment_credit: "0",
      }),
      record("unit_definition", ids.unit, {
        id: ids.unit,
        organization_id: null,
        code: "kg",
        dimension: "mass",
        base_unit_factor: "1",
        allows_ingredient_quantity: true,
      }),
      record("unit_definition", ids.retiredContribution, {
        id: ids.retiredContribution,
        organization_id: null,
        code: "g",
        dimension: "mass",
        base_unit_factor: "0.001",
        allows_ingredient_quantity: true,
      }),
      record("ingredient_version", ids.version, {
        id: ids.version,
        organization_id: ids.organization,
        ingredient_id: ids.recipe,
        canonical_unit_id: ids.unit,
      }),
      record("shopping_contribution", ids.contribution, {
        id: ids.contribution,
        organization_id: ids.organization,
        event_id: ids.event,
        shopping_list_id: ids.list,
        shopping_ingredient_row_id: ids.row,
        ingredient_id: ids.recipe,
        fulfilment_credit: "0",
      }),
      record("shopping_contribution_snapshot", ids.snapshot, {
        id: ids.snapshot,
        organization_id: ids.organization,
        event_id: ids.event,
        shopping_list_id: ids.list,
        generation_revision_id: ids.revision,
        shopping_contribution_id: ids.contribution,
        ingredient_id: ids.recipe,
        ingredient_version_id: ids.version,
        active_in_revision: true,
        generated_quantity: "2",
        price_amount: "3",
        priced_quantity: "1",
        priced_unit_id: ids.unit,
        currency: "EUR",
        source_details: {
          recipe_name: "Chili",
          recipe_description: "Smoky tomato stew",
          day: "2026-08-10",
          meal_role: "Dinner",
          line_notes: ["diced"],
          recipe_notes: ["serve warm"],
          ingredient_notes: ["use ripe fruit"],
        },
      }),
    ]);
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [
        {
          contributions: [
            expect.objectContaining({
              requiredQuantity: "2",
              fulfilled: false,
              partial: false,
              source: "Chili",
              recipeDescription: "Smoky tomato stew",
              day: "2026-08-10",
              mealRole: "Dinner",
              lineNotes: ["diced"],
              recipeNotes: ["serve warm"],
              ingredientNotes: ["use ripe fruit"],
              estimatedUnitPrice: "3 / 1 kg (EUR)",
              expectedCost: "30.00 EUR",
            }),
          ],
        },
      ],
    });
    const contributionKey: [string, string, string, string] = [
      ids.user,
      ids.organization,
      "shopping_contribution",
      ids.contribution,
    ];
    const seededContribution = await localDb.canonicalRecords.get(contributionKey);
    if (!seededContribution) throw new Error("seeded contribution missing");
    await localDb.canonicalRecords.update(
      contributionKey,
      { fields: { ...seededContribution.fields, fulfilment_credit: "1" } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [{ contributions: [{ fulfilled: false, partial: true }] }],
    });
    const zeroSnapshot = await localDb.canonicalRecords.get([
      ids.user,
      ids.organization,
      "shopping_contribution_snapshot",
      ids.snapshot,
    ]);
    if (!zeroSnapshot) throw new Error("seeded snapshot missing");
    await localDb.canonicalRecords.update(
      [
        ids.user,
        ids.organization,
        "shopping_contribution_snapshot",
        ids.snapshot,
      ],
      { fields: { ...zeroSnapshot.fields, generated_quantity: "0" } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [{ contributions: [{ fulfilled: false, partial: false }] }],
    });
    await localDb.canonicalRecords.update(
      [
        ids.user,
        ids.organization,
        "shopping_contribution_snapshot",
        ids.snapshot,
      ],
      { fields: zeroSnapshot.fields },
    );
    await localDb.canonicalRecords.update(
      contributionKey,
      { fields: { ...seededContribution.fields, fulfilment_credit: "2" } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [{ contributions: [{ fulfilled: true, partial: false }] }],
    });
    const contribution = await localDb.canonicalRecords.get(contributionKey);
    if (!contribution) throw new Error("test contribution missing");
    await localDb.canonicalRecords.update(contributionKey, {
      fields: { ...contribution.fields, ingredient_id: ids.retiredContribution },
    });
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({ rows: [{ contributions: [] }] });
    await localDb.canonicalRecords.update(contributionKey, {
      fields: contribution.fields,
    });
    const rowKey: [string, string, string, string] = [
      ids.user,
      ids.organization,
      "shopping_ingredient_row",
      ids.row,
    ];
    const row = await localDb.canonicalRecords.get(rowKey);
    if (!row) throw new Error("test row missing");
    await localDb.canonicalRecords.update(rowKey, {
      fields: {
        ...row.fields,
        calculation_unit_id: ids.retiredContribution,
        manual_purchase_target: "2000",
      },
    });
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [{ contributions: [expect.objectContaining({ expectedCost: "6.00 EUR" })] }],
    });
    await localDb.canonicalRecords.update(
      [ids.user, ids.organization, "unit_definition", ids.retiredContribution],
      {
        fields: {
          id: ids.retiredContribution,
          organization_id: null,
          code: "l",
          dimension: "volume",
          base_unit_factor: "0.001",
          allows_ingredient_quantity: true,
        },
      },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [{ contributions: [expect.objectContaining({ expectedCost: null })] }],
    });
    await localDb.canonicalRecords.update(
      [ids.user, ids.organization, "unit_definition", ids.retiredContribution],
      {
        fields: {
          id: ids.retiredContribution,
          organization_id: null,
          code: "g",
          dimension: "mass",
          base_unit_factor: "0.001",
          allows_ingredient_quantity: true,
        },
      },
    );
    await localDb.canonicalRecords.update(rowKey, { fields: row.fields });
    await localDb.canonicalRecords.update(
      [ids.user, ids.organization, "shopping_contribution_snapshot", ids.snapshot],
      { fields: { price_amount: "NaN", source_details: [] } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [
        {
          contributions: [
            expect.objectContaining({
              source: null,
              estimatedUnitPrice: null,
              expectedCost: null,
            }),
          ],
        },
      ],
    });
    const snapshotKey: [string, string, string, string] = [
      ids.user,
      ids.organization,
      "shopping_contribution_snapshot",
      ids.snapshot,
    ];
    const cached = await localDb.canonicalRecords.get(snapshotKey);
    if (!cached) throw new Error("test snapshot missing");
    await localDb.canonicalRecords.update(snapshotKey, {
      fields: { ...cached.fields, id: ids.retiredContribution },
    });
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [{ contributions: [expect.objectContaining({ source: null, expectedCost: null })] }],
    });
    await localDb.canonicalRecords.update(snapshotKey, {
      fields: { ...cached.fields, ingredient_id: ids.retiredContribution },
    });
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [{ contributions: [expect.objectContaining({ source: null, expectedCost: null })] }],
    });
    await localDb.canonicalRecords.update(snapshotKey, {
      fields: {
        ...cached.fields,
        id: ids.snapshot,
        organization_id: ids.organization,
        event_id: ids.event,
        shopping_list_id: ids.list,
        generation_revision_id: ids.revision,
        shopping_contribution_id: ids.contribution,
        ingredient_id: ids.recipe,
        ingredient_version_id: ids.version,
        active_in_revision: true,
        generated_quantity: "2",
        priced_quantity: "1",
        priced_unit_id: ids.unit,
        currency: "EUR",
        price_amount: "9".repeat(40),
        source_details: { recipe_name: "Chili" },
      },
    });
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({
      rows: [{ contributions: [expect.objectContaining({ source: "Chili", expectedCost: null })] }],
    });
    await localDb.canonicalRecords.update(
      [ids.user, ids.organization, "unit_definition", ids.unit],
      { fields: { organization_id: ids.retiredContribution } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({ rows: [] });
  });

  it("updates an ad-hoc item through the typed outbox without overwriting a newer field", async () => {
    await seedPlanner();
    const itemId = "2e8b2b21-c378-4574-9e46-9338c81305ef";
    const record = (entityType: string, entityId: string, fields: Record<string, unknown>, fieldClocks: Record<string, unknown> = {}) => ({
      userId: ids.user, organizationId: ids.organization, entityType, entityId,
      recordSchemaVersion: 1, lifecycle: "active" as const, fields, fieldClocks,
      immutable: false, updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await localDb.canonicalRecords.bulkAdd([
      record("shopping_list", ids.list, { id: ids.list, organization_id: ids.organization, event_id: ids.event }),
      record("unit_definition", ids.unit, { id: ids.unit, organization_id: null, code: "g", allows_ingredient_quantity: true }),
      record("store_section", ids.section, { id: ids.section, organization_id: ids.organization, name: "Produce" }),
      record("ad_hoc_shopping_item", itemId, {
        id: itemId, organization_id: ids.organization, event_id: ids.event, shopping_list_id: ids.list,
        name: "Lemons", target_amount: "3", unit_id: ids.unit, store_section_id: ids.section, note: null,
      }, { name: { winning_client_wall_time: "2999-01-01T00:00:00.000Z", winning_mutation_id: "ffffffff-ffff-4fff-8fff-ffffffffffff" } }),
    ]);
    await queueAdHocShoppingItemUpdate(ids.user, ids.organization, {
      shoppingListId: ids.list, adHocShoppingItemId: itemId, name: "Limes", targetAmount: "4", unitId: ids.unit, storeSectionId: ids.section, note: "fresh",
    });
    await expect(localDb.optimisticOverlays.get([ids.user, ids.organization, "ad_hoc_shopping_item", itemId])).resolves.toMatchObject({ fields: { name: "Lemons", target_amount: "4", note: "fresh" } });
    await expect(localDb.outbox.orderBy("createdAt").last()).resolves.toEqual(expect.objectContaining({ commandType: "shopping_list.update_ad_hoc_item" }));
    await replayAdHocShoppingItemUpdate(ids.user, ids.organization, {
      id: "1e8b2b21-c378-4574-9e46-9338c81305ef", actionAt: "invalid", payload: {},
    });
  });

  it("queues ad-hoc fulfilment without reviving a newer canonical value", async () => {
    await seedPlanner();
    const record = (
      entityType: string,
      entityId: string,
      fields: Record<string, unknown>,
      fieldClocks: Record<string, unknown> = {},
    ) => ({
      userId: ids.user,
      organizationId: ids.organization,
      entityType,
      entityId,
      recordSchemaVersion: 1,
      lifecycle: "active" as const,
      fields,
      fieldClocks,
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    const itemId = "2e8b2b21-c378-4574-9e46-9338c81305ef";
    await localDb.canonicalRecords.bulkAdd([
      record("shopping_list", ids.list, {
        id: ids.list,
        organization_id: ids.organization,
        event_id: ids.event,
        name: "Saturday",
        current_generation_revision_id: ids.revision,
        created_at: "2026-08-07T12:00:00.000Z",
      }),
      record(
        "ad_hoc_shopping_item",
        itemId,
        {
          id: itemId,
          organization_id: ids.organization,
          event_id: ids.event,
          shopping_list_id: ids.list,
          name: "Lemons",
          target_amount: "3.5",
          fulfilment_credit: "0",
          unit_id: ids.unit,
          store_section_id: ids.section,
        },
        { fulfilment_credit: null },
      ),
      record("unit_definition", ids.unit, {
        id: ids.unit,
        organization_id: null,
        code: "g",
        allows_ingredient_quantity: true,
      }),
      record("store_section", ids.section, {
        id: ids.section,
        organization_id: ids.organization,
        name: "Produce",
      }),
    ]);
    await queueAdHocShoppingItemFulfilment(ids.user, ids.organization, {
      shoppingListId: ids.list,
      adHocShoppingItemId: itemId,
      fulfilled: true,
    });
    const baseFields = {
      id: itemId,
      organization_id: ids.organization,
      event_id: ids.event,
      shopping_list_id: ids.list,
      name: "Lemons",
      target_amount: "3.5",
      fulfilment_credit: "0",
      unit_id: ids.unit,
      store_section_id: ids.section,
    };
    await localDb.optimisticOverlays.update(
      [ids.user, ids.organization, "ad_hoc_shopping_item", itemId],
      { fields: { ...baseFields } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({ adHocItems: [{ fulfilled: false, partial: false }] });
    await localDb.optimisticOverlays.update(
      [ids.user, ids.organization, "ad_hoc_shopping_item", itemId],
      { fields: { ...baseFields, fulfilment_credit: "1" } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({ adHocItems: [{ fulfilled: false, partial: true }] });
    await localDb.optimisticOverlays.update(
      [ids.user, ids.organization, "ad_hoc_shopping_item", itemId],
      { fields: { ...baseFields, target_amount: "3.50", fulfilment_credit: "3.5" } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({ adHocItems: [{ fulfilled: true, partial: false }] });
    await localDb.optimisticOverlays.update(
      [ids.user, ids.organization, "ad_hoc_shopping_item", itemId],
      { fields: { ...baseFields, target_amount: "0.5", fulfilment_credit: "0.25" } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({ adHocItems: [{ fulfilled: false, partial: true }] });
    await localDb.optimisticOverlays.update(
      [ids.user, ids.organization, "ad_hoc_shopping_item", itemId],
      { fields: { ...baseFields, target_amount: "0", fulfilment_credit: "1" } },
    );
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({ adHocItems: [{ fulfilled: false, partial: false }] });
    await expect(
      readShoppingList(ids.user, ids.organization, ids.event, ids.list),
    ).resolves.toMatchObject({ adHocItems: [{ fulfilled: false }] });
    await expect(localDb.outbox.toArray()).resolves.toContainEqual(
      expect.objectContaining({
        commandType: "shopping_list.set_ad_hoc_item_fulfilment",
        payload: {
          shopping_list_id: ids.list,
          ad_hoc_shopping_item_id: itemId,
          fulfilled: true,
        },
      }),
    );
    await localDb.optimisticOverlays.clear();
    await localDb.canonicalRecords.update(
      [ids.user, ids.organization, "ad_hoc_shopping_item", itemId],
      {
        fieldClocks: {
          fulfilment_credit: {
            winning_client_wall_time: "2026-08-08T12:00:00.000Z",
            winning_mutation_id: "3e8b2b21-c378-4574-9e46-9338c81305ef",
          },
        },
      },
    );
    await replayAdHocShoppingItemFulfilment(ids.user, ids.organization, {
      id: "4e8b2b21-c378-4574-9e46-9338c81305ef",
      actionAt: "2026-08-07T12:00:00.000Z",
      payload: {
        shopping_list_id: ids.list,
        ad_hoc_shopping_item_id: itemId,
        fulfilled: true,
      },
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
  });

  it("retires and explicitly restores an ad-hoc item through durable lifecycle intent", async () => {
    await seedPlanner();
    const itemId = "2e8b2b21-c378-4574-9e46-9338c81305ef";
    const record = (entityType: string, entityId: string, fields: Record<string, unknown>) => ({
      userId: ids.user, organizationId: ids.organization, entityType, entityId,
      recordSchemaVersion: 1, lifecycle: "active" as const, fields, fieldClocks: {},
      immutable: false, updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await localDb.canonicalRecords.bulkAdd([
      record("shopping_list", ids.list, { id: ids.list, organization_id: ids.organization, event_id: ids.event }),
      record("ad_hoc_shopping_item", itemId, { id: itemId, organization_id: ids.organization, event_id: ids.event, shopping_list_id: ids.list }),
    ]);
    await queueAdHocShoppingItemLifecycle(ids.user, ids.organization, {
      shoppingListId: ids.list, adHocShoppingItemId: itemId, operation: "retire",
    });
    const retired = await localDb.optimisticOverlays.get([ids.user, ids.organization, "ad_hoc_shopping_item", itemId]);
    expect(retired?.lifecycle).toBe("retired");
    await expect(localDb.outbox.toArray()).resolves.toContainEqual(expect.objectContaining({ commandType: "shopping_list.ad_hoc_item_lifecycle" }));
    await replayAdHocShoppingItemLifecycle(ids.user, ids.organization, {
      id: "3e8b2b21-c378-4574-9e46-9338c81305ef",
      actionAt: new Date(Date.parse(retired?.updatedAt ?? "") + 1).toISOString(),
      payload: { shopping_list_id: ids.list, ad_hoc_shopping_item_id: itemId, operation: "restore" },
    });
    await expect(localDb.optimisticOverlays.get([ids.user, ids.organization, "ad_hoc_shopping_item", itemId])).resolves.toMatchObject({ lifecycle: "active", fields: { retired_at: null } });
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

  it("durably queues refresh without advancing the canonical generation pointer", async () => {
    await seedPlanner();
    await localDb.canonicalRecords.bulkAdd([
      {
        userId: ids.user,
        organizationId: ids.organization,
        entityType: "shopping_list",
        entityId: ids.list,
        recordSchemaVersion: 1,
        lifecycle: "active",
        fields: {
          id: ids.list,
          organization_id: ids.organization,
          event_id: ids.event,
          current_generation_revision_id: ids.revision,
        },
        fieldClocks: {},
        immutable: false,
        updatedAt: "2026-08-07T12:00:00.000Z",
      },
    ]);

    await queueShoppingListRefresh(ids.user, ids.organization, {
      shoppingListId: ids.list,
      parentGenerationRevisionId: ids.revision,
      scheduledRecipeIds: [ids.scheduled],
    });

    await expect(
      localDb.canonicalRecords.get([
        ids.user,
        ids.organization,
        "shopping_list",
        ids.list,
      ]),
    ).resolves.toMatchObject({
      fields: { current_generation_revision_id: ids.revision },
    });
    await expect(
      localDb.optimisticOverlays.get([
        ids.user,
        ids.organization,
        "shopping_list",
        ids.list,
      ]),
    ).resolves.toBeUndefined();
    await expect(localDb.outbox.toArray()).resolves.toEqual([
      expect.objectContaining({
        commandType: "shopping_list.refresh",
        payload: expect.objectContaining({
          shopping_list_id: ids.list,
          parent_generation_revision_id: ids.revision,
          scheduled_recipe_ids: [ids.scheduled],
        }),
      }),
    ]);
  });

  it("orders a refresh after a locally created list without requiring a server pointer", async () => {
    await seedPlanner();
    await queueShoppingList(ids.user, ids.organization, {
      eventId: ids.event,
      name: "Saturday shopping",
      scheduledRecipeIds: [ids.scheduled],
    });
    const created = await localDb.optimisticOverlays
      .where("[userId+organizationId]")
      .equals([ids.user, ids.organization])
      .filter((record) => record.entityType === "shopping_list")
      .first();
    const parentGenerationRevisionId =
      created?.fields.current_generation_revision_id;
    expect(typeof parentGenerationRevisionId).toBe("string");
    await expect(
      queueShoppingListRefresh(ids.user, ids.organization, {
        shoppingListId: created?.entityId ?? "",
        parentGenerationRevisionId:
          typeof parentGenerationRevisionId === "string"
            ? parentGenerationRevisionId
            : "",
        scheduledRecipeIds: [ids.scheduled],
      }),
    ).resolves.toBe(true);
    const commands = (await localDb.outbox.toArray()).sort(
      compareOutboxCommands,
    );
    expect(commands.map((command) => command.commandType)).toEqual([
      "shopping_list.create",
      "shopping_list.refresh",
    ]);
    expect(
      await localDb.canonicalRecords.get([
        ids.user,
        ids.organization,
        "shopping_list",
        created?.entityId ?? "",
      ]),
    ).toBeUndefined();
  });

  it("deduplicates concurrent refresh clicks in the durable outbox", async () => {
    await seedPlanner();
    await localDb.canonicalRecords.add({
      userId: ids.user,
      organizationId: ids.organization,
      entityType: "shopping_list",
      entityId: ids.list,
      recordSchemaVersion: 1,
      lifecycle: "active",
      fields: {
        id: ids.list,
        organization_id: ids.organization,
        event_id: ids.event,
        current_generation_revision_id: ids.revision,
      },
      fieldClocks: {},
      immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    const input = {
      shoppingListId: ids.list,
      parentGenerationRevisionId: ids.revision,
      scheduledRecipeIds: [ids.scheduled],
    };
    await expect(
      Promise.all([
        queueShoppingListRefresh(ids.user, ids.organization, input),
        queueShoppingListRefresh(ids.user, ids.organization, input),
      ]),
    ).resolves.toEqual(expect.arrayContaining([true, false]));
    await expect(localDb.outbox.count()).resolves.toBe(1);
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
        ingredient_id: ids.recipe,
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
        ingredient_id: ids.recipe,
        fulfilment_credit: "6",
      }),
      {
        ...record("shopping_contribution", ids.retiredContribution, {
          id: ids.retiredContribution,
          organization_id: ids.organization,
          event_id: ids.event,
          shopping_list_id: ids.list,
          shopping_ingredient_row_id: ids.row,
          ingredient_id: ids.recipe,
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
        ingredient_id: ids.recipe,
        ingredient_version_id: ids.version,
        active_in_revision: true,
        generated_quantity: "10",
      }),
      record("ingredient_version", ids.version, {
        id: ids.version,
        organization_id: ids.organization,
        ingredient_id: ids.recipe,
      }),
      record("unit_definition", ids.unit, { id: ids.unit, organization_id: null, code: "kg" }),
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
            partial: true,
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

  it("queues, clears, and replays a valid active store-section override fail closed", async () => {
    const record = (entityType: string, entityId: string, fields: Record<string, unknown>, lifecycle: "active" | "retired" = "active") => ({
      userId: ids.user, organizationId: ids.organization, entityType, entityId,
      recordSchemaVersion: 1, lifecycle, fields, fieldClocks: {}, immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await localDb.canonicalRecords.bulkAdd([
      record("event", ids.event, { id: ids.event, lifecycle: "active" }),
      record("shopping_list", ids.list, { id: ids.list, organization_id: ids.organization, event_id: ids.event }),
      record("shopping_ingredient_row", ids.row, { id: ids.row, organization_id: ids.organization, shopping_list_id: ids.list, store_section_override_id: null }),
      record("store_section", ids.section, { id: ids.section, organization_id: ids.organization, name: "Produce" }),
      record("store_section", ids.retiredContribution, { id: ids.retiredContribution, organization_id: ids.organization, name: "Retired" }, "retired"),
      record("store_section", ids.contribution, { id: ids.contribution, organization_id: ids.user, name: "Foreign" }),
    ]);
    const input = { shoppingListId: ids.list, shoppingIngredientRowId: ids.row };
    await queueShoppingStoreSectionOverride(ids.user, ids.organization, { ...input, storeSectionId: ids.section });
    const queued = await localDb.outbox.toArray();
    expect(queued).toHaveLength(1);
    expect(queued[0]).toMatchObject({ commandType: "shopping_list.set_store_section_override", payload: { shopping_list_id: ids.list, shopping_ingredient_row_id: ids.row, store_section_id: ids.section } });
    expect((await localDb.canonicalRecords.get([ids.user, ids.organization, "shopping_ingredient_row", ids.row]))?.fields.store_section_override_id).toBeNull();
    await queueShoppingStoreSectionOverride(ids.user, ids.organization, { ...input, storeSectionId: null });
    expect((await localDb.optimisticOverlays.get([ids.user, ids.organization, "shopping_ingredient_row", ids.row]))?.fields.store_section_override_id).toBeNull();
    await expect(queueShoppingStoreSectionOverride(ids.user, ids.organization, { ...input, storeSectionId: ids.retiredContribution })).rejects.toThrow("shopping_operation");
    await expect(queueShoppingStoreSectionOverride(ids.user, ids.organization, { ...input, storeSectionId: ids.contribution })).rejects.toThrow("shopping_operation");
    await localDb.optimisticOverlays.clear();
    await replayShoppingOperation(ids.user, ids.organization, queued[0]);
    expect((await localDb.optimisticOverlays.get([ids.user, ids.organization, "shopping_ingredient_row", ids.row]))?.fields.store_section_override_id).toBe(ids.section);
    await localDb.optimisticOverlays.clear();
    await replayShoppingOperation(ids.user, ids.organization, { ...queued[0], payload: { ...queued[0].payload, unexpected: true } });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    const section = await localDb.canonicalRecords.get([ids.user, ids.organization, "store_section", ids.section]);
    if (!section) throw new Error("missing section");
    await localDb.canonicalRecords.put({ ...section, lifecycle: "retired" });
    await replayShoppingOperation(ids.user, ids.organization, queued[0]);
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await localDb.canonicalRecords.put({ ...section, fields: { ...section.fields, organization_id: ids.user } });
    await replayShoppingOperation(ids.user, ids.organization, queued[0]);
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await localDb.canonicalRecords.put(section);
    const canonicalRow = await localDb.canonicalRecords.get([ids.user, ids.organization, "shopping_ingredient_row", ids.row]);
    if (!canonicalRow) throw new Error("missing row");
    await localDb.canonicalRecords.put({
      ...canonicalRow,
      fieldClocks: {
        ...canonicalRow.fieldClocks,
        store_section_override_id: {
          winning_client_wall_time: "2026-08-07T12:00:00.000002Z",
          winning_mutation_id: "00000000-0000-0000-0000-000000000001",
        },
      },
    });
    await replayShoppingOperation(ids.user, ids.organization, {
      ...queued[0],
      id: "ffffffff-ffff-ffff-ffff-ffffffffffff",
      actionAt: "2026-08-07T12:00:00.000001Z",
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await replayShoppingOperation(ids.user, ids.organization, {
      ...queued[0],
      id: "not-a-uuid",
      actionAt: "not-a-timestamp",
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    const event = await localDb.canonicalRecords.get([ids.user, ids.organization, "event", ids.event]);
    const list = await localDb.canonicalRecords.get([ids.user, ids.organization, "shopping_list", ids.list]);
    if (!event || !list) throw new Error("missing shopping scope");
    await localDb.canonicalRecords.put({ ...event, fields: { ...event.fields, lifecycle: "archived" } });
    await expect(queueShoppingStoreSectionOverride(ids.user, ids.organization, { ...input, storeSectionId: ids.section })).rejects.toThrow("shopping_operation");
    await localDb.canonicalRecords.put(event);
    await localDb.canonicalRecords.put({ ...list, lifecycle: "retired" });
    await expect(queueShoppingStoreSectionOverride(ids.user, ids.organization, { ...input, storeSectionId: ids.section })).rejects.toThrow("shopping_operation");
    await localDb.canonicalRecords.put(list);
    await localDb.canonicalRecords.put({ ...canonicalRow, lifecycle: "retired" });
    await expect(queueShoppingStoreSectionOverride(ids.user, ids.organization, { ...input, storeSectionId: ids.section })).rejects.toThrow("shopping_operation");
  });

  it("canonicalizes and replays scoped row notes", async () => {
    const record = (entityType: string, entityId: string, fields: Record<string, unknown>) => ({
      userId: ids.user, organizationId: ids.organization, entityType, entityId,
      recordSchemaVersion: 1, lifecycle: "active" as const, fields, fieldClocks: {}, immutable: false,
      updatedAt: "2026-08-07T12:00:00.000Z",
    });
    await localDb.canonicalRecords.bulkAdd([
      record("event", ids.event, { id: ids.event, lifecycle: "active" }),
      record("shopping_list", ids.list, { id: ids.list, organization_id: ids.organization, event_id: ids.event }),
      record("shopping_ingredient_row", ids.row, { id: ids.row, organization_id: ids.organization, shopping_list_id: ids.list, note: null }),
    ]);
    const input = { shoppingListId: ids.list, shoppingIngredientRowId: ids.row };
    await queueShoppingRowNote(ids.user, ids.organization, { ...input, note: "  e\u0301\r\nnote  " });
    const queued = await localDb.outbox.toArray();
    expect(queued[0]?.payload).toMatchObject({ note: "é\nnote" });
    await localDb.optimisticOverlays.clear();
    await replayShoppingOperation(ids.user, ids.organization, queued[0]);
    await expect(localDb.optimisticOverlays.get([ids.user, ids.organization, "shopping_ingredient_row", ids.row])).resolves.toMatchObject({ fields: { note: "é\nnote" } });
    const canonicalRetry = {
      ...queued[0],
      payload: { ...queued[0].payload, note: "é\nnote" },
    };
    await localDb.optimisticOverlays.clear();
    await replayShoppingOperation(ids.user, ids.organization, canonicalRetry);
    await expect(localDb.optimisticOverlays.get([ids.user, ids.organization, "shopping_ingredient_row", ids.row])).resolves.toMatchObject({ fields: { note: "é\nnote" } });
    const row = await localDb.canonicalRecords.get([ids.user, ids.organization, "shopping_ingredient_row", ids.row]);
    if (!row) throw new Error("missing row");
    await localDb.canonicalRecords.put({
      ...row,
      fieldClocks: {
        note: {
          winning_client_wall_time: "2026-08-07T12:00:00.000002Z",
          winning_mutation_id: "00000000-0000-0000-0000-000000000001",
        },
      },
    });
    await localDb.optimisticOverlays.clear();
    await replayShoppingOperation(ids.user, ids.organization, {
      ...queued[0],
      id: "ffffffff-ffff-ffff-ffff-ffffffffffff",
      actionAt: "2026-08-07T12:00:00.000001Z",
    });
    await expect(localDb.optimisticOverlays.count()).resolves.toBe(0);
    await expect(queueShoppingRowNote(ids.user, ids.organization, { ...input, note: "\0" })).rejects.toThrow("shopping_operation");
    await expect(queueShoppingRowNote(ids.user, ids.organization, { ...input, note: "x".repeat(4001) })).rejects.toThrow("shopping_operation");
    await queueShoppingRowNote(ids.user, ids.organization, { ...input, note: "   " });
    await expect(localDb.optimisticOverlays.get([ids.user, ids.organization, "shopping_ingredient_row", ids.row])).resolves.toMatchObject({ fields: { note: null } });
  });
});
