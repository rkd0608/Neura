// The whole customer journey in the browser, against the real backend (design §16.1, M5):
// sign in, create a project and site, connect a GPU server with the install command the console
// shows, register contracts, check compatibility, add robots, create a deployment, prepare it at
// the site, request activation, approve it at the site, watch the robots run it, gate releases on
// evidence and shadow a candidate on their live observations. Every main screen is checked for
// accessibility (WCAG 2.1 AA).
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { REPO, stackState } from "./global-setup";
import { Site } from "./site";

const CONTRACTS = path.join(REPO, "examples", "contracts");
const ROBOTS = 2;

async function expectAccessible(page: Page, screen: string): Promise<void> {
  // Pages that follow live state never go quiet for long: wait a little, then check.
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined);
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
async function register(
  page: Page,
  button: string,
  file: string,
  signature?: string,
): Promise<void> {
  await page.getByRole("button", { name: button }).click();
  const dialog = page.getByRole("dialog", { name: /^Register an? / });
  await dialog.getByLabel("File", { exact: true }).setInputFiles(path.join(CONTRACTS, file));
  await expect(dialog.getByLabel("Document", { exact: true })).not.toHaveValue("");
  if (signature) await dialog.getByLabel("Publisher signatures (optional)").fill(signature);
  await dialog.getByRole("button", { name: "Register", exact: true }).click();
}

const SIDEBAR = new Set(["Fleet", "Policies", "Rollouts"]);

/** The four pages are in the sidebar; every other page is one search away (⌘K). */
async function nav(page: Page, name: string): Promise<void> {
  if (SIDEBAR.has(name)) {
    await page
      .getByRole("navigation", { name: "Project" })
      .getByRole("link", { name, exact: true })
      .click();
    return;
  }
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByRole("combobox", { name: /^Search pages/ }).fill(name);
  await page
    .getByRole("option", { name: new RegExp(`^${name}`) })
    .first()
    .click();
}

