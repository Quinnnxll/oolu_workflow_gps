# The node vitality plan — the V-series

Seven defects reported from live testing of the node-creation
capability, investigated against the code before anything was planned.
Every claim in Part I carries the file it was verified in; where a
report's framing didn't match the mechanism, the mechanism is stated
and the plan fixes the CAUSE, not the symptom. The phases are ordered
so each one stands on the one before it: first the surface tells the
truth, then the work survives, then creation works, then one ask
finishes, then the right node is found, then the web scales, then the
economics select, then the search policy opens.

Report → phase map: (1) indicator → V0; (2) background work → V1;
(3) model-call allowance → V3; (4) node discovery + energy ranking →
V4 and V5; (5) expense tracker + reaper + gravity → V6; (6) web-search
limit → V7; (7) birth failures + API secrets → V2.

---

## Part I — honest review: what the code actually does

### 1. The "working on it" bar does not persist

Confirmed, two causes, both structural:

- **The indicator is client-local.** The busy state is a `useState` in
  the mounted component (`Chat.tsx:151`, `NodeInteract.tsx:79`), set
  around the in-flight fetch and never derived from backend state.
  Unmounting (switching conversations — `Life.tsx:397` renders Chat
  only for the selected thread) resets it while the server still works.
- **There is nothing durable to derive it from.** A run checkpoint is
  written only AFTER the synchronous drive returns
  (`durable/service.py:55-68`); while a run executes there is no run
  row and no "executing" snapshot to poll. The SSE stream carries only
  `: ping` heartbeats during the run (`gateway/asgi.py:327-342`) — no
  progress frames — and if the stream drops, the client's catch prints
  "Sorry — that didn't go through" and clears busy while the server's
  worker thread finishes the run anyway (`Chat.tsx:474-486`). The main
  thread's user/assistant turns land in server history only at the END
  of `_chat_turn` (`app.py:2602-2626`), so returning mid-turn shows
  neither the ask nor any working state.

### 2. Leaving the window stops the work

Confirmed with one precision: work doesn't stop because the user
left — it never left the request in the first place.

