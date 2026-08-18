import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RecipeCopyPanel } from "./recipe-copy-panel";
import type { RecipeCopyCatalog } from "./recipe-copy-catalog";
import i18n from "./i18n";

const { organizations, pull, readSource, readDestination, copy, OrganizationRequestError, SyncRequestError, RecipeCopyRequestError } = vi.hoisted(() => ({
  organizations: vi.fn(), pull: vi.fn(), readSource: vi.fn(), readDestination: vi.fn(), copy: vi.fn(),
  OrganizationRequestError: class OrganizationRequestError extends Error { constructor(readonly status: number) { super(); } },
  SyncRequestError: class SyncRequestError extends Error { constructor(readonly status: number) { super(); } },
  RecipeCopyRequestError: class RecipeCopyRequestError extends Error { constructor(readonly status: number) { super(); } },
}));
vi.mock("./api/organizations", () => ({ getAvailableOrganizations: organizations, OrganizationRequestError }));
vi.mock("./api/recipe-copy", () => ({ copyRecipe: copy, RecipeCopyRequestError }));
vi.mock("./sync-bootstrap", () => ({ pullOrganization: pull, SyncRequestError }));
vi.mock("./recipe-copy-catalog", () => ({
  readRecipeCopyCatalog: readSource,
  readRecipeCopyDestinationCatalog: readDestination,
  matchingIds: (values: { id: string; name: string }[], name: string) => values.filter((value) => value.name.toLowerCase() === name.toLowerCase()).map((value) => value.id),
  normalized: (value: string) => value.normalize("NFC").trim().toLocaleLowerCase(),
  compatibleUnit: (source: { id: string; dimension?: string; baseUnitFactor?: string } | undefined, destination: { id: string; dimension?: string; baseUnitFactor?: string } | undefined) => Boolean(source && destination && source.dimension === destination.dimension && (source.dimension === "count" || source.dimension === "custom" ? source.id === destination.id : source.baseUnitFactor && source.baseUnitFactor === destination.baseUnitFactor)),
}));

const sourceOrganizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationOrganizationId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const recipeId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const versionId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientVersionId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
const sourceUnitId = "ace17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationUnitId = "bce17d2f-8365-4b1f-a80b-34d10425d51c";
const scalingUnitId = "cce17d2f-8365-4b1f-a80b-34d10425d51c";
type TestUnit = { id: string; name: string; dimension: string; baseUnitFactor: string | undefined };

const recipe = {
  id: recipeId, retired: false, versionId, versionHistory: [], name: "Soup", description: null,
  scalingUnitId, baseScalingAmount: "1", ingredientLines: [{ id: "dce17d2f-8365-4b1f-a80b-34d10425d51c", ingredientVersionId, baseQuantity: "1", scalingBehavior: "proportional" as const, includeInPortionWeight: true, note: "" }],
  hasRetiredIngredientReference: false, catalogUpdateAvailable: false, recipeTagIds: [],
};
const sourceIngredient = { id: "ece17d2f-8365-4b1f-a80b-34d10425d51c", versionId: ingredientVersionId, name: "Flour", canonicalUnitName: "source", canonicalUnitId: sourceUnitId, massPerCanonicalQuantity: "1" };
const destinationIngredient = { ...sourceIngredient, id: "fee17d2f-8365-4b1f-a80b-34d10425d51c", versionId: "0ce17d2f-8365-4b1f-a80b-34d10425d51c", canonicalUnitId: destinationUnitId };

function catalog(source: boolean, sourceUnit: TestUnit = { id: sourceUnitId, name: "source", dimension: "mass", baseUnitFactor: "1" }, destinationUnit: TestUnit = { id: destinationUnitId, name: "destination", dimension: "mass", baseUnitFactor: "1" }): RecipeCopyCatalog {
  return {
    recipe,
    recipes: [recipe], scalingUnits: [{ id: scalingUnitId, name: "portion", dimension: "count", baseUnitFactor: undefined }],
    ingredients: source ? [sourceIngredient] : [destinationIngredient], units: source ? [sourceUnit] : [destinationUnit],
    tags: [], costs: {},
  };
}

