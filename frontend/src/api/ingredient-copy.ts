import { readOrCreateBrowserInstallationId } from "../local-db";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type IngredientCopyMappingRequirement = {
  kind: "canonical_unit" | "default_store_section" | "dietary_tag";
  sourceId: string;
  seedKey: string | null;
};

export type IngredientCopyPreview = {
  sourceOrganizationId: string;
  destinationOrganizationId: string;
  sourceIngredientId: string;
  sourceVersionId: string;
  sourceName: string;
  canonicalUnitId: string;
  defaultStoreSectionId: string | null;
  dietaryTagIds: string[];
  preconditionFingerprint: string;
  mappingRequirements: IngredientCopyMappingRequirement[];
};

export type IngredientCopyMapping = {
  kind: IngredientCopyMappingRequirement["kind"];
  sourceId: string;
  destinationId: string | null;
};

export type IngredientCopyResult = {
  mutationId: string;
  sourceOrganizationId: string;
  destinationOrganizationId: string;
  sourceIngredientId: string;
  destinationIngredientId: string;
  sourceVersionId: string;
  destinationVersionId: string;
  sourceName: string;
  canonicalUnitId: string;
  defaultStoreSectionId: string | null;
  dietaryTagIds: string[];
  firstChangeSequence: number;
  lastChangeSequence: number;
  replayed: boolean;
};

export class IngredientCopyRequestError extends Error {
  constructor(readonly status: number) {
    super("Ingredient copy request failed.");
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredUuid(value: unknown): string {
  if (typeof value !== "string" || !uuid.test(value))
    throw new Error("Invalid ingredient copy response.");
  return value;
}

function nullableUuid(value: unknown): string | null {
  return value === null ? null : requiredUuid(value);
}

function stringValue(value: unknown): string {
  if (typeof value !== "string" || !value) throw new Error("Invalid ingredient copy response.");
  return value;
}

function uuidList(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error("Invalid ingredient copy response.");
  return value.map(requiredUuid);
}

function parseRequirement(value: unknown): IngredientCopyMappingRequirement {
  if (!record(value)) throw new Error("Invalid ingredient copy response.");
  if (
    value.kind !== "canonical_unit" &&
    value.kind !== "default_store_section" &&
    value.kind !== "dietary_tag"
  )
    throw new Error("Invalid ingredient copy response.");
  return {
    kind: value.kind,
    sourceId: requiredUuid(value.source_id),
    seedKey: value.seed_key === null ? null : stringValue(value.seed_key),
  };
}

function parsePreview(value: unknown): IngredientCopyPreview {
  if (!record(value) || !Array.isArray(value.mapping_requirements))
    throw new Error("Invalid ingredient copy response.");
  return {
    sourceOrganizationId: requiredUuid(value.source_organization_id),
    destinationOrganizationId: requiredUuid(value.destination_organization_id),
    sourceIngredientId: requiredUuid(value.source_ingredient_id),
    sourceVersionId: requiredUuid(value.source_version_id),
    sourceName: stringValue(value.source_name),
    canonicalUnitId: requiredUuid(value.canonical_unit_id),
    defaultStoreSectionId: nullableUuid(value.default_store_section_id),
    dietaryTagIds: uuidList(value.dietary_tag_ids),
    preconditionFingerprint: stringValue(value.precondition_fingerprint),
    mappingRequirements: value.mapping_requirements.map(parseRequirement),
  };
}

function parseResult(value: unknown): IngredientCopyResult {
  if (!record(value)) throw new Error("Invalid ingredient copy response.");
  if (typeof value.first_change_sequence !== "number" || !Number.isSafeInteger(value.first_change_sequence) || value.first_change_sequence < 0)
    throw new Error("Invalid ingredient copy response.");
  if (typeof value.last_change_sequence !== "number" || !Number.isSafeInteger(value.last_change_sequence) || value.last_change_sequence < value.first_change_sequence)
    throw new Error("Invalid ingredient copy response.");
  if (typeof value.replayed !== "boolean") throw new Error("Invalid ingredient copy response.");
  return {
    mutationId: requiredUuid(value.mutation_id),
    sourceOrganizationId: requiredUuid(value.source_organization_id),
    destinationOrganizationId: requiredUuid(value.destination_organization_id),
    sourceIngredientId: requiredUuid(value.source_ingredient_id),
    destinationIngredientId: requiredUuid(value.destination_ingredient_id),
    sourceVersionId: requiredUuid(value.source_version_id),
    destinationVersionId: requiredUuid(value.destination_version_id),
    sourceName: stringValue(value.source_name),
    canonicalUnitId: requiredUuid(value.canonical_unit_id),
    defaultStoreSectionId: nullableUuid(value.default_store_section_id),
    dietaryTagIds: uuidList(value.dietary_tag_ids),
    firstChangeSequence: value.first_change_sequence,
    lastChangeSequence: value.last_change_sequence,
    replayed: value.replayed,
  };
}

export async function getIngredientCopyPreview(
  destinationOrganizationId: string,
  sourceOrganizationId: string,
  ingredientId: string,
): Promise<IngredientCopyPreview> {
  const response = await fetch(
    `/api/v1/organizations/${destinationOrganizationId}/ingredient-copy-preview/${sourceOrganizationId}/${ingredientId}`,
    { credentials: "same-origin" },
  );
  if (!response.ok) throw new IngredientCopyRequestError(response.status);
  return parsePreview(await response.json());
}

export async function copyIngredient(
  userId: string,
  destinationOrganizationId: string,
  input: {
    sourceOrganizationId: string;
    ingredientId: string;
    mutationId: string;
    clientWallTime: string;
    preconditionFingerprint: string;
    mappings: IngredientCopyMapping[];
  },
): Promise<IngredientCopyResult> {
  if (!uuid.test(input.mutationId)) throw new Error("Invalid ingredient copy request.");
  const response = await fetch(
    `/api/v1/organizations/${destinationOrganizationId}/ingredient-copy`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        source_organization_id: input.sourceOrganizationId,
        ingredient_id: input.ingredientId,
        client_installation_id: await readOrCreateBrowserInstallationId(userId),
        precondition_fingerprint: input.preconditionFingerprint,
        mappings: input.mappings.map((mapping) => ({
          kind: mapping.kind,
          source_id: mapping.sourceId,
          destination_id: mapping.destinationId,
        })),
        mutation_id: input.mutationId,
        client_wall_time: input.clientWallTime,
      }),
    },
  );
  if (!response.ok) throw new IngredientCopyRequestError(response.status);
  return parseResult(await response.json());
}
