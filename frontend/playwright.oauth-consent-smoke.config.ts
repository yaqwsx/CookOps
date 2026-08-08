import { defineConfig } from "@playwright/test";

const baseURL = process.env.COOKOPS_OAUTH_SMOKE_ORIGIN;
if (!baseURL) throw new Error("COOKOPS_OAUTH_SMOKE_ORIGIN is required");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "oauth-consent-smoke.e2e.ts",
  workers: 1,
  use: { baseURL, ignoreHTTPSErrors: true },
});
