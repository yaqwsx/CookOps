import { expect, it } from "vitest";

import { timestampNanoseconds } from "./timestamp";

it("adds fractional digits exactly once for UTC and offset timestamps", () => {
  expect(timestampNanoseconds("2026-01-01T00:00:00.000001Z")).toBe(1767225600000001000n);
  expect(timestampNanoseconds("2026-01-01T00:00:00.123456789+02:00")).toBe(1767218400123456789n);
});

it("rejects fractions longer than nanoseconds", () => {
  expect(timestampNanoseconds("2026-01-01T00:00:00.1234567890Z")).toBeUndefined();
});
