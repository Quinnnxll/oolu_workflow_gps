"""The desktop shell's scaffold UI — one self-contained page, no build step.

Served by the loopback itself at ``GET /``: plain HTML + vanilla JS talking
to the same loopback endpoints the tests drive, so the page can never do
anything the API cannot. It is deliberately a SCAFFOLD — honest widgets over
every surface (assembly preview with budget verdicts and learned orderings,
confirm with review acknowledgement, the approval inbox with bearer-token
decisions, task submission, worker health) for a real front-end to replace
screen by screen.

Nothing here holds a secret: the approver token lives in page memory only,
exactly as typed, and every privileged decision is re-verified server-side.
"""

from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workflow-GPS Shell</title>
<style>
  :root { --fg:#1a1d21; --bg:#f6f7f9; --card:#fff; --line:#d9dde3;
          --accent:#2456c8; --ok:#1a7f37; --warn:#b35900; --bad:#b3261e; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#e6e8eb; --bg:#15171a; --card:#1e2126; --line:#33383f;
            --accent:#7aa2ff; --ok:#4bbf6b; --warn:#e0a24a; --bad:#e57373; }
  }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 system-ui,sans-serif; color:var(--fg);
         background:var(--bg); }
  header { display:flex; gap:.4rem; align-items:baseline; padding:.8rem 1.2rem;
           border-bottom:1px solid var(--line); background:var(--card); }
  header h1 { font-size:1.05rem; margin:0 1rem 0 0; }
  nav button { border:0; background:none; color:var(--fg); padding:.35rem .7rem;
               cursor:pointer; border-radius:6px; font-size:.95rem; }
  nav button.active { background:var(--accent); color:#fff; }
  main { max-width:920px; margin:1.2rem auto; padding:0 1rem; }
  section.screen { display:none; } section.screen.active { display:block; }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:10px; padding:1rem 1.2rem; margin-bottom:1rem; }
  label { display:block; font-size:.8rem; opacity:.75; margin-top:.6rem; }
  input[type=text], input[type=number] { width:100%; padding:.45rem .6rem;
      border:1px solid var(--line); border-radius:6px; background:var(--bg);
      color:var(--fg); font:inherit; }
  .row { display:flex; gap:1rem; flex-wrap:wrap; } .row > div { flex:1 1 12rem; }
  button.primary { background:var(--accent); color:#fff; border:0;
      padding:.5rem 1rem; border-radius:7px; cursor:pointer; margin-top:.8rem;
      font:inherit; }
  button.primary:disabled { opacity:.45; cursor:default; }
  table { width:100%; border-collapse:collapse; margin-top:.6rem; }
  th, td { text-align:left; padding:.35rem .5rem; border-bottom:1px solid var(--line);
           font-size:.9rem; }
  .badge { display:inline-block; padding:.1rem .55rem; border-radius:99px;
           font-size:.75rem; border:1px solid var(--line); margin-right:.3rem; }
  .ok { color:var(--ok); border-color:var(--ok); }
  .warn { color:var(--warn); border-color:var(--warn); }
  .bad { color:var(--bad); border-color:var(--bad); }
  .error { color:var(--bad); margin-top:.6rem; white-space:pre-wrap; }
  .muted { opacity:.7; font-size:.85rem; }
  ul.plain { padding-left:1.1rem; margin:.4rem 0; }
</style>
</head>
<body>
<header>
  <h1>Workflow-GPS</h1>
  <nav id="nav">
    <button data-screen="assemble" class="active">Assemble</button>
    <button data-screen="tasks">Tasks</button>
    <button data-screen="inbox">Inbox</button>
    <button data-screen="health">Health</button>
  </nav>
</header>
<main>

<section class="screen active" id="assemble">
  <div class="card">
    <strong>Goal</strong>
    <div class="row">
      <div><label>Name</label><input type="text" id="goal-name" value="my-goal"></div>
      <div><label>Want (slot names, comma-separated)</label>
           <input type="text" id="goal-want" placeholder="tidy"></div>
      <div><label>Have (slot names)</label><input type="text" id="goal-have"></div>
      <div><label>Search</label><input type="text" id="goal-q"></div>
    </div>
    <div class="row">
      <div><label>Budget cap</label><input type="number" step="0.01" id="budget-cap"></div>
      <div><label>Review threshold</label>
           <input type="number" step="0.01" id="review-threshold"></div>
      <div><label><input type="checkbox" id="fill-gaps"> fill gaps with scripts</label>
           <label><input type="checkbox" id="explore"> explore (Thompson-sample)</label>
           <label><input type="checkbox" id="review-ack"> acknowledge review</label></div>
    </div>
    <button class="primary" id="preview-btn">Preview</button>
    <div class="error" id="assemble-error"></div>
  </div>
  <div class="card" id="preview-card" hidden>
    <div id="preview-summary"></div>
    <table id="preview-steps"></table>
    <div id="preview-order"></div>
    <div id="preview-budget"></div>
    <button class="primary" id="confirm-btn" disabled>Confirm &amp; run</button>
    <div id="confirm-result"></div>
  </div>
