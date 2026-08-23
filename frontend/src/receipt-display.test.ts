import { describe, expect, it } from "vitest";

import {
  formatReceiptAmount,
  formatReceiptDate,
  isReceiptDate,
} from "./receipt-display";

describe("receipt detail presentation", () => {
  it("formats valid dates and amounts for the selected locale", () => {
    expect(isReceiptDate("2026-08-07")).toBe(true);
    expect(formatReceiptDate("2026-08-07", "cs")).toContain("srpna");
    expect(formatReceiptAmount("12.50", "CZK", "cs")).toContain("12,50");
    expect(formatReceiptAmount("12.50", "CZK", "en")).toContain("12.50");
    expect(formatReceiptAmount("9007199254740993", "CZK", "en")).toContain("9,007,199,254,740,993");
    expect(formatReceiptAmount("12.5000", "CZK", "en")).toContain("12.5000");
  });

  it("keeps invalid metadata as raw text", () => {
    expect(isReceiptDate("2026-02-30")).toBe(false);
    expect(formatReceiptDate("2026-02-30", "cs")).toBe("2026-02-30");
    expect(formatReceiptAmount("01", "CZK", "cs")).toBe("01 CZK");
    expect(formatReceiptAmount("12.50", "ZZ", "cs")).toBe("12.50 ZZ");
  });
});
