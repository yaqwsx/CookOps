import { expect, test } from "@playwright/test";

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