</section>

<section class="screen" id="tasks">
  <div class="card">
    <strong>Submit a task</strong>
    <label>Intent</label><input type="text" id="task-intent">
    <button class="primary" id="task-btn">Submit</button>
    <div class="error" id="task-error"></div>
  </div>
  <div class="card"><strong>Session tasks</strong><div id="task-list"
       class="muted">nothing submitted yet</div></div>
</section>

<section class="screen" id="inbox">
  <div class="card">
    <strong>Approver token</strong>
    <label>Bearer token (held in page memory only; verified server-side)</label>
    <input type="text" id="approver-token">
  </div>
  <div class="card">
    <strong>Inbox</strong>
    <button class="primary" id="inbox-refresh">Refresh</button>
    <div id="inbox-list" class="muted">not loaded</div>
    <div class="error" id="inbox-error"></div>
  </div>
</section>

<section class="screen" id="health">
  <div class="card"><strong>Worker health</strong><div id="health-body"
       class="muted">not loaded</div></div>
</section>

</main>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = (v) => (v == null ? "—" : Number(v).toFixed(4));
let lastPreview = null;
const sessionTasks = [];

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

// ------------------------------ navigation ------------------------------ //
$("nav").addEventListener("click", (e) => {
  const target = e.target.closest("button"); if (!target) return;
  document.querySelectorAll("#nav button").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("section.screen").forEach(s => s.classList.remove("active"));
  target.classList.add("active");
  $(target.dataset.screen).classList.add("active");
  if (target.dataset.screen === "health") loadHealth();
  if (target.dataset.screen === "inbox") loadInbox();
});

// ------------------------------- assemble ------------------------------- //
const slots = (text) => text.split(",").map(s => s.trim()).filter(Boolean)
  .map(name => ({ name, value_type: "path", role: "path" }));
const maybe = (id) => { const v = $(id).value; return v === "" ? undefined : Number(v); };

$("preview-btn").addEventListener("click", async () => {
  $("assemble-error").textContent = "";
  try {
    const preview = await api("POST", "/v1/assembly/preview", {
      goal: $("goal-name").value, want: slots($("goal-want").value),
      have: slots($("goal-have").value), q: $("goal-q").value,
      fill_gaps: $("fill-gaps").checked, explore: $("explore").checked,
      budget_cap: maybe("budget-cap"), review_threshold: maybe("review-threshold"),
    });
    lastPreview = preview;
    renderPreview(preview);
  } catch (err) { $("assemble-error").textContent = err.message; }
});

function renderPreview(p) {
  $("preview-card").hidden = false;
  $("confirm-result").textContent = "";
  $("preview-summary").innerHTML =
    (p.complete ? '<span class="badge ok">complete</span>'
                : '<span class="badge bad">incomplete</span>')
    + p.selected.map(n => '<span class="badge">' + esc(n) + "</span>").join("")
    + (p.missing.length
        ? '<div class="muted">missing: ' + esc(p.missing.join(", ")) + "</div>" : "")
    + '<div class="muted">estimated total: ' + money(p.estimated_gross_total)
    + " · platform margin: " + money(p.platform_margin_preview) + "</div>";
  $("preview-steps").innerHTML =
    "<tr><th>step</th><th>kind</th><th>price</th><th>payouts</th></tr>"
    + p.steps.map(s => "<tr><td>" + esc(s.name) + (s.gap ? " (gap)" : "")
      + "</td><td>" + esc(s.kind) + "</td><td>" + money(s.price) + "</td><td>"
      + s.payouts.map(x => esc(x.noder) + " " + money(x.amount)).join(", ")
      + "</td></tr>").join("");
  $("preview-order").innerHTML = p.learned_order.length
    ? '<div class="muted">learned order: ' + p.learned_order.map(o =>
        esc(o.first) + " → " + esc(o.then)).join(" · ") + "</div>" : "";
  const b = p.budget;
  $("preview-budget").innerHTML = !b ? "" :
    "<div>" + (b.allowed ? "" : '<span class="badge bad">over hard cap</span>')
    + (b.needs_review ? '<span class="badge warn">needs review</span>' : "")
    + (b.reasons.length
        ? '<ul class="plain">' + b.reasons.map(r => "<li>" + esc(r) + "</li>").join("")
          + "</ul>" : '<span class="badge ok">within budget</span>') + "</div>";
  $("confirm-btn").disabled = !(p.contract && (b ? b.allowed : true));
}

