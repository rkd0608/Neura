// Look at the NucleiLabs site and the docs the way a visitor does, and fail when a page is broken.
//
//   node tools/shoot.mjs [--only site,docs] [--out DIR] [--site DIR] [--docs DIR]
//
// Serves the built site (landing/dist at /) and docs (site/build at /docs/) the way production's
// nginx does (deploy/landing/default.conf), with the same headers, its Content-Security-Policy
// included, then opens every docs page and the site in Chromium. Each page is screenshotted on a
// desktop and the main ones also on a phone and in the light theme. A page fails on a console
// error, an uncaught exception, a failed or refused request, a request to another origin, an image
// that did not load, content wider than a phone's screen, or an accessibility violation (axe,
// WCAG 2.1 A and AA, the rules the console's end-to-end tests check, in the theme shot). Writes
// DIR/*.png and DIR/report.json (default .verify/shots); exits 1 when any page failed. Build
// first: `make landing-check` and `make docs-check` (verify does both).
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { extname, join, normalize, resolve } from "node:path";

function option(name, fallback) {
  const at = process.argv.indexOf(`--${name}`);
  return at > 0 ? process.argv[at + 1] : fallback;
}

const ROOT = resolve(new URL("..", import.meta.url).pathname);
const SITE = resolve(option("site", join(ROOT, "landing", "dist")));
const DOCS = resolve(option("docs", join(ROOT, "site", "build")));
const CONF = join(ROOT, "deploy", "landing");
const KEY_DOCS = ["/docs/", "/docs/quickstart", "/docs/deploy-a-policy"];
const VIEWPORTS = {
  desktop: { viewport: { width: 1440, height: 900 } },
  phone: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true },
};
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".json": "application/json",
  ".woff2": "font/woff2",
  ".txt": "text/plain",
  ".xml": "application/xml",
};

// The headers an nginx include file adds, following its includes (/etc/nginx/conf.d/X is
// deploy/landing/X, except docs-csp.inc, which the docs build writes).
function headers(file) {
  const out = {};
  for (const line of readFileSync(file, "utf8").split("\n")) {
    const include = /^\s*include\s+\/etc\/nginx\/conf\.d\/(\S+);/.exec(line);
    if (include) {
      const name = include[1];
      Object.assign(out, headers(name === "docs-csp.inc" ? join(DOCS, "csp.inc") : join(CONF, name)));
    }
    const header = /^\s*add_header\s+(\S+)\s+"([^"]*)"/.exec(line);
    if (header) out[header[1]] = header[2];
  }
  return out;
}

function file(root, path) {
  const full = normalize(join(root, path));
  return full.startsWith(root) && existsSync(full) && statSync(full).isFile() ? full : null;
}

// default.conf's locations: /docs/ tries $uri, $uri.html and $uri/index.html, then the docs' 404;
// everything else tries $uri, then the site's index.html.
function route(url, siteHeaders, docsHeaders) {
  const path = decodeURIComponent(new URL(url, "http://x").pathname);
  if (path === "/docs") return { status: 301, location: "/docs/", headers: docsHeaders };
  if (path.startsWith("/docs/")) {
    const rel = path.slice("/docs".length);
    const found = file(DOCS, rel) ?? file(DOCS, `${rel}.html`) ?? file(DOCS, join(rel, "index.html"));
    return found
      ? { status: 200, file: found, headers: docsHeaders }
      : { status: 404, file: join(DOCS, "404.html"), headers: docsHeaders };
  }
  return { status: 200, file: file(SITE, path) ?? join(SITE, "index.html"), headers: siteHeaders };
}

function serve(only) {
  const siteHeaders = headers(join(CONF, "security-headers.inc"));
  const docsHeaders = only.includes("docs") ? headers(join(CONF, "docs-headers.inc")) : {};
  const server = createServer((request, response) => {
    const answer = route(request.url ?? "/", siteHeaders, docsHeaders);
    if (answer.location) {
      response.writeHead(answer.status, { ...answer.headers, Location: answer.location }).end();
      return;
    }
    const type = TYPES[extname(answer.file)] ?? "application/octet-stream";
    response.writeHead(answer.status, { ...answer.headers, "Content-Type": type });
    response.end(readFileSync(answer.file));
  });
  return new Promise((done) => server.listen(0, "127.0.0.1", () => done(server)));
}

// Playwright: the console's (the version its end-to-end tests pin), else a global install.
function playwright() {
  const candidates = [
    () => createRequire(join(ROOT, "console", "package.json"))("@playwright/test"),
    () => createRequire(import.meta.url)("playwright"),
    () => {
      const global = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
      return createRequire(join(global, "noop.js"))("playwright");
    },
  ];
  for (const load of candidates) {
    try {
      return load();
    } catch {
      // try the next
    }
  }
  throw new Error("Playwright is not installed: run `make console-setup`");
}

// axe-core's Playwright runner, the console's, as its end-to-end tests use it.
function axeBuilder() {
  try {
    return createRequire(join(ROOT, "console", "package.json"))("@axe-core/playwright").AxeBuilder;
  } catch {
    throw new Error("@axe-core/playwright is not installed: run `make console-setup`");
  }
}
const AxeBuilder = axeBuilder();
const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

