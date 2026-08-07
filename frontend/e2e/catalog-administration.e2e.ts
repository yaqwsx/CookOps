import { expect, test } from "@playwright/test";

const ids = {
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  section: "6ce17d2f-8365-4b1f-a80b-34d10425d51c",
};

test("edits a store-section order from the cached catalog offline", async ({ page }) => {
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
        organizations: [{ id: ids.organization, name: "Test kitchen" }],
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
          {
            organization_id: ids.organization,
            entity_id: ids.section,
            entity_kind: "store_section",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: ids.section,
                organization_id: ids.organization,
                name: "Pantry",
                normalized_name: "pantry",
                position_key: "a",
                retired_at: null,
                field_clocks: {},
              },
            },
          },
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

  await page.goto(`/organizations/${ids.organization}/settings`);
  await expect(page.getByRole("heading", { name: "Správa katalogu" })).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.getByText("Upravit", { exact: true }).click();
  await page.getByLabel("Pořadí").fill("z");
  await page.getByRole("button", { name: "Uložit" }).click();
  await expect(page.getByLabel("Pořadí")).toHaveValue("z");
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      ),
    )
    .toBe(true);
});