$("confirm-btn").addEventListener("click", async () => {
  $("confirm-result").textContent = "";
  try {
    const run = await api("POST", "/v1/assembly/confirm", {
      contract: lastPreview.contract,
      confirm_id: crypto.randomUUID(),
      budget_cap: maybe("budget-cap"), review_threshold: maybe("review-threshold"),
      review_acknowledged: $("review-ack").checked,
    });
    $("confirm-result").innerHTML = run.status === "awaiting_approval"
      ? '<span class="badge warn">held for approval</span> '
        + '<span class="muted">pending id ' + esc(run.run_id)
        + " — decide it from the Inbox</span>"
      : '<span class="badge ' + (run.status === "succeeded" ? "ok" : "bad") + '">'
        + esc(run.status) + "</span> <span class='muted'>gross "
        + money(run.gross) + " · noders: " + esc(run.noders.join(", ")) + "</span>";
  } catch (err) { $("confirm-result").innerHTML =
      '<div class="error">' + esc(err.message) + "</div>"; }
});

// -------------------------------- tasks --------------------------------- //
$("task-btn").addEventListener("click", async () => {
  $("task-error").textContent = "";
  try {
    const view = await api("POST", "/v1/tasks", { intent: $("task-intent").value });
    sessionTasks.unshift(view.run_id);
    renderTasks();
  } catch (err) { $("task-error").textContent = err.message; }
});

async function renderTasks() {
  if (!sessionTasks.length) return;
  const rows = [];
  for (const runId of sessionTasks) {
    try {
      const t = await api("GET", "/v1/tasks/" + runId);
      rows.push("<tr><td>" + esc(t.intent) + "</td><td>" + esc(t.phase)
        + "</td><td>" + esc(t.awaiting || "—") + "</td><td class='muted'>"
        + esc(runId.slice(0, 8)) + "</td></tr>");
    } catch { rows.push("<tr><td colspan=4 class='muted'>" + esc(runId)
        + " unavailable</td></tr>"); }
  }
  $("task-list").innerHTML =
    "<table><tr><th>intent</th><th>phase</th><th>awaiting</th><th>id</th></tr>"
    + rows.join("") + "</table>";
}

// -------------------------------- inbox --------------------------------- //
$("inbox-refresh").addEventListener("click", loadInbox);

async function loadInbox() {
  $("inbox-error").textContent = "";
  try {
    const inbox = await api("GET", "/v1/inbox");
    if (!inbox.items.length) {
      $("inbox-list").innerHTML = '<div class="muted">nothing waiting</div>';
      return;
    }
    $("inbox-list").innerHTML = inbox.items.map((item, i) =>
      '<div class="card"><span class="badge">' + esc(item.kind) + "</span> <strong>"
      + esc(item.intent) + "</strong><div class='muted'>" + esc(item.prompt)
      + "</div>" + (item.kind === "contract-approval"
        ? '<button class="primary" data-decide="approve" data-id="'
          + esc(item.run_id) + '">Approve</button> '
          + '<button class="primary" data-decide="decline" data-id="'
          + esc(item.run_id) + '">Decline</button>'
        : "") + "</div>").join("");
  } catch (err) { $("inbox-error").textContent = err.message; }
}

$("inbox-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-decide]"); if (!btn) return;
  $("inbox-error").textContent = "";
  try {
    const run = await api("POST", "/v1/assembly/approvals/" + btn.dataset.id,
      { approved: btn.dataset.decide === "approve" }, $("approver-token").value);
    $("inbox-error").textContent = "";
    btn.closest(".card").innerHTML = '<span class="badge '
      + (run.status === "succeeded" ? "ok" : "warn") + '">' + esc(run.status)
      + "</span>";
  } catch (err) { $("inbox-error").textContent = err.message; }
});

// -------------------------------- health -------------------------------- //
async function loadHealth() {
  try {
    const [health, policy] = await Promise.all([
      api("GET", "/v1/worker-health"), api("GET", "/v1/offline-policy")]);
    $("health-body").innerHTML =
      "<div>docker: " + (health.docker_available
        ? '<span class="badge ok">available</span>'
        : '<span class="badge warn">unavailable</span>') + "</div>"
      + '<ul class="plain">' + health.labels.map(l => "<li>" + esc(l.label)
        + ' <span class="muted">(' + esc(l.allowed_backends.join(", "))
        + ")</span></li>").join("") + "</ul>"
      + '<div class="muted">network policy: ' + esc(policy.network) + "</div>";
  } catch (err) { $("health-body").textContent = err.message; }
}
</script>
</body>
</html>
"""
