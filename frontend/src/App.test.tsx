import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "./App";
import i18n, { defaultLocale } from "./i18n";

function renderApp() {
  return render(<App />);
}

describe("application shell localization", () => {
  beforeEach(async () => {
    await i18n.changeLanguage(defaultLocale);
  });

  it("starts in Czech", () => {
    expect(defaultLocale).toBe("cs");
    renderApp();

    expect(
      screen.getByRole("heading", { name: "Plánování společného vaření" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Navigace organizace" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Nastavení organizace" }),
    ).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute("lang", "cs");
  });

  it("switches the complete shell to English", async () => {
    const user = userEvent.setup();
    renderApp();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Jazyk" }),
      "en",
    );

    expect(
      screen.getByRole("heading", { name: "Plan group cooking" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Organization navigation" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Language" })).toHaveValue(
      "en",
    );
    expect(screen.getByRole("link", { name: "Events" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Recipes" })).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Ingredients" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Organization settings" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Prepare events, recipes, and shopping for the whole group in one place.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText("This area will become available in a later step."),
    ).toHaveLength(4);
    expect(document.documentElement).toHaveAttribute("lang", "en");
  });
});