async function openAndSelect() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Copy to another organization" }));
  await user.selectOptions(await screen.findByRole("combobox", { name: "Destination organization" }), destinationOrganizationId);
  return user;
}

describe("RecipeCopyPanel", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await i18n.changeLanguage("en");
    organizations.mockResolvedValue([{ id: sourceOrganizationId, name: "Source" }, { id: destinationOrganizationId, name: "Destination" }]);
    pull.mockResolvedValue(true); copy.mockResolvedValue({});
    readSource.mockResolvedValue(catalog(true)); readDestination.mockResolvedValue(catalog(false));
  });

  it("exposes an accessible heading and rejects missing unit metadata", async () => {
    readSource.mockResolvedValue(catalog(true, { id: sourceUnitId, name: "source", dimension: "mass", baseUnitFactor: undefined }));
    render(<RecipeCopyPanel recipe={recipe} organizationId={sourceOrganizationId} userId="dce17d2f-8365-4b1f-a80b-34d10425d51c" onUnauthenticated={vi.fn()} />);
    await openAndSelect();
    expect(screen.getByRole("heading", { name: "Copy to another organization" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Flour" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "Copy recipe" })).toBeDisabled();
  });

  it("does not map count units by name when their stable identities differ", async () => {
    readSource.mockResolvedValue(catalog(true, { id: sourceUnitId, name: "piece", dimension: "count", baseUnitFactor: undefined }));
    readDestination.mockResolvedValue(catalog(false, { id: destinationUnitId, name: "piece", dimension: "count", baseUnitFactor: undefined }));
    render(<RecipeCopyPanel recipe={recipe} organizationId={sourceOrganizationId} userId="dce17d2f-8365-4b1f-a80b-34d10425d51c" onUnauthenticated={vi.fn()} />);
    await openAndSelect();
    expect(screen.getByRole("combobox", { name: "Flour" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "Copy recipe" })).toBeDisabled();
  });

  it("notifies the host for an expired session", async () => {
    const onUnauthenticated = vi.fn();
    organizations.mockRejectedValueOnce(new OrganizationRequestError(401));
    render(<RecipeCopyPanel recipe={recipe} organizationId={sourceOrganizationId} userId="dce17d2f-8365-4b1f-a80b-34d10425d51c" onUnauthenticated={onUnauthenticated} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Copy to another organization" }));
    await waitFor(() => expect(onUnauthenticated).toHaveBeenCalledOnce());
  });

  it("discards the prepared snapshot after a definitive copy error", async () => {
    const user = userEvent.setup();
    copy.mockRejectedValueOnce(new RecipeCopyRequestError(409));
    render(<RecipeCopyPanel recipe={recipe} organizationId={sourceOrganizationId} userId="dce17d2f-8365-4b1f-a80b-34d10425d51c" onUnauthenticated={vi.fn()} />);
    await openAndSelect();
    await waitFor(() => expect(screen.getByRole("button", { name: "Copy recipe" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Copy recipe" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Destination organization" })).toHaveValue(""));
  });

  it("retains the prepared snapshot after an uncertain copy error", async () => {
    const user = userEvent.setup();
    copy.mockRejectedValueOnce(new RecipeCopyRequestError(503));
    render(<RecipeCopyPanel recipe={recipe} organizationId={sourceOrganizationId} userId="dce17d2f-8365-4b1f-a80b-34d10425d51c" onUnauthenticated={vi.fn()} />);
    await openAndSelect();
    await waitFor(() => expect(screen.getByRole("button", { name: "Copy recipe" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Copy recipe" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Copy recipe" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Copy recipe" }));
    await waitFor(() => expect(copy).toHaveBeenCalledTimes(2));
  });
});
