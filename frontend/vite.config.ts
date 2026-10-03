/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API runs on its own port in development (python scripts/serve.py); the dev server
// proxies to it, so the browser talks to one origin and no CORS setting is needed. In
// production (M19) the static build and the API sit behind the same origin.
const env = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env;
const API = env?.VITE_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": API,
      "/auth": API,
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/test/**", "src/main.tsx", "src/api/schema.d.ts"],
      thresholds: { statements: 85, branches: 85, functions: 85, lines: 85 },
    },
  },
});
