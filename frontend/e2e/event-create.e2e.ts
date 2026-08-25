import { expect, test } from "@playwright/test";

const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";

test("creates an event in the offline outbox without horizontal overflow", async ({
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
          id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
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
          { id: organizationId, name: "CookOps test organization" },
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
          {
            organization_id: organizationId,
            entity_id: organizationId,
            entity_kind: "organization",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: organizationId,
                name: "CookOps test organization",
                default_currency: "CZK",
                retired_at: null,
              },
            },
          },
          {
            organization_id: organizationId,
            entity_id: organizationId,
            entity_kind: "organization_capabilities",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                actor_user_id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
                can_manage_organization: true,
                organization_id: organizationId,
                role: "organization_admin",
              },
            },
          },
        ],
      }),
    });
  });

  await page.goto(`/organizations/${organizationId}/events`);
  await expect(page.getByRole("heading", { name: "Nová akce" })).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.getByLabel("Název").fill("Offline picnic");
  await page.getByLabel("Začátek").fill("2026-08-10");
  await page.getByLabel("Konec").fill("2026-08-10");
  await page.getByLabel("Očekávaná účast").fill("8");
  await page.getByLabel("Rozpočet").fill("50.25");
  await page.getByRole("button", { name: "Uložit akci" }).click();

  await expect(
    page.getByRole("heading", { name: "Offline picnic" }),
  ).toBeVisible();
  await expect(
    page.getByText("Akce je uložena místně a bude synchronizována."),
  ).toBeVisible();
  await expect(
    page
      .getByTestId("synchronization-status")
      .getByText("Bez připojení", { exact: true }),
  ).toHaveText("Bez připojení");
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
