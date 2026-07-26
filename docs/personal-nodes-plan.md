# Personal nodes — the starter shelf and the pulse

Status: Proposed. Scope: issue 7 — two asks that make one product
story:

1. **OoLu should trigger workflows automatically** — daily at a
   time, weekly, monthly, yearly — without a human pressing run.
2. **Every new account starts with pre-built personal nodes**, so
   everyone has something to wire and work with on day one: calendar,
   tasks, automation trigger, reminder, stock management, cashflow
   chart, invoice-scan-to-sheet. The SHELF is standard; the CONTENT
   is personal — everyone's drawer holds different events, tasks,
   stock, and money, but everyone needs the same seven shapes to
   automate their life.

Companion reading: `docs/conversational-building-plan.md` (B1 labels,
B2 drawer landing, B3 stored io, B4 hand-offs — the completeness laws
every starter node is born under), `docs/memory-stack-plan.md` (M1
projections), `docs/node-bundles.md` (the drawer that holds the
personal content).

---

## 1. The laws this plan builds under

- **Pre-built is not pre-filled.** The starter shelf ships the same
  seven nodes to everyone; the drawers start empty and fill with each
  person's own life. A starter node is DELETABLE, and a deletion is
  respected forever — the shelf is a gift, not furniture bolted to
  the floor.
- **Deterministic functions at birth; grown by the standing doors.**
  Like the org templates: no model writes a starter function and no
  model spend happens at seeding — each function is a curated,
  reviewed, deterministic script (reliability by construction). A
  user who wants more says "revise …" and the standing B-plan
  machinery grows it, audited, through the same gates as any node.
- **A starter node is an ordinary node.** Born through the contribute
  door, function in `src/main.py` (B2), declared io with plain-word
  labels (B1), run io in its drawer (B3), hand-offs offered and
  cited (B4), failures in the inbox. Nothing in the runtime knows the
  word "starter" — only the seeding pass does.
- **The sandbox never reaches the host's stores.** A personal node's
  function reads its bindings and its drawer and emits a result; when
  that result must move a HOST store (a reminder row, a fired
  schedule), a completion hook files it — the same seam that already
  files values (M-plan) and run io (B3). The node stays a pure
  function; the host stays the only writer of its own books.
- **The pulse is lazy and honest.** No daemon, no cron dependency:
  request traffic drives the tick (the sweep scheduler's proven
  pattern — monotonic gate + durable claim, never raising into a
  request), the desktop shell's standing poll keeps quiet hosts
  ticking, and a host that slept fires each schedule's MISSED
  occurrence at most once, naming what it skipped — it never
  fabricates a backlog of runs that "should have" happened.
- **A fired run is an ordinary run.** Scheduled firing submits the
  same run the owner would have submitted, as the owner, through
  `_start_intent_run`/`_function_for_node` — audited, metered,
  inbox-visible on failure, B3-filed on success. Automation adds a
  clock, never a side door.

## 2. What already exists (the seams are cut)

