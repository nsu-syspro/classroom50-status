import { JSDOM } from "jsdom";
const BASE = "https://nsu-syspro.github.io/classroom50-status/";
let failures = 0;
const check = (n, c) => { console.log((c ? "ok       " : "FAIL     ") + n); if (!c) failures++; };

async function runView(search, waitText) {
  const dom = new JSDOM('<div id="app"></div>', { url: BASE + search, runScripts: "outside-only" });
  const { window } = dom;
  window.fetch = async (url) => {
    const resp = await fetch(new URL(url, BASE));
    return { ok: resp.ok, json: async () => resp.json() };
  };
  const page = await (await fetch(BASE)).text();
  window.eval(page.match(/<script>([\s\S]*?)<\/script>/)[1]);
  await new Promise(r => { const t = setInterval(() => {
    if (window.document.body.textContent.includes(waitText)) { clearInterval(t); setTimeout(r, 50); }
  }, 50); setTimeout(() => { clearInterval(t); r(); }, 10000); });
  return window.document.body.textContent;
}

const t1 = await runView("?classroom=mpt", "Обновлено:");
check("LIVE mpt: rendered fully (footer present)", t1.includes("Обновлено:"));
check("LIVE mpt: no [object artifacts", !t1.includes("[object"));
check("LIVE mpt: classroom name from classroom50", t1.includes("Modern Programmer's Tools"));
check("LIVE mpt: legend present", t1.includes("зачтено / баллов автотеста"));
check("LIVE mpt: scores present", /\d+\/10/.test(t1));
check("LIVE mpt: zachet column", t1.includes("Зачет"));

const t2 = await runView("", "Modern Programmer's Tools");
check("LIVE index: rendered fully", t2.includes("Modern Programmer's Tools"));
check("LIVE index: NSU Sys.Pro", t2.includes("NSU Sys.Pro"));
check("LIVE index: no [object artifacts", !t2.includes("[object"));
process.exit(failures ? 1 : 0);
