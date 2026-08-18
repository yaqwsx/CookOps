import { beforeEach, describe, expect, it, vi } from "vitest";

const { readInstallationId } = vi.hoisted(() => ({
  readInstallationId: vi.fn(),
}));

vi.mock("../local-db", () => ({
  readOrCreateBrowserInstallationId: readInstallationId,
}));

import {
  copyIngredient,
  getIngredientCopyPreview,
} from "./ingredient-copy";

const sourceOrganizationId = "5ce17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationOrganizationId = "6ce17d2f-8365-4b1f-a80b-34d10425d51c";
const ingredientId = "7ce17d2f-8365-4b1f-a80b-34d10425d51c";
const sourceVersionId = "8ce17d2f-8365-4b1f-a80b-34d10425d51c";
const unitId = "9ce17d2f-8365-4b1f-a80b-34d10425d51c";
const mutationId = "ace17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationIngredientId = "bce17d2f-8365-4b1f-a80b-34d10425d51c";
const destinationVersionId = "cce17d2f-8365-4b1f-a80b-34d10425d51c";
const clientWallTime = "2026-08-18T12:34:56.789Z";

const preview = {
  source_organization_id: sourceOrganizationId,
  destination_organization_id: destinationOrganizationId,
  source_ingredient_id: ingredientId,
  source_version_id: sourceVersionId,
  source_name: "Flour",
  canonical_unit_id: unitId,
  default_store_section_id: null,
  dietary_tag_ids: [],
  precondition_fingerprint: "fingerprint",
  mapping_requirements: [
    { kind: "canonical_unit", source_id: unitId, seed_key: null },
  ],
};

beforeEach(() => {
  vi.restoreAllMocks();
  readInstallationId.mockResolvedValue("dce17d2f-8365-4b1f-a80b-34d10425d51c");
  vi.stubGlobal("fetch", vi.fn());
});

describe("ingredient copy HTTP API", () => {
  it("rejects malformed preview responses instead of exposing partial data", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ ...preview, source_version_id: "not-a-uuid" }), { status: 200 }),
    );
    await expect(
      getIngredientCopyPreview(destinationOrganizationId, sourceOrganizationId, ingredientId),
    ).rejects.toThrow("Invalid ingredient copy response.");
  });

  it("parses preview and sends the typed online command without an Origin header", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(JSON.stringify(preview), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            mutation_id: mutationId,
            source_organization_id: sourceOrganizationId,
            destination_organization_id: destinationOrganizationId,
            source_ingredient_id: ingredientId,
            destination_ingredient_id: destinationIngredientId,
            source_version_id: sourceVersionId,
            destination_version_id: destinationVersionId,
            source_name: "Flour",
            canonical_unit_id: unitId,
            default_store_section_id: null,
            dietary_tag_ids: [],
            first_change_sequence: 1,
            last_change_sequence: 2,
            replayed: false,
          }),
          { status: 200 },
        ),
      );
    await expect(
      getIngredientCopyPreview(destinationOrganizationId, sourceOrganizationId, ingredientId),
    ).resolves.toMatchObject({ sourceName: "Flour", mappingRequirements: [{ sourceId: unitId }] });
    const result = await copyIngredient("user-id", destinationOrganizationId, {
      sourceOrganizationId,
      ingredientId,
      mutationId,
      clientWallTime,
      preconditionFingerprint: "fingerprint",
      mappings: [{ kind: "canonical_unit", sourceId: unitId, destinationId: unitId }],
    });
    expect(result.destinationIngredientId).toBe(destinationIngredientId);
    expect(readInstallationId).toHaveBeenCalledTimes(1);
    expect(readInstallationId).toHaveBeenCalledWith("user-id");
    const [, init] = vi.mocked(fetch).mock.calls[1];
    if (!init) throw new Error("fetch init missing");
    expect(init).toMatchObject({ method: "POST", credentials: "same-origin" });
    expect((init.headers as Record<string, string>).Origin).toBeUndefined();
    expect(JSON.parse(String(init?.body))).toMatchObject({
      source_organization_id: sourceOrganizationId,
      ingredient_id: ingredientId,
      mutation_id: mutationId,
      client_wall_time: clientWallTime,
      precondition_fingerprint: "fingerprint",
      mappings: [{ kind: "canonical_unit", source_id: unitId, destination_id: unitId }],
    });
  });
});
