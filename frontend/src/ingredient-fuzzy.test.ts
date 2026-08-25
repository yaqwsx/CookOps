import { describe, expect, it } from "vitest";

import { matchesIngredient, rankIngredients } from "./ingredient-fuzzy";

const ingredient = (
  versionId: string,
  name: string,
  flags: Partial<{ retired: boolean; historical: boolean }> = {},
) => ({
  id: versionId,
  versionId,
  name,
  canonicalUnitName: "g",
  massPerCanonicalQuantity: "1",
  ...flags,
});

describe("rankIngredients", () => {
  it("matches adjacent transpositions", () => {
    expect(matchesIngredient("soup", "sopu")).toBe(true);
  });

  it("does not match an empty or whitespace-only query", () => {
    expect(matchesIngredient("soup", "")).toBe(false);
    expect(matchesIngredient("soup", "   ")).toBe(false);
  });

  it("handles adjacent transpositions of astral code points", () => {
    expect(matchesIngredient("🍅🥕", "🥕🍅")).toBe(true);
  });

  it("matches diacritics and ranks exact/prefix before substrings", () => {
    const result = rankIngredients(
      [
        ingredient("00000000-0000-0000-0000-000000000002", "Červená paprika"),
        ingredient("00000000-0000-0000-0000-000000000001", "Paprika"),
      ],
      "paprika",
    );
    expect(result.map(({ name }) => name)).toEqual([
      "Paprika",
      "Červená paprika",
    ]);
  });

  it("accepts valid astral Unicode names", () => {
    const result = rankIngredients(
      [ingredient("00000000-0000-0000-0000-000000000001", "🍅 Tomatoes")],
      "tomatoes",
    );
    expect(result).toHaveLength(1);
  });

  it("offers a near-token typo match and excludes retired history", () => {
    const result = rankIngredients(
      [
        ingredient("00000000-0000-0000-0000-000000000001", "Tomatoes"),
        ingredient("00000000-0000-0000-0000-000000000002", "Old tomatoes", {
          retired: true,
        }),
        ingredient("00000000-0000-0000-0000-000000000003", "Historic tomato", {
          historical: true,
        }),
      ],
      "tomatoe",
    );
    expect(result.map(({ name }) => name)).toEqual(["Tomatoes"]);
  });

  it("uses normalized name then version ID as a stable tie break", () => {
    const result = rankIngredients(
      [
        ingredient("00000000-0000-0000-0000-000000000002", "Álfa"),
        ingredient("00000000-0000-0000-0000-000000000001", "Alfa"),
      ],
      "zzz",
    );
    expect(result.map(({ versionId }) => versionId)).toEqual([
      "00000000-0000-0000-0000-000000000001",
      "00000000-0000-0000-0000-000000000002",
    ]);
  });

  it("fails closed for malformed candidates and deterministically deduplicates roots", () => {
    const validId = "00000000-0000-0000-0000-000000000001";
    const result = rankIngredients(
      [
        {
          ...ingredient("00000000-0000-0000-0000-000000000003", "Zulu"),
          id: validId,
        },
        {
          ...ingredient("00000000-0000-0000-0000-000000000002", "Alpha"),
          id: validId,
        },
        { ...ingredient("bad", "Bad ID") },
        {
          ...ingredient("00000000-0000-0000-0000-000000000003", "Bad root"),
          id: "bad",
        },
        { ...ingredient("00000000-0000-0000-0000-000000000004", "\u0000bad") },
        {
          ...ingredient("00000000-0000-0000-0000-000000000005", "Retired", {
            retired: true,
          }),
        },
      ],
      "",
    );
    expect(result.map(({ name }) => name)).toEqual(["Alpha"]);
  });
});
