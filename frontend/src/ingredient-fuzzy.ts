import type { CatalogIngredient } from "./ingredient-catalog";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const maxSearchLength = 256;

export function normalizeIngredientSearch(value: string): string {
  return value
    .slice(0, maxSearchLength)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim();
}

function editDistance(left: string, right: string): number {
  const leftCharacters = [...left];
  const rightCharacters = [...right];
  const previous = Array.from(
    { length: rightCharacters.length + 1 },
    (_, index) => index,
  );
  for (let row = 1; row <= leftCharacters.length; row += 1) {
    let diagonal = previous[0];
    previous[0] = row;
    for (let column = 1; column <= rightCharacters.length; column += 1) {
      const above = previous[column];
      previous[column] =
        leftCharacters[row - 1] === rightCharacters[column - 1]
          ? diagonal
          : 1 + Math.min(diagonal, above, previous[column - 1]);
      diagonal = above;
    }
  }
  return previous[rightCharacters.length];
}

function validCandidate(
  value: unknown,
  includeRetired = false,
): value is CatalogIngredient {
  if (!value || typeof value !== "object") return false;
  const candidate = value as CatalogIngredient & Record<string, unknown>;
  if (
    !uuid.test(candidate.id) ||
    !uuid.test(candidate.versionId) ||
    typeof candidate.name !== "string" ||
    candidate.name.length > maxSearchLength ||
    candidate.name.includes("\u0000") ||
    (!includeRetired && candidate.retired === true) ||
    candidate.historical === true
  )
    return false;
  if (
    "lifecycle" in candidate &&
    candidate.lifecycle !== "active" &&
    !(includeRetired && candidate.lifecycle === "retired")
  )
    return false;
  for (let index = 0; index < candidate.name.length; index += 1) {
    const code = candidate.name.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = candidate.name.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return false;
    }
  }
  return true;
}

function score(name: string, query: string): number {
  if (!query) return 0;
  if (name === query) return 0;
  if (name.startsWith(query)) return 10;
  const tokens = name.split(/\s+/);
  if (tokens.some((token) => token.startsWith(query))) return 20;
  if (name.includes(query)) return 30;
  if (query.length < 3) return 100;
  const nameCharacters = [...name];
  const queryCharacters = [...query];
  if (nameCharacters.length === queryCharacters.length) {
    const differences = nameCharacters.flatMap((character, index) =>
      character === queryCharacters[index] ? [] : [index],
    );
    if (
      differences.length === 2 &&
      differences[1] === differences[0] + 1 &&
      nameCharacters[differences[0]] === queryCharacters[differences[1]] &&
      nameCharacters[differences[1]] === queryCharacters[differences[0]]
    )
      return 40;
  }
  const nearest = tokens.reduce(
    (best, token) => Math.min(best, editDistance(token, query)),
    Number.POSITIVE_INFINITY,
  );
  return nearest <= Math.max(1, Math.floor(query.length / 4))
    ? 40 + nearest
    : 100;
}

export function matchesIngredient(name: string, query: string): boolean {
  const normalizedQuery = normalizeIngredientSearch(query);
  return (
    normalizedQuery !== "" &&
    score(normalizeIngredientSearch(name), normalizedQuery) < 100
  );
}

/** Rank only active current ingredient versions; callers may keep a historic selection separately. */
export function rankIngredients(
  ingredients: CatalogIngredient[],
  query: string,
  includeRetired = false,
): CatalogIngredient[] {
  const normalizedQuery = normalizeIngredientSearch(query);
  const unique = new Map<string, CatalogIngredient>();
  for (const ingredient of ingredients) {
    if (!validCandidate(ingredient, includeRetired)) continue;
    const current = unique.get(ingredient.id);
    if (!current || ingredient.versionId.localeCompare(current.versionId) < 0)
      unique.set(ingredient.id, ingredient);
  }
  return [...unique.values()]
    .slice(0, 200)
    .map((ingredient) => ({
      ingredient,
      normalizedName: normalizeIngredientSearch(ingredient.name),
    }))
    .sort(
      (left, right) =>
        score(left.normalizedName, normalizedQuery) -
          score(right.normalizedName, normalizedQuery) ||
        left.normalizedName.localeCompare(right.normalizedName) ||
        left.ingredient.versionId.localeCompare(right.ingredient.versionId),
    )
    .map(({ ingredient }) => ingredient);
}
