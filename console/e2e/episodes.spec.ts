// The site's episode viewer (served by the site agent, not the console) in Chromium: sign in with
// the operator credential, open an episode, step through its camera frames and commands, and
// label it. The page must load only itself and pass the same accessibility checks as the console.
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";

import { REPO } from "./global-setup";

const PORT = 18911;
const VIEWER = `http://127.0.0.1:${PORT}/episodes`;
const TOKEN = "ntop_" + "v".repeat(43);

let site: ChildProcess | undefined;

test.beforeAll(async () => {
  site = spawn("uv", ["run", "python", "console/e2e/episodes_site.py", String(PORT)], {
    cwd: REPO,
    stdio: ["ignore", "pipe", "inherit"],
  });
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("the site did not start in 60 s")), 60_000);
    site?.stdout?.on("data", (chunk: Buffer) => {
      if (chunk.toString().includes("READY")) {
        clearTimeout(timer);
        resolve();
      }
    });
    site?.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`the site exited with ${String(code)}`));
    });
  });
});

test.afterAll(async () => {
  if (!site || site.exitCode !== null) return;
  const exited = new Promise((resolve) => site?.on("exit", resolve));
  site.kill("SIGTERM");
  await exited; // it removes its episodes as it exits
});

test("the site's operator watches and labels a recorded episode", async ({ page }) => {
  const problems: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(message.text());
  });
  page.on("pageerror", (error) => problems.push(String(error)));
  // The server may take a moment to bind after printing READY.
  await expect.poll(async () => (await page.request.get(VIEWER)).status()).toBe(200);
  await page.goto(VIEWER);
  await page.getByLabel(/^Credential/).fill(TOKEN);
  await page.getByLabel("Your name, for the labels you set").fill("ops@site");
  await page.getByRole("button", { name: "Open", exact: true }).click();
  await expect(page.locator("#summary")).toContainText("2 episodes at this site");
  await expect(page.locator("#summary")).toContainText("1 failure");

  await page
    .getByRole("button", { name: /^Open episode/ })
    .first()
    .click();
  const front = page.getByRole("img", { name: /^front camera, frame 1 of 30/ });
  await expect(front).toBeVisible();
  await expect(page.locator("#traces figure")).toHaveCount(6);
  await page.locator("#step").fill("60");
  await expect(page.getByText(/^61 of 90/)).toBeVisible();
  await expect(page.getByRole("img", { name: /^front camera, frame 21 of 30/ })).toBeVisible();

  await page.getByLabel("Success").check();
  await page.getByLabel("Note").fill("placed on the second try");
  await page.getByRole("button", { name: "Save label" }).click();
  await expect(page.getByText("Label saved.")).toBeVisible();
  await expect(page.locator("#detail-meta")).toContainText("outcome: success (operator)");
  await expect(page.locator("#summary")).toHaveText("2 episodes at this site: 2 success.");

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const serious = results.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
  expect(serious.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  expect(problems).toEqual([]); // no script error, no content-security-policy violation
});

test("a wrong credential opens nothing", async ({ page }) => {
  await page.goto(VIEWER);
  await page.getByLabel(/^Credential/).fill("wrong");
  await page.getByLabel("Your name, for the labels you set").fill("ops@site");
  await page.getByRole("button", { name: "Open", exact: true }).click();
  await expect(page.getByText("The operator credential was not accepted.")).toBeVisible();
  await expect(page.getByRole("table")).toBeHidden();
});
