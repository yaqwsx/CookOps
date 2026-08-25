import { beforeEach, describe, expect, it } from "vitest";

import { readEventCosts } from "./event-cost-projections";
import { localDb } from "./local-db";

const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ids = {
  event: "6ce17d2f-8365-4b1f-a80b-34d10425d51c",
  scheduled: "7ce17d2f-8365-4b1f-a80b-34d10425d51c",
  recipeVersion: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
  line: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
  ingredientVersion: "ace17d2f-8365-4b1f-a80b-34d10425d51c",
  ingredient: "bce17d2f-8365-4b1f-a80b-34d10425d51c",
  unit: "cce17d2f-8365-4b1f-a80b-34d10425d51c",
  kilogram: "dde17d2f-8365-4b1f-a80b-34d10425d51c",
  price: "dce17d2f-8365-4b1f-a80b-34d10425d51c",
  snapshot: "ece17d2f-8365-4b1f-a80b-34d10425d51c",
  shoppingList: "fce17d2f-8365-4b1f-a80b-34d10425d51c",
  revision: "1de17d2f-8365-4b1f-a80b-34d10425d51c",
  shoppingSnapshot: "2de17d2f-8365-4b1f-a80b-34d10425d51c",
  receipt: "3de17d2f-8365-4b1f-a80b-34d10425d51c",
  row: "7de17d2f-8365-4b1f-a80b-34d10425d51c",
  contribution: "8de17d2f-8365-4b1f-a80b-34d10425d51c",
  secondContribution: "0ee17d2f-8365-4b1f-a80b-34d10425d51c",
  fixedLine: "4de17d2f-8365-4b1f-a80b-34d10425d51c",
  replacement: "5de17d2f-8365-4b1f-a80b-34d10425d51c",
  added: "6de17d2f-8365-4b1f-a80b-34d10425d51c",
  retiredList: "9de17d2f-8365-4b1f-a80b-34d10425d51c",
  retiredRevision: "ade17d2f-8365-4b1f-a80b-34d10425d51c",
  retiredRow: "bde17d2f-8365-4b1f-a80b-34d10425d51c",
  retiredContribution: "cde17d2f-8365-4b1f-a80b-34d10425d51c",
  retiredSnapshot: "fde17d2f-8365-4b1f-a80b-34d10425d51c",
  secondShoppingSnapshot: "0fe17d2f-8365-4b1f-a80b-34d10425d51c",
  duplicateShoppingSnapshot: "13e17d2f-8365-4b1f-a80b-34d10425d51c",
  foreignIngredient: "10e17d2f-8365-4b1f-a80b-34d10425d51c",
  foreignPrice: "11e17d2f-8365-4b1f-a80b-34d10425d51c",
  foreignSnapshot: "12e17d2f-8365-4b1f-a80b-34d10425d51c",
};

async function record(
  entityType: string,
  entityId: string,
  fields: Record<string, unknown>,
  immutable = false,
) {
  await localDb.canonicalRecords.add({
    userId,
    organizationId,
    entityType,
    entityId,
    recordSchemaVersion: 1,
    lifecycle: "active",
    fields: { id: entityId, organization_id: organizationId, ...fields },
    fieldClocks: {},
    immutable,
    updatedAt: "2026-08-07T12:00:00.000Z",
  });
}

