/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The console talks only to the public API (design §19.1). In development and in the end-to-end
// tests the API is proxied on the same origin, as it is in production behind the console's server.
const api = process.env.NEURA_API_URL ?? "http://127.0.0.1:8080";
const proxy = { "/v1": { target: api, changeOrigin: false } };

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy },
  preview: { port: 4173, strictPort: true, proxy },
  build: { sourcemap: true, target: "es2023" },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
