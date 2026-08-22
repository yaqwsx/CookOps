import { expect, test } from "@playwright/test";

const ids = {
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
  role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
  recipe: "6d8b2b21-c378-4574-9e46-9338c81305ef",
  version: "7d8b2b21-c378-4574-9e46-9338c81305ef",
  scheduled: "8d8b2b21-c378-4574-9e46-9338c81305ef",
  list: "9d8b2b21-c378-4574-9e46-9338c81305ef",
  revision: "0e8b2b21-c378-4574-9e46-9338c81305ef",
  row: "1e8b2b21-c378-4574-9e46-9338c81305ef",
  contribution: "2e8b2b21-c378-4574-9e46-9338c81305ef",
  snapshot: "3e8b2b21-c378-4574-9e46-9338c81305ef",
  unit: "4e8b2b21-c378-4574-9e46-9338c81305ef",
  ingredient: "5e8b2b21-c378-4574-9e46-9338c81305ef",
  ingredientVersion: "6e8b2b21-c378-4574-9e46-9338c81305ef",
};

function record(entity_id: string, entity_kind: string, value: object) {
  return {
    organization_id: ids.organization,
    entity_id,
    entity_kind,
    operation: "upsert",
    payload: { record_schema_version: 1, record: value },
  };
}

test("creates a cached shopping list from selected plan sources while offline", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  });
  await page.route("**/auth/**", async (route) => {
    if (new URL(route.request().url()).pathname === "/auth/session") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          id: ids.user,
          display_name: "Alice Member",
          verified_email: "alice@example.test",
        }),
      });
      return;
    }
    await route.fulfill({ status: 404 });
  });
  await page.route("**/api/v1/organizations", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        organizations: [
          { id: ids.organization, name: "CookOps test organization" },
        ],
      }),
    });
  });
  await page.route("**/api/v1/sync/bootstrap", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        sync_schema_version: 1,
        server_time: "2026-08-07T12:00:00.000Z",
        cursor: "opaque-cursor",
        records: [
          record(ids.organization, "organization", {
            id: ids.organization,
            default_currency: "CZK",
            retired_at: null,
          }),
          record(ids.event, "event", {
            id: ids.event,
            organization_id: ids.organization,
            name: "Letní vaření",
            start_date: "2026-08-10",
            end_date: "2026-08-10",
            base_expected_attendance: 12,
            budget_amount: "0",
            currency: "CZK",
            lifecycle: "active",
            archived_at: null,
          }),
          record(ids.day, "event_day", {
            id: ids.day,
            event_id: ids.event,
            calendar_date: "2026-08-10",
            note: null,
            retired_at: null,
          }),
          record(ids.role, "event_meal_role", {
            id: ids.role,
            event_id: ids.event,
            custom_name: "Večeře",
            position_key: "a",
            retired_at: null,
          }),
          record(ids.recipe, "recipe", {
            id: ids.recipe,
            organization_id: ids.organization,
            current_version_id: ids.version,
            retired_at: null,
          }),
          record(ids.version, "recipe_version", {
            id: ids.version,
            organization_id: ids.organization,
            recipe_id: ids.recipe,
            name: "Chili",
            immutable: true,
            scaling_unit_id: ids.unit,
            base_scaling_amount: "1",
          }),
          record(ids.scheduled, "scheduled_recipe", {
            id: ids.scheduled,
            organization_id: ids.organization,
            event_id: ids.event,
            event_day_id: ids.day,
            event_meal_role_id: ids.role,
            recipe_id: ids.recipe,
            recipe_version_id: ids.version,
            diner_count: 12,
            consumption_percentage: "100",
            selected_scale_amount: "1",
            scale_mode: "suggested",
            position_key: "a",
            retired_at: null,
          }),
          record(ids.list, "shopping_list", {
            id: ids.list,
            organization_id: ids.organization,
            event_id: ids.event,
            name: "Pátek",
            current_generation_revision_id: ids.revision,
            created_at: "2026-08-07T12:00:00.000Z",
            retired_at: null,
          }),
          record(ids.row, "shopping_ingredient_row", {
            id: ids.row,
            organization_id: ids.organization,
            event_id: ids.event,
            shopping_list_id: ids.list,
            ingredient_id: ids.ingredient,
            ingredient_name: "Rajčata",
            calculation_unit_id: ids.unit,
            available_supply_quantity: "0",
            manual_purchase_target: null,
            aggregate_fulfilment_credit: "0",
            default_store_section_name: "Zelenina",
            retired_at: null,
          }),
          record(ids.contribution, "shopping_contribution", {
            id: ids.contribution,
            organization_id: ids.organization,
            event_id: ids.event,
            shopping_list_id: ids.list,
            shopping_ingredient_row_id: ids.row,
            ingredient_id: ids.ingredient,
            fulfilment_credit: "0",
            retired_at: null,
          }),
          record(ids.snapshot, "shopping_contribution_snapshot", {
            id: ids.snapshot,
            organization_id: ids.organization,
            event_id: ids.event,
            shopping_list_id: ids.list,
            generation_revision_id: ids.revision,
            shopping_contribution_id: ids.contribution,
            ingredient_id: ids.ingredient,
            ingredient_version_id: ids.ingredientVersion,
            active_in_revision: true,
            generated_quantity: "2",
            source_details: { recipe_name: "Chili" },
          }),
          record(ids.ingredientVersion, "ingredient_version", { id: ids.ingredientVersion, organization_id: ids.organization, ingredient_id: ids.ingredient, name: "Rajčata", immutable: true, canonical_unit_id: ids.unit }),
          record(ids.unit, "unit_definition", { id: ids.unit, organization_id: null, code: "kg", allows_recipe_scaling: true, allows_ingredient_quantity: true, dimension: "mass", base_unit_factor: "1" }),
        ],
      }),
    });
  });
  await page.route("**/api/v1/sync/pull", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        sync_schema_version: 1,
        server_time: "2026-08-07T12:00:00.000Z",
        status: "ok",
        next_cursor: "opaque-cursor",
        transaction_groups: [],
      }),
    });
  });

  await page.goto(
    `/organizations/${ids.organization}/events/${ids.event}/shopping`,
  );
  await expect(page.getByRole("heading", { name: "Nákupy" })).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.getByLabel("Název").fill("Sobota");
  await page.getByLabel("Chili · Strávníci: 12").check();
  await page.getByRole("button", { name: "Vytvořit nákupní seznam" }).click();
  await expect(page.getByText("Sobota", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Nákupní seznam je uložen místně a bude synchronizován."),
  ).toBeVisible();
  await page
    .getByRole("listitem")
    .filter({ hasText: "Pátek" })
    .getByRole("button", { name: "Otevřít seznam" })
    .click();
  await expect(page.getByRole("heading", { name: "Pátek" })).toBeVisible();
  await page.getByLabel("Nakoupeno").click();
  await expect(page.getByText("0 kg", { exact: true })).toBeVisible();
  await page.getByText("Příspěvky receptů").click();
  await page.getByLabel("Chili · 2 kg").click();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
