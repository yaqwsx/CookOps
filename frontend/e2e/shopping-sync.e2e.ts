import { expect, test, type BrowserContext, type Page } from "@playwright/test";

const organizationId = "dd6ababf-24ef-5644-b075-c893d461f965";
const eventId = "80d049db-4052-5a3e-bf41-202b94cdf550";

async function signIn(context: BrowserContext, subject: string) {
  const page = await context.newPage();
  await page.addInitScript(() => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  });
  await page.goto("/auth/dummy/identities");
  expect(
    await page.evaluate(
      async (selectedSubject) =>
        (
          await fetch("/auth/dummy/session", {
            body: JSON.stringify({ subject: selectedSubject }),
            headers: { "content-type": "application/json" },
            method: "POST",
          })
        ).status,
      subject,
    ),
  ).toBe(204);
  return page;
}

async function openShopping(page: Page) {
  await page.goto(
    `/organizations/${organizationId}/events/${eventId}/shopping`,
  );
  await expect(page.getByRole("heading", { name: "Nákupy" })).toBeVisible();
  await page.getByRole("button", { name: "Otevřít seznam" }).click();
  await expect(
    page.getByRole("heading", { name: "Development shopping", exact: true }),
  ).toBeVisible();
}

test("two members receive a real shopping fulfilment change through sync hints", async ({
  browser,
}) => {
  const writerContext = await browser.newContext({
    viewport: { width: 1280, height: 800 },
  });
  const readerContext = await browser.newContext({
    viewport: { width: 360, height: 800 },
  });
  try {
    const writer = await signIn(writerContext, "dummy-organization-admin");
    const reader = await signIn(readerContext, "dummy-member");
    const hintSocket = reader.waitForEvent("websocket", {
      predicate: (socket) => socket.url().endsWith("/api/v1/sync/hints"),
    });
    await Promise.all([openShopping(writer), openShopping(reader)]);
    await hintSocket;

    const readerCheckbox = reader.getByRole("checkbox", { name: "Nakoupeno" });
    await expect(readerCheckbox).toBeVisible();
    await expect(readerCheckbox).not.toBeChecked();
    await writer.getByRole("checkbox", { name: "Nakoupeno" }).click();
    await expect(readerCheckbox).toBeChecked({ timeout: 15_000 });

    await reader.goto(
      `/organizations/7f64e8c8-e4bd-51b6-b910-38ceaf605b42/events/${eventId}/shopping`,
    );
    await expect(reader.getByText("Nákupy")).not.toBeVisible();
  } finally {
    await Promise.all([writerContext.close(), readerContext.close()]);
  }
});
