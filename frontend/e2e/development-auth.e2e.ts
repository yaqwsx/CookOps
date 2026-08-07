import { expect, type Locator, type Page, test } from "@playwright/test";

const alice = {
  id: "a6a58bd6-214e-49af-8fae-e5f974bf8e08",
  display_name: "Alice Member",
  verified_email: "alice@example.test",
};

async function installDevelopmentAuthFixture(page: Page) {
  await page.addInitScript(() => {
    window.COOKOPS_RUNTIME_CONFIG = { authentication: { provider: "dummy" } };
  });
  let signedIn = false;
  await page.route("**/auth/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/auth/session" && request.method() === "GET") {
      await route.fulfill({
        status: signedIn ? 200 : 401,
        contentType: "application/json",
        body: JSON.stringify(
          signedIn ? alice : { detail: "not authenticated" },
        ),
      });
      return;
    }
    if (
      url.pathname === "/auth/dummy/identities" &&
      request.method() === "GET"
    ) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          identities: [
            { subject: "dummy-alice", display_name: "Alice Member" },
            { subject: "dummy-zoe", display_name: "Zoe No Access" },
          ],
        }),
      });
      return;
    }
    if (url.pathname === "/auth/dummy/session" && request.method() === "POST") {
      signedIn = true;
      await route.fulfill({ status: 204 });
      return;
    }
    if (
      url.pathname === "/auth/session/logout" &&
      request.method() === "POST"
    ) {
      signedIn = false;
      await route.fulfill({ status: 204 });
      return;
    }
    await route.fulfill({ status: 404 });
  });
}

async function installGoogleAuthFixture(page: Page, failFirstToken = false) {
  await page.addInitScript(() => {
    let callback: ((response: { credential: string }) => void) | undefined;
    window.COOKOPS_RUNTIME_CONFIG = {
      authentication: {
        provider: "google",
        googleClientId: "cookops-test.apps.googleusercontent.com",
      },
    };
    window.google = {
      accounts: {
        id: {
          initialize: (configuration) => {
            callback = configuration.callback;
          },
          renderButton: (element) => {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = "Continue with Google";
            button.addEventListener("click", () =>
              callback?.({ credential: "google-id-token" }),
            );
            element.append(button);
          },
        },
      },
    };
  });
  let signedIn = false;
  let tokenAttempts = 0;
  await page.route("**/auth/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/auth/session" && request.method() === "GET") {
      await route.fulfill({
        status: signedIn ? 200 : 401,
        contentType: "application/json",
        body: JSON.stringify(
          signedIn ? alice : { detail: "not authenticated" },
        ),
      });
      return;
    }
    if (
      url.pathname === "/auth/google/session" &&
      request.method() === "POST"
    ) {
      expect(request.postDataJSON()).toEqual({ id_token: "google-id-token" });
      tokenAttempts += 1;
      if (failFirstToken && tokenAttempts === 1) {
        await route.fulfill({
          status: 403,
          contentType: "application/json",
          body: JSON.stringify({ detail: "authentication denied" }),
        });
        return;
      }
      signedIn = true;
      await route.fulfill({ status: 204 });
      return;
    }
    await route.fulfill({ status: 404 });
  });
}

async function expectVisibleKeyboardFocus(target: Locator) {
  await expect(target).toBeFocused();
  const outline = await target.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      style: style.outlineStyle,
      width: Number.parseFloat(style.outlineWidth),
    };
  });
  expect(outline.style).not.toBe("none");
  expect(outline.width).toBeGreaterThan(0);
}

async function expectNoPageOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth);
}

test("selects a development identity, switches locale, and logs out accessibly", async ({
  page,
}) => {
  await installDevelopmentAuthFixture(page);
  await page.goto("/");

  await expect(page.locator("html")).toHaveAttribute("lang", "cs");
  await expect(
    page.getByRole("heading", { name: "Vývojové přihlášení" }),
  ).toBeVisible();
  await expect(
    page.getByText("Vývojová autentizace je aktivní", { exact: false }),
  ).toBeVisible();
  await expectNoPageOverflow(page);

  const identity = page.getByRole("button", {
    name: "Přihlásit se jako Alice Member",
  });
  await page.keyboard.press("Tab");
  await expectVisibleKeyboardFocus(identity);
  await identity.click();

  await expect(page.getByText("Alice Member")).toBeVisible();
  await expect(page.getByRole("button", { name: "Odhlásit se" })).toBeVisible();
  await expectNoPageOverflow(page);

  await page.getByRole("link", { name: "CookOps" }).focus();
  await page.keyboard.press("Tab");
  await expectVisibleKeyboardFocus(page.getByRole("link", { name: "Akce" }));
  await page.keyboard.press("Tab");
  await expectVisibleKeyboardFocus(page.getByRole("link", { name: "Recepty" }));
  await page.keyboard.press("Tab");
  await expectVisibleKeyboardFocus(
    page.getByRole("link", { name: "Suroviny" }),
  );
  await page.keyboard.press("Tab");
  await expectVisibleKeyboardFocus(
    page.getByRole("link", { name: "Nastavení organizace" }),
  );
  await page.keyboard.press("Tab");
  await expectVisibleKeyboardFocus(
    page.getByRole("combobox", { name: "Jazyk" }),
  );

  await page.getByRole("combobox", { name: "Jazyk" }).selectOption("en");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.getByRole("button", { name: "Log out" })).toBeVisible();
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(
    page.getByRole("heading", { name: "Development sign-in" }),
  ).toBeVisible();
  await expectNoPageOverflow(page);
});

test("uses runtime-configured Google Identity Services and never the dummy API", async ({
  page,
}) => {
  await installGoogleAuthFixture(page);
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: "Přihlášení do CookOps" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Continue with Google" }),
  ).toBeVisible();
  await expectNoPageOverflow(page);
  await page.getByRole("button", { name: "Continue with Google" }).click();

  await expect(page.getByText("Alice Member")).toBeVisible();
  await expect(page.getByRole("button", { name: "Odhlásit se" })).toBeVisible();
  await expectNoPageOverflow(page);
});

test("remounts the Google sign-in control after a rejected token", async ({
  page,
}) => {
  await installGoogleAuthFixture(page, true);
  await page.goto("/");
  await page.getByRole("button", { name: "Continue with Google" }).click();

  await expect(page.getByRole("alert")).toContainText(
    "Přihlášení se nepodařilo",
  );
  await page.getByRole("button", { name: "Zkusit znovu" }).click();
  await page.getByRole("button", { name: "Continue with Google" }).click();
  await expect(page.getByText("Alice Member")).toBeVisible();
});