| Need | Standing machinery |
|---|---|
| Fire on a clock without a daemon | `runtime/sweep.py` `SweepScheduleStore` + `_maybe_scheduled_sweep`: request-driven lazy tick, monotonic gate (one due-check/minute/host), durable claim so multi-process hosts fire once |
| A row with a clock, delivered once | `reminders.py` `ReminderStore`: due/upcoming/mark_delivered, client poll as the tick — the reminder node's backing store and the pulse's delivery precedent |
| Deterministic pre-built nodes | `nodeplace/org_templates.py` + `_org_template_apply`: curated catalog, `role_script` (no model), minted through the real contribute door, idempotent by name |
| Where a new account is born | `_auth_register` / `_auth_verify` / `_google_finish` / `_phone_verify` — the doors the seeding pass hooks; the account row records what was seeded |
| Fire a node by id | `_function_for_node` (the webhook door's resolver) + `_start_intent_run` — the exact hands a schedule pulls |
| Personal content home | the drawer (`UserFileStore`): seat-walled writes, B2 src landing, B3 `runs/<id>/` io, bundle export |
| Typed asks and forms | B1 `Slot`/`ValueInput` labels + `validate_user_inputs` — every starter node's inputs speak plain words on every surface |
| Chained automation | B4: `output://` edges, offered defaults, run-cited `handoff` graph edges |
| Failures that reach a human | `workflow.failed` / `node.build_failed` → the operator/member inbox (issue 5) |
| Charts at investor scales | `telemetry/investor.py` series scales + the panel's chart idiom — the cashflow chart's rendering precedent |

---

## 3. The phases

### Phase P0 — the pulse: schedules that fire runs

**Status: LANDED** — `src/oolu/pulse.py`: the durable `PulseStore`
(rows with a rhythm — daily/weekly/monthly/yearly on the owner's own
clock, `tz_offset_minutes` aboard, month-length clamped honestly)
plus pure occurrence arithmetic and the deterministic ear
(`spoken_schedule`). The gateway's lazy tick (`_pulse_tick`, its own
minute gate inside `_maybe_scheduled_sweep`'s traffic) elects each
(schedule, occurrence) by an INSERT-OR-IGNORE claim — exactly once
across processes and restarts — and fires through
`_start_intent_run` AS THE OWNER, the run stamped
``metadata["pulse"]`` (schedule, occurrence, skipped) and
`pulse.fired` on the audit chain; a refusal lands `pulse.fire_failed`
and never raises into the serving request. The floor makes catch-up
honest: creation and every resume reset it, so a slept-through week
fires ONCE with six skips named and a paused stretch never fires at
all. Doors: GET/POST `/v1/pulse`, POST/DELETE
`/v1/pulse/{schedule_id}`; spoken: "every day at 9 run …", "every
monday at 7:30pm …", "every month on the 1st at 9 …", "every year on
june 5 at 8am …", plus schedules / cancel / pause / resume — each
answered by reading the store back (`tests/test_pulse.py`).
Remaining: none for P0; P4 wears this store as the Automation
Trigger node.

*OoLu runs it because the calendar says so, not because you asked
again.*

- **The schedule store**: durable rows owned by (tenant, principal) —
  `daily @ HH:MM`, `weekly @ day+HH:MM`, `monthly @ day-of-month`,
  `yearly @ month+day`, each naming WHAT fires: a node id (the
  node's own function) or a goal sentence (the standing resolver).
  Timezone rides the row; the words that created it ride as its
  label.
- **The tick**: `_maybe_scheduled_sweep`'s pattern, generalized — on
  request traffic (and the shell's standing poll), at most one
  due-check per minute per host; a durable claim marks each
  (schedule, occurrence) fired exactly once across processes.
- **Catch-up honesty**: a host waking from sleep fires each overdue
  schedule ONCE for its newest missed occurrence and records the
  count it skipped ("missed 3 daily runs while the host slept") on
  the run's metadata and the audit chain — no fabricated backlog.
- **Fired as the owner**: the run submits through the standing doors
  with the owner's identity and walls (tenant, egress, consent);
  failures land in the owner's inbox exactly like a hand-started
  run's.
- **Spoken and formed**: "every day at 9 run my invoice node" — a
  deterministic chat command (the reminder-command doctrine: the row
  is created and read back from the store, never narrated) — and a
  plain form on the node's page rendering the schedule manifest.
- Acceptance: a daily schedule fires exactly once per day across
  restarts and concurrent processes (the claim decides); the fired
  run is indistinguishable from a hand-started run in every store;
  a slept-through week yields ONE catch-up run with the skips named;
  "every day at 9 …" creates the row and reads it back.

### Phase P1 — the starter shelf: seven nodes at account birth

**Status: LANDED** — `nodeplace/personal_templates.py`: the reviewed
catalog (seven `StarterSpec`s with one-sentence responsibilities,
executable goals, and B1 plain-word asks — every label passes the B0
lexicon) and `starter_script` (deterministic, bindings-reading,
record-emitting — every script passes the standing birth gate and
actually executes, proven by subprocess in the tests). The seeding
pass (`_seed_starter_shelf`) rides the `StarterLedger`'s
INSERT-OR-IGNORE claim — exactly once per person, racing sign-ins
seed once, and a DELETED starter is never resurrected because the
claim never leaves — minting each node through the ordinary doors:
`nodeplace.contribute` with labeled consumes, the person as the desk
account's responsible, the function landed in `src/main.py` (B2),
one `node.starter_seeded` audit line each, refusals audited as
`node.starter_failed` without blanking the shelf. Hooked best-effort
at all four sign-in doors (login, e-mail verify, phone verify,
Google finish) — seeding can never break a sign-in
(`tests/test_starter_shelf.py`: the login-door circle lands seven on
the desk; a seeded Tasks node runs through the standing run door
with no build and no model spend). Remaining: P2/P3 grow the real
record-keeping functions on these seats.

*Day one is never an empty desk.*

- **The personal catalog** (`nodeplace/personal_templates.py`,
  mirroring the org catalog's shape): seven specs — Calendar, Tasks,
  Reminders, Automation Trigger, Stock, Cashflow, Invoice Scan —
  each with a name, one responsibility, a deterministic
  `starter_script`, and declared io with B1 plain-word labels. The
  catalog is the reviewed, versioned truth of what everyone gets.
- **The seeding pass**: on account creation (all four auth doors)
  and on first sign-in for accounts that predate the shelf — mint
  the seven through the contribute door, owned by the person,
  idempotent by title, one audit line per node
  (`node.starter_seeded`). The account records the seeding, so it
  happens once; a deleted starter is never re-seeded.
- **Deletable, honestly**: deleting a starter node is the standing
  node deletion — tombstoned, respected, no resurrection.
- Acceptance: a fresh account's My-nodes shows the seven, each
  live-runnable (verify-by-execution passes on the deterministic
  functions); registering twice seeds once; delete one, sign in
  again, it stays gone; an account from before the shelf receives it
  on next sign-in.

### Phase P2 — the personal records trio: calendar, tasks, reminders

**Status: LANDED** — the record discipline is one circle: the
drawer's ``records/rows.json`` stages into the sandbox as
``./records.json`` (a new ``_records`` carrier — DATA, never part of
the frozen code tree or the cache key), the function reads its book,
changes it, and emits it with the projections; the completion hook
(`_land_records`, riding the same terminal-run chain as B3/B4) lands
the emitted book back in the drawer verbatim — it is the user's own
content, exportable with the node. The trio's REAL deterministic
functions replaced the P1 filing scripts in the catalog (same birth
gates, subprocess-proven): Calendar keeps events with the day plain
words name (today/tomorrow/weekday/ISO — undated is honest, never
guessed) and projects ``events_today``/``events_week`` — standing on
the port index for the trigger to consume (B4); Tasks adds, marks
done by words, projects ``open_tasks``, and a DATED task emits a
``reminder_offer`` the conversation turns into a standing question —
the reminder is created only on the user's yes; Reminders parses the
day and clock ("call the bank tomorrow at 9") and its emitted ask is
filed into the standing ``ReminderStore`` AS THE OWNER by the
`_file_reminder` hook (law 4: the sandbox never touches host
stores), audited ``reminder.filed``, delivered by the client's
standing poll. En route, forms became real for node runs: POST
``/v1/runs`` accepts ``values``, checked through B1's ONE strict
door (`validate_user_inputs` over the declared inputs' own labels —
unknown keys refuse in words) and bound into ``bindings.json``
verbatim (`tests/test_records_trio.py`). Remaining: none for P2; P3
inherits the record discipline.

*The three shapes every life runs on, each a node that keeps its own
book.*

- **One record discipline**: each node keeps its rows as files in its
  own drawer (`records/…`, JSON rows) — the drawer IS the personal
  content (law 1), exportable with the node, governed by the standing
  sweep. Every mutation is a RUN of the node's function (add, done,
  list, today), so B3 files every change's io and the interact window
  answers "what did you produce last" for free.
- **Calendar**: add an event in words ("dentist Tuesday 3pm"),
  `today`/`week` projections emitted as the result — the slot other
  nodes consume (`events_today`).
- **Tasks**: add / done / open-list; produces `open_tasks`; a task
  with a due date OFFERS a reminder (B4's offered-default doctrine —
  suggested, confirmed, never silent).
- **Reminders**: the node face of the standing `ReminderStore` — its
  function emits the reminder row as its result, and the completion
  hook (law 4) files it into the store the client already polls;
  listing reads the store back through the node's declared output.
- Acceptance: "add dentist tuesday 3pm" through the calendar node's
  form or interact window lands a row in the drawer and the run's io
  in `runs/`; the trigger node can consume `events_today`; a task's
  due date offers (and only on yes creates) a reminder that fires
  through the standing delivery; every ask uses B1 labels — zero
  mechanism words.

### Phase P3 — the business trio: stock, cashflow, invoice scan

**Status: LANDED** — the trio joined the P2 record discipline with
real deterministic functions (same birth gates, subprocess-proven).
Stock parses movements and floors in plain words ("received 40 boxes
of paper", "keep at least 10 …" — an unreadable movement records
NOTHING and says how to say it), levels are a PROJECTION summed from
the movement ledger at every read (M1's law), and a level under its
floor emits the restock offer the P2 reminder machinery turns into a
standing question. Cashflow reads amount, direction, and day from
the entry's own words (an unclassified direction is read as money in
and SAYS so), and every run emits the chart REBUILT from the ledger
alone — a self-contained HTML/SVG at all four scales (day / week /
month / year, in as the calm blue, out as amber below the line),
landed in the drawer by the new `_land_emitted_files` hook alongside
the invoice sheet (``records/cashflow.html``, ``records/
invoices.csv``). Invoice Scan reads a DELIVERED document — the new
generic attachment stage: a binding that names a file in the node's
``messages/`` drawer rides into the sandbox as ``attachments/<name>``
(a ``_attachments`` carrier, data like ``_records``) — parses total /
date / vendor deterministically, refuses in words when no total
reads (nothing guessed, nothing landed), and emits the parsed
``entry`` as a standing output the EXISTING B4 hand-off offers to
Cashflow: running Cashflow alone asks, yes binds the exact words
through the binder, and the movement lands run-cited on the graph
(`tests/test_business_trio.py`). Remaining: the author-seat model
consult for invoices the deterministic parse cannot read — the
worded refusal stands either way; the model door can land with a
later phase.

*The small-business spine: what's on the shelf, what the money did,
and the paper that proves it.*

- **Stock** (inventory of goods — items and quantities, not
  equities): in/out movements as runs ("received 40 units of X",
  "sold 3 X"), current levels as a projection over the movement
  ledger (M1's law — derived, never stored as a second truth), a
  `low_stock` output naming items under their floor — which OFFERS a
  reminder/inbox item, confirmed like every hand-off.
- **Cashflow**: money-in/money-out entries (each with date, amount,
  words), producing a `cashflow_summary` and a CHART — an HTML/SVG
  file rendered into the drawer at the investor panel's time scales
  (day/week/month/year), served on the node's page. The chart is a
  projection: delete it and the next run redraws it from the ledger.
- **Invoice scan**: a dropped file (the drawer's `messages/` inbox —
  the forward door already lands files there) parsed into structured
  rows appended to `records/invoices.csv`. Parsing is the ONE
  starter function that may consult the model (the author seat,
  metered, B0's laws standing); with no model configured it refuses
  in words — never a guessed number. Every extracted row passes the
  B1 strict value check before it lands, and the row OFFERS itself
  to Cashflow as a hand-off (B4) — an invoice becomes a cashflow
  entry on a yes.
- Acceptance: stock levels equal the movement ledger's sum at every
  read; a level under the floor stands as an offer until answered;
  the cashflow chart file regenerates from the ledger alone and the
  page shows it at all four scales; a scanned invoice's rows appear
  in the sheet only after the value check, and a yes lands the
  cashflow entry with a run-cited hand-off edge.

### Phase P4 — the automated life: the pieces wired

**Status: LANDED** — the Automation Trigger node is the pulse worn
as a node: its deterministic function parses a rhythm in the
sandbox (daily/weekly/monthly/yearly, the same shapes as the chat
ear, self-contained) and EMITS the parsed schedule; the
`_file_schedule` completion hook keeps the beat in the standing
``PulseStore`` as the owner — once per run, ``pulse.created`` cited
with the run — while an unreadable rhythm refuses in words and sets
nothing. Its every run also emits ``fired_at``/``occasion`` onto the
port index, routable upstream like any standing output. The morning
pulse seeds DISABLED with the shelf (a daily 09:00 schedule whose
goal is its own recognized sentence) and switches on in one plain
sentence ("turn on the morning pulse"); its fire runs the owner's
Calendar and Tasks as ORDINARY pulse-stamped runs and lands the two
answers as ONE message through the reminder channel the client
already polls — two accounts get two different mornings from the
same shelf, proven side by side, and "what fired you, when" answers
from the stores: every fired run's metadata names the schedule and
occurrence, the claim names a fired run. The cross-node offers
(invoice→cashflow, task→reminder, stock→reminder) stood from P2/P3
(`tests/test_automated_life.py`). Remaining: none — the plan is
complete.

*The seven nodes plus the pulse equal automation nobody had to
build.*

- **Trigger → anything**: the Automation Trigger node is P0's
  schedule store worn as a node — its form and interact window
  create/list/cancel schedules; its declared output (`fired_at`,
  `occasion`) makes it routable upstream of any node (B4 edges), so
  "every Monday, run the stock check" is a route, not a feature.
- **The morning pulse** (the shelf's shipped example): a daily
  schedule seeded DISABLED with the shelf — switched on in one
  sentence — that runs Calendar's `today` and Tasks' `open_tasks`
  and lands the combined result as OoLu's own message (the
  reminder-delivery channel): the day's shape, before it starts.
- **Cross-node offers standing**: invoice→cashflow, task→reminder,
  stock→reminder — each an offered, cited hand-off; the interact
  window and run detail name what moved, from whom, in which run
  (B4's visibility).
- Acceptance: enabling the morning pulse yields, next tick after
  9am, one message containing that account's OWN events and tasks
  (two accounts get different messages from the same shelf); the
  schedule→node route answers "what fired you, when" with run ids;
  every offer binds only on a yes.

---

## 4. Sequencing and the loop-closure rule

P0 → P1 → P2 → P3 → P4. P0 is independent and first — it is the
"trigger automatically" half of the ask and P4's engine. P1 needs
only the catalog and the auth doors. P2 and P3 fill the shelf's
functions (P2 before P3: the records discipline is set by the simple
trio, the business trio inherits it). P4 wires what P0–P3 built and
ships the story.

The loop-closure rule, inherited: each phase ships one test driving
the full circle through the real doors — a schedule fires a real run;
a fresh account runs a starter node it never built; a calendar entry
made in words is consumed by another node; an invoice row lands in
cashflow with a citation; the morning pulse says two different
mornings to two different people.

## 5. Metrics (into the standing investor catalog, group `building`)

- `starter.accounts_seeded_pct` — accounts holding (or having
  deliberately deleted) the shelf; target 100% of new accounts.
- `starter.nodes_alive_30d_pct` — seeded starter nodes still alive
  after 30 days — the shelf's usefulness, honestly measured against
  the delete button.
- `pulse.schedules_active` — standing enabled schedules.
- `pulse.fires_daily` — scheduled runs fired today; the automation
  the user asked for, visible as a number.
- `pulse.catchup_fires` — catch-up fires after host sleep (direction
  down: a healthy host rarely needs them).
