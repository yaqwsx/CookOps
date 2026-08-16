import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import i18n, { defaultLocale } from "./i18n";
import { RecipeCatalog } from "./recipe-catalog-view";

vi.mock("dexie", async (importOriginal) => ({
  ...(await importOriginal<typeof import("dexie")>()),
  liveQuery: (query: () => Promise<unknown>) => ({
    subscribe: (observer: { next: (value: unknown) => void }) => {
      void query().then(observer.next);
      return { unsubscribe: () => undefined };
    },
  }),
}));
vi.mock("./recipe-catalog", () => ({
  readRecipeCatalog: vi.fn(async () => ({
    recipes: [
      {
        id: "6ce17d2f-8365-4b1f-a80b-34d10425d51c",
        retired: false,
        versionId: "7ce17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Soup",
        description: null,
        scalingUnitId: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
        baseScalingAmount: "1",
        ingredientLines: [
          {
            id: "dce17d2f-8365-4b1f-a80b-34d10425d51c",
            ingredientVersionId: "bde17d2f-8365-4b1f-a80b-34d10425d51c",
            baseQuantity: "1",
            scalingBehavior: "proportional",
            includeInPortionWeight: true,
            note: "",
          },
        ],
        hasRetiredIngredientReference: true,
        catalogUpdateAvailable: true,
        recipeTagIds: ["1ce17d2f-8365-4b1f-a80b-34d10425d51c"],
      },
      {
        id: "7ce17d2f-8365-4b1f-a80b-34d10425d51c",
        retired: false,
        versionId: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Pasta",
        description: "Family tomato dinner",
        scalingUnitId: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
        baseScalingAmount: "1",
        ingredientLines: [],
        hasRetiredIngredientReference: false,
        catalogUpdateAvailable: false,
        recipeTagIds: [],
      },
      {
        id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
        retired: true,
        versionId: "ace17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Archived cake",
        description: "Old recipe",
        scalingUnitId: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
        baseScalingAmount: "1",
        ingredientLines: [],
        hasRetiredIngredientReference: false,
        catalogUpdateAvailable: false,
        recipeTagIds: [],
      },
    ],
    scalingUnits: [],
    ingredients: [
      {
        id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
        versionId: "ace17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Current carrot",
        canonicalUnitName: "g",
        massPerCanonicalQuantity: "1",
      },
      {
        id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
        versionId: "bde17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Historical carrot",
        canonicalUnitName: "g",
        massPerCanonicalQuantity: "1",
        historical: true,
        retired: true,
      },
    ],
    tags: [
      {
        id: "1ce17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Quick meals",
      },
    ],
  })),
}));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization: vi.fn(async () => undefined),
  SyncRequestError: class extends Error {},
}));

describe("recipe retired ingredient warning", () => {
  beforeEach(async () => {
    await i18n.changeLanguage(defaultLocale);
  });

  it("renders a localized accessible warning", async () => {
    render(
      <RecipeCatalog
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
        onUnauthenticated={() => undefined}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Tento recept obsahuje vyřazenou surovinu",
    );
    expect(screen.getByText("Je dostupná aktualizace verzí surovin v katalogu.")).toBeVisible();
  });

  it("keeps historical options readable but excludes them from new lines", async () => {
    const user = userEvent.setup();
    render(
      <RecipeCatalog
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
        onUnauthenticated={() => undefined}
      />,
    );
    await user.type(
      await screen.findByRole("searchbox", { name: "Hledat recepty" }),
      "soup",
    );
    await user.click(
      await screen.findByRole("button", { name: "Upravit recept" }),
    );
    const ingredientSelect = screen.getByRole("combobox", { name: "Surovina" });
    expect(ingredientSelect).toHaveTextContent("Historical carrot");
    expect(screen.getByRole("option", { name: "Historical carrot" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Odebrat surovinu" }));
    await user.click(screen.getByRole("button", { name: "Přidat surovinu" }));
    const newIngredientSelect = screen.getByRole("combobox", { name: "Surovina" });
    expect(newIngredientSelect).toHaveTextContent("Current carrot");
    expect(newIngredientSelect).not.toHaveTextContent("Historical carrot");
  });

  it("matches description, tag, and ingredient with normalized search", async () => {
    const user = userEvent.setup();
    render(
      <RecipeCatalog
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
        onUnauthenticated={() => undefined}
      />,
    );
    const search = await screen.findByRole("searchbox", {
      name: "Hledat recepty",
    });
    await user.type(search, "  FAMILY ");
    expect(screen.getByRole("heading", { name: "Pasta" })).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Soup" }),
    ).not.toBeInTheDocument();
    await user.clear(search);
    await user.type(search, "quick");
    expect(screen.getByRole("heading", { name: "Soup" })).toBeVisible();
    await user.clear(search);
    await user.type(search, "CARROT");
    expect(screen.getByRole("heading", { name: "Soup" })).toBeVisible();
    await user.clear(search);
    await user.type(search, "missing");
    expect(
      screen.getByText("Hledání neodpovídá žádnému receptu."),
    ).toBeVisible();
  });

  it("keeps retired filtering independent from search", async () => {
    const user = userEvent.setup();
    render(
      <RecipeCatalog
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
        onUnauthenticated={() => undefined}
      />,
    );
    const search = await screen.findByRole("searchbox", {
      name: "Hledat recepty",
    });
    await user.type(search, "archived");
    expect(
      screen.getByText("Hledání neodpovídá žádnému receptu."),
    ).toBeVisible();
    await user.click(
      screen.getByRole("checkbox", { name: "Zobrazit vyřazené recepty" }),
    );
    expect(
      screen.getByRole("heading", { name: "Archived cake" }),
    ).toBeVisible();
  });
});