async function seedShoppingCost() {
  await record("event", ids.event, { currency: "CZK", budget_amount: "20" });
  await record("unit_definition", ids.unit, {
    dimension: "mass",
    base_unit_factor: "1",
  });
  await record("unit_definition", ids.kilogram, {
    dimension: "mass",
    base_unit_factor: "1000",
  });
  await record("ingredient", ids.ingredient, {
    current_version_id: ids.ingredientVersion,
  });
  await record(
    "ingredient_version",
    ids.ingredientVersion,
    {
      ingredient_id: ids.ingredient,
      canonical_unit_id: ids.unit,
      name: "Beans",
    },
    true,
  );
  await record("event_ingredient_price_snapshot", ids.snapshot, {
    event_id: ids.event,
    ingredient_id: ids.ingredient,
    event_ingredient_price_id: ids.price,
    state: "available",
    price_amount: "3",
    priced_quantity: "2",
    priced_unit_id: ids.unit,
    currency: "CZK",
  });
  await record("event_ingredient_price", ids.price, {
    event_id: ids.event,
    ingredient_id: ids.ingredient,
    current_snapshot_id: ids.snapshot,
  });
  await record("shopping_list", ids.shoppingList, {
    event_id: ids.event,
    current_generation_revision_id: ids.revision,
  });
  await record(
    "shopping_generation_revision",
    ids.revision,
    {
      event_id: ids.event,
      shopping_list_id: ids.shoppingList,
      parent_revision_id: null,
    },
    true,
  );
  await record("shopping_ingredient_row", ids.row, {
    event_id: ids.event,
    shopping_list_id: ids.shoppingList,
    ingredient_id: ids.ingredient,
    ingredient_name: "Beans",
    available_supply_quantity: "0",
    manual_purchase_target: null,
  });
  await record("shopping_contribution", ids.contribution, {
    event_id: ids.event,
    shopping_list_id: ids.shoppingList,
    shopping_ingredient_row_id: ids.row,
    ingredient_id: ids.ingredient,
  });
  await record(
    "shopping_contribution_snapshot",
    ids.shoppingSnapshot,
    {
      event_id: ids.event,
      shopping_list_id: ids.shoppingList,
      generation_revision_id: ids.revision,
      shopping_contribution_id: ids.contribution,
      ingredient_id: ids.ingredient,
      active_in_revision: true,
      ingredient_version_id: ids.ingredientVersion,
      generated_quantity: "2",
      event_price_snapshot_id: ids.snapshot,
      price_amount: "3",
      priced_quantity: "2",
      priced_unit_id: ids.unit,
      currency: "CZK",
    },
    true,
  );
}

async function updateShoppingSnapshot(fields: Record<string, unknown>) {
  const key: [string, string, string, string] = [
    userId,
    organizationId,
    "shopping_contribution_snapshot",
    ids.shoppingSnapshot,
  ];
  const current = await localDb.canonicalRecords.get(key);
  if (!current) throw new Error("Missing shopping snapshot fixture");
  await localDb.canonicalRecords.update(key, {
    fields: { ...current.fields, ...fields },
  });
}

