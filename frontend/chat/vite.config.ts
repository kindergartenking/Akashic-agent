import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { emitThemeCatalog } from "../theme/src/vite-theme-plugin";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");

export default defineConfig(({ command }) => ({
  root: here,
  // The built app is mounted by the runtime at /assets/, while Vite's
  // standalone development server serves the SPA from its own origin.
  // Keeping the two bases separate makes /settings and /chat work directly
  // during development without changing production asset URLs.
  base: command === "serve" ? "/" : "/assets/",
  plugins: [react(), emitThemeCatalog()],
  resolve: {
    alias: {
      "@": resolve(here, "src"),
    },
  },
  build: {
    outDir: resolve(repoRoot, "static", "chat"),
    emptyOutDir: true,
    assetsDir: "",
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "[name]-[hash].js",
        chunkFileNames: "[name]-[hash].js",
        assetFileNames: "[name]-[hash][extname]",
      },
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:2236",
      "/ws": {
        target: "ws://127.0.0.1:2236",
        ws: true,
      },
    },
  },
}));
