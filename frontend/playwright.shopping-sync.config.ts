import { defineConfig } from "@playwright/test";

const baseURL = process.env.COOKOPS_SHOPPING_SYNC_ORIGIN;
if (!baseURL) throw new Error("COOKOPS_SHOPPING_SYNC_ORIGIN is required");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "shopping-sync.e2e.ts",
  workers: 1,
  use: { baseURL, ignoreHTTPSErrors: true },
});
