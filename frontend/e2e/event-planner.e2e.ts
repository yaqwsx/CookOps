import { expect, test } from "@playwright/test";

const ids = {
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
  role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
  recipe: "6d8b2b21-c378-4574-9e46-9338c81305ef",
  version: "7d8b2b21-c378-4574-9e46-9338c81305ef",
  line: "8d8b2b21-c378-4574-9e46-9338c81305ef",
  ingredient: "9d8b2b21-c378-4574-9e46-9338c81305ef",
  ingredientVersion: "ad8b2b21-c378-4574-9e46-9338c81305ef",
  unit: "bd8b2b21-c378-4574-9e46-9338c81305ef",
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

test("opens the cached planner and schedules a recipe offline", async ({
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
            note: "Připravit zeleninu",
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
            description: "# Pinned chili",
            scaling_unit_id: ids.unit,
            base_scaling_amount: "1",
          }),
          record(ids.unit, "unit_definition", { id: ids.unit, organization_id: null, code: "ks", custom_name: "ks", allows_ingredient_quantity: true, allows_recipe_scaling: true, dimension: "count", base_unit_factor: "1" }),
          record(ids.ingredient, "ingredient", { id: ids.ingredient, organization_id: ids.organization, current_version_id: ids.ingredientVersion, retired_at: null, immutable: true }),
          record(ids.ingredientVersion, "ingredient_version", { id: ids.ingredientVersion, organization_id: ids.organization, ingredient_id: ids.ingredient, based_on_version_id: null, name: "Paprika", normalized_name: "paprika", canonical_unit_id: ids.unit, mass_per_canonical_quantity: "1", immutable: true }),
          record(ids.line, "recipe_ingredient_line", {
            id: ids.line,
            line_key: ids.line,
            organization_id: ids.organization,
            recipe_version_id: ids.version,
            base_quantity: "1",
            ingredient_version_id: ids.ingredientVersion,
            preferred_display_unit_id: ids.unit,
            scaling_behavior: "proportional",
            include_in_portion_weight: true,
          }),
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

  await page.goto(`/organizations/${ids.organization}/events`);
  await page.getByRole("button", { name: "Otevřít plán" }).click();
  await expect(page).toHaveURL(
    `/organizations/${ids.organization}/events/${ids.event}/planner`,
  );
  await expect(page.getByRole("heading", { name: "Plán akce" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Odhad nákladů" }),
  ).toBeVisible();
  await expect(page.getByRole("paragraph").filter({ hasText: "Připravit zeleninu" })).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.getByRole("button", { name: "Aktualizovat odhady cen" }).click();
  await expect(
    page.getByRole("button", {
      name: "Aktualizace odhadů čeká na synchronizaci",
    }),
  ).toBeDisabled();
  if (await page.getByRole("button", { name: "Přidat do plánu" }).count() === 0)
    await page.getByRole("button", { name: "Otevřít katalog receptů" }).click();
  await page.getByRole("button", { name: "Přidat do plánu" }).click();
  await expect(page.getByText("Recept je uložen místně a bude synchronizován.")).toBeVisible();
  if (await page.getByRole("dialog").count()) {
    await page.getByRole("button", { name: "Zavřít katalog receptů" }).click();
  }
  const scheduled = page.getByRole("listitem").filter({ hasText: "Chili · Strávníci: 12" });
  await expect(scheduled).toBeVisible();
  await scheduled.getByText("Upravit škálování", { exact: true }).click();
  await scheduled.getByLabel("Měřítko").fill("1");
  await scheduled.getByRole("button", { name: "Uložit měřítko" }).click();
  await scheduled.getByText("Upravit škálování", { exact: true }).click();
  await expect(scheduled.getByLabel("Měřítko")).toHaveValue("1");
  await scheduled.getByText("Podrobnosti receptu", { exact: true }).click();
  await expect(page.getByText("Paprika: 1 ks")).toBeVisible();
  const moveDetails = page.locator("details").filter({ hasText: "Přesunout" });
  await moveDetails.locator("summary").click();
  await moveDetails.locator("select").nth(2).selectOption("end");
  await moveDetails.getByRole("button", { name: "Přesunout sem" }).click();
  await page.getByText("Změnit množství suroviny", { exact: true }).click();
  await page.getByLabel("Množství").fill("2");
  await page.getByRole("button", { name: "Uložit změnu" }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
