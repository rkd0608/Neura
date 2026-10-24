// After a build: the Content-Security-Policy the docs are served with (deploy/landing). The site
// allows no inline script, so the few Docusaurus writes into its pages (the colour mode, before the
// page paints) are allowed by their SHA-256 hashes, and nothing else is. Writes build/csp.inc.
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const root = new URL("../build/", import.meta.url).pathname;
const hashes = new Set();

function walk(directory) {
  for (const name of readdirSync(directory)) {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) walk(path);
    else if (name.endsWith(".html")) scan(readFileSync(path, "utf8"));
  }
}

function scan(html) {
  for (const [, attributes, body] of html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)) {
    if (/\bsrc=/.test(attributes) || !body.trim()) continue;
    const type = /\btype="([^"]+)"/.exec(attributes)?.[1];
    if (type && type !== "text/javascript" && type !== "module") continue; // data, never run
    hashes.add(`'sha256-${createHash("sha256").update(body).digest("base64")}'`);
  }
}

walk(root);
const scripts = ["'self'", ...[...hashes].sort()].join(" ");
const policy =
  `default-src 'self'; script-src ${scripts}; style-src 'self' 'unsafe-inline'; ` +
  "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; " +
  "form-action 'self'; object-src 'none'";
writeFileSync(join(root, "csp.inc"), `add_header Content-Security-Policy "${policy}" always;\n`);
console.log(`csp.inc: ${hashes.size} inline script(s) allowed by hash`);
