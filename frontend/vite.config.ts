import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "service-worker.ts",
      registerType: "autoUpdate",
      manifest: {
        name: "CookOps",
        short_name: "CookOps",
        description: "Plan group cooking and collaborative shopping.",
        lang: "cs",
        start_url: "/",
        display: "standalone",
        background_color: "#fffaf2",
        theme_color: "#254a36",
      },
    }),
  ],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
});
