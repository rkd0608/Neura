import { defineConfig, devices } from "@playwright/test";

// End-to-end tests drive the real console against the real backend: control plane, site agent,
// gateway and simulated robots (see e2e/stack.py and e2e/site.ts).
export default defineConfig({
  testDir: "e2e",
  timeout: 240_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "pnpm run build && pnpm exec vite preview --port 4173 --strictPort",
    url: "http://127.0.0.1:4173/sign-in",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { NEURA_API_URL: "http://127.0.0.1:18080" },
  },
});
