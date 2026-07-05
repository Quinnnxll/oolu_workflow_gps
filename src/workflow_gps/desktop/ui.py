"""The desktop shell's front-end — a real app, still one page, no build step.

Served by the loopback at ``GET /``. This replaced the first scaffold screen
by screen: instead of innerHTML string templates there is a small DOM-builder
kernel (``h()``) so every dynamic value is a text node — XSS-safe by
construction; a hash router gives each screen (and each task) a deep-linkable
address; and the screens now drive the WHOLE loopback surface, including the
routes the scaffold never used: clarification answers, route confirmation,
incident resolution, cancellation, the live websocket timeline, and the
skills library.

Still deliberately dependency-free: the page speaks only the endpoints the
test suite drives, so it can never do anything the API cannot, and the repo
needs no node_modules to serve it. Nothing here holds a secret — the
approver token lives in page memory only and every privileged decision is
re-verified server-side.
"""

from __future__ import annotations

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workflow-GPS Shell</title>
<style>
  :root { --fg:#1a1d21; --bg:#f6f7f9; --card:#fff; --line:#d9dde3;
          --accent:#2456c8; --ok:#1a7f37; --warn:#b35900; --bad:#b3261e;
          --muted:#5c6570; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#e6e8eb; --bg:#15171a; --card:#1e2126; --line:#33383f;
            --accent:#7aa2ff; --ok:#4bbf6b; --warn:#e0a24a; --bad:#e57373;
            --muted:#9aa3ad; }
  }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 system-ui,sans-serif; color:var(--fg);
         background:var(--bg); }
  header { display:flex; gap:.4rem; align-items:baseline; padding:.8rem 1.2rem;
           border-bottom:1px solid var(--line); background:var(--card);
           position:sticky; top:0; }
  header h1 { font-size:1.05rem; margin:0 1rem 0 0; }
  nav a { color:var(--fg); text-decoration:none; padding:.35rem .7rem;
          border-radius:6px; font-size:.95rem; }
  nav a.active { background:var(--accent); color:#fff; }
  main { max-width:960px; margin:1.2rem auto; padding:0 1rem; }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:10px; padding:1rem 1.2rem; margin-bottom:1rem; }
  label { display:block; font-size:.8rem; color:var(--muted); margin-top:.6rem; }
  input, select { width:100%; padding:.45rem .6rem; border:1px solid var(--line);
      border-radius:6px; background:var(--bg); color:var(--fg); font:inherit; }
  input[type=checkbox] { width:auto; }
  .row { display:flex; gap:1rem; flex-wrap:wrap; } .row > div { flex:1 1 12rem; }
  button { background:var(--accent); color:#fff; border:0; padding:.5rem 1rem;
           border-radius:7px; cursor:pointer; margin-top:.8rem; font:inherit; }
  button.quiet { background:transparent; color:var(--fg);
                 border:1px solid var(--line); }
  button:disabled { opacity:.45; cursor:default; }
  table { width:100%; border-collapse:collapse; margin-top:.6rem; }
  th, td { text-align:left; padding:.35rem .5rem;
           border-bottom:1px solid var(--line); font-size:.9rem; }
  .badge { display:inline-block; padding:.1rem .55rem; border-radius:99px;
           font-size:.75rem; border:1px solid var(--line); margin-right:.3rem; }
  .ok { color:var(--ok); border-color:var(--ok); }
  .warn { color:var(--warn); border-color:var(--warn); }
  .bad { color:var(--bad); border-color:var(--bad); }
  .error { color:var(--bad); margin-top:.6rem; white-space:pre-wrap; }
  .muted { color:var(--muted); font-size:.85rem; }
  ul.plain { padding-left:1.1rem; margin:.4rem 0; }
  a.rowlink { color:var(--accent); cursor:pointer; text-decoration:underline; }
  details { margin-top:.4rem; } summary { cursor:pointer; font-size:.85rem; }
</style>
</head>
<body>
<header>
  <h1>Workflow-GPS</h1>
  <nav id="nav">
    <a href="#/assemble">Assemble</a>
    <a href="#/tasks">Tasks</a>
    <a href="#/inbox">Inbox</a>
    <a href="#/skills">Skills</a>
    <a href="#/health">Health</a>
  </nav>
</header>
<main id="app"></main>
<script>
"use strict";