- A chat-initiated run executes **fully synchronously inside the HTTP
  request** (`app.py:2571-2576` "submit runs synchronously to the
  first pause or terminal phase"; `orchestrator/engine.py:101-105`).
- **No daemon exists.** The async seam is BUILT — durable queue,
  lease, reclaim (`durable/service.py:86-120` `submit_async` /
  `process_next`) — but has zero production callers (tests only). The
  host runtime spawns no worker (`assembly.py`), and all scheduled
  work (pulse, paver, propagation, sweeps) advances only on the
  request-driven lazy tick (`app.py:1095-1101`, `14785-14818`).
- Node-thread turns are deliberately excluded from server history
  (`app.py:2606` `not body.get("node_id")`) and live only in the
  browser's localStorage — leaving a node window mid-turn silently
  discards the eventual reply (`NodeInteract.tsx:86-111`).
- Closing the desktop app kills the sidecar engine; an in-flight
  first-submit leaves NO run row (checkpoint-at-pause-only), and
  nothing on restart scans for or re-drives non-terminal runs.

### 3. The model-call allowance feels too small

Confirmed — but the binding constraint is not a call quota. There is
no per-ask/per-day call allowance anywhere; the felt scarcity is a
stack of small FIXED loop caps plus consent pauses:

- Chat: `MAX_TOOL_ROUNDS = 4` per ask (`chat.py:333`), then it must
  speak. Authoring: `max_steps = 12` + 2 birth-repair rounds
  (`author.py:111-114` — whose own comment says the seat's SPEND cap,
  not the constant, is meant to be the real budget). Run-time node
  repair: 2 repairs + 1 resynthesis (`runtime/script_node.py`).
  Orchestrator: 1 automatic recovery, then an INCIDENT pause for a
  human; 2 user retries before the single per-run rebuild, which then
  needs re-confirmation (`orchestrator/state.py:375-384`).
- A fresh goal with auto-build consent off (the default) takes 2–3
  asks BY DESIGN: fail → growth offer → the user's "yes" builds → a
  model-written route pauses for confirmation before writing anything
  (`app.py:7572-7629`).
- There is no autonomous execute → verify → review → report loop that
  crosses pauses: one ask is one chat turn plus one synchronous run to
  its first pause. Exhaustion is honest everywhere — the caps refuse
  in words, never silently.

### 4. The standing reminder node was not found

Confirmed, three distinct mechanisms:

- **Reminder asks are steered away from nodes on purpose.** The
  reminder capability is a built-in service ("a row with a clock",
  `reminders.py:1-8`); a deterministic regex fires BEFORE the model
  (`chat.py:2790-2795`) and the model's own tool doctrine routes
  "remind me" to `create_reminder` — a standing reminder NODE is
  unreachable through reminder-shaped asks.
- **The pre-build search exists but is narrow.** `_build_function_node`
  DOES search before building — an exact goal-hash lookup plus a
  lexical twin guard (token/trigram Jaccard ≥ 0.6, `naming.py:92-131`)
  — but only over the user's OWN `fn-` nodes, "never the marketplace"
  (`app.py:9783-9785`). Program nodes carry random `program-` ids and
  are structurally invisible to both lookups (`app.py:9345`, `9798`).
- **Nothing searches at ask time.** The run lane resolves a node only
  by exact goal hash, else submits a plain TaskContract
  (`app.py:9880-9941`). The chat model has `list_nodes` but no
  node-SEARCH or marketplace-discover tool. `discover()` itself is a
  single SQL LIKE substring over title/summary/tags ordered by
  `updated_at` — no relevance ranking, no index, a full-table scan
  (`nodeplace/store.py:210-229`) — not a millions-of-nodes path.
- What EXISTS for the energy idea: cold start is already neutral-not-
  zero (Beta(1,1) → success mean 0.5) with Thompson sampling and
  model-proposal pseudo-counts in the assembler
  (`orchestrator/assembler.py:261-285`); `utility()` is a
  per-candidate scalar; `AssemblyPreview.expected_success` (product of
  posterior means over a web, `nodeplace/assembly.py:84-89`) is one
  `-log` away from an additive, minimizable web energy — but assembly
  today is greedy per-slot, never optimized across alternative webs.

### 5. No per-node economics exist

Confirmed, with one load-bearing surprise:

- Run gross/cost per VERSION is recorded (`MeteringEvent`) but
  `provider_cost` is a self-referential estimate (the mean of prior
  estimates, `nodeplace/economics.py:332-336`); sandbox compute is
  never measured. Model spend — the dominant real cost — is metered by
  purpose and tenant, never by node (`billing/model_usage.py:44-71`);
  the `model.seat` audit names node_id but carries no cost.
- Earnings key on NODER PRINCIPAL; per-node income is a three-way-join
  approximation that splits multi-node runs evenly
  (`nodeplace/desk.py:154-186`).
- **`MeteringDeriver.derive()` has no production call site** — the
  substrate an expense tracker would sum may never materialize on a
  live host (tests only). This is a named prerequisite, not a detail.
- No lifecycle path is economics-triggered. The hygiene sweep
  (clone/fraud/zombie, `nodeplace/hygiene.py`) is the natural home but
  is manual-admin-only; `SweepScheduleStore` is the standing recurring
  pattern to ride. Trust/stability signals (verified runs, NodeHealth,
  KYC multiplier, disputes, `days_since_update`) all exist,
  per-candidate, with no neighbor term anywhere.

### 6. The web-search limit

Confirmed narrow: the only hard numeric limit is `max_uses: 3` per
API call on the provider's server-side search tool
(`providers/apikey.py:377-384`), behind a boolean `model.web_search`
setting (default on, Anthropic-routed models only). Node-side web
access is bounded separately (32 calls/run, 8 hosts, GET-only broker,
SSRF guard) — those are security walls, not thrift. The closed-loop
no-search law binds the press packages only. So the report is right:
the one THRIFT limit is a hardcoded 3, and it is either too small for
research asks or pointless — while the real (money) budget already
covers the cost.

### 7. No node created successfully

Confirmed as a lattice of birth-gate holes — the reported repro
(interface declares `source_file`, code reads settings from
`bindings.json` and never uses it) traverses them exactly:

- The interface and the code are two independent model utterances
  (the IO declaration and the script), and **no deterministic gate
  holds them together on the input side**: birth verify stages NO
  inputs and checks output ports only (`runtime/script_node.py:692-795`);
  the static wall checks only the REVERSE direction (reads
  bindings.json with zero declared inputs → refused, `app.py:11571`);
  the one check that names the rule — "every declared input is
  actually read" — is an advisory LLM reviewer that is absent by
  default and fails OPEN on any availability problem
  (`reviewer.py:16-20`, `99-105`).
- On the Docker backend an input-starved crash at birth converts to a
  structured "honest error" that verify maps to `ok=True` — the broken
  node PUBLISHES with a success message; the honest-error note lands
  only in the audit transaction, never in the user's words
  (`docker/entrypoint.py:51-53` → `app.py:11618-11624`, `8172-8181`).
- On the agentic authoring path even OUTPUT ports are skipped: the
  finish gate verifies with no ports and the birth gate honors
  `already_verified` (`app.py:8163-8167`).
- After publish, the phantom input becomes user-facing: the B-series
  ask machinery mints real questions from the declared slot, stages
  the user's answer into bindings.json, and the run ignores it.
- Secrets (7b): an API-calling script is NOT refused — the sandbox is
  severed, the brokered `http_request` hand exists, host grants are
  already conversational (`grant_host`). What's missing is only the
  secret-value channel: the `SecretVault` exists but is in-memory and
  nothing resolves its refs into a node run; the skills layer's
  `credential_ref` plumbing is unconnected; and no form-type block
  exists in the chat block union (six kinds, none an input form). The
  exact-value store is plaintext by design — not a secret channel.

---

## Part II — the laws this plan builds under

1. **The surface never lies.** A working backend shows working; a
   finished backend shows the result; a failed backend shows the
   failure in words. Client-local state may decorate, never carry.
2. **Work belongs to the host, not the window.** A user's ask, once
   accepted, is the platform's obligation; the browser tab is a
   viewport. (This AMENDS the no-daemon doctrine deliberately: the
   lazy tick remains right for scheduled sweeps; user-initiated runs
   earn a real runner. The amendment is named, not smuggled.)
3. **The budget is money, the loops are honest.** Iteration constants
   are floors for safety, not ceilings for value; within the seat's
   spend budget the task loop runs to a finish or a worded refusal.
4. **Find before build; deterministic edges, sampled rank.** Edges
   between nodes stay exact (`Slot.matches` — a synonym is a different
   universe); RANKING over candidates may be semantic and sampled,
   with recorded seeds. Models advise ranking; they never mint edges.
   (The desk doctrine, applied to discovery.)
5. **Economics select; the audit remembers.** A node that costs more
   than it returns retires through the standing revocation with the
   owner told and the reason on the chain — never a silent delete, and
   never erased history.
6. **Secrets ride the vault or they don't ride.** A key never appears
   in chat text, the values store, logs, or the sandbox environment;
   it enters a durable vault through a typed form and leaves only as
   a host-side header at the broker seam.
7. **Contract and code agree at birth or nothing publishes.** The
   deterministic wall — not an optional reviewer — enforces that every
   declared input is consumed and every declared output is emitted,
   with the inputs staged and the run proven against them.

---

## Part III — the phases

Ordering rule: truth of the surface first (V0) — every later phase is
debugged through it; then durability (V1), then birth (V2) — nothing
downstream matters while creation fails; then the loop, discovery,
scale, economics, policy.

### V0 — the run surface tells the truth (report 1)

Goal: the working indicator is server-anchored and survives anything.

- **Durable turn-in-progress.** `_chat_turn` writes the USER turn and
  a `working` marker turn to history at START; the assistant turn
  replaces the marker at the end. Node threads lose the
  `not body.get("node_id")` exclusion — node turns persist
  server-side like every other conversation (localStorage becomes a
  cache, not the record).
- **An executing snapshot.** The durable service checkpoints a
  lightweight `executing` state at submit and at each phase
  transition / action settle (a progress row, not a full state dump),
  so `GET /v1/runs/{id}` answers honestly DURING the drive.
- **The indicator derives from the server.** The busy bubble binds to
  (open stream OR unresolved working marker OR non-terminal run row);
  remount re-derives it from history + run polls. SSE gains progress
  frames (phase, current action) alongside the pings; stream loss
  degrades to polling, never to a fabricated "didn't go through"
  while the server still works — the error bubble appears only when
  the RUN reports failure.

Exit gate: start a long task; switch conversations, reload the page,
reopen the app — every return shows the ask and a live working state
that resolves into the real reply; the false "didn't go through" is
gone; a node thread left mid-turn shows its reply on return.

**Status: LANDED.** The turn is durable from its first breath:
`_chat_turn` appends the user turn and a `working` marker before the
work and resolves the marker at the reply — under `BaseException`
protection, so a raised turn never leaves a promise standing. Node
threads persist server-side under agent `node:<id>` (the
`not body.get("node_id")` exclusion is gone; the history door answers
for `node:` agents; localStorage is now the instant-paint cache, not
the record). A marker that outlives its process is swept into the
honest interruption note — at gateway boot and by an age-bounded
(15-minute) lazy-tick sweep. The engine split `prepare`/`apply_resume`
out of the drive and gained an `on_step` hook; the durable service
stages an executing runs-row snapshot before and during the drive
(`_stage_progress` + `_step_hook`), so a status poll answers honestly
mid-run and a resumed decision lands durably before the drive. The
gateway threads SSE `progress` frames (phase) through
`_start_intent_run` → durable `on_progress`, and `asgi.handle` moved
to `asyncio.to_thread` so blocking turns no longer freeze the event
loop that answers the polls. The frontend derives the indicator from
the server: `serverStillWorking` + a 2.5-second poll while the marker
stands, and a broken transport consults the record before speaking —
marker standing → keep the bubble; reply landed (`replyLanded`) →
show it; neither → the honest apology. Pinned by
`tests/test_run_surface.py` and the V0 cases in `Chat.test.tsx` /
`NodeInteract.test.tsx`. (Single-host assumption on the boot sweep is
documented; the multi-process story arrives with V1's worker.)

### V1 — work survives the window (report 2)

Goal: leaving, closing, or crashing the client never stops accepted
work.

- **Activate the standing async seam.** Chat task submission moves to
  `submit_async`; a host worker (one in-process thread on the host
  runtime, lease/reclaim already durable in `process_next`) drains
  the queue — the deliberate daemon of law 2. The chat turn returns
  immediately with the run marker (V0 renders it live); completion
  appends the assistant report turn and a reminder ping (the standing
  reminder ring).
- **Restart re-drive.** On host start, scan for non-terminal run rows
  and leased-but-dead queue items; re-drive or fail them in words.
  The desktop shell drains gracefully on close (finish the current
  action, checkpoint, exit); a killed process is recovered by the
  scan.
- The lazy tick stays for scheduled sweeps (unchanged doctrine); the
  worker also gives the pulse a heartbeat when the app is open but
  idle.

Exit gate: start a CSV task, close the app entirely; reopen later —
the run completed while away, the reply and the file are waiting;
kill -9 mid-run → restart re-drives to completion or a worded
failure; nothing is silently lost.

**Status: LANDED.** The chat task lane submits async now: the ask is
ACCEPTED (durable run row + `workflow.advance` queue task, via
`submit_async`/`resume_async`/`restart_async` — the engine grew
`apply_restart` and `abort` to split decisions from drives) and the
turn returns with the queued run; the client's run card narrates the
drive as before. The ONE deliberate worker (Part IV decision 2) lives
on `GatewayApp` (`start_worker`/`stop_worker`/`drive_queue`), started
by the ASGI lifespan wherever the app is served and closed through
`HostRuntime`; it drains the queue and gives the lazy tick a heartbeat
while the app is idle. `process_next` hardened: per-step lease
heartbeats, graceful drain between steps (`should_stop` → the lease is
handed back with the attempt refunded — the staged row is the
checkpoint), and a RAISED drive becomes a FAILED run in the
exception's words instead of an eternal reclaim loop. The finish
REPORTS: a `run_reports` binding (filed at enqueue, re-armed by the
resume doors and revivals) lands one report turn in the thread that
asked — success words, or the failing node + growth offer + P2
reminder offer, the exact vocabulary the synchronous reply used to
carry — plus a reminder-ring ping, exactly once per arming
(claim-first). The four resume doors queue their drives the same way
(a confirmed run no longer dies with the window that confirmed it).
Restart re-drive at boot: reclaim expired leases, re-queue owed runs
that lost their task, report what settled unreported, and say plainly
when a run row is gone. The frontend watches a queued run
(`watchRunReport`) and folds the report turn in when it settles.
Pinned by tests/test_run_worker.py and the V1 cases in Chat.test.tsx /
NodeInteract.test.tsx; the synchronous-era chat tests were updated to
the accepted-queued-reported order. (Typed run commands, tool-lane
runs, and POST /v1/runs submissions still drive synchronously — the
V3 budget work revisits the tool lane; the Tauri shell's hard kill is
the kill -9 case, covered by boot recovery.)

### V2 — birth without lies (report 7)

Goal: a node that publishes is a node whose contract, code, and run
were proven to agree — and a build that needs a key asks for it
cleanly instead of failing.

- **The agreement wall (7a), deterministic:**
  - Birth verify STAGES the declared inputs — a generated sample
    bindings.json with every declared slot (the F1.1 empty-bindings
    staging, generalized to typed samples) — so an input-starved or
    input-ignoring script is exercised against its real contract.
  - The static wall gains the forward direction: a declared input
    whose name the script never reads refuses with exact words
    ("the interface promises `source_file`; the function never reads
    it"), symmetric to the existing reverse check.
  - An honest-error outcome at birth is a FAILURE of the birth gate
    (it proves the function cannot consume real bindings), not a
    pass; the Docker and subprocess backends converge on one verdict.
  - The agentic authoring path stops skipping verification:
    `already_verified` counts only when ports were checked; the
    finish gate verifies with the declared outputs.
  - The reviewer stays, advisory, on top of the wall — judgment above
    law, never instead of it. The success message stops lying: any
    residue (honest-error history, repair rounds) is named to the
    user in words.
- **The secret ask (7b):**
  - A new chat block kind `secret_form` — typed fields, masked entry,
    rendered like the device-grant ask; the answer posts to a
    dedicated door, NEVER as chat text.
  - The `SecretVault` gains durable, encrypted-at-rest backing; the
    form's values land there and mint a `CredentialRef` stored on the
    node's account beside its host grants.
  - Injection at the broker seam: `authorize_header` resolves the ref
    HOST-SIDE in the WebBroker for granted hosts only — the secret
    never enters the sandbox, the values store, or a log (redaction
    on every path).
  - Build-time detection: when the authored function targets an
    authenticated API (401/403 at birth verify against the granted
    host, or the author declares it), the build pauses with the form
    block instead of publishing a node that fails its first run.

Exit gate: the reported repro (declares `source_file`, reads settings)
refuses at birth naming the unused input; a build against a keyed API
pauses with the form, stores the key in the durable vault, and the
node's first run authenticates; the key appears nowhere greppable;
every publish either proves contract-code agreement or says exactly
why not.

**Status: LANDED.** The agreement wall (7a): `_birth_problem` gained
the forward direction — a declared input the script never reads
refuses in the planned words ("the interface promises `source_file`;
the function never reads it"), and inputs with no bindings.json read
at all refuse symmetrically. `_author_verifier` stages a typed sample
bindings.json for every declared slot (`_sample_bindings`: example
wins, numbers parse, `path` slots stage a real sample file the binding
points at), holds declared outputs against the payload, and an honest
error against those staged samples now FAILS the gate — the one
honest error that still passes is the web-grant gap, because birth
never touches the network by law (which is also why the plan's
401-at-birth detection leg was deliberately not taken: the
author-declared leg carries it). The agent's finish gate verifies with
the declared contract (`_script_problem(script, io)`, io assembled
before judgment; one-argument verify doubles keep working), so
`already_verified` now means ports-and-inputs-checked. The reviewer
stands unchanged, advisory above the wall. The receipt names its
residue: repair rounds and the honest web note are in the user's
words, not only on the ledger. The secret ask (7b):
`DurableSecretVault` seals values at rest under the install's
`machine.key` (the keyring's stdlib encrypt-then-MAC — nowhere
greppable, wrong key fails closed); the author declares keyed APIs
(the finish_node `secrets` field, or the prose `SECRETS:` line, both
taught by the prompt); a declaring build PAUSES — the judged build
waits in `pending_builds`, the reply carries the `secret_form` chat
block (a seventh block kind, masked entry in Chat and NodeInteract) —
and the door (`POST /v1/builds/{id}/secrets`, or
`/v1/work/nodes/{id}/secrets` for a standing node) completes the
publish through the same tail every build walks, binds the credential
beside the node's host grants (`node_credentials`, rotation revokes
the old ref), and GRANTS the host through the real account door.
Injection at the broker seam: `_egress_auth` ref stamps ride the
action into `WebGrant.auth`, and the `WebBroker` resolves them
host-side for granted hosts only — the script's own header never
wins, the value never enters the sandbox, the action, the call log,
or the durable state. Pinned by tests/test_birth_wall.py and
tests/test_secret_ask.py (including the raw-database-bytes grep) plus
the SecretFormBlock case in Chat.test.tsx; four test fixtures that
declared inputs their scripts never read — the exact lie the wall
refuses — were corrected to consume their contracts.

### V3 — one ask earns its finish (report 3)

Goal: reply → execute → verify → retry → review → report inside ONE
ask, bounded by the seat's money budget.

- **The task loop.** For a turn that starts a task, the loop continues
  across recoverable incidents autonomously: mechanical retries and
  repairs proceed without a human pause up to the SEAT BUDGET (the
  spend cap the author's own comment names as the real budget), not
  the fixed constants; the constants become floors (safety against
  loops), raised and budget-scaled: chat tool rounds for task turns,
  author steps, repair rounds. A final REVIEW step checks the produced
  artifact against the ask (the file exists, the row landed, the
  reminder stands) and the REPORT turn states what was done and what
  it cost.
- **Consent compression.** The standing approval law is untouched for
  writes that need it — but the fail → offer → "yes" → build →
  confirm chain collapses under a standing delegation setting
  ("build and run under my budget"): one ask, one confirmation at
  most, everything else inside the loop. Auto-build consent surfaces
  as a first-run question instead of a buried default.
- **Incidents pause only for judgment.** An incident pauses for the
  human only when the fix requires a DECISION (destructive retry,
  changed scope, money); a mechanical re-execution is the loop's job
  (V1 keeps it alive off-window).

Exit gate: on a consented install, "create a CSV and list an item on
it" and "set up a reminder" each complete in ONE ask with an honest
report turn; budget exhaustion still refuses in words naming the
spend; nothing retries past the budget or the user's scope.

**Status: LANDED.** The delegation pair joined the settings catalog:
`account.task_delegation` ("Build and run under my budget", personal,
implies auto-build — the gateway's consent read and the rebuilder's
both honor the implication) and `budget.task_cap` (the per-ask spend
allowance, in the user's currency; 0 = the safety floors alone). The
consent surfaces as a FIRST-RUN question: the first buildable task ask
with neither consent ever answered becomes the question itself (a
`delegation` growth offer; yes stores both consents and builds-and-runs
straight through; no is recorded so the question is asked exactly once,
ever — `SettingsNode.answered` tells a stored no from a defaulted
silence). The loop: the worker's drain now presses Retry itself
(`_auto_retry_incident` → `resume_async(INCIDENT, retry)`, audited as
`run.auto_retry`, the report withheld while the loop works) on
MECHANICAL incidents under the delegation — never a blocked gate, a
reserved or irreversible action, or one carrying money markers
(`_incident_is_mechanical`) — bounded by the engine's own ladder (two
retries, then the one rebuild, which re-earns the human's ONE
confirmation; the ladder's counters were built for exactly this) and by
the task budget, read per retry from the ask's meter mark
(`run_reports.charges_before`). The floors scaled: tool rounds 4→12
for a delegated account (`tool_rounds` threaded through both chat
loops), author steps 12→24 plus a `budget_left` hand the loop reads
before every consultation, birth repairs 2→4 budget-checked. The
REPORT grew the REVIEW (the artifacts the result promised, checked
against the stores they land in — the file in the drawer, the
reminder standing, the rows landed — MISSING said plainly), the COST
("The whole ask drew ≈N tokens (about $X)", from the ask's meter
window — an honest approximation on a shared host), and the loop's
own account on failures ("I retried N times on your standing
delegation first"; "I stopped there: the ask reached your task budget
(≈$X spent)"). Pinned by tests/test_task_loop.py. (Noted honestly:
worker retries count into `user_retries` — under the delegation the
standing consent IS the user's hand on the Retry button, and the
ladder's rebuild threshold is exactly the escalation the plan wants;
the run-time script-repair caps in script_node.py stay fixed, being
per-execution, inside the ladder's outer loop.)

### V4 — find the standing node first (report 4, reach)

Goal: an ask reaches the right existing node before anything builds.

- **Search-before-build widens.** The build door's own-nodes search
  gains a tenant-registry capability search (the derived `fn:`/`io:`
  tokens + goal similarity); program nodes become findable (a
  goal-derived alias row beside their random id). The reuse offer
  fires BEFORE a failed run, not only after.
- **Search-at-ask.** The run lane consults discovery before
  submitting a bare TaskContract: exact hash → own-node similarity →
  tenant capability search; a confident match runs (under standing
  consent) or is offered in words.
- **The model can look.** The chat toolbox gains `find_nodes`
  (capability + words over own + tenant registry) so the model routes
  to standing work instead of describing it.
- **Built-ins yield to standing nodes.** The deterministic doors
  (reminder regex and kin) check for a matching standing node FIRST;
  when one exists, the ask surfaces it ("your Reminder desk node can
  take this — use it, or the built-in?") instead of silently routing
  to the service. The user's exact reported failure becomes the
  test's exact pin.

Exit gate: a reminder-work ask with a standing reminder node surfaces
that node; a re-ask of a program node's goal finds it; a capability
present in the tenant registry is found without naming the node.

### V5 — the web at scale: links and the energy reading (report 4, scale)

Goal: at millions of nodes, discovery is indexed, multi-dimensional,
and settles on the most effective web — with fresh nodes still
winning their share.

- **Indexed discovery.** `discover()` moves from LIKE-scan to a real
  inverted/FTS index over title, summary, tags, and derived
  capabilities; the own-node scans lose their per-node version
  fetches. Bounded-time search is the exit criterion, not a nicety.
- **The link dimensions.** Beside the deterministic slot edges (the
  paver's, untouched): (a) the embedding dimension — the
  `retrieval.py` LexicalEmbedder seam replaced by a model-backed
  embedder, used to RANK candidates and propose near-misses to the
  negotiator, never to mint edges (law 4); (b) usage co-occurrence
  edges from run bindings (which nodes actually ran together, at what
  outcome); (c) the lineage and class dimensions already stored,
  joined into one candidate graph.
- **The energy reading — the EBM ask, translated honestly.** One
  additive scalar per candidate web:
  `E(web) = Σ_nodes [ −log(posterior success) + λ·cost + μ·(1−trust) ]
  − ν·cohesion(edges)`, minimized ACROSS alternative assemblies (a
  bounded beam over the greedy per-slot chainer — today's
  `expected_success` product is exactly `e^{−Σ −log p}`, so the seam
  exists). Selection THOMPSON-SAMPLES the posteriors with a recorded
  seed — cold start explores by construction, a freshly published fit
  node keeps earning draws, and every reading replays from stored
  inputs + stored seed (the desk doctrine, verbatim). Model advice
  enters only as the standing bounded pseudo-counts.

Exit gate: a synthetic registry at scale answers discovery in bounded
time; across 100 seeded assemblies a fresh fit node wins some and a
proven node wins most (the N1-style statistical pin); the chosen
web's energy and seed are recorded and replayable.

### V6 — the node books and the vitality law (report 5)

Goal: every node has honest books; the books select.

- **Prerequisite, named:** drive `MeteringDeriver.derive()` on the
  standing lazy tick (and V1's worker) so metering events actually
  materialize — today nothing calls it in production.
- **The expense side.** Thread `node_id` through model-usage recording
  (the seat audit already names it; the store gains the column) and
  METER sandbox compute (wall time × rate at the isolation seam) per
  run — replacing the self-referential estimate with a measured cost.
- **The books.** A `NodeBooks` read per node: cost year-to-date
  (model + compute), income year-to-date (the principal→version→node
  join, hardened from even-split to binding-weighted), net, plus the
  standing trust/stability signals. Rendered on the node's desk and
  the owner's work surface.
- **The vitality law.** An economics sweep on `SweepScheduleStore`:
  a node whose rolling-365-day net is below −$5 (after a grace age,
  e.g. 90 days — a newborn is not reaped for being new) RETIRES
  through the standing revocation — owner notified with the books
  attached, reason on the audit chain, history intact (law 5; the
  report said "deleted", the plan retires — erasing history would
  break the platform's own provenance laws, and a retired node's
  successor can always republish).
- **Gravity.** A bounded vitality multiplier — net income, stability,
  trust, decayed by staleness — enters `utility()`, the assembler's
  posterior score, and V5's energy as the trust term; plus a bounded
  NEIGHBOR term from co-occurrence edges, so a strong node pulls
  assemblies toward itself and its proven collaborators.
  Redistribute, never inflate: the multiplier shifts selection and
  slices, never grows any pool (the rewards law).

Exit gate: the books answer cost/income/net for any node from durable
records; the sweep retires a net-negative node with notice and an
auditable reason; vitality measurably and boundedly shifts assembly
choice; no payout pool changes size.

### V7 — the open door, honestly bounded (report 6)

Goal: web search is governed by consent and money, not a magic 3.

- The hardcoded `max_uses: 3` becomes a setting (`model.web_search_
  depth`), default generous, capped only by the seat's money budget —
  which already prices every search-bearing call. The
  `model.web_search` boolean stays the consent switch.
- The doctrine line stays: one-off questions search inline;
  repeatable web work becomes a node on the brokered hand. Node-side
  web bounds (32 calls/run, 8 hosts, SSRF) are security and stay.
  The press closed-loop law is editorial and stays.
- The settings surface says all of this in words, so "too
  conservative or not needed" becomes a dial the owner reads and
  sets.

Exit gate: a research ask is no longer starved at 3 searches; the
setting renders, binds, and is spoken in the settings surface; press
import-scan pins stay green.

---

## Part IV — decisions taken, and questions for the owner

Decisions 1–4 were ratified by the owner on 2026-08-04, in these
words: the reaper nodes retire with notice; the daemon amendment is
explicit; the requested energy-based model lands as an additive; the
deterministic wall stands above the advisory reviewer.

1. **Retire, not delete** (V6): the −$5/year rule triggers the
   standing revocation with notice — never row deletion. Provenance
   and audit laws outrank tidiness; the reaped node's history is what
   justifies the reaping.
2. **The daemon amendment is explicit** (V1): the no-daemon doctrine
   was load-bearing for scheduled sweeps and remains; user-initiated
   runs get the one deliberate worker, built on the async seam that
   already exists rather than new machinery.
3. **Energy = −log posterior + costs, Thompson-sampled** (V5): the
   requested energy-based model lands as an additive, minimizable,
   replayable reading over candidate webs — auditable stochasticity,
   not a learned black box; a learned embedder ranks, it never wires.
4. **Deterministic wall above advisory reviewer** (V2): the
   input-agreement check becomes law in code; the LLM reviewer remains
   as judgment on top and keeps failing open — a dead model must
   never block a build, and now it cannot let a lie through either.
5. Open questions — answered by the owner on 2026-08-04:
   - Sandbox backend during the failing tests: **Docker**. V2 pins
     the Docker honest-error path first (the subprocess backend
     converges on the same verdict, but Docker is the reproduced
     failure).
   - Reviewer seated during the tests: **probably not** (owner
     unsure). V2 therefore assumes the wall carried nothing at test
     time — the deterministic wall is law regardless of the seat.
   - **Ad-dividend earnings count** toward vitality income (V6): the
     income join brings press reward citations in beside
     marketplace-run income when the paid principal traces to a
     node's lineage.
   - The vitality thresholds are **confirmed**: −$5/year and the
     90-day grace land as law in V6, tunable by settings later.
