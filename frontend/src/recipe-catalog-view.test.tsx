import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import i18n, { defaultLocale } from "./i18n";
import { RecipeCatalog } from "./recipe-catalog-view";
import { queueRecipeVersionPublish } from "./recipe-publish";
import { queueIngredientCreateWithVersion } from "./ingredient-create";

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
  projectRecipeCatalogUpdate: vi.fn((recipe: { id: string }) => recipe.id === "6ce17d2f-8365-4b1f-a80b-34d10425d51c" ? ({
    blocked: false,
    lines: [{
      lineId: "dce17d2f-8365-4b1f-a80b-34d10425d51c",
      oldIngredient: { name: "Historical carrot", versionId: "bde17d2f-8365-4b1f-a80b-34d10425d51c" },
      newIngredient: { name: "Current carrot", versionId: "ace17d2f-8365-4b1f-a80b-34d10425d51c" },
      oldQuantity: "1",
      newQuantity: "1",
      compatible: true,
    }],
  }) : ({ blocked: false, lines: [] })),
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
            lineKey: "ece17d2f-8365-4b1f-a80b-34d10425d51c",
            positionKey: "a",
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
        canonicalUnitId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
      },
      {
        id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
        versionId: "bde17d2f-8365-4b1f-a80b-34d10425d51c",
        name: "Historical carrot",
        canonicalUnitName: "g",
        massPerCanonicalQuantity: "1",
        canonicalUnitId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
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
    units: [{ id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c", name: "g", dimension: "mass", baseUnitFactor: "1" }],
    organizationDefaultCurrency: "EUR",
    costs: {
      "6ce17d2f-8365-4b1f-a80b-34d10425d51c": {
        currency: "EUR",
        total: null,
        missingCount: 1,
      },
      "7ce17d2f-8365-4b1f-a80b-34d10425d51c": {
        currency: "EUR",
        total: "0.00",
        missingCount: 0,
      },
      "9ce17d2f-8365-4b1f-a80b-34d10425d51c": {
        currency: "EUR",
        total: "0.00",
        missingCount: 0,
      },
    },
  })),
}));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization: vi.fn(async () => undefined),
  SyncRequestError: class extends Error {},
}));
vi.mock("./recipe-publish", () => ({
  queueRecipeCreate: vi.fn(async () => undefined),
  queueRecipeVersionPublish: vi.fn(async () => "new-version"),
}));
vi.mock("./ingredient-create", () => ({
  defaultMassForUnit: () => "1",
  queueIngredientCreateWithVersion: vi.fn(async () => ({
    ingredientId: "bce17d2f-8365-4b1f-a80b-34d10425d51c",
    ingredientVersionId: "cce17d2f-8365-4b1f-a80b-34d10425d51c",
  })),
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

  it("previews and confirms one atomic ingredient update", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(
      <RecipeCatalog
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
        onUnauthenticated={() => undefined}
      />,
    );
    await user.click(await screen.findByText("Náhled aktualizace katalogu"));
    expect(screen.getByText(/Historical carrot/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Použít aktualizace surovin" }));
    expect(queueRecipeVersionPublish).toHaveBeenCalledTimes(1);
    expect(queueRecipeVersionPublish).toHaveBeenCalledWith(
      expect.any(String),
      expect.any(String),
      expect.objectContaining({
        recipeId: "6ce17d2f-8365-4b1f-a80b-34d10425d51c",
        ingredientLines: [expect.objectContaining({ ingredientVersionId: "ace17d2f-8365-4b1f-a80b-34d10425d51c" })],
        recipeTagIds: ["1ce17d2f-8365-4b1f-a80b-34d10425d51c"],
      }),
    );
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
    expect(ingredientSelect).toHaveValue("Historical carrot");
    await user.click(ingredientSelect);
    expect(screen.queryByRole("option", { name: "Historical carrot" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Odebrat surovinu" }));
    await user.click(screen.getByRole("button", { name: "Přidat surovinu" }));
    const newIngredientSelect = screen.getByRole("combobox", { name: "Surovina" });
    expect(newIngredientSelect).toHaveValue("Current carrot");
    await user.click(newIngredientSelect);
    expect(screen.queryByRole("option", { name: "Historical carrot" })).not.toBeInTheDocument();
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

  it("selects only an explicit keyboard choice", async () => {
    const user = userEvent.setup();
    render(
      <RecipeCatalog
        editRecipeId="6ce17d2f-8365-4b1f-a80b-34d10425d51c"
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId="6ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    const combobox = await screen.findByRole("combobox", { name: "Surovina" });
    await user.clear(combobox);
    await user.type(combobox, "current");
    expect(screen.getByRole("option", { name: /Current carrot/ })).not.toHaveAttribute("tabindex");
    expect(combobox).toHaveValue("current");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(combobox).toHaveValue("Current carrot");
  });

  it("blocks publishing a line after query text changes without a selection", async () => {
    const user = userEvent.setup();
    render(
      <RecipeCatalog
        editRecipeId="6ce17d2f-8365-4b1f-a80b-34d10425d51c"
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId="6ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    const combobox = await screen.findByRole("combobox", { name: "Surovina" });
    await user.clear(combobox);
    await user.type(combobox, "uncommitted text");
    await user.click(screen.getByRole("button", { name: "Publikovat verzi" }));
    expect(queueRecipeVersionPublish).toHaveBeenCalledWith(
      expect.any(String),
      expect.any(String),
      expect.objectContaining({ ingredientLines: [expect.objectContaining({ ingredientVersionId: "" })] }),
    );
  });

  it("Escape restores the committed label and ID, including after a query edit", async () => {
    const user = userEvent.setup();
    render(
      <RecipeCatalog
        editRecipeId="6ce17d2f-8365-4b1f-a80b-34d10425d51c"
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId="6ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    const combobox = await screen.findByRole("combobox", { name: "Surovina" });
    await user.clear(combobox);
    await user.type(combobox, "other");
    await user.keyboard("{Escape}");
    expect(combobox).toHaveValue("Historical carrot");
  });

  it("creates inline through the outbox and selects the returned immutable version", async () => {
    const user = userEvent.setup();
    render(
      <RecipeCatalog
        editRecipeId="6ce17d2f-8365-4b1f-a80b-34d10425d51c"
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId="6ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    const combobox = await screen.findByRole("combobox", { name: "Surovina" });
    await user.clear(combobox);
    await user.type(combobox, "not found");
    await user.click(screen.getByRole("button", { name: "Vytvořit novou surovinu" }));
    await user.type(screen.getByRole("textbox", { name: "Název nové suroviny" }), "New ingredient");
    await user.click(screen.getByRole("button", { name: "Uložit a vybrat surovinu" }));
    expect(queueIngredientCreateWithVersion).toHaveBeenCalledOnce();
    expect(combobox).toHaveValue("New ingredient");
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

  it("shows only the selected recipe and returns to the catalog", async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    render(
      <RecipeCatalog
        onBackToCatalog={onBack}
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId="7ce17d2f-8365-4b1f-a80b-34d10425d51c"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    expect(await screen.findByRole("heading", { name: "Pasta" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Soup" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Zpět do katalogu" }));
    expect(onBack).toHaveBeenCalledOnce();
  });

  it("reports an unavailable direct recipe and opens edit mode from the URL", async () => {
    const recipeId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const { rerender } = render(
      <RecipeCatalog
        onBackToCatalog={() => undefined}
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId="missing-recipe"
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    expect(await screen.findByText("Tento recept není v této organizaci dostupný.")).toBeVisible();
    rerender(
      <RecipeCatalog
        onBackToCatalog={() => undefined}
        editRecipeId={recipeId}
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId={recipeId}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    expect(await screen.findByRole("heading", { name: "Nová verze receptu" })).toBeVisible();
  });

  it("resets a discarded edit to the current recipe snapshot", async () => {
    const user = userEvent.setup();
    const recipeId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const { rerender } = render(
      <RecipeCatalog
        discardToken={0}
        editRecipeId={recipeId}
        onDirtyChange={() => undefined}
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId={recipeId}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    const name = (await screen.findAllByRole("textbox", { name: "Název" }))[1];
    await user.clear(name);
    await user.type(name, "Changed locally");
    rerender(
      <RecipeCatalog
        discardToken={1}
        editRecipeId={recipeId}
        onDirtyChange={() => undefined}
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId={recipeId}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    expect(await screen.findByRole("button", { name: "Upravit recept" })).toBeVisible();
    rerender(
      <RecipeCatalog
        discardToken={1}
        editRecipeId={recipeId}
        onDirtyChange={() => undefined}
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId={recipeId}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    await user.click(screen.getByRole("button", { name: "Upravit recept" }));
    expect((screen.getAllByRole("textbox", { name: "Název" })[1])).toHaveValue("Soup");
  });

  it("removes a dirty recipe when direct navigation unmounts its editor", async () => {
    const user = userEvent.setup();
    const recipeA = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const recipeB = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
    const dirtyStates: boolean[] = [];
    const { rerender } = render(
      <RecipeCatalog
        editRecipeId={recipeA}
        onDirtyChange={(dirty) => dirtyStates.push(dirty)}
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId={recipeA}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    const name = (await screen.findAllByRole("textbox", { name: "Název" }))[1];
    await user.type(name, " changed");
    rerender(
      <RecipeCatalog
        editRecipeId={recipeB}
        onDirtyChange={(dirty) => dirtyStates.push(dirty)}
        onUnauthenticated={() => undefined}
        organizationId="5ce17d2f-8365-4b1f-a80b-34d10425d51c"
        selectedRecipeId={recipeB}
        userId="a6a58bd6-214e-49af-8fae-e5f974bf8e08"
      />,
    );
    await screen.findByRole("heading", { name: "Pasta" });
    expect(dirtyStates.at(-1)).toBe(false);
  });
});
