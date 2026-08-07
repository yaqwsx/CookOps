import { expect, test } from "@playwright/test";

const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";

test("registers the production service worker and keeps the synchronized status visible", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  });
  await page.route("**/auth/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/auth/session" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
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

  await page.goto("/");
  await expect(page.getByTestId("synchronization-status")).toHaveText(
    "Synchronizováno",
  );
  await page.evaluate(() => navigator.serviceWorker.ready.then(() => true));
  await page.reload();
  await expect
    .poll(() =>
      page.evaluate(() => navigator.serviceWorker.controller !== null),
    )
    .toBe(true);
});

test("shows a cached event projection without mobile horizontal overflow", async ({
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
            entity_id: "3d8b2b21-c378-4574-9e46-9338c81305ef",
            entity_kind: "event",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: "3d8b2b21-c378-4574-9e46-9338c81305ef",
                organization_id: organizationId,
                name: "Letní vaření",
                start_date: "2026-08-10",
                end_date: "2026-08-12",
                base_expected_attendance: 24,
                budget_amount: "1200.50",
                currency: "CZK",
                lifecycle: "active",
                archived_at: null,
              },
            },
          },
        ],
      }),
    });
  });

  await page.goto(`/organizations/${organizationId}/events`);
  await expect(
    page.getByRole("heading", { name: "Letní vaření" }),
  ).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await expect(
    page.getByText("Zobrazujeme uložené akce bez připojení."),
  ).toHaveText("Zobrazujeme uložené akce bez připojení.");
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
