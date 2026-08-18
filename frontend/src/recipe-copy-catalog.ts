import { readRecipeCatalog, type CatalogRecipe, type RecipeCatalogProjection } from "./recipe-catalog";

export type RecipeCopyCatalog = RecipeCatalogProjection & { recipe: CatalogRecipe };

type UnitMetadata = { id: string; dimension?: string; baseUnitFactor?: string };

/** Copy mappings require complete, canonical-compatible unit metadata. */
export function compatibleUnit(source: UnitMetadata | undefined, destination: UnitMetadata | undefined): boolean {
  if (!source || !destination || !source.dimension || !destination.dimension) return false;
  if (source.dimension !== destination.dimension) return false;
  if (source.dimension === "count" || source.dimension === "custom") return source.id === destination.id;
  return Boolean(source.baseUnitFactor && destination.baseUnitFactor && source.baseUnitFactor === destination.baseUnitFactor);
}

export async function readRecipeCopyCatalog(
  userId: string,
  organizationId: string,
  recipeId: string,
  source = false,
): Promise<RecipeCopyCatalog> {
  const catalog = await readRecipeCatalog(userId, organizationId, source, false);
  const recipe = catalog.recipes.find((item) => item.id === recipeId);
  if (!recipe || recipe.retired) throw new Error("Recipe copy source is unavailable.");
  return { ...catalog, units: source ? catalog.sourceUnits ?? catalog.units : catalog.units, recipe };
}

export async function readRecipeCopyDestinationCatalog(userId: string, organizationId: string) {
  return readRecipeCatalog(userId, organizationId, false, false);
}

export function normalized(value: string): string {
  return value.normalize("NFC").trim().toLocaleLowerCase();
}

export function matchingIds(values: { id: string; name: string }[], name: string): string[] {
  const target = normalized(name);
  return values.filter((value) => normalized(value.name) === target).map((value) => value.id);
}
