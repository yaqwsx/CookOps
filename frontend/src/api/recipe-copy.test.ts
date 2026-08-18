import { beforeEach, describe, expect, it, vi } from "vitest";

const { installation } = vi.hoisted(() => ({ installation: vi.fn() }));
vi.mock("../local-db", () => ({ readOrCreateBrowserInstallationId: installation }));
import { copyRecipe } from "./recipe-copy";

const ids = {
  sourceOrganizationId: "5ce17d2f-8365-4b1f-a80b-34d10425d51c",
  destinationOrganizationId: "6ce17d2f-8365-4b1f-a80b-34d10425d51c",
  sourceRecipeId: "7ce17d2f-8365-4b1f-a80b-34d10425d51c",
  sourceCurrentRecipeVersionId: "8ce17d2f-8365-4b1f-a80b-34d10425d51c",
  destinationRecipeId: "9ce17d2f-8365-4b1f-a80b-34d10425d51c",
  destinationRecipeVersionId: "ace17d2f-8365-4b1f-a80b-34d10425d51c",
  mutationId: "bce17d2f-8365-4b1f-a80b-34d10425d51c",
};
const input = { ...ids, ingredientVersionMappings: {}, recipeTagMappings: {}, scalingUnitMappings: {}, preferredDisplayUnitMappings: {}, clientWallTime: "2026-08-18T12:34:56.789Z" };

beforeEach(() => { installation.mockResolvedValue("cce17d2f-8365-4b1f-a80b-34d10425d51c"); vi.stubGlobal("fetch", vi.fn()); });

describe("recipe copy API", () => {
  it("keeps the exact payload stable for an uncertain retry", async () => {
    vi.mocked(fetch).mockImplementation(async () => new Response(JSON.stringify({ mutation_id: ids.mutationId, source_organization_id: ids.sourceOrganizationId, destination_organization_id: ids.destinationOrganizationId, source_recipe_id: ids.sourceRecipeId, destination_recipe_id: ids.destinationRecipeId, source_recipe_version_id: ids.sourceCurrentRecipeVersionId, destination_recipe_version_id: ids.destinationRecipeVersionId, first_change_sequence: 1, last_change_sequence: 2, replayed: false }), { status: 200 }));
    await copyRecipe("user", ids.destinationOrganizationId, input);
    await copyRecipe("user", ids.destinationOrganizationId, input);
    const first = JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body));
    const second = JSON.parse(String(vi.mocked(fetch).mock.calls[1][1]?.body));
    expect(second).toEqual(first);
  });

  it.each([
    ["zero mutation id", { mutation_id: "00000000-0000-0000-0000-000000000000" }],
    ["zero source id", { source_organization_id: "00000000-0000-0000-0000-000000000000" }],
    ["zero destination id", { destination_organization_id: "00000000-0000-0000-0000-000000000000" }],
    ["non-positive first sequence", { first_change_sequence: 0 }],
    ["non-positive last sequence", { last_change_sequence: 0 }],
  ])("rejects %s in the response", async (_name, override) => {
    const valid = {
      mutation_id: ids.mutationId, source_organization_id: ids.sourceOrganizationId,
      destination_organization_id: ids.destinationOrganizationId, source_recipe_id: ids.sourceRecipeId,
      destination_recipe_id: ids.destinationRecipeId, source_recipe_version_id: ids.sourceCurrentRecipeVersionId,
      destination_recipe_version_id: ids.destinationRecipeVersionId, first_change_sequence: 1,
      last_change_sequence: 2, replayed: false, ...override,
    };
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify(valid), { status: 200 }));
    await expect(copyRecipe("user", ids.destinationOrganizationId, input)).rejects.toThrow("Invalid recipe copy response");
  });
});
