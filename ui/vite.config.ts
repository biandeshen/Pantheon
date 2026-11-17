import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vitest/config";

// The engine (Python sidecar). In a plain browser on this dev server, /api is proxied to
// it; inside the desktop app the UI calls it directly (src/lib/engine.ts).
const ENGINE = process.env.PANTHEON_ENGINE_URL ?? "http://127.0.0.1:8720";
const shim = (name: string) => path.resolve(__dirname, "src/shims", name);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  clearScreen: false, // keep Tauri's and the engine's output visible
  resolve: {
    alias: [
      // v1 components import Next.js APIs; these map them onto React Router.
      { find: /^next\/link$/, replacement: shim("next-link.tsx") },
      { find: /^next\/navigation$/, replacement: shim("next-navigation.ts") },
      { find: /^next\/dynamic$/, replacement: shim("next-dynamic.tsx") },
      { find: /^@\//, replacement: `${path.resolve(__dirname, "src")}/` },
    ],
  },
  server: {
    port: 1420,
    strictPort: true, // Tauri's devUrl points here
    // Bind IPv4 loopback explicitly. Without this, Vite binds only `localhost`,
    // which on Windows resolves to ::1 -- so http://127.0.0.1:1420/ is refused.
    // The Workbench links to 127.0.0.1:1420 on purpose (local-only contract),
    // so the two sides must agree. 127.0.0.1 (not 0.0.0.0) keeps it loopback-only.
    host: "127.0.0.1",
    proxy: { "/api": { target: ENGINE, ws: true, changeOrigin: false } },
  },
  build: { target: "es2022", chunkSizeWarningLimit: 4000 },
  test: { include: ["src/**/*.test.ts"] },
});