describe("cached event cost projection", () => {
  beforeEach(() => localDb.canonicalRecords.clear());

  it("uses an event snapshot, exact decimal arithmetic, and exposes missing prices", async () => {
    await record("event", ids.event, { currency: "CZK", budget_amount: "20" });
    await record("unit_definition", ids.unit, {
      dimension: "mass",
      base_unit_factor: "1",
    });
    await record("unit_definition", ids.kilogram, {
      dimension: "mass",
      base_unit_factor: "1000",
    });
    await record("ingredient", ids.ingredient, {
      current_version_id: ids.ingredientVersion,
    });
    await record(
      "ingredient_version",
      ids.ingredientVersion,
      {
        ingredient_id: ids.ingredient,
        canonical_unit_id: ids.unit,
        name: "Beans",
      },
      true,
    );
    await record("recipe_version", ids.recipeVersion, {
      base_scaling_amount: "2",
    });
    await record("recipe_ingredient_line", ids.line, {
      recipe_version_id: ids.recipeVersion,
      line_key: ids.line,
      ingredient_version_id: ids.ingredientVersion,
      base_quantity: "3",
      scaling_behavior: "proportional",
    });
    await record("scheduled_recipe", ids.scheduled, {
      event_id: ids.event,
      recipe_version_id: ids.recipeVersion,
      selected_scale_amount: "4",
      consumption_percentage: "50",
      diner_count: 3,
    });
    await record("event_ingredient_price_snapshot", ids.snapshot, {
      event_id: ids.event,
      ingredient_id: ids.ingredient,
      event_ingredient_price_id: ids.price,
      state: "available",
      price_amount: "2500",
      priced_quantity: "1",
      priced_unit_id: ids.kilogram,
      currency: "CZK",
    });
    await record("event_ingredient_price", ids.price, {
      event_id: ids.event,
      ingredient_id: ids.ingredient,
      current_snapshot_id: ids.snapshot,
    });
    await record("shopping_list", ids.shoppingList, {
      event_id: ids.event,
      current_generation_revision_id: ids.revision,
    });
    await record(
      "shopping_generation_revision",
      ids.revision,
      {
        event_id: ids.event,
        shopping_list_id: ids.shoppingList,
        parent_revision_id: null,
      },
      true,
    );
    await record(
      "shopping_contribution_snapshot",
      ids.shoppingSnapshot,
      {
        event_id: ids.event,
        shopping_list_id: ids.shoppingList,
        generation_revision_id: ids.revision,
        shopping_contribution_id: ids.contribution,
        active_in_revision: true,
        ingredient_id: ids.ingredient,
        ingredient_version_id: ids.ingredientVersion,
        generated_quantity: "2",
        event_price_snapshot_id: ids.snapshot,
        price_amount: "3",
        priced_quantity: "2",
        priced_unit_id: ids.unit,
        currency: "CZK",
      },
      true,
    );
    await record("shopping_ingredient_row", ids.row, {
      event_id: ids.event,
      shopping_list_id: ids.shoppingList,
      ingredient_id: ids.ingredient,
      ingredient_name: "Beans",
      available_supply_quantity: "0",
      manual_purchase_target: null,
    });
    await record("shopping_contribution", ids.contribution, {
      event_id: ids.event,
      shopping_list_id: ids.shoppingList,
      shopping_ingredient_row_id: ids.row,
      ingredient_id: ids.ingredient,
    });
    await record("receipt", ids.receipt, {
      event_id: ids.event,
      total_amount: "5",
      currency: "CZK",
    });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({
      total: "15.00",
      budget: "20.00",
      expectedShopping: "3.00",
      actual: "5.00",
      remaining: "15.00",
      missingIngredients: [],
      scheduled: new Map([
        [ids.scheduled, { total: "15.00", perDiner: "5.00", missing: false }],
      ]),
    });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event", ids.event],
      {
        fields: {
          id: "4ce17d2f-8365-4b1f-a80b-34d10425d51c",
          organization_id: organizationId,
          currency: "CZK",
          budget_amount: "20",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toBeUndefined();
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event", ids.event],
      {
        fields: {
          id: ids.event,
          organization_id: organizationId,
          currency: "CZK",
          budget_amount: "20",
        },
      },
    );
    await localDb.canonicalRecords.update(
      [userId, organizationId, "unit_definition", ids.unit],
      {
        fields: {
          id: ids.unit,
          organization_id: organizationId,
          dimension: "evil",
          base_unit_factor: "1",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ total: "0.00", missingIngredients: ["Beans"] });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "unit_definition", ids.unit],
      {
        fields: {
          id: ids.unit,
          organization_id: organizationId,
          dimension: "mass",
          base_unit_factor: "1",
        },
      },
    );
    await localDb.canonicalRecords.update(
      [userId, organizationId, "ingredient", ids.ingredient],
      {
        fields: {
          id: ids.ingredient,
          organization_id: "4ce17d2f-8365-4b1f-a80b-34d10425d51c",
          current_version_id: ids.ingredientVersion,
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({
      total: "0.00",
      missingIngredients: [ids.ingredientVersion],
    });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "ingredient", ids.ingredient],
      {
        fields: {
          id: ids.ingredient,
          organization_id: organizationId,
          current_version_id: ids.ingredientVersion,
        },
      },
    );
    await localDb.canonicalRecords.update(
      [userId, organizationId, "unit_definition", ids.unit],
      {
        fields: {
          id: ids.unit,
          organization_id: "4ce17d2f-8365-4b1f-a80b-34d10425d51c",
          dimension: "mass",
          base_unit_factor: "1",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ total: "0.00", missingIngredients: ["Beans"] });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "unit_definition", ids.unit],
      {
        fields: {
          id: ids.unit,
          organization_id: organizationId,
          dimension: "mass",
          base_unit_factor: "1",
        },
      },
    );
    await localDb.canonicalRecords.update(
      [userId, organizationId, "ingredient_version", ids.ingredientVersion],
      {
        fields: {
          id: ids.ingredientVersion,
          organization_id: organizationId,
          ingredient_id: "4ce17d2f-8365-4b1f-a80b-34d10425d51c",
          canonical_unit_id: ids.unit,
          name: "Beans",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({
      total: "0.00",
      missingIngredients: [ids.ingredientVersion],
    });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "ingredient_version", ids.ingredientVersion],
      {
        fields: {
          id: ids.ingredientVersion,
          organization_id: organizationId,
          ingredient_id: ids.ingredient,
          canonical_unit_id: ids.unit,
          name: "Beans",
        },
      },
    );
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event_ingredient_price_snapshot", ids.snapshot],
      {
        fields: {
          id: ids.snapshot,
          organization_id: organizationId,
          event_id: "4ce17d2f-8365-4b1f-a80b-34d10425d51c",
          ingredient_id: ids.ingredient,
          event_ingredient_price_id: ids.price,
          state: "available",
          price_amount: "2500",
          priced_quantity: "1",
          priced_unit_id: ids.kilogram,
          currency: "CZK",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ total: "0.00", missingIngredients: ["Beans"] });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event_ingredient_price_snapshot", ids.snapshot],
      {
        fields: {
          id: ids.snapshot,
          organization_id: organizationId,
          event_id: ids.event,
          ingredient_id: ids.ingredient,
          event_ingredient_price_id: ids.price,
          state: "available",
          price_amount: "not-a-decimal",
          priced_quantity: "0",
          priced_unit_id: ids.kilogram,
          currency: "CZK",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ total: "0.00", missingIngredients: ["Beans"] });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event_ingredient_price_snapshot", ids.snapshot],
      {
        fields: {
          id: ids.snapshot,
          organization_id: "4ce17d2f-8365-4b1f-a80b-34d10425d51c",
          event_id: ids.event,
          ingredient_id: ids.ingredient,
          event_ingredient_price_id: ids.price,
          state: "available",
          price_amount: "2500",
          priced_quantity: "1",
          priced_unit_id: ids.kilogram,
          currency: "CZK",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ total: "0.00", missingIngredients: ["Beans"] });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event_ingredient_price_snapshot", ids.snapshot],
      {
        fields: {
          id: ids.snapshot,
          organization_id: organizationId,
          event_id: ids.event,
          ingredient_id: ids.ingredient,
          event_ingredient_price_id: ids.price,
          state: "available",
          price_amount: "2500",
          priced_quantity: "1",
          priced_unit_id: ids.kilogram,
          currency: "CZK",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ total: "15.00", missingIngredients: [] });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event_ingredient_price_snapshot", ids.snapshot],
      {
        fields: {
          id: ids.snapshot,
          organization_id: organizationId,
          event_id: ids.event,
          ingredient_id: "4ce17d2f-8365-4b1f-a80b-34d10425d51c",
          event_ingredient_price_id: ids.price,
          state: "available",
          price_amount: "2500",
          priced_quantity: "1",
          priced_unit_id: ids.kilogram,
          currency: "CZK",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ total: "0.00", missingIngredients: ["Beans"] });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event_ingredient_price_snapshot", ids.snapshot],
      {
        fields: {
          id: ids.snapshot,
          organization_id: organizationId,
          event_id: ids.event,
          ingredient_id: ids.ingredient,
          event_ingredient_price_id: ids.price,
          state: "available",
          price_amount: "2500",
          priced_quantity: "1",
          priced_unit_id: ids.kilogram,
          currency: "CZK",
        },
      },
    );
    await localDb.canonicalRecords.update(
      [userId, organizationId, "shopping_ingredient_row", ids.row],
      {
        fields: {
          id: ids.row,
          organization_id: organizationId,
          event_id: ids.event,
          shopping_list_id: ids.shoppingList,
          ingredient_id: ids.ingredient,
          ingredient_name: "Beans",
          available_supply_quantity: "1",
          manual_purchase_target: null,
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "1.50" });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "shopping_ingredient_row", ids.row],
      {
        fields: {
          id: ids.row,
          organization_id: organizationId,
          event_id: ids.event,
          shopping_list_id: ids.shoppingList,
          ingredient_id: ids.ingredient,
          ingredient_name: "Beans",
          available_supply_quantity: "99",
          manual_purchase_target: "3",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "4.50" });
    await record("recipe_ingredient_line", ids.fixedLine, {
      recipe_version_id: ids.recipeVersion,
      line_key: ids.fixedLine,
      ingredient_version_id: ids.ingredientVersion,
      base_quantity: "2",
      scaling_behavior: "fixed",
    });
    await record("scheduled_ingredient_override", ids.replacement, {
      event_id: ids.event,
      scheduled_recipe_id: ids.scheduled,
      override_kind: "replace",
      target_line_key: ids.line,
      ingredient_version_id: ids.ingredientVersion,
      quantity: "0",
    });
    await record("scheduled_ingredient_override", ids.added, {
      event_id: ids.event,
      scheduled_recipe_id: ids.scheduled,
      override_kind: "add",
      ingredient_version_id: ids.ingredientVersion,
      quantity: "1",
    });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({
      total: "7.50",
      scheduled: new Map([
        [ids.scheduled, { total: "7.50", perDiner: "2.50", missing: false }],
      ]),
    });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "shopping_ingredient_row", ids.row],
      {
        fields: {
          id: ids.row,
          organization_id: organizationId,
          event_id: ids.event,
          shopping_list_id: ids.shoppingList,
          ingredient_id: ids.ingredient,
          ingredient_name: "Beans",
          available_supply_quantity: "99",
          manual_purchase_target: "0",
        },
      },
    );
    await localDb.canonicalRecords.update(
      [
        userId,
        organizationId,
        "shopping_contribution_snapshot",
        ids.shoppingSnapshot,
      ],
      {
        fields: {
          id: ids.shoppingSnapshot,
          organization_id: organizationId,
          event_id: ids.event,
          shopping_list_id: ids.shoppingList,
          generation_revision_id: ids.revision,
          shopping_contribution_id: ids.contribution,
          active_in_revision: true,
          ingredient_id: ids.ingredient,
          ingredient_version_id: ids.ingredientVersion,
          generated_quantity: "2",
          event_price_snapshot_id: null,
          price_amount: null,
          priced_quantity: null,
          priced_unit_id: null,
          currency: null,
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({
      expectedShopping: "0.00",
      missingIngredients: [],
    });
    await record("shopping_list", ids.retiredList, {
      event_id: ids.event,
      current_generation_revision_id: ids.retiredRevision,
    });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "shopping_list", ids.retiredList],
      { lifecycle: "retired" },
    );
    await record("shopping_ingredient_row", ids.retiredRow, {
      event_id: ids.event,
      shopping_list_id: ids.retiredList,
      ingredient_id: ids.ingredient,
      ingredient_name: "Retired beans",
      available_supply_quantity: "0",
      manual_purchase_target: null,
    });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "shopping_ingredient_row", ids.retiredRow],
      { lifecycle: "retired" },
    );
    await record("shopping_contribution", ids.retiredContribution, {
      event_id: ids.event,
      shopping_list_id: ids.retiredList,
      shopping_ingredient_row_id: ids.retiredRow,
      ingredient_id: ids.ingredient,
    });
    await localDb.canonicalRecords.update(
      [
        userId,
        organizationId,
        "shopping_contribution",
        ids.retiredContribution,
      ],
      { lifecycle: "retired" },
    );
    await record("shopping_contribution_snapshot", ids.retiredSnapshot, {
      event_id: ids.event,
      shopping_list_id: ids.retiredList,
      generation_revision_id: ids.retiredRevision,
      shopping_contribution_id: ids.retiredContribution,
      active_in_revision: true,
      ingredient_id: ids.ingredient,
      ingredient_version_id: ids.ingredientVersion,
      generated_quantity: "2",
      event_price_snapshot_id: null,
      price_amount: null,
      priced_quantity: null,
      priced_unit_id: null,
      currency: null,
    });
    await localDb.canonicalRecords.update(
      [
        userId,
        organizationId,
        "shopping_contribution_snapshot",
        ids.retiredSnapshot,
      ],
      { lifecycle: "retired" },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ missingIngredients: [] });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event_ingredient_price_snapshot", ids.snapshot],
      {
        fields: {
          id: ids.snapshot,
          organization_id: organizationId,
          event_id: ids.event,
          ingredient_id: ids.ingredient,
          event_ingredient_price_id: ids.price,
          state: "available",
          price_amount: "2500",
          priced_quantity: "1",
          priced_unit_id: ids.kilogram,
          currency: "EUR",
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ total: "0.00", missingIngredients: ["Beans"] });
    await localDb.canonicalRecords.update(
      [userId, organizationId, "event_ingredient_price_snapshot", ids.snapshot],
      {
        fields: {
          id: ids.snapshot,
          organization_id: organizationId,
          event_id: ids.event,
          ingredient_id: ids.ingredient,
          event_ingredient_price_id: ids.price,
          state: "unavailable",
          price_amount: null,
          priced_quantity: null,
          priced_unit_id: null,
          currency: null,
        },
      },
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ total: "0.00", missingIngredients: ["Beans"] });
  });

  it("ignores poisoned current-revision links and keeps only valid snapshots", async () => {
    await seedShoppingCost();
    await updateShoppingSnapshot({
      shopping_list_id: ids.retiredList,
    });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });

    await updateShoppingSnapshot({ shopping_list_id: ids.shoppingList });
    await updateShoppingSnapshot({
      generation_revision_id: ids.retiredRevision,
    });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });

    await updateShoppingSnapshot({ generation_revision_id: ids.revision });
    await updateShoppingSnapshot({ event_id: ids.retiredList });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });

    await updateShoppingSnapshot({ event_id: ids.event });
    await record(
      "shopping_contribution_snapshot",
      ids.secondShoppingSnapshot,
      {
        event_id: ids.event,
        shopping_list_id: ids.shoppingList,
        generation_revision_id: ids.revision,
        shopping_contribution_id: ids.contribution,
        ingredient_id: ids.ingredient,
        active_in_revision: true,
        ingredient_version_id: ids.ingredientVersion,
        generated_quantity: "1",
        event_price_snapshot_id: ids.snapshot,
        price_amount: "3",
        priced_quantity: "2",
        priced_unit_id: ids.unit,
        currency: "EUR",
      },
      true,
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "3.00" });

    await updateShoppingSnapshot({ ingredient_id: ids.retiredList });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });
    await updateShoppingSnapshot({ ingredient_id: ids.ingredient });
    await updateShoppingSnapshot({
      ingredient_version_id: ids.retiredRevision,
    });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });
    await updateShoppingSnapshot({
      ingredient_version_id: ids.ingredientVersion,
    });
    await updateShoppingSnapshot({ price_amount: "-1" });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });
    await updateShoppingSnapshot({ price_amount: "3" });
    await updateShoppingSnapshot({ priced_unit_id: ids.retiredList });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });
  });

  it("prices a normal row with multiple valid contribution snapshots", async () => {
    await seedShoppingCost();
    await record("shopping_contribution", ids.secondContribution, {
      event_id: ids.event,
      shopping_list_id: ids.shoppingList,
      shopping_ingredient_row_id: ids.row,
      ingredient_id: ids.ingredient,
    });
    await record(
      "shopping_contribution_snapshot",
      ids.secondShoppingSnapshot,
      {
        event_id: ids.event,
        shopping_list_id: ids.shoppingList,
        generation_revision_id: ids.revision,
        shopping_contribution_id: ids.secondContribution,
        ingredient_id: ids.ingredient,
        active_in_revision: true,
        ingredient_version_id: ids.ingredientVersion,
        generated_quantity: "1",
        event_price_snapshot_id: ids.snapshot,
        price_amount: "3",
        priced_quantity: "2",
        priced_unit_id: ids.unit,
        currency: "CZK",
      },
      true,
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "4.50" });
  });

  it("omits a contribution with duplicate valid snapshots", async () => {
    await seedShoppingCost();
    await record(
      "shopping_contribution_snapshot",
      ids.duplicateShoppingSnapshot,
      {
        event_id: ids.event,
        shopping_list_id: ids.shoppingList,
        generation_revision_id: ids.revision,
        shopping_contribution_id: ids.contribution,
        ingredient_id: ids.ingredient,
        active_in_revision: true,
        ingredient_version_id: ids.ingredientVersion,
        generated_quantity: "2",
        event_price_snapshot_id: ids.snapshot,
        price_amount: "3",
        priced_quantity: "2",
        priced_unit_id: ids.unit,
        currency: "CZK",
      },
      true,
    );
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });
  });

  it("rejects a malformed non-null captured price snapshot id", async () => {
    await seedShoppingCost();
    await updateShoppingSnapshot({
      event_price_snapshot_id: 42,
      price_amount: null,
      priced_quantity: null,
      priced_unit_id: null,
      currency: null,
    });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });
  });

  it("rejects a captured price with a foreign event-price parent", async () => {
    await seedShoppingCost();
    await record("ingredient", ids.foreignIngredient, {});
    await record("event_ingredient_price", ids.foreignPrice, {
      event_id: ids.event,
      ingredient_id: ids.foreignIngredient,
      current_snapshot_id: ids.foreignSnapshot,
    });
    await record("event_ingredient_price_snapshot", ids.foreignSnapshot, {
      event_id: ids.event,
      ingredient_id: ids.foreignIngredient,
      event_ingredient_price_id: ids.foreignPrice,
      state: "available",
      price_amount: "3",
      priced_quantity: "2",
      priced_unit_id: ids.unit,
      currency: "CZK",
    });
    await updateShoppingSnapshot({
      event_price_snapshot_id: ids.foreignSnapshot,
    });
    await expect(
      readEventCosts(userId, organizationId, ids.event),
    ).resolves.toMatchObject({ expectedShopping: "0.00" });
  });
});
