import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { IngredientCatalog } from "./ingredient-catalog-view";
import i18n from "./i18n";

const { readCatalog, pullOrganization } = vi.hoisted(() => ({
  readCatalog: vi.fn(),
  pullOrganization: vi.fn(async () => false),
}));

vi.mock("dexie", () => ({
  liveQuery: (query: () => Promise<unknown>) => ({
    subscribe(observer: { next: (value: unknown) => void; error: (error: unknown) => void }) {
      void query().then(observer.next, observer.error);
      return { unsubscribe: () => undefined };
    },
  }),
}));
vi.mock("./ingredient-catalog", () => ({ readIngredientCatalog: readCatalog }));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization,
  SyncRequestError: class SyncRequestError extends Error {},
}));
vi.mock("./ingredient-create", () => ({ defaultMassForUnit: () => "1", queueIngredientCreate: vi.fn() }));
vi.mock("./ingredient-lifecycle", () => ({ queueIngredientLifecycle: vi.fn() }));
vi.mock("./ingredient-version-publish", () => ({ queueIngredientVersionPublish: vi.fn() }));
vi.mock("./ingredient-price-publish", () => ({ queueIngredientPricePublish: vi.fn() }));

const organizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const userId = "a6a58bd6-214e-49af-8fae-e5f974bf8e08";
const ingredientId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const versionId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const olderVersionId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";

const catalog = {
  ingredients: [{
    id: ingredientId,
    versionId,
    name: "Flour",
    canonicalUnitName: "gram",
    canonicalUnitId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
    massPerCanonicalQuantity: "1",
    versions: [
      { id: olderVersionId, name: "Old flour", canonicalUnitName: "gram", mass: "1", canonicalUnitId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c", dietaryTagIds: [], defaultStoreSectionId: null },
      { id: versionId, name: "Flour", canonicalUnitName: "gram", mass: "1", canonicalUnitId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c", dietaryTagIds: [], defaultStoreSectionId: null },
    ],
    currentPrice: { amount: "2.50", quantity: "1", unitId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c", currency: "EUR" },
  }],
  units: [{ id: "9ce17d2f-8365-4b1f-a80b-34d10425d51c", name: "gram", dimension: "mass", baseUnitFactor: "1" }],
  dietaryTags: [],
  organizationDefaultCurrency: "EUR",
};

describe("IngredientCatalog detail", () => {
  beforeEach(() => {
    readCatalog.mockResolvedValue(catalog);
    pullOrganization.mockClear();
    void i18n.changeLanguage("en");
  });

  it("renders current, historical, price, and accessible back link", async () => {
    const onBack = vi.fn();
    render(<IngredientCatalog organizationId={organizationId} userId={userId} onUnauthenticated={vi.fn()} onBackToCatalog={onBack} selectedIngredientId={ingredientId} />);
    expect(await screen.findByRole("heading", { name: "Flour" })).toBeInTheDocument();
    expect(screen.getAllByText(/gram/).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Back to ingredient catalog" })).toHaveAttribute("href", `/organizations/${organizationId}/ingredients`);
    expect(screen.getByText(/Current price: 2\.50 EUR/)).toBeInTheDocument();
    expect(screen.getByText("Edit and publish version")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Version history" })).toHaveTextContent("Old flour");
  });

  it("renders an unavailable state for an unknown selection", async () => {
    render(<IngredientCatalog organizationId={organizationId} userId={userId} onUnauthenticated={vi.fn()} selectedIngredientId="__invalid__" />);
    expect(await screen.findByText(/not available/)).toBeInTheDocument();
    expect(screen.queryByText("Flour")).not.toBeInTheDocument();
  });

  it("aggregates dirty state from actual version and price inputs", async () => {
    const onDirtyChange = vi.fn();
    render(<IngredientCatalog organizationId={organizationId} userId={userId} onUnauthenticated={vi.fn()} onDirtyChange={onDirtyChange} selectedIngredientId={ingredientId} />);
    await screen.findByRole("heading", { name: "Flour" });
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "New flour" } });
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
    fireEvent.change(screen.getByRole("textbox", { name: "Amount" }), { target: { value: "3" } });
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
  });

  it("fuzzy-filters active ingredients and gates retired results", async () => {
    readCatalog.mockResolvedValue({
      ...catalog,
      ingredients: [
        catalog.ingredients[0],
        { ...catalog.ingredients[0], id: "8ce17d2f-8365-4b1f-a80b-34d10425d51c", versionId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c", name: "Sugar" },
        { ...catalog.ingredients[0], id: "ace17d2f-8365-4b1f-a80b-34d10425d51c", versionId: "bce17d2f-8365-4b1f-a80b-34d10425d51c", name: "Archived salt", retired: true },
      ],
    });
    render(<IngredientCatalog organizationId={organizationId} userId={userId} onUnauthenticated={vi.fn()} />);
    await screen.findByRole("heading", { name: "Flour" });
    const search = screen.getByRole("searchbox", { name: "Search ingredients" });
    fireEvent.change(search, { target: { value: "sugr" } });
    expect(screen.getByText("Sugar")).toBeInTheDocument();
    expect(screen.queryByText("Flour")).not.toBeInTheDocument();
    fireEvent.change(search, { target: { value: "sallt" } });
    expect(screen.queryByText("Archived salt")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Show retired ingredients" }));
    expect(screen.getByText("Archived salt")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByText("Flour")).toBeInTheDocument();
  });
});
