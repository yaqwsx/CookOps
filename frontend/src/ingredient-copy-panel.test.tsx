import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { IngredientCopyPanel } from "./ingredient-copy-panel";
import type { IngredientCopyCatalog } from "./ingredient-copy-catalog";
import { SyncRequestError } from "./sync-bootstrap";
import i18n from "./i18n";

const { fetchMock, readVisibleRecords, readCopyCatalog, pull, readInstallationId } = vi.hoisted(() => ({
  fetchMock: vi.fn(),
  readVisibleRecords: vi.fn(),
  readCopyCatalog: vi.fn(),
  pull: vi.fn(),
  readInstallationId: vi.fn(),
}));

vi.mock("./visible-records", () => ({ readVisibleRecords }));
vi.mock("./ingredient-copy-catalog", () => ({ readIngredientCopyCatalog: readCopyCatalog }));
vi.mock("./sync-bootstrap", () => ({
  pullOrganization: pull,
  SyncRequestError: class SyncRequestError extends Error {
    constructor(readonly status: number) {
      super("Sync request failed.");
    }
  },
}));
vi.mock("./local-db", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./local-db")>()),
  readOrCreateBrowserInstallationId: readInstallationId,
}));

const sourceOrganizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationOrganizationId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const versionId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
const sourceUnitId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationUnitId = "ace17d2f-8365-4b1f-a80b-34d10425d51c";
const mutationId = "bce17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationIngredientId = "cce17d2f-8365-4b1f-a80b-34d10425d51c";
const sourceCountUnitId = "dee17d2f-8365-4b1f-a80b-34d10425d51c";
const sourceCustomUnitId = "eee17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationCountUnitId = "fee17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationCountUnitId2 = "0ce17d2f-8365-4b1f-a80b-34d10425d51c";
const refreshedVersionId = "1ce17d2f-8365-4b1f-a80b-34d10425d51c";
let sourceProjectionVersionId = versionId;

const ingredient = {
  id: ingredientId,
  versionId,
  name: "Flour",
  canonicalUnitName: "gram",
  canonicalUnitId: sourceUnitId,
  massPerCanonicalQuantity: "1",
  dietaryTagIds: [],
  defaultStoreSectionId: null,
};

const previewWire = {
  source_organization_id: sourceOrganizationId,
  destination_organization_id: destinationOrganizationId,
  source_ingredient_id: ingredientId,
  source_version_id: versionId,
  source_name: "Flour",
  canonical_unit_id: sourceUnitId,
  default_store_section_id: null,
  dietary_tag_ids: [],
  precondition_fingerprint: "fingerprint",
  mapping_requirements: [{ kind: "canonical_unit", source_id: sourceUnitId, seed_key: null }],
};

const resultWire = {
  mutation_id: mutationId,
  source_organization_id: sourceOrganizationId,
  destination_organization_id: destinationOrganizationId,
  source_ingredient_id: ingredientId,
  destination_ingredient_id: destinationIngredientId,
  source_version_id: versionId,
  destination_version_id: "dce17d2f-8365-4b1f-a80b-34d10425d51c",
  source_name: "Flour",
  canonical_unit_id: sourceUnitId,
  default_store_section_id: null,
  dietary_tag_ids: [],
  first_change_sequence: 1,
  last_change_sequence: 2,
  replayed: false,
};

const sourceCatalog: IngredientCopyCatalog = {
  units: [{ id: sourceUnitId, name: "gram", dimension: "mass", baseUnitFactor: "1" }],
  sections: [],
  dietaryTags: [],
};
const destinationCatalog: IngredientCopyCatalog = {
  units: [{ id: destinationUnitId, name: "gramme", dimension: "mass", baseUnitFactor: "1" }],
  sections: [],
  dietaryTags: [],
};

function wireRecord(
  organizationId: string,
  entityType: string,
  entityId: string,
  fields: Record<string, unknown>,
) {
  return {
    userId: "dce17d2f-8365-4b1f-a80b-34d10425d51c",
    organizationId,
    entityType,
    entityId,
    recordSchemaVersion: 1,
    lifecycle: "active" as const,
    fields: { id: entityId, organization_id: organizationId, ...fields },
    fieldClocks: {},
    immutable: false,
    updatedAt: "2026-08-18T00:00:00Z",
  };
}

function sourceIngredientRecord() {
  return wireRecord(sourceOrganizationId, "ingredient", ingredientId, {
    current_version_id: sourceProjectionVersionId,
  });
}