async function tab(page: Page, name: string): Promise<void> {
  await page.getByRole("tab", { name: new RegExp(`^${name}`) }).click();
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
    await expect(page.getByRole("heading", { name: "Fleet" })).toBeVisible();
    await expectAccessible(page, "fleet, first run");
    await nav(page, "Overview");
    await expect(page.getByRole("heading", { name: "Packing line" })).toBeVisible();
    await expect(page.getByText("0 of 6 steps done")).toBeVisible();
    await expectAccessible(page, "home");

    // --- add a site and connect its GPU server ------------------------------------------------
    await page.getByRole("link", { name: "Add a site" }).click();
    await page.getByRole("button", { name: "Add a site" }).first().click();
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
    // Updates are merge patches with the version read: renaming the site saves.
    await page.getByRole("button", { name: "Edit site" }).click();
    const edit = page.getByRole("dialog", { name: "Edit site" });
    await edit.getByLabel("Site name").fill("Warehouse 3 north");
    await edit.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("heading", { name: "Warehouse 3 north" })).toBeVisible();

    // --- register contracts and check compatibility -------------------------------------------
    await nav(page, "Models & contracts");
    await register(page, "Register robot profile", "so101.robot.yaml");
    await tab(page, "Robot profiles");
    await expect(page.getByRole("link", { name: "so101-follower r1" })).toBeVisible();
    // Signed by the customer's release key, which the site operator trusts on the server.
    const signature = site.publish(path.join(CONTRACTS, "act-so101.artifact.yaml"));
    await register(page, "Register model artifact", "act-so101.artifact.yaml", signature);
    await tab(page, "Model artifacts");
    await expect(page.getByRole("link", { name: "act-so101 example-1" })).toBeVisible();
    await expect(page.locator(".badge", { hasText: /^Signed$/ })).toBeVisible();
    // A wrong kind is refused before anything is sent.
    await tab(page, "Execution profiles");
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
    await tab(page, "Compatibility");
    await expect(
      page.getByRole("table", { name: /Compatibility/ }).getByText("Compatible"),
    ).toBeVisible();
    await expectAccessible(page, "models");

    // --- robots -------------------------------------------------------------------------------
    await nav(page, "Robots");
    await page.getByRole("button", { name: "Add robots" }).first().click();
    const add = page.getByRole("dialog", { name: "Add robots" });
    await add.getByLabel("Name").fill("Arm");
    await add.getByLabel("How many").fill(String(ROBOTS));
    await add.getByRole("button", { name: "Add", exact: true }).click();
    await expect(page.getByRole("link", { name: "Arm 0" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Arm 1" })).toBeVisible();
    await expectAccessible(page, "robots");
    site.startRobots();

    // --- deployment: revision, prepare at the site ---------------------------------------------
    await nav(page, "Deployments");
    await page.getByRole("button", { name: "New deployment" }).first().click();
    const release = page.getByRole("dialog", { name: "New deployment" });
    await release.getByLabel("Name").fill("Pilot");
    await release.getByRole("button", { name: "Create deployment" }).click();
    await expect(page.getByRole("heading", { name: "Pilot" })).toBeVisible();
    await page.getByRole("button", { name: "New revision" }).first().click();
    await page
      .getByRole("dialog", { name: "New revision" })
      .getByRole("button", { name: "Create revision" })
      .click();
    await tab(page, "Revisions");
    await expect(page.getByText("Compatible", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Prepare at site" }).click();
    await expect(page.getByText("Preparing at the site: Succeeded", { exact: true })).toBeVisible();
    await expect(page.getByText(/robots per batch group/)).toBeVisible();
    await expectAccessible(page, "deployment");

    // --- activation: requested in the cloud, approved at the site --------------------------------
    await page.getByRole("button", { name: "Request activation" }).click();
    const activation = page.getByRole("dialog", { name: "Request activation" });
    await activation.getByLabel("Note for the site operator").fill("first deployment");
    await activation.getByRole("button", { name: "Request activation" }).click();
    await expect(page.getByText("Waiting for approval at the site", { exact: true })).toBeVisible();
    await tab(page, "Activations");
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
    await nav(page, "Overview");
    await expect(
      page.getByRole("main").getByRole("link", { name: /Robots running/ }),
    ).toContainText(`${ROBOTS} / ${ROBOTS}`);
    await expect(page.getByRole("main").getByRole("link", { name: /Live sessions/ })).toContainText(
      "on 1 of 1 connected sites",
    );
    await expectAccessible(page, "overview");

    // --- improve: a release gate, and a shadow on the robots' live observations ---------------
    await nav(page, "Deployments");
    await page.getByRole("main").getByRole("link", { name: "Pilot", exact: true }).click();
    await tab(page, "Improve");
    await expect(page.getByRole("heading", { name: "Recorded episodes" })).toBeVisible();
    await page
      .getByLabel("A shadow on live observations, close to the running revision's actions")
      .check();
    await expect(page.getByText("Release gates saved.")).toBeVisible();
    await page.getByRole("button", { name: "Evaluate", exact: true }).click();
    const evaluate = page.getByRole("dialog", { name: "Evaluate a candidate" });
    await evaluate.getByLabel("Suite").selectOption("shadow");
    await evaluate.getByLabel("Against").selectOption({ index: 1 }); // the revision the robots run
    await evaluate.getByLabel("Minutes").fill("0.2");
    await evaluate.getByLabel("Observations needed").fill("3");
    await evaluate.getByRole("button", { name: "Start evaluation" }).click();
    const evaluations = page.getByRole("region", { name: "Evaluations" });
    await expect(evaluations.getByText("Shadow")).toBeVisible();
    // The same model as its own candidate: its actions are the running model's.
    await expect(evaluations.getByText("No worse")).toBeVisible({ timeout: 90_000 });
    await expect(evaluations.getByText(/0\.0%/)).toBeVisible();
    await expectAccessible(page, "improve");

    // --- capacity and audit --------------------------------------------------------------------
    await nav(page, "Capacity");
    await expect(page.getByText("Configured latency profile")).toBeVisible();
    await expectAccessible(page, "capacity");
    await nav(page, "Activity");
    await tab(page, "Audit log");
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

  await page.getByRole("tab", { name: "API tokens" }).click();
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

const HOLD = `
import numpy as np

import neura


@neura.policy(chunk_length=10, cameras={})
class Hold:
    """Holds the arm where it is."""

    def act(self, observation):
        return np.tile(observation["observation.state"], (10, 1))
`;

test("the four pages: a site finds its arm, a pushed policy is deployed and approved at the site", async ({
  page,
}) => {
  // A production site whose server finds a simulated arm by itself (design §29.5).
  const site = new Site(0, 1);
  const work = mkdtempSync(path.join(tmpdir(), "neura-e2e-policy-"));
  try {
    await page.goto("/sign-in");
    await page.getByLabel("API token").fill(stackState().token);
    await page.getByRole("button", { name: "Sign in with token" }).click();
    const fleet = page.getByRole("heading", { name: "Fleet" });
    const welcome = page.getByRole("heading", { name: "Welcome to Neura" });
    await expect(fleet.or(welcome)).toBeVisible();
    if (await welcome.isVisible()) {
      await page.getByLabel("Project name").fill("Lab");
      await page.getByRole("button", { name: "Create project" }).click();
    }
    await expect(fleet).toBeVisible();

    // --- Fleet: a site, and the server that joins it ------------------------------------------
    await page
      .getByRole("button", { name: /^(Add|Create) a site$/ })
      .first()
      .click();
    const create = page.getByRole("dialog", { name: "Create a site" });
    await create.getByLabel("Name").fill("Lab line");
    await create.getByLabel(/^Production/).check();
    await create.getByRole("button", { name: "Create site" }).click();
    const card = page.getByRole("region", { name: /lab-line/ });
    await expect(card.getByText("No server yet", { exact: true })).toBeVisible();
    await card.getByRole("button", { name: "Add a server" }).click();
    const connect = page.getByRole("dialog", { name: "Add a server to lab-line" });
    await connect.getByRole("button", { name: "Generate the commands" }).click();
    await expect(connect.getByLabel("A workstation or bench")).toHaveText(
      /^neura site join lab-line --control-plane \S+ --token \S+$/,
    );
    // This stack offers no agent release, so a production server's command is the enroll one.
    const command = (await connect.getByLabel("A production server").textContent()) ?? "";
    await connect.getByRole("button", { name: "Done" }).click();
    expect(site.enroll(command)).toContain("enrolled server");
    await expect(card.getByText("Connected", { exact: true })).toBeVisible();
    await expect(card.getByRole("link", { name: "sim-1" })).toBeVisible();
    await expect(card.getByText("ready", { exact: true })).toBeVisible();
    await expectAccessible(page, "fleet");

    // --- Policies: pushed from the command line, deployed from here -------------------------
    const file = path.join(work, "hold.py");
    writeFileSync(file, HOLD);
    const pushed = site.push(file, "hold", "sim-1", stackState().token, site.publisherKey());
    expect(pushed).toContain("hold v1");
    await nav(page, "Policies");
    await page.getByRole("link", { name: "hold", exact: true }).click();
    await expect(page.getByRole("cell", { name: /Python\s*Signed/ })).toBeVisible();
    await expectAccessible(page, "policy");
    await page.getByRole("button", { name: "Deploy v1" }).click();
    const deploy = page.getByRole("dialog", { name: "Deploy hold v1" });
    await deploy.getByLabel("sim-1").check();
    await deploy.getByLabel("Shadow test").selectOption({ label: "No shadow test" });
    await deploy.getByLabel("Why (for the history)").fill("first try on the line");
    await deploy.getByRole("button", { name: "Deploy to 1 robot" }).click();

    // --- Rollouts: it waits for a person at the site, never for a cloud credential -----------
    await expect(page.getByRole("heading", { name: "Deploy: hold v1" })).toBeVisible();
    await expect(page.getByText("Waiting for a person at the site")).toBeVisible();
    await expectAccessible(page, "rollout, awaiting approval");
    await nav(page, "Fleet");
    await expect(page.getByText("hold v1 is waiting")).toBeVisible();
    await expect.poll(() => site.pendingApprovals().length, { timeout: 60_000 }).toBeGreaterThan(0);
    const [pending] = site.pendingApprovals();
    if (!pending) throw new Error("nothing waits for approval");
    site.approve(pending.command_id, "Ada at the line");
    await expect(page.getByText("hold v1 is waiting")).toBeHidden();
    await expect(card.getByRole("cell", { name: "hold v1" })).toBeVisible();
    await expect(card.getByText("running", { exact: true })).toBeVisible();

    await nav(page, "Rollouts");
    await page.getByRole("link", { name: "Deploy: hold v1" }).click();
    await expect(page.locator(".page-meta").getByText("live")).toBeVisible();
    await expect(
      page.getByRole("list", { name: "Steps" }).getByText("approved by Ada at the line"),
    ).toBeVisible();
    await expect(page.getByText("sim-1 now runs hold v1")).toBeVisible();
    await expectAccessible(page, "rollout, live");
  } finally {
    site.stop();
    rmSync(work, { recursive: true, force: true });
  }
});

/** What `neura login` does first: start a sign-in, as anyone can, and get its link's code. */
async function startCliSignIn(): Promise<{ id: string; user_code: string; secret: string }> {
  const response = await fetch(`${stackState().api_url}/v1/login-sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": `e2e-${Date.now()}` },
    body: JSON.stringify({ client_name: "neura CLI on the e2e machine" }),
  });
  expect(response.status).toBe(201);
  return (await response.json()) as { id: string; user_code: string; secret: string };
}

test("signing a CLI in: a person types the code their terminal shows, sees what it signs in, and approves it", async ({
  page,
}) => {
  const started = await startCliSignIn();
  // Signed in with an API token: tokens cannot mint tokens.
  await page.goto("/sign-in");
  await page.getByLabel("API token").fill(stackState().token);
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await expect(page.getByRole("navigation", { name: /Project|Organization/ })).toBeVisible();
  await page.goto(`/activate?code=${started.user_code}`);
  await expect(page.getByText("Only a person can approve a sign-in")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);

  // A person, signed in with the stack's SSO issuer. Its sign-in is a redirect to an identity
  // provider this stack does not run, so the test hands the console the person's access token.
  await page.evaluate((token) => {
    sessionStorage.setItem("neura.apiToken", token);
  }, stackState().person_token);
  await page.goto(`/activate?code=${started.user_code}`);
  // The link alone approves nothing: its code is shown to compare, not filled in.
  const code = page.getByLabel("Code");
  await expect(code).toHaveValue("");
  await expect(page.getByText(`The link you opened is for ${started.user_code}`)).toBeVisible();
  const approve = page.getByRole("button", { name: "Approve" });
  await expect(approve).toBeDisabled();
  await code.fill(started.user_code.toLowerCase().replace("-", ""));
  await expect(page.getByText("neura CLI on the e2e machine")).toBeVisible();
  await expect(page.getByText(/It gets admin access to .*, for 30 days/)).toBeVisible();
  await expect(approve).toBeEnabled();
  await expectAccessible(page, "activate");
  await approve.click();
  await expect(page.getByText(/neura CLI on the e2e machine now has admin access/)).toBeVisible();

  // The CLI, polling with the secret only it holds, gets its token once.
  const exchange = await fetch(`${stackState().api_url}/v1/login-sessions/${started.id}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": `e2e-${Date.now()}` },
    body: JSON.stringify({ secret: started.secret }),
  });
  expect(exchange.status).toBe(200);
  expect(((await exchange.json()) as { token: string }).token).toMatch(/^ntk_/);
});
