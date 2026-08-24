import { describe, expect, it, vi } from "vitest";

import {
  compatibleUnit,
  readRecipeCopyCatalog,
  readRecipeCopyDestinationCatalog,
} from "./recipe-copy-catalog";
import type { CatalogRecipe } from "./recipe-catalog";

const { readRecipeCatalog } = vi.hoisted(() => ({
  readRecipeCatalog: vi.fn(),
}));
vi.mock("./recipe-catalog", () => ({ readRecipeCatalog }));

const recipe = {
  id: "7ce17d2f-8365-4b1f-a80b-34d10425d51c",
  retired: false,
} as unknown as CatalogRecipe;
const projection = {
  recipes: [recipe],
  scalingUnits: [],
  ingredients: [],
  units: [],
  tags: [],
  costs: {},
};

describe("recipe copy catalog", () => {
  it("uses retired source units only in the source projection", async () => {
    readRecipeCatalog.mockResolvedValue({
      ...projection,
      sourceUnits: [
        { id: "unit", name: "old", dimension: "mass", baseUnitFactor: "1" },
      ],
    });
    const source = await readRecipeCopyCatalog(
      "user",
      "source",
      recipe.id,
      true,
    );
    expect(source.units).toEqual(source.sourceUnits);
    expect(readRecipeCatalog).toHaveBeenLastCalledWith(
      "user",
      "source",
      true,
      false,
    );
    readRecipeCatalog.mockResolvedValue(projection);
    const destination = await readRecipeCopyDestinationCatalog(
      "user",
      "destination",
    );
    expect(destination.units).toEqual([]);
    expect(readRecipeCatalog).toHaveBeenLastCalledWith(
      "user",
      "destination",
      false,
      false,
    );
  });

  it("requires complete canonical compatibility", () => {
    expect(
      compatibleUnit(undefined, {
        id: "x",
        dimension: "mass",
        baseUnitFactor: "1",
      }),
    ).toBe(false);
    expect(
      compatibleUnit(
        { id: "a", dimension: "mass" },
        { id: "b", dimension: "mass", baseUnitFactor: "1" },
      ),
    ).toBe(false);
    expect(
      compatibleUnit(
        { id: "a", dimension: "mass", baseUnitFactor: "1" },
        { id: "b", dimension: "mass", baseUnitFactor: "2" },
      ),
    ).toBe(false);
    expect(
      compatibleUnit(
        { id: "a", dimension: "count" },
        { id: "b", dimension: "count" },
      ),
    ).toBe(false);
    expect(
      compatibleUnit(
        { id: "a", dimension: "custom" },
        { id: "a", dimension: "custom" },
      ),
    ).toBe(true);
  });
});