/* ------------------------------- kernel -------------------------------- *
 * h() builds real DOM nodes; every dynamic value becomes a text node, so
 * nothing user- or server-controlled is ever parsed as HTML.              */
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "checked" || key === "disabled" || key === "hidden")
      el[key] = Boolean(value);
    else if (key === "value") el.value = value;
    else el.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}
const badge = (text, tone) => h("span", { class: "badge " + (tone || "") }, text);
const money = (v) => (v == null ? "—" : Number(v).toFixed(4));

async function api(method, path, body, token) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = "Bearer " + token;
  const resp = await fetch(path, { method, headers,
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || resp.status + " " + resp.statusText);
  return data;
}

/* ------------------------------- state --------------------------------- */
const state = {
  token: "",             // approver bearer token: page memory only
  sessionTasks: [],      // run ids submitted from this page
  assemble: { form: null, preview: null },
  socket: null,          // the open timeline websocket, if any
};

/* ------------------------------- router -------------------------------- */
const routes = [
  [/^#\/assemble$/, () => assembleScreen()],
  [/^#\/tasks$/, () => tasksScreen()],
  [/^#\/task\/([^/]+)$/, (m) => taskScreen(m[1])],
  [/^#\/inbox$/, () => inboxScreen()],
  [/^#\/skills$/, () => skillsScreen()],
  [/^#\/health$/, () => healthScreen()],
];

function render() {
  if (state.socket) { state.socket.close(); state.socket = null; }
  const hash = location.hash || "#/assemble";
  for (const link of document.querySelectorAll("#nav a"))
    link.classList.toggle("active", hash.startsWith(link.getAttribute("href")));
  const app = document.getElementById("app");
  app.replaceChildren();
  for (const [pattern, screen] of routes) {
    const match = hash.match(pattern);
    if (match) { app.append(screen(match)); return; }
  }
  location.hash = "#/assemble";
}
window.addEventListener("hashchange", render);

/* ------------------------------ assemble ------------------------------- */
function assembleScreen() {
  const saved = state.assemble.form || {};
  const field = (name, props) =>
    h("input", { id: "f-" + name, value: saved[name] ?? "", ...props });
  const errorBox = h("div", { class: "error" });
  const resultBox = h("div");

  const form = h("div", { class: "card" },
    h("strong", {}, "Goal"),
    h("div", { class: "row" },
      h("div", {}, h("label", {}, "Name"), field("name", { value: saved.name ?? "my-goal" })),
      h("div", {}, h("label", {}, "Want (slot names, comma-separated)"),
        field("want", { placeholder: "tidy" })),
      h("div", {}, h("label", {}, "Have (slot names)"), field("have")),
      h("div", {}, h("label", {}, "Search"), field("q"))),
    h("div", { class: "row" },
      h("div", {}, h("label", {}, "Budget cap"),
        field("cap", { type: "number", step: "0.01" })),
      h("div", {}, h("label", {}, "Review threshold"),
        field("threshold", { type: "number", step: "0.01" })),
      h("div", {},
        h("label", {}, field("gaps", { type: "checkbox", checked: saved.gaps }),
          " fill gaps with scripts"),
        h("label", {}, field("explore", { type: "checkbox", checked: saved.explore }),
          " explore (Thompson-sample)"),
        h("label", {}, field("ack", { type: "checkbox", checked: saved.ack }),
          " acknowledge review"))),
    h("button", { onclick: preview }, "Preview"),
    errorBox);

  const read = () => {
    const v = (name) => document.getElementById("f-" + name).value;
    const checked = (name) => document.getElementById("f-" + name).checked;
    return { name: v("name"), want: v("want"), have: v("have"), q: v("q"),
             cap: v("cap"), threshold: v("threshold"),
             gaps: checked("gaps"), explore: checked("explore"), ack: checked("ack") };
  };
  const slots = (text) => text.split(",").map(s => s.trim()).filter(Boolean)
    .map(name => ({ name, value_type: "path", role: "path" }));
  const maybe = (v) => (v === "" ? undefined : Number(v));

  async function preview() {
    errorBox.textContent = "";
    state.assemble.form = read();
    const f = state.assemble.form;
    try {
      const p = await api("POST", "/v1/assembly/preview", {
        goal: f.name, want: slots(f.want), have: slots(f.have), q: f.q,
        fill_gaps: f.gaps, explore: f.explore,
        budget_cap: maybe(f.cap), review_threshold: maybe(f.threshold),
      });
      state.assemble.preview = p;
      resultBox.replaceChildren(previewCard(p, f));
    } catch (err) { errorBox.textContent = err.message; }
  }

  if (state.assemble.preview)
    resultBox.append(previewCard(state.assemble.preview, saved));
  return h("section", {}, form, resultBox);
}

function previewCard(p, form) {
  const confirmOut = h("div");
  const budget = p.budget;
  const confirmable = Boolean(p.contract) && (budget ? budget.allowed : true);

  async function confirm() {
    confirmOut.replaceChildren();
    const maybe = (v) => (v === "" || v == null ? undefined : Number(v));
    try {
      const run = await api("POST", "/v1/assembly/confirm", {
        contract: p.contract, confirm_id: crypto.randomUUID(),
        budget_cap: maybe(form.cap), review_threshold: maybe(form.threshold),
        review_acknowledged: Boolean(form.ack),
      });
      confirmOut.append(run.status === "awaiting_approval"
        ? h("div", {}, badge("held for approval", "warn"),
            h("span", { class: "muted" },
              " pending id " + run.run_id + " — decide it from the "),
            h("a", { href: "#/inbox" }, "Inbox"))
        : h("div", {}, badge(run.status, run.status === "succeeded" ? "ok" : "bad"),
            h("span", { class: "muted" }, " gross " + money(run.gross)
              + " · noders: " + run.noders.join(", "))));
    } catch (err) { confirmOut.append(h("div", { class: "error" }, err.message)); }
  }

  return h("div", { class: "card" },
    h("div", {},
      badge(p.complete ? "complete" : "incomplete", p.complete ? "ok" : "bad"),
      p.selected.map(name => badge(name)),
      p.missing.length
        ? h("div", { class: "muted" }, "missing: " + p.missing.join(", ")) : null,
      h("div", { class: "muted" },
        "estimated total: " + money(p.estimated_gross_total)
        + " · platform margin: " + money(p.platform_margin_preview))),
    h("table", {},
      h("tr", {}, h("th", {}, "step"), h("th", {}, "kind"),
        h("th", {}, "price"), h("th", {}, "payouts")),
      p.steps.map(step => h("tr", {},
        h("td", {}, step.name + (step.gap ? " (gap)" : ""),
          step.price_notes.length
            ? h("details", {}, h("summary", {}, "clearing forces"),
                h("ul", { class: "plain" },
                  step.price_notes.map(note => h("li", { class: "muted" }, note))))
            : null),
        h("td", {}, step.kind),
        h("td", {}, money(step.price)),
        h("td", {}, step.payouts.map(x => x.noder + " " + money(x.amount))
          .join(", "))))),
    p.learned_order.length
      ? h("div", { class: "muted" }, "learned order: "
          + p.learned_order.map(o => o.first + " → " + o.then).join(" · "))
      : null,
    budget ? h("div", {},
      budget.allowed ? null : badge("over hard cap", "bad"),
      budget.needs_review ? badge("needs review", "warn") : null,
      budget.reasons.length
        ? h("ul", { class: "plain" },
            budget.reasons.map(reason => h("li", {}, reason)))
        : badge("within budget", "ok")) : null,
    h("button", { onclick: confirm, disabled: !confirmable }, "Confirm & run"),
    confirmOut);
}

/* -------------------------------- tasks -------------------------------- */
function tasksScreen() {
  const errorBox = h("div", { class: "error" });
  const listBox = h("div", { class: "muted" }, "nothing submitted yet");
  const intent = h("input", { type: "text", placeholder: "what should happen?" });

  async function submit() {
    errorBox.textContent = "";
    try {
      const view = await api("POST", "/v1/tasks", { intent: intent.value });
      state.sessionTasks.unshift(view.run_id);
      location.hash = "#/task/" + view.run_id;
    } catch (err) { errorBox.textContent = err.message; }
  }

  async function refresh() {
    if (!state.sessionTasks.length) return;
    const rows = [h("tr", {}, h("th", {}, "intent"), h("th", {}, "phase"),
      h("th", {}, "awaiting"), h("th", {}, ""))];
    for (const runId of state.sessionTasks) {
      try {
        const t = await api("GET", "/v1/tasks/" + runId);
        rows.push(h("tr", {},
          h("td", {}, t.intent), h("td", {}, t.phase),
          h("td", {}, t.awaiting || "—"),
          h("td", {}, h("a", { class: "rowlink", href: "#/task/" + runId },
            "open"))));
      } catch {
        rows.push(h("tr", {}, h("td", { colspan: 4, class: "muted" },
          runId + " unavailable")));
      }
    }
    listBox.replaceChildren(h("table", {}, rows));
  }
  refresh();

  return h("section", {},
    h("div", { class: "card" }, h("strong", {}, "Submit a task"),
      h("label", {}, "Intent"), intent,
      h("button", { onclick: submit }, "Submit"), errorBox),
    h("div", { class: "card" }, h("strong", {}, "Session tasks"), listBox));
}

/* ----------------------------- task detail ----------------------------- */
function taskScreen(runId) {
  const statusBox = h("div", { class: "card" }, h("span", { class: "muted" }, "loading…"));
  const actionBox = h("div");
  const timelineBox = h("ul", { class: "plain" });
  const errorBox = h("div", { class: "error" });

  async function drive(path, body) {
    errorBox.textContent = "";
    try { await api("POST", "/v1/tasks/" + runId + path, body); await load(); }
    catch (err) { errorBox.textContent = err.message; }
  }

  function questionForm(questions) {
    const inputs = new Map();
    return h("div", {},
      h("strong", {}, "Questions"),
      questions.map(q => {
        const input = h("input", { type: "text",
          placeholder: (q.suggested_values || []).join(" / ") });
        inputs.set(q.parameter, input);
        return h("div", {}, h("label", {}, q.question), input);
      }),
      h("button", { onclick: () => drive("/answers",
        { answers: Object.fromEntries([...inputs].map(([k, el]) => [k, el.value])) })
      }, "Answer"));
  }

  async function routePanel() {
    try {
      const route = await api("GET", "/v1/tasks/" + runId + "/route");
      if (!route.chosen) return h("div", { class: "muted" }, "no route yet");
      return h("div", {},
        h("strong", {}, "Proposed route: "), route.chosen.name,
        h("span", { class: "muted" },
          " · estimated cost " + money(route.chosen.estimated_cost)
          + (route.chosen.reserved_action_count
             ? " · " + route.chosen.reserved_action_count + " reserved action(s)" : "")),
        route.exclusions.length
          ? h("ul", { class: "plain" }, route.exclusions.map(x =>
              h("li", { class: "muted" }, x.name + ": " + x.reason)))
          : null,
        h("button", { onclick: () => drive("/confirm", { approved: true }) }, "Approve"),
        " ",
        h("button", { class: "quiet",
          onclick: () => drive("/confirm", { approved: false }) }, "Decline"));
    } catch (err) { return h("div", { class: "error" }, err.message); }
  }

  async function load() {
    try {
      const t = await api("GET", "/v1/tasks/" + runId);
      statusBox.replaceChildren(
        h("strong", {}, t.intent), " ",
        badge(t.phase, t.phase === "completed" ? "ok"
          : t.phase === "failed" ? "bad" : ""),
        t.awaiting ? badge("awaiting " + t.awaiting, "warn") : null,
        t.prompt ? h("div", { class: "muted" }, t.prompt) : null,
        t.failure_reason ? h("div", { class: "error" }, t.failure_reason) : null,
        t.result ? h("details", {}, h("summary", {}, "result"),
          h("pre", { class: "muted" }, JSON.stringify(t.result, null, 2))) : null,
        t.can_cancel
          ? h("button", { class: "quiet", onclick: () => drive("/cancel") }, "Cancel")
          : null);
      actionBox.replaceChildren();
      if (t.awaiting === "clarification") actionBox.append(questionForm(t.questions));
      if (t.awaiting === "confirmation") actionBox.append(await routePanel());
      if (t.awaiting === "incident") actionBox.append(h("div", {},
        h("button", { onclick: () => drive("/resolve-incident",
          { decision: "retry" }) }, "Retry"), " ",
        h("button", { class: "quiet", onclick: () => drive("/resolve-incident",
          { decision: "abort" }) }, "Abort")));
    } catch (err) { errorBox.textContent = err.message; }
  }

  function stream() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    try {
      const socket = new WebSocket(
        proto + "://" + location.host + "/v1/tasks/" + runId + "/events");
      socket.onmessage = (msg) => {
        const event = JSON.parse(msg.data);
        timelineBox.append(h("li", {}, h("span", { class: "muted" },
          event.at + " "), event.label,
          event.detail ? h("span", { class: "muted" }, " — " + event.detail) : null));
      };
      state.socket = socket;
    } catch { /* no live stream: the page still works via reloads */ }
  }

  load(); stream();
  return h("section", {},
    h("a", { href: "#/tasks", class: "muted" }, "← tasks"),
    statusBox,
    h("div", { class: "card" }, actionBox, errorBox),
    h("div", { class: "card" }, h("strong", {}, "Timeline (live)"), timelineBox));
}

/* -------------------------------- inbox -------------------------------- */
function inboxScreen() {
  const errorBox = h("div", { class: "error" });
  const listBox = h("div", { class: "muted" }, "loading…");
  const token = h("input", { type: "text", value: state.token,
    oninput: (e) => { state.token = e.target.value; } });

  async function decide(item, approved, card) {
    errorBox.textContent = "";
    try {
      const run = await api("POST", "/v1/assembly/approvals/" + item.run_id,
        { approved }, state.token);
      card.replaceChildren(badge(run.status, run.status === "succeeded"
        ? "ok" : "warn"));
    } catch (err) { errorBox.textContent = err.message; }
  }

  async function load() {
    try {
      const inbox = await api("GET", "/v1/inbox");
      if (!inbox.items.length) {
        listBox.replaceChildren(h("div", { class: "muted" }, "nothing waiting"));
        return;
      }
      listBox.replaceChildren(...inbox.items.map(item => {
        const card = h("div", { class: "card" },
          badge(item.kind), " ", h("strong", {}, item.intent),
          h("div", { class: "muted" }, item.prompt));
        if (item.kind === "contract-approval") {
          card.append(
            h("button", { onclick: () => decide(item, true, card) }, "Approve"), " ",
            h("button", { class: "quiet",
              onclick: () => decide(item, false, card) }, "Decline"));
        } else {
          card.append(h("a", { class: "rowlink", href: "#/task/" + item.run_id },
            "open task"));
        }
        return card;
      }));
    } catch (err) { errorBox.textContent = err.message; }
  }
  load();

  return h("section", {},
    h("div", { class: "card" }, h("strong", {}, "Approver token"),
      h("label", {}, "Bearer token (page memory only; verified server-side)"),
      token),
    h("div", { class: "card" }, h("strong", {}, "Inbox"), " ",
      h("button", { class: "quiet", onclick: load }, "Refresh"),
      listBox, errorBox));
}

/* -------------------------------- skills ------------------------------- */
function skillsScreen() {
  const listBox = h("div", { class: "muted" }, "loading…");
  const query = h("input", { type: "text", placeholder: "search the library",
    oninput: () => load() });

  async function load() {
    try {
      const path = "/v1/skills" + (query.value
        ? "?q=" + encodeURIComponent(query.value) : "");
      const found = await api("GET", path);
      listBox.replaceChildren(found.items.length
        ? h("div", {}, found.items.map(skill => h("div", { class: "card" },
            h("strong", {}, skill.name), " ",
            h("span", { class: "muted" }, skill.semver),
            skill.score != null
              ? badge("score " + Number(skill.score).toFixed(2)) : null,
            h("div", { class: "muted" }, skill.summary),
            h("div", {}, (skill.tags || []).map(tag => badge(tag))))))
        : h("div", { class: "muted" }, "no skills found"));
    } catch (err) { listBox.replaceChildren(h("div", { class: "error" }, err.message)); }
  }
  load();

  return h("section", {}, h("div", { class: "card" },
    h("strong", {}, "Skill library"), h("label", {}, "Search"), query), listBox);
}

/* -------------------------------- health ------------------------------- */
function healthScreen() {
  const body = h("div", { class: "muted" }, "loading…");
  (async () => {
    try {
      const [health, policy] = await Promise.all([
        api("GET", "/v1/worker-health"), api("GET", "/v1/offline-policy")]);
      body.replaceChildren(
        h("div", {}, "docker: ", health.docker_available
          ? badge("available", "ok") : badge("unavailable", "warn")),
        h("ul", { class: "plain" }, health.labels.map(label =>
          h("li", {}, label.label, " ",
            h("span", { class: "muted" },
              "(" + label.allowed_backends.join(", ") + ")")))),
        h("div", { class: "muted" }, "network policy: " + policy.network));
    } catch (err) { body.replaceChildren(h("div", { class: "error" }, err.message)); }
  })();
  return h("section", {}, h("div", { class: "card" },
    h("strong", {}, "Worker health"), body));
}

render();
</script>
</body>
</html>
"""