// Every violation, each with the elements it was found on and, for the first, why (a contrast
// ratio, a missing name).
async function accessibility(tab) {
  const { violations } = await new AxeBuilder({ page: tab }).withTags(WCAG).analyze();
  return violations.map((v) => {
    const where = v.nodes.slice(0, 3).map((n) => n.target.join(" "));
    const more = v.nodes.length > 3 ? ` and ${v.nodes.length - 3} more` : "";
    const why = v.nodes[0]?.failureSummary?.split("\n").slice(1).join(" ").trim();
    return `accessibility: ${v.id} (${v.impact}): ${v.help}: ${where.join(", ")}${more}: ${why}`;
  });
}

// NEURA_CHROMIUM, else Playwright's own browser when it is installed, else /opt/pw-browsers'.
// Chosen before launching: a launch that fails leaves its profile behind in the temp directory.
function launch(chromium) {
  const fallback = "/opt/pw-browsers/chromium";
  const own = process.env.NEURA_CHROMIUM;
  if (own) return chromium.launch({ executablePath: own });
  if (existsSync(chromium.executablePath()) || !existsSync(fallback)) return chromium.launch();
  return chromium.launch({ executablePath: fallback });
}

function docsPages() {
  const pages = [];
  const walk = (directory) => {
    for (const name of readdirSync(directory)) {
      const path = join(directory, name);
      if (statSync(path).isDirectory()) {
        if (name !== "assets") walk(path);
      } else if (name.endsWith(".html") && name !== "404.html") {
        const rel = path.slice(DOCS.length).replace(/index\.html$/, "").replace(/\.html$/, "");
        pages.push(`/docs${rel}`);
      }
    }
  };
  walk(DOCS);
  return pages.sort();
}

async function shoot(browser, base, page, view, theme, out) {
  const context = await browser.newContext({ ...VIEWPORTS[view], colorScheme: theme });
  if (theme === "light") await context.addInitScript(() => localStorage.setItem("theme", "light"));
  const tab = await context.newPage();
  const problems = [];
  const origin = new URL(base).origin;
  tab.on("console", (m) => m.type() === "error" && problems.push(`console: ${m.text()}`));
  tab.on("pageerror", (e) => problems.push(`exception: ${e.message}`));
  tab.on("requestfailed", (r) => problems.push(`failed: ${r.url()} (${r.failure()?.errorText})`));
  tab.on("response", (r) => r.status() >= 400 && problems.push(`HTTP ${r.status()}: ${r.url()}`));
  await tab.route("**/*", (r) => {
    if (new URL(r.request().url()).origin === origin || r.request().url().startsWith("data:")) {
      return r.continue();
    }
    problems.push(`another origin: ${r.request().url()}`);
    return r.abort();
  });
  const response = await tab.goto(base + page, { waitUntil: "networkidle" });
  if (!response?.ok()) problems.push(`HTTP ${response?.status()} for the page itself`);
  const layout = await tab.evaluate(() => ({
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    images: [...document.images].filter((i) => i.complete && i.naturalWidth === 0).map((i) => i.src),
    title: document.title,
  }));
  if (layout.overflow > 1) problems.push(`content is ${layout.overflow}px wider than the screen`);
  for (const src of layout.images) problems.push(`image did not load: ${src}`);
  const name = `${(page.replace(/^\/|\/$/g, "") || "home").replaceAll("/", "_")}-${view}-${theme}.png`;
  await tab.screenshot({ path: join(out, name), fullPage: view === "desktop" });
  problems.push(...(await accessibility(tab)));
  await context.close();
  return { page, view, theme, title: layout.title, screenshot: name, problems };
}

const only = option("only", "site,docs").split(",");
const out = resolve(option("out", join(ROOT, ".verify", "shots")));
const missing = [
  only.includes("site") && !existsSync(join(SITE, "index.html")) && "landing/dist (make landing-check)",
  only.includes("docs") && !existsSync(join(DOCS, "index.html")) && "site/build (make docs-check)",
].filter(Boolean);
if (missing.length) {
  console.error(`shoot: build first: ${missing.join(", ")}`);
  process.exit(1);
}
mkdirSync(out, { recursive: true });
const server = await serve(only);
const base = `http://127.0.0.1:${server.address().port}`;
const { chromium } = playwright();
const browser = await launch(chromium);
const plan = [];
if (only.includes("site")) plan.push(["/", "desktop", "dark"], ["/", "phone", "dark"]);
if (only.includes("docs")) {
  for (const page of docsPages()) plan.push([page, "desktop", "dark"]);
  for (const page of KEY_DOCS) plan.push([page, "phone", "dark"], [page, "desktop", "light"]);
}
const results = [];
for (const [page, view, theme] of plan) results.push(await shoot(browser, base, page, view, theme, out));
await browser.close();
server.close();
writeFileSync(join(out, "report.json"), `${JSON.stringify(results, null, 2)}\n`);
const failed = results.filter((r) => r.problems.length);
for (const r of failed) {
  console.error(`✗ ${r.page} (${r.view}, ${r.theme}):\n  ${r.problems.join("\n  ")}`);
}
const pages = new Set(results.map((r) => r.page)).size;
console.log(
  `shots: ${pages} pages, ${results.length} screenshots, ${failed.length} with problems (${out})`,
);
process.exit(failed.length ? 1 : 0);
