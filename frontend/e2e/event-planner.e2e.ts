import { expect, test } from "@playwright/test";

const ids = {
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  event: "3d8b2b21-c378-4574-9e46-9338c81305ef",
  day: "4d8b2b21-c378-4574-9e46-9338c81305ef",
  role: "5d8b2b21-c378-4574-9e46-9338c81305ef",
  recipe: "6d8b2b21-c378-4574-9e46-9338c81305ef",
  version: "7d8b2b21-c378-4574-9e46-9338c81305ef",
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
            current_version_id: ids.version,
            retired_at: null,
          }),
          record(ids.version, "recipe_version", {
            id: ids.version,
            recipe_id: ids.recipe,
            name: "Chili",
            immutable: true,
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
  await expect(page.getByText("Připravit zeleninu")).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.getByRole("button", { name: "Přidat do plánu" }).click();
  await expect(page.getByText("Chili · Strávníci: 12")).toBeVisible();
  await expect(
    page.getByText("Recept je uložen místně a bude synchronizován."),
  ).toBeVisible();
  await page.getByText("Přesunout", { exact: true }).click();
  await page.getByLabel("Pořadí").fill("z9");
  await page.getByRole("button", { name: "Přesunout sem" }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
