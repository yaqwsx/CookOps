import { expect, test } from "@playwright/test";

const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "3d8b2b21-c378-4574-9e46-9338c81305ef";

test("keeps a normalized receipt photo queued offline", async ({ page }) => {
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
  await page.route("**/api/v1/organizations", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        organizations: [
          { id: organizationId, name: "CookOps test organization" },
        ],
      }),
    }),
  );
  await page.route("**/api/v1/sync/bootstrap", (route) =>
    route.fulfill({
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
                default_currency: "CZK",
                retired_at: null,
              },
            },
          },
          {
            organization_id: organizationId,
            entity_id: eventId,
            entity_kind: "event",
            operation: "upsert",
            payload: {
              record_schema_version: 1,
              record: {
                id: eventId,
                organization_id: organizationId,
                name: "Letní vaření",
                start_date: "2026-08-10",
                end_date: "2026-08-10",
                base_expected_attendance: 12,
                budget_amount: "0",
                currency: "CZK",
                lifecycle: "active",
                archived_at: null,
              },
            },
          },
        ],
      }),
    }),
  );
  await page.route("**/api/v1/sync/pull", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        sync_schema_version: 1,
        server_time: "2026-08-07T12:00:00.000Z",
        status: "ok",
        next_cursor: "opaque-cursor",
        transaction_groups: [],
      }),
    }),
  );

  await page.goto(`/organizations/${organizationId}/events`);
  await page.getByRole("button", { name: "Otevřít plán" }).click();
  await page.getByRole("button", { name: "Účtenky" }).click();
  await expect(page.getByRole("heading", { name: "Účtenky" })).toBeVisible();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await page.getByLabel("Obchod nebo stručný název").fill("Pekárna");
  await page.getByLabel("Celková částka").fill("12.50");
  await page.getByRole("button", { name: "Uložit účtenku" }).click();
  await expect(page.getByRole("heading", { name: "Pekárna" })).toBeVisible();
  await expect(page.getByText("12.50 CZK")).toBeVisible();
  const picker = page.getByLabel("Přidat fotografii účtenky");
  await expect(picker).toBeVisible();
  await picker.setInputFiles({
    name: "receipt.png",
    mimeType: "image/png",
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL3hwAAAABJRU5ErkJggg==",
      "base64",
    ),
  });
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const database = await new Promise<IDBDatabase>((resolve, reject) => {
          const request = indexedDB.open("cookops");
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        const count = await new Promise<number>((resolve, reject) => {
          const request = database
            .transaction("pendingUploads")
            .objectStore("pendingUploads")
            .count();
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        database.close();
        return count;
      }),
    )
    .toBe(1);
  const scoped = await page.evaluate(async () => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("cookops");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const row = await new Promise<{ receiptId?: string }>((resolve, reject) => {
      const request = database
        .transaction("pendingUploads")
        .objectStore("pendingUploads")
        .getAll();
      request.onsuccess = () => resolve(request.result[0]);
      request.onerror = () => reject(request.error);
    });
    database.close();
    return row.receiptId;
  });
  expect(scoped).toBe(
    await page.locator(".receipt-item").getAttribute("data-receipt-id"),
  );
  await expect(
    page.getByRole("button", { name: "Odstranit fotografii" }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
