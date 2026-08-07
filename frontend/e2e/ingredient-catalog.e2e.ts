import { expect, test } from "@playwright/test";

const ids = {
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  unit: "6ce17d2f-8365-4b1f-a80b-34d10425d51c",
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

test("creates and immediately reads an ingredient from the cached catalog offline", async ({
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
          record(ids.organization, "organization", {
            id: ids.organization,
            default_currency: "CZK",
            retired_at: null,
          }),
          record(ids.unit, "unit_definition", {
            id: ids.unit,
            organization_id: null,
            code: "g",
            dimension: "mass",
            base_unit_factor: "1",
            allows_ingredient_quantity: true,
            retired_at: null,
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

  await page.goto(`/organizations/${ids.organization}/ingredients`);
  await expect(
    page.getByRole("heading", { name: "Nová surovina" }),
  ).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.getByLabel("Název").fill("Rajčata");
  await page.getByRole("button", { name: "Uložit surovinu" }).click();
  await expect(page.getByRole("heading", { name: "Rajčata" })).toBeVisible();
  await expect(
    page.getByText("Surovina je uložena místně a bude synchronizována."),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
