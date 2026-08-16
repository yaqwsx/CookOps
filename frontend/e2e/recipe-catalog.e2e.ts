import { expect, test } from "@playwright/test";

const ids = {
  organization: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  user: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  unit: "6ce17d2f-8365-4b1f-a80b-34d10425d51c",
  recipe: "7ce17d2f-8365-4b1f-a80b-34d10425d51c",
  version: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
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

test("creates and immediately reads a recipe from the cached catalog offline", async ({
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
            code: "person",
            allows_recipe_scaling: true,
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

  await page.goto(`/organizations/${ids.organization}/recipes`);
  await expect(
    page.getByRole("heading", { name: "Nový recept" }),
  ).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.getByLabel("Název").fill("Čočková polévka");
  await page.getByLabel("Popis").fill("Přinést chléb.");
  await page.getByLabel("Základní množství škálování").fill("10.5");
  await page.getByRole("button", { name: "Uložit recept" }).click();
  await expect(
    page.getByRole("heading", { name: "Čočková polévka" }),
  ).toBeVisible();
  await expect(
    page.getByText("Recept je uložen místně a bude synchronizován."),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});

test("edits recipe Markdown through the visual editor without executing unsafe source", async ({ page }) => {
  await page.addInitScript(() => { window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } }; });
  await page.route("**/auth/session", async (route) => await route.fulfill({ contentType: "application/json", body: JSON.stringify({ id: ids.user, display_name: "Alice Member", verified_email: "alice@example.test" }) }));
  await page.route("**/api/v1/organizations", async (route) => await route.fulfill({ contentType: "application/json", body: JSON.stringify({ organizations: [{ id: ids.organization, name: "Test kitchen" }] }) }));
  await page.route("**/api/v1/sync/bootstrap", async (route) => await route.fulfill({ contentType: "application/json", body: JSON.stringify({ sync_schema_version: 1, server_time: "2026-08-07T12:00:00.000Z", cursor: "opaque-cursor", records: [
    record(ids.organization, "organization", { id: ids.organization, default_currency: "CZK", retired_at: null }),
    record(ids.unit, "unit_definition", { id: ids.unit, organization_id: null, code: "person", allows_recipe_scaling: true, retired_at: null }),
    record(ids.recipe, "recipe", { id: ids.recipe, organization_id: ids.organization, current_version_id: ids.version, retired_at: null }),
    record(ids.version, "recipe_version", { id: ids.version, organization_id: ids.organization, recipe_id: ids.recipe, name: "Soup", description: "Original", scaling_unit_id: ids.unit, base_scaling_amount: "1" }),
  ] }) }));
  await page.route("**/api/v1/sync/pull", async (route) => await route.fulfill({ contentType: "application/json", body: JSON.stringify({ sync_schema_version: 1, server_time: "2026-08-07T12:00:00.000Z", status: "ok", next_cursor: "opaque-cursor", transaction_groups: [] }) }));
  await page.goto(`/organizations/${ids.organization}/recipes/${ids.recipe}/edit`);
  const visual = page.locator("[contenteditable=true]");
  await expect(visual).toBeVisible();
  await visual.focus();
  await visual.press("ControlOrMeta+A");
  await visual.pressSequentially("Visual **edit**");
  await expect(visual).toContainText("Visual");
  await page.getByRole("tab").nth(1).click();
  const raw = page.locator(`#recipe-description-${ids.recipe}-markdown textarea`);
  await expect(raw).toHaveValue("Visual **edit**\n");
  await raw.fill("[unsafe](javascript:alert(1))\n<script>alert(1)</script>");
  await page.getByRole("tab").nth(0).click();
  await expect(page.locator(".milkdown script")).toHaveCount(0);
  await expect(page.locator('.milkdown a[href^="javascript:"]')).toHaveCount(0);
  await page.getByRole("button", { name: "Publikovat verzi" }).click();
  const payload = await page.evaluate(() => new Promise((resolve, reject) => {
    const request = indexedDB.open("cookops");
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const getAll = request.result.transaction("outbox").objectStore("outbox").getAll();
      getAll.onsuccess = () => resolve(getAll.result.at(-1)?.payload);
      getAll.onerror = () => reject(getAll.error);
    };
  }));
  expect(payload).toMatchObject({ description: "[unsafe](javascript:alert(1))\n<script>alert(1)</script>" });
});
