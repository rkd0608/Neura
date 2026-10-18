// The site's episode viewer: lists what this site's robots recorded, shows an episode's camera
// frames and commands step by step, and labels or deletes it. Everything comes from this agent's
// local API, with the operator credential; no data is written into the page as HTML.
"use strict";

const KEY = "neura-site-operator";
const SVG = "http://www.w3.org/2000/svg";
const PAGE = 500; // episodes listed at most
const $ = (id) => document.getElementById(id);

let credential = null; // { token, operator }
let current = null; // { record, steps }
const images = new Map(); // "episode/camera/frame" -> object URL

function say(text) {
  $("status").textContent = text;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${credential.token}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (response.status === 401) {
    signOut();
    throw new Error("The operator credential was not accepted.");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch {
      // not JSON
    }
    throw new Error(detail);
  }
  return response;
}

function cell(row, text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  row.append(td);
  return td;
}

async function loadList() {
  const outcome = $("outcome-filter").value;
  const query = new URLSearchParams({ limit: String(PAGE) });
  if (outcome) {
    query.set("outcomes", outcome);
  } else {
    // The store lists oldest first: start where the newest page begins.
    const { summary } = await (await api("/v1/local/episodes?limit=1")).json();
    query.set("offset", String(Math.max(0, summary.episodes - PAGE)));
  }
  const { items, summary } = await (await api(`/v1/local/episodes?${query}`)).json();
  const outcomes = Object.entries(summary.outcomes || {})
    .map(([label, count]) => `${count} ${label}`)
    .join(", ");
  $("summary").textContent = `${summary.episodes} episodes at this site${outcomes ? `: ${outcomes}` : ""}.`;
  const body = $("episodes");
  body.replaceChildren();
  for (const record of [...items].reverse()) {
    const row = document.createElement("tr");
    cell(row, new Date(record.started_at).toLocaleString());
    cell(row, record.robot_id);
    cell(row, record.revision_id);
    cell(row, record.end_reason);
    cell(row, record.outcome, `outcome-${record.outcome}`);
    cell(row, String(record.steps));
    const open = document.createElement("button");
    open.type = "button";
    open.className = "quiet";
    open.textContent = "Open";
    open.setAttribute("aria-label", `Open episode ${record.episode_id}`);
    open.addEventListener("click", () => openEpisode(record.episode_id).catch((e) => say(e.message)));
    row.append(document.createElement("td"));
    row.lastChild.append(open);
    body.append(row);
  }
  if (items.length === 0) say("No episode recorded yet.");
}

async function frameUrl(episodeId, role, index) {
  const key = `${episodeId}/${role}/${index}`;
  if (!images.has(key)) {
    const blob = await (await api(`/v1/local/episodes/${episodeId}/frames/${role}/${index}`)).blob();
    images.set(key, URL.createObjectURL(blob));
  }
  return images.get(key);
}

function trace(name, values, every) {
  const finite = values.filter((v) => v !== null);
  const low = Math.min(...finite);
  const high = Math.max(...finite);
  const span = high > low ? high - low : 1;
  const figure = document.createElement("figure");
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", `0 0 ${Math.max(values.length - 1, 1)} 100`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${name} over the episode, from ${low.toFixed(2)} to ${high.toFixed(2)}`);
  const line = document.createElementNS(SVG, "polyline");
  line.setAttribute("class", "command");
  line.setAttribute("vector-effect", "non-scaling-stroke");
  line.setAttribute(
    "points",
    values
      .map((v, i) => (v === null ? null : `${i},${(100 - ((v - low) / span) * 96 - 2).toFixed(2)}`))
      .filter(Boolean)
      .join(" "),
  );
  const cursor = document.createElementNS(SVG, "line");
  cursor.setAttribute("class", "cursor");
  cursor.setAttribute("vector-effect", "non-scaling-stroke");
  cursor.setAttribute("y1", "0");
  cursor.setAttribute("y2", "100");
  svg.append(line, cursor);
  const caption = document.createElement("figcaption");
  caption.textContent = name;
  figure.append(svg, caption);
  return { figure, cursor, every };
}

let cursors = [];

async function showStep(index) {
  const { record, steps } = current;
  const t = steps.t_s[index] ?? 0;
  $("step-label").textContent = `${index * steps.every + 1} of ${steps.steps} · ${t.toFixed(2)} s`;
  for (const { cursor } of cursors) {
    cursor.setAttribute("x1", String(index));
    cursor.setAttribute("x2", String(index));
  }
  const frame = steps.frame[index] ?? -1;
  for (const camera of steps.cameras) {
    const img = $(`camera-${camera.role}`);
    if (frame < 0 || frame >= camera.frames) {
      img.removeAttribute("src");
      img.alt = `${camera.role}: no frame recorded yet at this step`;
      continue;
    }
    img.alt = `${camera.role} camera, frame ${frame + 1} of ${camera.frames}`;
    img.src = await frameUrl(record.episode_id, camera.role, frame);
  }
}

