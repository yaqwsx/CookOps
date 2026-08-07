import { expect, test } from "@playwright/test";

const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const eventId = "3d8b2b21-c378-4574-9e46-9338c81305ef";
const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";

test("an administrator explicitly confirms a guarded online event archive", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  });
  await page.route("**/auth/**", async (route) => {
    if (new URL(route.request().url()).pathname === "/auth/session")
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
          display_name: "Alice Admin",
          verified_email: "alice@example.test",
        }),
      });
    await route.fulfill({ status: 404 });
  });
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
              record: { id: organizationId, default_currency: "CZK" },
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
                id: organizationId,
                actor_user_id: userId,
                can_manage_organization: true,
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
                base_expected_attendance: 24,
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
  await page.goto(`/organizations/${organizationId}/events`);
  const card = page.getByRole("article");
  await card.getByRole("button", { name: "Archivovat akci" }).click();
  await expect(
    card.getByText(
      "Archivace vytvoří neměnný historický záznam. Aktivní plán už nepůjde upravovat.",
    ),
  ).toBeVisible();
  await card.getByRole("button", { name: "Potvrdit archivaci" }).click();
  await expect(card.getByText("Aktivní", { exact: true })).toBeVisible();
  await expect(card.getByLabel("Očekávaná účast")).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
