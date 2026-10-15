// The whole customer journey in the browser, against the real backend (design §16.1, M5):
// sign in, create a project and site, connect a GPU server with the install command the console
// shows, register contracts, check compatibility, add robots, create a release, prepare it at the
// site, request activation, approve it at the site, and watch the robots run it. Every main screen
// is checked for accessibility (WCAG 2.1 AA).
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

import { REPO, stackState } from "./global-setup";
import { Site } from "./site";

const CONTRACTS = path.join(REPO, "examples", "contracts");
const ROBOTS = 2;

async function expectAccessible(page: Page, screen: string): Promise<void> {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  // NEURA_E2E_SCREENSHOTS=<dir> keeps a full-page screenshot of every checked screen.
  const shots = process.env.NEURA_E2E_SCREENSHOTS;
  if (shots) {
    await page.screenshot({
      path: path.join(shots, `${screen.replaceAll(" ", "-")}.png`),
      fullPage: true,
    });
  }
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const serious = results.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
  expect(
    serious.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).join(", ")})`),
    `accessibility violations on ${screen}`,
  ).toEqual([]);
}

/** Register a contract document through the console's dialog, as a file upload. */
async function register(page: Page, button: string, file: string): Promise<void> {
  await page.getByRole("button", { name: button }).click();
  const dialog = page.getByRole("dialog", { name: /^Register an? / });
  await dialog.getByLabel("File", { exact: true }).setInputFiles(path.join(CONTRACTS, file));
  await expect(dialog.getByLabel("Document", { exact: true })).not.toHaveValue("");
  await dialog.getByRole("button", { name: "Register", exact: true }).click();
}

async function nav(page: Page, name: string): Promise<void> {
  await page
    .getByRole("navigation", { name: "Project" })
    .getByRole("link", { name, exact: true })
    .click();
}

test.describe.configure({ mode: "serial" });

test("onboard, connect a server, deploy and approve at the site", async ({ page }) => {
  const site = new Site(ROBOTS);
  try {
    // --- sign in and create the first project ----------------------------------------------
    await page.goto("/");
    await expect(page).toHaveURL(/\/sign-in$/);
    await expectAccessible(page, "sign in");
    await page.getByLabel("API token").fill(stackState().token);
    await page.getByRole("button", { name: "Sign in with token" }).click();
    await expect(page.getByRole("heading", { name: "Welcome to Neura" })).toBeVisible();
    await expectAccessible(page, "welcome");
    await page.getByLabel("Project name").fill("Packing line");
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(page.getByRole("heading", { name: "Packing line" })).toBeVisible();
    await expect(page.getByText("0 of 6 steps done")).toBeVisible();
    await expectAccessible(page, "home");

    // --- add a site and connect its GPU server ------------------------------------------------
    await page.getByRole("link", { name: "Add a site" }).click();
    await page.getByRole("button", { name: "Add a site" }).click();
    const dialog = page.getByRole("dialog", { name: "Add a site" });
    await dialog.getByLabel("Site name").fill("Warehouse 3");
    await dialog.getByRole("button", { name: "Add site" }).click();
    await expect(page.getByRole("heading", { name: "Warehouse 3" })).toBeVisible();
    await expect(page.getByText("The site has never connected")).toBeVisible();
    await page.getByRole("button", { name: "Generate install command" }).click();
    const command = (await page.getByLabel("Install command").textContent()) ?? "";
    expect(command).toMatch(/^neura-agent enroll --control-plane \S+ --token \S+$/);
    await expectAccessible(page, "connect a server");
    expect(site.enroll(command)).toContain("enrolled server");
    await expect(page.getByText(/enrolled$/)).toBeVisible();
    await expect(page.getByText("Every preflight check passed.")).toBeVisible();
    await expect(page.locator(".badge", { hasText: "Ready" }).first()).toBeVisible();

    // --- register contracts and check compatibility -------------------------------------------
    await nav(page, "Models");
    await register(page, "Register robot profile", "so101.robot.yaml");
    await expect(page.getByRole("link", { name: "so101-follower r1" })).toBeVisible();
    await register(page, "Register model artifact", "act-so101.artifact.yaml");
    await expect(page.getByRole("link", { name: "act-so101 example-1" })).toBeVisible();
    // A wrong kind is refused before anything is sent.
    await nav(page, "Execution profiles");
    await register(page, "Register execution profile", "so101.robot.yaml");
    await expect(
      page.getByText("This is a robot profile, not an execution profile."),
    ).toBeVisible();
    const profileDialog = page.getByRole("dialog", { name: "Register an execution profile" });
    await profileDialog
      .getByLabel("File", { exact: true })
      .setInputFiles(path.join(CONTRACTS, "act-so101-30hz.profile.yaml"));
    await profileDialog.getByRole("button", { name: "Register", exact: true }).click();
    await expect(page.getByRole("link", { name: "act-so101-30hz example-1" })).toBeVisible();
    await expectAccessible(page, "execution profiles");
    await nav(page, "Models");
    await expect(
      page.getByRole("table", { name: /Compatibility/ }).getByText("Compatible"),
    ).toBeVisible();
    await expectAccessible(page, "models");

    // --- robots -------------------------------------------------------------------------------
    await nav(page, "Robots");
    await page.getByRole("button", { name: "Add robots" }).click();
    const add = page.getByRole("dialog", { name: "Add robots" });
    await add.getByLabel("Name").fill("Arm");
    await add.getByLabel("How many").fill(String(ROBOTS));
    await add.getByRole("button", { name: "Add", exact: true }).click();
    await expect(page.getByRole("link", { name: "Arm 0" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Arm 1" })).toBeVisible();
    await expectAccessible(page, "robots");
    site.startRobots();

    // --- release: revision, prepare at the site ------------------------------------------------
    await nav(page, "Releases");
    await page.getByRole("button", { name: "New release" }).click();
    const release = page.getByRole("dialog", { name: "New release" });
    await release.getByLabel("Name").fill("Pilot");
    await release.getByRole("button", { name: "Create release" }).click();
    await expect(page.getByRole("heading", { name: "Pilot" })).toBeVisible();
    await page.getByRole("button", { name: "New revision" }).click();
    await page
      .getByRole("dialog", { name: "New revision" })
      .getByRole("button", { name: "Create revision" })
      .click();
    await expect(page.getByText("Compatible", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Prepare at site" }).click();
    await expect(page.getByText("Preparing at the site: Succeeded", { exact: true })).toBeVisible();
    await expect(page.getByText(/robots per batch group/)).toBeVisible();
    await expectAccessible(page, "release");

    // --- activation: requested in the cloud, approved at the site --------------------------------
    await page.getByRole("button", { name: "Request activation" }).click();
    const activation = page.getByRole("dialog", { name: "Request activation" });
    await activation.getByLabel("Note for the site operator").fill("first deployment");
    await activation.getByRole("button", { name: "Request activation" }).click();
    await expect(page.getByText("Waiting for approval at the site", { exact: true })).toBeVisible();
    await expect(page.getByText("Awaiting local approval", { exact: true })).toBeVisible();

    await expect.poll(() => site.pendingApprovals().length, { timeout: 30_000 }).toBe(1);
    const [pending] = site.pendingApprovals();
    expect(pending?.robot_names.sort()).toEqual(["arm-0", "arm-1"]);
    site.approve(pending?.command_id ?? "", "ops@warehouse");

    await expect(page.getByText("Activation: Succeeded", { exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByRole("cell", { name: "ops@warehouse" })).toBeVisible();
    await expect(page.getByText("Activated", { exact: true })).toBeVisible();

    // --- live: robots running the revision -----------------------------------------------------
    await nav(page, "Live sessions");
    await expect(page.getByRole("table", { name: "Live sessions" }).getByRole("row")).toHaveCount(
      ROBOTS + 1,
      {
        timeout: 60_000,
      },
    );
    await expectAccessible(page, "live sessions");
    await nav(page, "Robots");
    await expect(page.locator(".badge", { hasText: "Running" })).toHaveCount(ROBOTS, {
      timeout: 60_000,
    });
    await nav(page, "Home");
    await expect(page.getByRole("group", { name: "Robots running" })).toContainText(
      `${ROBOTS} / ${ROBOTS}`,
    );
    await expect(page.getByRole("group", { name: "Sites connected" })).toContainText("1 / 1");

    // --- capacity and audit --------------------------------------------------------------------
    await nav(page, "Capacity");
    await expect(page.getByText("Configured latency profile")).toBeVisible();
    await expectAccessible(page, "capacity");
    await nav(page, "Audit log");
    const audit = page.getByRole("table", { name: "Audit records" });
    for (const action of [
      "server.enroll",
      "robot_profile.register",
      "operation.activate_revision",
    ]) {
      await expect(audit.getByRole("cell", { name: action, exact: true })).toBeVisible();
    }
    await expectAccessible(page, "audit log");
  } finally {
    site.stop();
  }
});

test("administer the organization: members and API tokens", async ({ page }) => {
  await page.goto("/sign-in");
  await page.getByLabel("API token").fill(stackState().token);
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Organization settings" })).toBeVisible();

  await page.getByRole("button", { name: "Invite" }).click();
  const invite = page.getByRole("dialog", { name: "Invite a member" });
  await invite.getByLabel("Email").fill("ana@customer.example");
  await invite.getByLabel(/Deployer/).check();
  await invite.getByRole("button", { name: "Send invitation" }).click();
  await expect(page.getByRole("cell", { name: "ana@customer.example", exact: true })).toBeVisible();
  await expect(page.getByText("Invited", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Create token" }).click();
  const token = page.getByRole("dialog", { name: "Create an API token" });
  await token.getByLabel("Name").fill("ci-releases");
  await token.getByLabel("Access").selectOption("deploy");
  await token.getByRole("button", { name: "Create" }).click();
  const created = page.getByRole("dialog", { name: "Token created" });
  await expect(created.getByLabel("API token")).toHaveText(/^ntk_tok_/);
  await created.getByRole("button", { name: "Done" }).click();
  await expect(page.getByRole("row", { name: /ci-releases/ })).toBeVisible();
  await expectAccessible(page, "settings");
});
