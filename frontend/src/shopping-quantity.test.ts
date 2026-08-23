import { describe, expect, it } from "vitest";

import { formatShoppingQuantity } from "./shopping-quantity";

describe("shopping quantity presentation", () => {
  it("localizes separators while preserving precision", () => {
    expect(formatShoppingQuantity("1234567.5000", "kg", "cs")).toBe(
      "1 234 567,5000 kg",
    );
    expect(formatShoppingQuantity("1234567.5000", "kg", "en")).toBe(
      "1,234,567.5000 kg",
    );
    expect(formatShoppingQuantity("9007199254740993", "ks", "en")).toBe(
      "9,007,199,254,740,993 ks",
    );
  });

  it("keeps malformed quantities unchanged", () => {
    expect(formatShoppingQuantity("01", "kg", "cs")).toBe("01 kg");
    expect(formatShoppingQuantity("1e2", "kg", "cs")).toBe("1e2 kg");
  });
});
