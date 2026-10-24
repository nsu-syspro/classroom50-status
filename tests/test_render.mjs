// Render-test for classroom50-status/index.html using jsdom.
// Usage: node test_render.mjs <repo_dir>
// Verifies both views (classroom list, single classroom) render fully:
// no "[object" artifacts, expected texts/badges/links present.

import { readFileSync } from "fs";
import { join } from "path";
import { JSDOM } from "jsdom";

const repo = new URL("..", import.meta.url).pathname;

const html = readFileSync(join(repo, "index.html"), "utf8");
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const data = JSON.parse(readFileSync(join(repo, "mpt/data/progress.json"), "utf8"));
const classrooms = JSON.parse(readFileSync(join(repo, "classrooms.json"), "utf8"));

let failures = 0;
function check(name, cond) {
  console.log((cond ? "ok       " : "FAIL     ") + name);
  if (!cond) failures++;
}

async function runView(search, files, checks) {
  const dom = new JSDOM('<!DOCTYPE html><html><body><div id="app"></div></body></html>', {
    url: "https://nsu-syspro.github.io/classroom50-status/" + search,
    runScripts: "outside-only",
  });
  const { window } = dom;
  window.fetch = async (url) => {
    const u = new window.URL(url, window.location.href);
    if (u.hostname === "api.github.com") {
      // stub Actions API: last run succeeded recently
      return { ok: true, json: async () => ({ workflow_runs: [{ conclusion: "success", run_started_at: "2026-09-29T12:00:00Z" }] }) };
    }
    const path = u.pathname.replace("/classroom50-status", "").replace(/^\//, "");
    if (!(path in files)) throw new Error("unexpected fetch: " + path);
    // Serve the in-memory object, not a fresh disk read: checks may mutate
    // the data (e.g. the fix_submitted badge cases below), and test_footer
    // already works this way.
    return { ok: true, json: async () => files[path] };
  };
  window.eval(js);
  await new Promise((resolve) => {
    const t = setInterval(() => {
      if (window.document.getElementById("app").children.length > 0) { clearInterval(t); setTimeout(resolve, 20); }
    }, 10);
    setTimeout(() => { clearInterval(t); resolve(); }, 2000);
  });
  const text = window.document.body.textContent;
  for (const [name, cond] of checks(text, window.document)) check(name, cond);
  check(`[${search}] no "[object" artifacts`, !text.includes("[object"));
}

// --- classroom list view ---
await runView("", { "classrooms.json": classrooms }, (text, doc) => [
  ["index: h1 is NSU Sys.Pro", (doc.querySelector("h1") || {}).textContent === "NSU Sys.Pro"],
  ["index: old subtitle gone", !text.includes("classroom50")],
  ["index: no old course-header format", !text.includes("Курс «")],
  ["index: classroom title from classroom50", text.includes("Modern Programmer's Tools")],
]);

// --- fix_submitted badge: changes_requested + student re-submitted after
// the review. Fixture-tolerant: force the first two cells into both
// variants regardless of what the generated data currently holds.
data.cells[0].review = "changes_requested";
data.cells[0].fix_submitted = true;
if (data.cells[1]) {
  data.cells[1].review = "changes_requested";
  delete data.cells[1].fix_submitted;
}

// --- single classroom view ---
await runView("?classroom=mpt", { "mpt/data/progress.json": data }, (text, doc) => [
  ["mpt: title with separator", (doc.querySelector("h1") || {}).textContent === "Modern Programmer's Tools \u00b7 NSU Sys.Pro"],
  ["mpt: no meta line under title", !doc.querySelector("p.meta")],
  ["mpt: legend text", text.includes("Оценки и статус проверки")],
  ["mpt: badges rendered with labels", text.includes("проверено") && text.includes("на проверке")],
  ["mpt: fix_submitted cell: blue na-proverke badge, no asterisk", (() => {
    const b = doc.querySelector("td .badge.changes_requested_seen");
    return !!b && b.textContent === "на проверке" && !text.includes("на проверке*");
  })()],
  ["mpt: legend na-proverke badge is split two-color", !!doc.querySelector(".legend .badge.seen-split")],
  ["mpt: plain changes_requested badge intact in a cell", !!doc.querySelector("td .badge.changes_requested")],
  ["mpt: no asterisk note in legend", !text.includes("Звёздочка")],
  ["mpt: student names last-first", text.includes("Данилов Лев Антонович")],
  ["mpt: nameless student last", (() => {
    const rows = [...doc.querySelectorAll("tbody tr")];
    const disp = rows.map(r => r.querySelector("td").textContent.trim());
    const named = disp.filter(d => d !== "vikxxie");
    return disp[disp.length - 1] === "vikxxie" &&
      named.every((d, i) => i === 0 || named[i - 1].split(" ")[0] <= d.split(" ")[0]);
  })()],
  ["mpt: score cells", /\d+\/10/.test(text)],
  ["mpt: PR links", doc.querySelectorAll("a[href*='/pull/1']").length > 0],
  ["mpt: footer has updated timestamp", text.includes("Обновлено:")],
  ["mpt: assignment headers", text.includes("Regex basics")],
]);

process.exit(failures ? 1 : 0);