function describe(record) {
  $("detail-meta").textContent =
    `${record.robot_id} · ${record.revision_id} · ${new Date(record.started_at).toLocaleString()} · ` +
    `ended: ${record.end_reason} · outcome: ${record.outcome} (${record.outcome_source})` +
    (record.outcome_note ? ` · “${record.outcome_note}”` : "");
}

async function openEpisode(episodeId) {
  say("Loading the episode…");
  const [record, steps] = await Promise.all([
    (await api(`/v1/local/episodes/${episodeId}`)).json(),
    (await api(`/v1/local/episodes/${episodeId}/steps`)).json(),
  ]);
  current = { record, steps };
  $("detail").hidden = false;
  $("detail-title").textContent = `Episode ${record.episode_id}`;
  describe(record);
  const cameras = $("cameras");
  cameras.replaceChildren();
  for (const camera of steps.cameras) {
    const figure = document.createElement("figure");
    const img = document.createElement("img");
    img.id = `camera-${camera.role}`;
    img.width = camera.width;
    img.height = camera.height;
    const caption = document.createElement("figcaption");
    caption.textContent = `${camera.role} · ${camera.frames} frames`;
    figure.append(img, caption);
    cameras.append(figure);
  }
  const traces = $("traces");
  traces.replaceChildren();
  cursors = steps.command_names.map((name, column) =>
    trace(name, steps.action.map((row) => row[column]), steps.every),
  );
  for (const { figure } of cursors) traces.append(figure);
  const slider = $("step");
  slider.max = String(Math.max(steps.t_s.length - 1, 0));
  slider.value = "0";
  for (const input of document.querySelectorAll("input[name=outcome]")) {
    input.checked = input.value === record.outcome;
  }
  $("note").value = record.outcome_note || "";
  await showStep(0);
  say(`Episode ${record.episode_id}: ${steps.steps} steps.`);
  $("detail").scrollIntoView({ block: "start" });
}

function signOut() {
  credential = null;
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // storage unavailable
  }
  for (const url of images.values()) URL.revokeObjectURL(url);
  images.clear();
  $("list").hidden = true;
  $("detail").hidden = true;
  $("sign-in").hidden = false;
}

async function signIn(token, operator) {
  credential = { token, operator };
  await loadList();
  try {
    sessionStorage.setItem(KEY, JSON.stringify(credential));
  } catch {
    // storage unavailable: signed in for this page only
  }
  $("sign-in").hidden = true;
  $("list").hidden = false;
}

document.addEventListener("DOMContentLoaded", () => {
  $("sign-in-form").addEventListener("submit", (event) => {
    event.preventDefault();
    signIn($("token").value, $("operator").value.trim()).catch((e) => say(e.message));
  });
  $("sign-out").addEventListener("click", signOut);
  $("refresh").addEventListener("click", () => loadList().catch((e) => say(e.message)));
  $("outcome-filter").addEventListener("change", () => loadList().catch((e) => say(e.message)));
  $("step").addEventListener("input", (event) => {
    showStep(Number(event.target.value)).catch((e) => say(e.message));
  });
  $("label-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const outcome = document.querySelector("input[name=outcome]:checked")?.value;
    if (!outcome || !current) return;
    api(`/v1/local/episodes/${current.record.episode_id}/label`, {
      method: "POST",
      body: JSON.stringify({ outcome, note: $("note").value, operator: credential.operator }),
    })
      .then((response) => response.json())
      .then((record) => {
        current.record = record;
        describe(record);
        say("Label saved.");
      })
      .then(loadList)
      .catch((e) => say(e.message));
  });
  $("delete").addEventListener("click", () => {
    if (!current || !window.confirm("Delete this episode from the site? This cannot be undone.")) {
      return;
    }
    api(`/v1/local/episodes/${current.record.episode_id}`, { method: "DELETE" })
      .then(() => {
        $("detail").hidden = true;
        current = null;
        say("Episode deleted.");
      })
      .then(loadList)
      .catch((e) => say(e.message));
  });
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(KEY) || "null");
  } catch {
    saved = null;
  }
  if (saved?.token) signIn(saved.token, saved.operator || "").catch(() => signOut());
});
