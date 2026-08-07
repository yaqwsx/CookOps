import { expect, type Locator, type Page, test } from "@playwright/test";

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

test("renders the localized shell and preserves keyboard navigation", async ({
  page,
}) => {
  await page.goto("/");

  await expect(page.locator("html")).toHaveAttribute("lang", "cs");
  await expect(
    page.getByRole("navigation", { name: "Navigace organizace" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Plánování společného vaření" }),
  ).toBeVisible();
  await expectNoPageOverflow(page);

  const focusOrder = [
    page.getByRole("link", { name: "CookOps" }),
    page.getByRole("link", { name: "Akce" }),
    page.getByRole("link", { name: "Recepty" }),
    page.getByRole("link", { name: "Suroviny" }),
    page.getByRole("link", { name: "Nastavení organizace" }),
    page.getByRole("combobox", { name: "Jazyk" }),
  ];

  for (const target of focusOrder) {
    await page.keyboard.press("Tab");
    await expectVisibleKeyboardFocus(target);
  }

  await page.getByRole("combobox", { name: "Jazyk" }).selectOption("en");

  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(
    page.getByRole("navigation", { name: "Organization navigation" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Plan group cooking" }),
  ).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Language" })).toHaveValue(
    "en",
  );
  await expectNoPageOverflow(page);
});