function sourceVersionRecord() {
  return wireRecord(sourceOrganizationId, "ingredient_version", sourceProjectionVersionId, {
    ingredient_id: ingredientId,
    name: ingredient.name,
    canonical_unit_id: sourceUnitId,
    mass_per_canonical_quantity: ingredient.massPerCanonicalQuantity,
    dietary_tag_ids: [],
    default_store_section_id: null,
  });
}

function renderPanel(onUnauthenticated = vi.fn()) {
  return render(
    <IngredientCopyPanel
      ingredient={ingredient}
      onUnauthenticated={onUnauthenticated}
      organizationId={sourceOrganizationId}
      userId="dce17d2f-8365-4b1f-a80b-34d10425d51c"
    />,
  );
}

async function chooseDestination() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Copy to another organization" }));
  const destination = await screen.findByRole("combobox", { name: "Destination organization" });
  await user.selectOptions(destination, destinationOrganizationId);
  return user;
}

describe("IngredientCopyPanel", () => {
  let previewStatus = 200;
  let copyStatus = 200;

  beforeEach(async () => {
    await i18n.changeLanguage("en");
    previewStatus = 200;
    copyStatus = 200;
    sourceProjectionVersionId = versionId;
    previewWire.mapping_requirements = [{ kind: "canonical_unit", source_id: sourceUnitId, seed_key: null }];
    sourceCatalog.units = [{ id: sourceUnitId, name: "gram", dimension: "mass", baseUnitFactor: "1" }];
    destinationCatalog.units = [{ id: destinationUnitId, name: "gramme", dimension: "mass", baseUnitFactor: "1" }];
    fetchMock.mockReset();
    readVisibleRecords.mockReset();
    readCopyCatalog.mockReset();
    pull.mockReset();
    readInstallationId.mockReset();
    readCopyCatalog.mockImplementation(async (_userId: string, organizationId: string) =>
      organizationId === sourceOrganizationId ? sourceCatalog : destinationCatalog,
    );
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/organizations" && !init?.method)
        return new Response(JSON.stringify({
          organizations: [
            { id: sourceOrganizationId, name: "Source kitchen" },
            { id: destinationOrganizationId, name: "Destination kitchen" },
          ],
        }), { status: 200 });
      if (url.includes("ingredient-copy-preview"))
        return new Response(JSON.stringify(previewWire), { status: previewStatus });
      if (init?.method === "POST" && url.endsWith("/ingredient-copy"))
        return new Response(JSON.stringify(resultWire), { status: copyStatus });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    pull.mockResolvedValue(true);
    readInstallationId.mockResolvedValue("dce17d2f-8365-4b1f-a80b-34d10425d51c");
    readVisibleRecords.mockImplementation(async (_userId: string, organizationId: string, entityType: string) => {
      const catalog = organizationId === sourceOrganizationId ? sourceCatalog : destinationCatalog;
      if (entityType === "ingredient") return organizationId === sourceOrganizationId ? [sourceIngredientRecord()] : [];
      if (entityType === "ingredient_version") return organizationId === sourceOrganizationId ? [sourceVersionRecord()] : [];
      if (entityType !== "unit_definition") return [];
      return catalog.units.map((unit) => wireRecord(organizationId, entityType, unit.id, {
        custom_name: unit.name,
        dimension: unit.dimension,
        base_unit_factor: unit.baseUnitFactor,
        allows_ingredient_quantity: true,
      }));
    });
  });

  it("opens, previews, maps, and confirms without exposing technical version IDs", async () => {
    renderPanel();
    const user = await chooseDestination();

    expect(await screen.findByText("Source ingredient: Flour")).toBeVisible();
    expect(screen.getByText("Current published version")).toBeVisible();
    expect(screen.queryByText(versionId)).not.toBeInTheDocument();
    const mapping = screen.getByRole("combobox", { name: "gram unit" });
    expect(mapping).toHaveValue(destinationUnitId);
    const confirm = screen.getByRole("button", { name: "Confirm copy" });
    expect(confirm).toBeEnabled();

    await user.click(confirm);
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(post).toBeDefined();
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      source_organization_id: sourceOrganizationId,
      ingredient_id: ingredientId,
      precondition_fingerprint: "fingerprint",
      mappings: [{ kind: "canonical_unit", source_id: sourceUnitId, destination_id: destinationUnitId }],
    });
    expect(readInstallationId).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Ingredient copied\./)).toBeVisible();
    expect(pull).toHaveBeenCalledTimes(3);
  });

  it("disables confirmation when a required mapping has no destination candidate", async () => {
    readCopyCatalog.mockImplementation(async (_userId: string, organizationId: string) => {
      if (organizationId === destinationOrganizationId) return { ...destinationCatalog, units: [] };
      return sourceCatalog;
    });
    renderPanel();
    await chooseDestination();
    expect(await screen.findByText("Source ingredient: Flour")).toBeVisible();
    expect(screen.getByRole("button", { name: "Confirm copy" })).toBeDisabled();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("uses a generic unavailable state for inaccessible previews", async () => {
    previewStatus = 404;
    renderPanel();
    await chooseDestination();
    expect(await screen.findByText("Copying is not available for this organization.")).toBeVisible();
    expect(screen.queryByText("not found")).not.toBeInTheDocument();
  });

  it("keeps unauthorized confirmation generic and notifies the session owner", async () => {
    copyStatus = 401;
    const onUnauthenticated = vi.fn();
    renderPanel(onUnauthenticated);
    const user = await chooseDestination();
    await screen.findByText("Source ingredient: Flour");
    await user.click(screen.getByRole("button", { name: "Confirm copy" }));
    expect(await screen.findByText("Copying is not available for this organization.")).toBeVisible();
    expect(screen.queryByText("unauthorized")).not.toBeInTheDocument();
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
  });

  it("keeps the mutation id across a rejected retry and suppresses double confirmation", async () => {
    let attempts = 0;
    let releaseFirstPost: (() => void) | undefined;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/organizations" && !init?.method)
        return new Response(JSON.stringify({
          organizations: [
            { id: sourceOrganizationId, name: "Source kitchen" },
            { id: destinationOrganizationId, name: "Destination kitchen" },
          ],
        }), { status: 200 });
      if (url.includes("ingredient-copy-preview"))
        return new Response(JSON.stringify(previewWire), { status: 200 });
      if (init?.method === "POST" && url.endsWith("/ingredient-copy")) {
        attempts += 1;
        if (attempts === 1)
          return new Promise<Response>((resolve) => {
            releaseFirstPost = () => resolve(new Response("", { status: 500 }));
          });
        return new Response(JSON.stringify(resultWire), { status: 200 });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    renderPanel();
    const user = await chooseDestination();
    await screen.findByText("Source ingredient: Flour");
    const confirm = screen.getByRole("button", { name: "Confirm copy" });
    await user.click(confirm);
    await user.click(confirm);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    releaseFirstPost?.();
    await screen.findByText("Copying is not available for this organization.");
    await user.click(screen.getByRole("button", { name: "Confirm copy" }));
    await screen.findByText(/Ingredient copied\./);
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(2);
    expect(JSON.parse(String(posts[0][1]?.body)).mutation_id).toBe(
      JSON.parse(String(posts[1][1]?.body)).mutation_id,
    );
    expect(JSON.parse(String(posts[0][1]?.body)).client_wall_time).toBe(
      JSON.parse(String(posts[1][1]?.body)).client_wall_time,
    );
  });

  it("requires a fresh preview after a definite copy rejection", async () => {
    copyStatus = 422;
    renderPanel();
    const user = await chooseDestination();
    await screen.findByText("Source ingredient: Flour");
    await user.click(screen.getByRole("button", { name: "Confirm copy" }));
    expect(await screen.findByText("Copying is not available for this organization.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Confirm copy" })).not.toBeInTheDocument();

    copyStatus = 200;
    await user.selectOptions(screen.getByRole("combobox", { name: "Destination organization" }), destinationOrganizationId);
    await screen.findByText("Source ingredient: Flour");
    await user.click(screen.getByRole("button", { name: "Confirm copy" }));
    await screen.findByText(/Ingredient copied\./);
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(2);
    expect(JSON.parse(String(posts[0][1]?.body)).mutation_id).not.toBe(
      JSON.parse(String(posts[1][1]?.body)).mutation_id,
    );
  });

  it("rejects a source version that changed during refresh without posting", async () => {
    sourceProjectionVersionId = refreshedVersionId;
    renderPanel();
    await chooseDestination();
    expect(await screen.findByText("Copying is not available for this organization.")).toBeVisible();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("propagates a compatible count mapping and submits both requirements", async () => {
    previewWire.mapping_requirements = [
      { kind: "canonical_unit", source_id: sourceCountUnitId, seed_key: null },
      { kind: "canonical_unit", source_id: sourceCustomUnitId, seed_key: null },
    ];
    sourceCatalog.units = [
      { id: sourceUnitId, name: "gram", dimension: "mass", baseUnitFactor: "1" },
      { id: sourceCountUnitId, name: "portion", dimension: "count", baseUnitFactor: undefined },
      { id: sourceCustomUnitId, name: "serving", dimension: "count", baseUnitFactor: undefined },
    ];
    destinationCatalog.units = [
      { id: destinationCountUnitId, name: "piece", dimension: "count", baseUnitFactor: undefined },
      { id: destinationCountUnitId2, name: "item", dimension: "count", baseUnitFactor: undefined },
    ];
    renderPanel();
    const user = await chooseDestination();
    await screen.findByText("Source ingredient: Flour");
    const mappings = screen.getAllByRole("combobox").slice(1);
    expect(mappings).toHaveLength(2);
    await user.selectOptions(mappings[0], destinationCountUnitId2);
    expect(mappings[1]).toHaveValue(destinationCountUnitId2);
    await user.click(screen.getByRole("button", { name: "Confirm copy" }));
    await screen.findByText(/Ingredient copied\./);
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body)).mappings).toEqual([
      { kind: "canonical_unit", source_id: sourceCountUnitId, destination_id: destinationCountUnitId2 },
      { kind: "canonical_unit", source_id: sourceCustomUnitId, destination_id: destinationCountUnitId2 },
    ]);
  });

  it("disables confirmation when grouped requirements have no compatible destination", async () => {
    previewWire.mapping_requirements = [
      { kind: "canonical_unit", source_id: sourceCountUnitId, seed_key: null },
      { kind: "canonical_unit", source_id: sourceCustomUnitId, seed_key: null },
    ];
    sourceCatalog.units = [
      { id: sourceUnitId, name: "gram", dimension: "mass", baseUnitFactor: "1" },
      { id: sourceCountUnitId, name: "portion", dimension: "count", baseUnitFactor: undefined },
      { id: sourceCustomUnitId, name: "serving", dimension: "custom", baseUnitFactor: undefined },
    ];
    destinationCatalog.units = [{ id: destinationCountUnitId, name: "piece", dimension: "count", baseUnitFactor: undefined }];
    renderPanel();
    await chooseDestination();
    await screen.findByText("Source ingredient: Flour");
    expect(screen.getByRole("button", { name: "Confirm copy" })).toBeDisabled();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("reports an unauthorized organization list generically", async () => {
    const onUnauthenticated = vi.fn();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/v1/organizations" && !init?.method)
        return new Response("", { status: 401 });
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    renderPanel(onUnauthenticated);
    await userEvent.setup().click(screen.getByRole("button", { name: "Copy to another organization" }));
    expect(await screen.findByText("Copying is not available for this organization.")).toBeVisible();
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
  });

  it("reports an unauthorized preview generically", async () => {
    const onUnauthenticated = vi.fn();
    previewStatus = 401;
    renderPanel(onUnauthenticated);
    await chooseDestination();
    expect(await screen.findByText("Copying is not available for this organization.")).toBeVisible();
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
  });

  it("reports an unauthorized source refresh generically", async () => {
    const onUnauthenticated = vi.fn();
    pull.mockRejectedValueOnce(new SyncRequestError(401));
    renderPanel(onUnauthenticated);
    await chooseDestination();
    expect(await screen.findByText("Copying is not available for this organization.")).toBeVisible();
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
  });

  it("reports an unauthorized destination refresh generically", async () => {
    const onUnauthenticated = vi.fn();
    pull.mockResolvedValueOnce(true).mockRejectedValueOnce(new SyncRequestError(401));
    renderPanel(onUnauthenticated);
    await chooseDestination();
    expect(await screen.findByText("Copying is not available for this organization.")).toBeVisible();
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
  });

  it("keeps committed copy success honest while reporting unauthorized refresh", async () => {
    const onUnauthenticated = vi.fn();
    pull.mockResolvedValueOnce(true).mockResolvedValueOnce(true).mockRejectedValueOnce(new SyncRequestError(401));
    renderPanel(onUnauthenticated);
    const user = await chooseDestination();
    await screen.findByText("Source ingredient: Flour");
    await user.click(screen.getByRole("button", { name: "Confirm copy" }));
    expect(await screen.findByText(/Ingredient copied\./)).toBeVisible();
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
  });

  it("does not reopen after closing while organizations are still loading", async () => {
    let releaseOrganizations: ((response: Response) => void) | undefined;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/v1/organizations" && !init?.method)
        return new Promise<Response>((resolve) => { releaseOrganizations = resolve; });
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Copy to another organization" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    releaseOrganizations?.(new Response(JSON.stringify({ organizations: [] }), { status: 200 }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Copy to another organization" })).toBeVisible());
    expect(screen.queryByRole("heading", { name: "Copy ingredient" })).not.toBeInTheDocument();
  });
});
