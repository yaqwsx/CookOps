import { expect, test } from "@playwright/test";

const alice = {
  id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  display_name: "Alice Member",
  verified_email: "alice@example.test",
};
const organizations = [
  { id: "5ce17d2f-8365-4b1f-a80b-34d10425d51c", name: "First kitchen" },
  { id: "b6a58bd6-214e-49af-8fae-e5f974bf8e08", name: "Second kitchen" },
];
const [firstOrganization, secondOrganization] = organizations;
if (!firstOrganization || !secondOrganization)
  throw new Error("Organization fixtures are required.");

test("opens and switches the authenticated organization event overview", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  });
  let signedIn = false;
  await page.route("**/auth/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const pathname = new URL(request.url()).pathname;
    if (pathname === "/auth/session" && method === "GET") {
      await route.fulfill({
        status: signedIn ? 200 : 401,
        contentType: "application/json",
        body: JSON.stringify(
          signedIn ? alice : { detail: "not authenticated" },
        ),
      });
      return;
    }
    if (pathname === "/auth/dummy/identities") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          identities: [
            { subject: "dummy-alice", display_name: "Alice Member" },
          ],
        }),
      });
      return;
    }
    if (pathname === "/auth/dummy/session" && method === "POST") {
      signedIn = true;
      await route.fulfill({ status: 204 });
      return;
    }
    await route.fulfill({ status: 404 });
  });
  await page.route("**/api/v1/organizations", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ organizations }),
    });
  });
  await page.route("**/api/v1/sync/bootstrap", async (route) => {
    const organizationId = route.request().postDataJSON().organization_id;
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
                name: organizations.find(({ id }) => id === organizationId)
                  ?.name,
                default_currency: "CZK",
                retired_at: null,
              },
            },
          },
        ],
      }),
    });
  });

  await page.goto("/");
  await page
    .getByRole("button", { name: "Přihlásit se jako Alice Member" })
    .click();

  const picker = page.getByRole("combobox", { name: "Organizace" });
  await expect(picker).toHaveValue(firstOrganization.id);
  await expect(page).toHaveURL(`/organizations/${firstOrganization.id}/events`);
  await expect(
    page.getByText("V této organizaci zatím nejsou žádné akce."),
  ).toBeVisible();

  await picker.selectOption(secondOrganization.id);
  await expect(page).toHaveURL(
    `/organizations/${secondOrganization.id}/events`,
  );
  await expect(picker).toHaveValue(secondOrganization.id);
  await expect(
    page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
    ),
  ).resolves.toBe(true);
});
