/// <reference types="vitest/config" />
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const devTarget = process.env.CODE_REVIEW_MCP_DEV_TARGET ?? "http://127.0.0.1:7790";

// The daemon rejects a state-changing request whose Origin is not its own, so the
// dev proxy presents the daemon's origin.
const proxyToDaemon = { target: devTarget, changeOrigin: true, headers: { origin: devTarget } };

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/static/" : "/",
  plugins: [react()],
  build: {
    outDir: fileURLToPath(new URL("../src/code_review_mcp/static", import.meta.url)),
    emptyOutDir: true,
    // Stable names keep a UI change from renaming every chunk that imports the entry
    // chunk. The daemon serves /static with no-cache, so browsers revalidate instead.
    rolldownOptions: {
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
  server: {
    proxy: { "/api": proxyToDaemon, "/mcp": proxyToDaemon },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
}));
