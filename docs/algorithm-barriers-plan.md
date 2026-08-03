# The three algorithm barriers — review and build plan

Status: Proposed. Three phase series, one per barrier: **F0–F3** (the
program node), **W0–W5** (route paving — the Paver), **G0–G3** (gate
edges). Every phase Proposed; each lands as one commit titled
`<CODE> landed: <name> — <subtitle>` with its loop-closure test, the
plan-doc status flip, and the CHANGELOG entry in the same commit.

The three barriers, in the words of the ask:

1. **Node building.** OoLu still has a large gap for building a
   *functional program* in a node — larger than a few CLI scripts,
   smaller than an app; most interfaces internal (backend-to-backend),
   with **one unified interface** presenting the resulting information.
2. **Route sweeping.** A backend capability that links related nodes and
   makes them communicate deterministically — a coding-agent-like pass
   that **codes and tests the path to full**, so that when a user
   triggers one node, a whole **web** of nodes fires efficiently.
3. **The edge.** An edge on a route may not be a simple connector — it
   may be a **logic gate**: a loop, an OR, a guard — to suit the
   complexity of a real-world system.

One naming correction up front, because the word is load-bearing:
**"sweep" is already taken three ways** in this repository —
`runtime/sweep.py` (CAS garbage collection + `SweepScheduleStore`),
`nodeplace/hygiene.py` (policy sweep), and the representative's drafting
sweep (`POST /v1/representative/sweep`). The barrier-two capability is
therefore named **the Paver** — in the road-network idiom the engine
already speaks: the Paver surveys candidate roads, negotiates junctions,
pours and load-tests the connective code, and opens the route to
traffic. Likewise barrier one always says **program node** (bare
"program" collides with the actuation edge's `MachineProgram` /
`ProgramIR`, `src/oolu/actuation_edge/program_ir.py`), and barrier three
says **gate edges**.

Companion reading: `docs/node-generation.md` (the node-synthesis laws),
`docs/conversational-building-plan.md` (B0–B4, landed),
`docs/context-harness-plan.md` (the authoring harness, landed),
`docs/route-finding-proof.md` (why routing is deterministic search, not
model whim), `docs/node-bundles.md` (the artifact side of big programs),
`docs/memory-stack-plan.md` (M3 negative knowledge, M4 skill induction),
`docs/adr/0002-unified-run-state.md` (durability seams).

---

## 0. The review, in one page

Everything below was verified by direct read at HEAD; the load-bearing
citations ride each Part.

**Barrier one is real, and it is an *authoring* gap, not an artifact
gap.** The artifact substrate for big programs already exists: a node's
drawer tree freezes into a content-addressed bundle (4096 files /
64 MiB, `runtime/bundle.py:51-52`), rides into the sandbox in one
staging pass, and joins the execution cache key
(`runtime/script_node.py:358-365`). But **nothing generates a tree**.
The builder prompt demands "ONE complete, self-contained Python script"
(`chat.py:1004-1055`); publish lands exactly one file
(`_land_src` → `src/main.py`, `gateway/app.py:7380-7413`); birth
verification runs the candidate with `files=None, bundle=None`
(`script_node.py:676-707`), so a multi-module program cannot even be
*judged* as itself at birth. Result presentation is words and
attachment-downloads — a node-produced HTML page is served with
`Content-Disposition: attachment` (`app.py:10802-10809`), never a
rendered view. There is no unified presentation surface.

**Barrier two is half-built, and the missing half is exactly what was
asked for.** Deterministic route *discovery* exists
(`ContractAssembler.assemble` backward-chains wanted slots to producers,
`orchestrator/assembler.py:158-225`); the dataflow *primitive* exists
(`output://{producer}/{port}` refs resolve through the port index,
`values.py:213-296`). But the port index is fed only after single
node-function runs complete (`_file_run_values`,
`app.py:18272-18301`) — contract runs file **no** per-child values, so
inside a multi-node run a consumer would be refused (compiled actions
carry no tenant, `orchestrator/contract.py:157-169` +
`script_node.py:341-351`). Cross-node data movement is human-gated per
value (B4 handoffs, `app.py:6813-6843`). Trigger doors fire exactly one
node (`app.py:8008-8059`). There is a foreground, consent-gated coding
agent (`NodeAuthorAgent`, `author.py`) — there is **no standing backend
pass** that finds related nodes, writes and tests the connective code,
and promotes a web a single trigger can fire.

**Barrier three is genuinely open — the narrowest gap and the sharpest.**
The entire executable edge vocabulary is two literals:
`relation: Literal["before", "fallback"]` (`orchestrator/state.py:121-137`,
mirrored at `skills/contract.py:161-169`). AND-join is hardwired —
readiness is `all(deps SUCCEEDED)` after edge identity is erased into
dep-sets (`orchestrator/scheduler.py:172-199, 321-327`). Loops are
structurally refused at preflight (`scheduler.py:168-169, 210-217`).
The only conditional is the fixed failure predicate behind `fallback`.
Meanwhile a deterministic, model-free predicate language already exists
and is edge-ready (`predicates.py` — "a check NEVER raises"), used today
only for node pass/fail — never for routing. And the *inner* agent loop
already runs principled bounded loops (`graph/edges.py`,
`EdgePolicy.max_recalcs`) — the precedent for gates at the route layer.

**How they compose.** Gate edges (G) give routes the shapes real systems
need; the Paver (W) fills those routes with tested connective code and
makes one trigger fire the web; program nodes (F) give the web's
stations real depth and its user one face. G0, W0, and F0 are each
buildable today, independently.

---

## 1. The laws this plan builds under

A change that violates one of these is a release blocker, in any phase:

- **Verify by execution before publish.** Every authored byte passes
  `screen_script` and runs in the sandbox before it is trusted, cached,
  or listed (`script_node.py:803-836`); birth verification stays
  repair-free (`verify_function` heals imports only,
  `script_node.py:684-690`).
- **Two-phase sandbox, severed network.** Phase A installs from a pinned
  index; the network is disconnected and severance *verified* before
  Phase B runs synthesized code (`runtime/isolation.py:7-10, 681-691`).
  No residency, no listeners, no daemons — ever.
- **Models propose; the type system disposes.** LLMs never author routes
  (route-finding-proof); model advice enters as bounded pseudo-
  observations or candidate code, disposed by exact `Slot.matches` and
  verify-by-execution. No model ever routes at run time.
- **Consent is called and asked, never assumed.** Standing background
  work happens only under an approved, audited enable
  (the `SweepScheduleStore` doctrine, `runtime/sweep.py:257-303`);
  autobuild rides `account.autobuild_consent` and the growth trigger;
  B4's ask-first handoff remains the default for anything unpaved.
- **Approval friction is a feature.** Reserved / irreversible /
  audit-hold semantics (`app.py:15141-15209`) hold *inside* every new
  path. No bypass flag, ever.
- **Determinism and replay.** The unified run state round-trips
  (ADR-0002); execution records are append-only; caches key on content
  fingerprints; data (records, books, state) rides outside cache keys,
  code inside.
- **Local-first.** New stores are tables on the shared durable SQLite
  conn (or the drawer); projections are derived, rebuildable, never
  authoritative (M1 law).
- **Loud refusal.** Malformed specs, edges, loops refuse by name at
  construction, not at run time.

---

## 2. What already exists (the seams are cut)

| Seam | Where | What it gives this plan |
|---|---|---|
| Content-addressed bundles | `runtime/bundle.py`, `_freeze_tree` (`app.py:7874-7908`) | F: the artifact form of a program tree, cache-keyed |
| Birth gate | `verify_function` (`script_node.py:676-753`), `_build_function_node` (`app.py:6960+`) | F, W: the one door any authored code passes |
| Author agent + seats | `author.py`, `seats.py`, context packs (`contextpack.py`) | F: the bounded authoring loop to extend; W: the adapter author |
| Transactional landing | `_land_src` / B2 (`app.py:7380-7460`) | F, W: code becomes drawer truth loudly or not at all |
| Records/attachments data seam | `script_node.py:390-404`, P2/P3 hooks | F: the state-is-data-not-code precedent |
| Life books discipline | `lifebooks.py` (typed rows, dedup, pointer) | F: the shape program state reuses |
| Backward-chaining assembler | `orchestrator/assembler.py` | W: deterministic web discovery core |
| `output://` binder + port index | `values.py:213-296`, `script_node.py:339-383` | W: the dataflow primitive, awaiting a mid-run feeder |
| DAG scheduler | `orchestrator/scheduler.py` (`DagRouteRunner`) | W: parallel web execution; G: the machine that learns gates |
| Predicate language | `predicates.py`, `Postcondition` (`skills/models.py:44-59`) | G: the guard language — no new dialect |
| Inner-loop gate precedent | `graph/edges.py` (pure `state -> next`, loop ceiling) | G: the pattern `gates.py` copies |
| Scheduled-pass discipline | `SweepScheduleStore` (`runtime/sweep.py:243-350`), lazy tick (`app.py:12690-12712`) | W: the Paver's heartbeat, consent-first |
| Durable outbox | `durable/outbox.py` (`TransactionalOutbox.relay` — zero callers today) | W: deferred propagation, exactly-once |
| Negative knowledge | `negative.py` (M3, graduated block) | W: the Paver never grinds a failing junction |
| Trace store + posteriors | `knowledge/traces.py`, `scheduler.py:436-463` | W, G: verified history; G must keep it honest |

---

# Part I — Barrier one: the program node (F-series)

## I.1 Current state (verified)

- A node's function is one file: `src/main.py` **is** the function
  (`_drawer_function`, `app.py:272-305`); the published skill carries a
  single `adapter="script"` action `{goal, script, node_key}`
  (`app.py:7241-7252`).
- Authoring is single-script by construction: the one-script /
  `emit_result`-exactly-once prompt (`chat.py:1004-1055`); ≤2
  edit-don't-rewrite repair rounds (`app.py:7147-7186`). The preferred
  authoring path is the **agentic** `NodeAuthorAgent` (12-step bounded
  loop with `verify_function` and `finish_node` hands, `author.py`;
  wired at `app.py:7086` via `_author_function`), with one-shot
  `author_node_function` as the fallback for models without tool
  calling. Any program-authoring design must extend this agent, not sit
  beside it.
- Birth verification ignores trees: `verify_function` hardwires
  `files=None, bundle=None` (`script_node.py:705-707`).
- Screening covers only the entry script: `screen_script` applies to the
  script argument (`script_node.py:817`); staged files and bundles ride
  in unscreened (`backend.py:152-158`, `bundle.py:31-35`);
  `mock_smells` likewise sees only the entry (`app.py:9971-9976`).
- Execution is one-shot batch under 30 s / 512 MB / 64 MB scratch
  defaults (`backend.py:92-114`); the runner pins one `ResourceLimits`
  for every node (`script_node.py:256, 270`), though the backend
  contract allows per-request limits (`backend.py:86-88, 135`).
- Persistent state is one hardcoded book (`records/rows.json` staged as
  `./records.json`, `script_node.py:390-404`); emitted files land back
  **verbatim** (basename-flattened, size-capped 5 × 1 MB,
  `_land_emitted_files` `app.py:17853-17907` — there is no scrub at this
  seam, by the own-drawer doctrine of `_land_records`).
- Presentation: "last result" is a sentence around `outputs.json`
  (`chat.py:1538-1552`); drawer files download, never render; every
  response carries `X-Frame-Options: DENY` and
  `frame-ancestors 'none'` (`http.py:17-24, 75-77`), and sessions are
  bearer-header-only (`http.py:49-53`) — so an iframe-able,
  tenant-scripted view is **not** reachable with today's headers or
  credentials, and `oolu host` binds beyond loopback
  (`cli.py:349`), so "loopback" is a deployment mode, not an invariant.
- Internal interfaces don't exist: the only interface vocabulary is
  routing-level `Slot`s with exact matching (`skills/contract.py:74-99`);
  module-to-module interfaces inside one node have no representation.

## I.2 Target design

**Reframe (keeps every law):** a "backend service" in OoLu is *not* a
daemon — Phase B forbids residency, and that stays. It is a **program
node**: a drawer tree of internal modules with declared module-level
interfaces, a set of internal **operations** (backend-to-backend
functions invoked per run through one deterministic dispatcher), named
**program state** files in the drawer, and exactly **one unified
interface** — the node's declared ports plus one rendered **view** —
presenting the program's standing result. The external contract
(Slots/ports) stays singular; internal interfaces never touch routing.

### Data models — new `src/oolu/skills/program.py`

```python
class OpSig(BaseModel):        # internal, NOT a Slot — invisible to routes
    name: str; takes: list[str]; returns: str; description: str = ""

class ModuleSpec(BaseModel):
    path: str                  # "lib/ingest.py" (POSIX-relative, bundle-safe)
    purpose: str
    api: list[OpSig]
    depends: list[str] = []    # sibling module paths this module imports —
                               # authoring order is the topological order,
                               # cycles refuse by name at parse
    check: str | None = None   # "tests/check_ingest.py" — per-module birth check

class OperationSpec(BaseModel):
    name: str; entry: str      # "lib.report:build" — dispatcher target
    reads: list[str] = []; writes: list[str] = []   # state names

class StateSpec(BaseModel):
    name: str                  # drawer "state/<name>.json", staged "./state/<name>.json"
    kind: Literal["rows", "value"] = "rows"   # rows reuse the Life-books
    schema_hint: str = ""                     # typed-row discipline (lifebooks.py)

class ViewSpec(BaseModel):
    kind: Literal["ports", "html"] = "ports"  # html is F-follow-on, see I.4
    entry: str = ""

class UnifiedInterface(BaseModel):   # singular BY CONSTRUCTION — one field
    operation: str = "main"          # the ONE externally-invoked operation
    ports: list[dict] = []           # runtime/contract.py port vocabulary
    view: ViewSpec = ViewSpec()

class ProgramSpec(BaseModel):        # frozen; canonical (sorted) JSON → stable bundle_id
    modules: list[ModuleSpec]; operations: list[OperationSpec]
    state: list[StateSpec]; interface: UnifiedInterface
    limits_profile: Literal["step", "program"] = "step"

def parse_program_spec(raw) -> tuple[ProgramSpec | None, str]  # ("", problem) refusal style
```

Refusals at parse, by name: more than one interface (structural — the
field is singular), more than `MAX_PROGRAM_MODULES = 12` modules,
dependency cycles among modules, mechanism-flavored labels (reuse the
B0 lexicon, `plainlanguage.py`), and any declared output port named
with a **reserved payload key** — `state`, `files`, `records` — the
keys completion hooks consume as side channels (`app.py:17862, 17936`;
today this collision is silent, the check is new and also guards the
single-file path).

**Home:** `src/program.json` in the drawer tree — data the node ships,
riding the bundle, so spec edits change `bundle_id` and force re-verify
(`script_node.py:358-365`). `NodeContract` is untouched: ProgramSpec is
implementation (skill-side); the citizen face remains one
`consumes/produces/inputs` contract, and internal `OpSig`s never enter
`derive_data_edges` slot unification.

### The builder — extend the author agent, not bypass it

New `src/oolu/programbuilder.py`, wired as new **hands on
`NodeAuthorAgent`** (the agentic path is the primary path; the one-shot
fallback gains a mirrored staged pipeline):

1. `plan_program` — one consultation under new seat `node.plan_program`
   (register in `seats.py`) returns a `ProgramSpec` or a refusal.
2. `author_module` — per-module consultation in `depends`-topological
   order; context = spec + signatures of already-written modules
   (budgeted via `contextpack.py`).
3. `check_module` — runs the module's check script through
   `verify_function` **with the partial tree staged** (F0's new
   parameters); a failing module gets ≤2 repair rounds
   (`repair_node_function` shape) *before the next module is authored* —
   fail fast per module, never whole-program repair at the end.
   **Every authored module and check script passes `screen_script` and
   `mock_smells` as text at authoring time** — the entry-only screening
   gap above is real, and a program build closes it for its tree; a
   module whose check merely re-asserts its own fabricated output is
   the exact mock the smell screen exists to catch.
4. `render_dispatcher` — the generated `src/main.py` is a
   **deterministic template, never model-written**: selects
   `_operation` (default `interface.operation`), loads `./state/*.json`
   that the operation declares, imports `lib/*`, calls the entry,
   `emit_result` exactly once. When the interface declares **no
   inputs**, the template **omits the `bindings.json` read entirely** —
   the static birth wall refuses a bindings-reading script with no
   declared inputs (`app.py:9983-9988`), and the wall is right.

**Trigger (v1): explicit request only.** A regex sibling of
`_NODE_BUILD_RE` ("build me a program that …", "program node …")
routes to the program pipeline *before* any single-file authoring
happens. No plan-sniffing fork: a fork placed after single-file
authoring would pay for a discarded build, and a plan step on every
build would tax the common path. Implicit promotion ("this goal smells
multi-stage") is future work, listed in the decision log.

Same twin guard, negative-knowledge block, meter and receipt, reviewer
(the `node.review` seat additionally receives `program.json`), build
ledger states per module (`module:lib/ingest.py:verified`).

**Landing:** new `_land_tree` generalizing `_land_src` — writes
`src/main.py`, `src/lib/*`, `src/program.json`, `src/tests/*` through
one `DeskFiles` pass, one audit event, same loud `node.src_unlanded`
miss path. A tree that exceeds the inline staging walls (32 files /
2 MB, `isolation.py:55-56`) **pre-freezes to a bundle before publish**
— the birth gate then verifies with `bundle=` rather than `files=`;
the pre-publish freeze (a bundle without a node, adopted at publish) is
a named F0 work item.

### Runtime changes (`script_node.py`, constants in `backend.py`)

- `verify_function(goal, script, *, session_id, ports=None, files=None,
  bundle=None)` — thread the staged tree into `_run_script` (today
  hardwired `None`). **This is a shared seam: W3 (adapter verification
  with sampled values staged) consumes the same signature — land it
  once, in F0.**
- Per-node limits: action parameter `_limits_profile` → `"program"`
  widens to hard-clamped `PROGRAM_LIMITS_MAX` (wall ≤ 180 s, memory
  ≤ 1 GB, scratch ≤ 256 MB) through the sanctioned per-request
  mechanism (`backend.py:86-88`). **The widen is consented, not
  model-decided:** the profile is confirmed by the human in the build
  offer/receipt (the same consent posture as egress grants,
  `app.py:7337-7345`), audited, and clamped regardless. Align the
  polyglot wrapper's internal `timeout=240` (`polyglot.py:65`) with the
  effective wall in the same change.
- Program state: action parameter `_state: dict[name, json_str]` staged
  as `state/<name>.json` beside the records seam
  (`script_node.py:390-392`). **Data, never code: excluded from the
  cache key exactly like records**, so a cached program replays against
  current state. `kind="rows"` state reuses the Life-books typed-row
  discipline (`{at, label, value, note}`, dedup) rather than minting a
  third book concept.
- Cache/replay untouched: dispatcher fingerprint + `bundle_id` already
  key the cache.

### Completion hooks (`gateway/app.py`)

`_stage_state` (pre-run) and `_land_state` (post-run): payload key
`"state"` (now reserved, see refusals) lands to drawer
`state/<name>.json` **verbatim** — the node's own book, the P2 records
doctrine — plus a copy under `runs/<run_id>/state/` beside B3's
`inputs.json`/`outputs.json` so the audit chain reconstructs any run's
world. Emitted-file caps stay as they are in F; raising them is not
needed for state (state rides its own key) and widening the verbatim
seam is a separate decision this plan does not smuggle.

### The unified view

`GET /v1/nodes/{node_id}/view` → `_node_view`, session-authed:

- **v1 (F3): the deterministic server-rendered view.** For every node,
  free: a server-rendered page (template owned by the gateway, zero
  tenant-authored markup or script) showing the declared ports, the
  latest verified values (`_node_last_result` projection,
  `app.py:18252-18270`), run stamp, and the program's state names. Safe
  under today's security headers precisely because no tenant byte is
  interpreted; works on every deployment mode including shared public
  hosts.
- **Follow-on (not in F, named honestly): tenant-authored HTML views**
  (`ViewSpec.kind="html"`). Blocked today by three real walls the
  design must clear together, not wish away: every response carries
  `X-Frame-Options: DENY` / `frame-ancestors 'none'`
  (`http.py:75-77`), sessions are bearer-only so an `<iframe src>`
  cannot authenticate, and a token-in-URL hands the credential to the
  very inline script the view runs — with top-level navigation as an
  exfiltration path CSP `default-src` does not close. The follow-on
  requires a **scoped, short-lived, view-only credential** (the
  WebSocket `access_token` precedent, `asgi.py:93-105`), an explicit
  navigation lockdown, and a per-route header carve-out that is a
  deliberate, documented security decision — gated to owner-tenant
  sessions and off by default on shared hosts.
- Interact window: `_last_result_command` appends "view it at
  /v1/nodes/…/view" when the node is a program node.

**View files live in the drawer under `view/`, not `src/`** (when the
follow-on lands): the `node.build` seat's write scope is `src/`
(`seats.py:118-119`), `src/` is staged into every sandbox run and joins
the frozen bundle — a presentation-only edit must not change
`bundle_id`, invalidate the execution cache, or force re-verify. A
`node.view` write scope is part of that follow-on.

## I.3 Phases

**F0 — Program substrate: spec, tree landing, tree-true birth verify.**
*The tree becomes a first-class thing a build can produce and a birth
can judge.*
Changes: `skills/program.py` (models, `parse_program_spec`, refusals
including reserved payload keys and module-dependency cycles);
`verify_function` gains `files=`/`bundle=`; `_land_tree`; the
pre-publish bundle freeze for over-inline-wall trees; an internal
publish door accepting `{script, files, program}` that runs the birth
gate with the tree staged; per-module `screen_script`+`mock_smells` as
build-time text checks.
Tests: spec parse/refusals by name; canonical serialization stability
(same spec ⇒ same `bundle_id`); hand-written 3-module tree with check
scripts publishes and birth-verifies with the tree staged
(Subprocess/Stub backend); over-wall tree freezes then verifies via
`bundle=`; `node.src_unlanded` fires on a broken store; reserved-key
port refused; existing single-file birth tests unchanged;
`verify_function` default-args regression (both existing call sites).
Done when: a hand-authored multi-module program node publishes through
the gate, its checks run in-sandbox against the staged tree, and its
bundle round-trips freeze → run → cache hit. **No dependency on the
other barriers; buildable today.**

**F1 — The program author: plan → module loop → deterministic
dispatcher.** *The builder writes trees, one verified module at a time.*
Changes: `programbuilder.py`; program hands on `NodeAuthorAgent` +
mirrored one-shot fallback; explicit-request regex routed **before**
single-file authoring; `node.plan_program` seat; per-module ledger
states; reviewer sees `program.json`; meter covers the consultation
bound.
Tests: scripted-model author births a 3-module program end-to-end;
module check failure → bounded repair → next module; plan refusals (two
interfaces, 13 modules, module cycle) refuse by name; zero-input
program passes the static birth wall (no `bindings.json` read in the
dispatcher); single-file goals take the old path byte-identically;
authored module text failing `mock_smells` refuses before any sandbox
run.
Done when: "build me a program that ingests X, computes Y, reports Z"
births a ≥3-module node passing the birth gate, with the model bill
bounded by 1 + M×(1 author + ≤2 repairs) consultations (each check run
may cost up to 3 backend executions for dependency healing — the
receipt says so honestly).

**F2 — Program state + the program limits profile.** *The program keeps
its own books; the sandbox stays hostile.*
Changes: `_state` staging + cache-key exclusion; `_stage_state` /
`_land_state` + `runs/<id>/state/` copies; `_limits_profile` stamping,
`PROGRAM_LIMITS_MAX` clamps, consent surfaced in the build receipt;
polyglot timeout alignment; rows-kind state through the lifebooks row
discipline.
Tests: run 2 reads run 1's state; cache key identical across state
changes (replay determinism pinned); clamp test (requested 10 h wall ⇒
180 s); state lands verbatim to the drawer and copies under `runs/*`;
records seam untouched; a port named `state` still refuses at parse.
Done when: a program node accumulates typed rows across runs, its
cached script replays against current state, and replayed history
reconstructs each run's staged state from `runs/*` alone.

**F3 — The unified view, deterministic form.** *One face: every program
node renders its standing result.*
Changes: `_node_view` route; the server-rendered ports/state/run-stamp
template (gateway-owned, zero tenant bytes interpreted);
`_last_result_command` pointer; shell panel link.
Tests: authed fetch renders the standing result inline; foreign-tenant
404; unauth 401; no-program node gets the same free view of its ports;
security headers unchanged (pinned); `/v1/files` attachment behavior
unchanged.
Done when: a browser shows a program node's rendered standing result,
updated after each verified run, with zero tenant-authored bytes
executed — on every deployment mode.

## I.4 Risks and invariants

- **One interface by construction** — singular field, deterministic
  dispatcher (never model-written), internal OpSigs invisible to
  routing. The unified face cannot be mocked *because* it is generated;
  the modules behind it are screened as text and verified by check
  scripts because *they* are where the model writes.
- **Sandbox posture unchanged** — trees ride existing bundle walls;
  severance untouched; no residency; the F3 view interprets no tenant
  bytes. The HTML-view follow-on is named, with its three walls, so
  nobody lands it casually.
- **Determinism/replay** — canonical `program.json` keeps `bundle_id`
  stable; state is data outside the cache key (records precedent);
  every tree edit re-verifies via bundle-in-key.
- **Must not regress** — single-file birth path byte-identical; twin
  guard, negative knowledge, B2 landing, honest-`emit_error`-at-birth
  all stand; `verify_function` keyword defaults keep both existing
  callers.
- **Vocabulary** — "program node", "operations", "program state",
  "the view"; never bare "program", never "app", never "sweep".

---

# Part II — Barrier two: route paving — the Paver (W-series)

## II.1 Current state (verified)

- Deterministic discovery exists: backward-chaining with Beta-posterior
  choice, gap-filling `ScriptBody` nodes, one `SubgraphBody` result
  (`assembler.py:158-225, 316-338`). Producer choice scans the whole
  library per slot — no slot index (`_pick_producer`,
  `assembler.py:237-242`; conceded in `route-finding-proof.md`).
- Ordering is derived; dataflow is not wired: `derive_data_edges` yields
  ordering-only edges; compiled bindings are compile-time-static copies
  (`orchestrator/contract.py:157-169`).
- The `output://` binder exists and works — fed only post-terminal,
  single-node (`_file_run_values` requires `Phase.COMPLETED` +
  node-function metadata, `app.py:18272-18301`). Contract runs file no
  per-child values; compiled actions carry no `_value_tenant`, so
  inside a DAG run a consumer's `output://` ref is **refused** —
  never resolved stale (`script_node.py:341-351`, `values.py:356-368`).
- Three id namespaces name "the same node": the desk `node_id`
  (what `_file_run_values` files under, `app.py:18291`), the
  `skill_id` (node_key `node:{skill_id}`, `app.py:8047-8053`), and the
  marketplace listing `version_id` (what assembled child contracts
  carry, `nodeplace/economics.py:209`). Any mid-run value piping must
  normalize to **one canonical producer key** or fast and slow paths
  will file under different names.
- Consumer-side lookup doesn't exist (`producers_of` has one caller;
  no consumers-of; `TransactionalOutbox.relay` has zero non-test
  callers — a built, unused deferred-delivery seam).
- Cross-node data movement is human-gated per value (B4); trigger doors
  are single-target (webhook → one node's function; pulse → goals;
  multi-node = POST an assembled contract).
- The standing-pass discipline is proven (lazy tick, fleet-exactly-once
  claim, consent-first enable — `sweep.py:243-350`,
  `app.py:12690-12712`); the coding agent is foreground-only
  (`NodeAuthorAgent`, growth trigger, `autobuild_consent`).
- Load-bearing cache subtlety: the script cache keys on the
  **resolved** bindings fingerprint (`script_node.py:290-310`), so a
  gap node with no landed script re-invokes synthesis on every fresh
  input. Determinism under fresh data requires a **landed provided
  script** in the drawer, not cache hits.
- Contract-run completion bypasses `_record_function_verification`
  entirely (`execute_contract`, `nodeplace/execution.py:240-380`) — the
  post-terminal seam fires only for single node-function runs. And the
  unreserved contract-run door requires market machinery
  (`_require_market` + attribution, `app.py:15077-15081`) — a
  personal, market-free install has no multi-node door at all.

## II.2 Target design

New package `src/oolu/paver/` + surgical core changes. Everything the
Paver authors passes the same doors as any node: screen → sandbox
verify → transactional landing → `needs_verification` listing.

### Models (`paver/contracts.py`)

```python
class WebEdge(BaseModel):
    source: str            # canonical producer key: the desk node_id
    target: str
    slot: str              # slot carried (post-adapter = consumer's name)
    kind: Literal["direct", "adapted"]
    adapter_id: str | None
    status: Literal["candidate", "paved", "broken", "retired"]
    evidence: dict         # verify reports, rehearsal run_ids, paved_at

class RouteWeb(BaseModel):
    web_id: str; tenant: str
    anchor: str            # node whose trigger fans out
    edges: list[WebEdge]
    contract_id: str | None    # registered SubgraphBody once paved to full
    status: Literal["surveying", "paving", "paved", "stale"]

class PaveReport(BaseModel):   # one tick's audited outcome
    surveyed: int; candidates: int; adapters_built: int
    rehearsed: int; promoted: list[str]; refused: list[dict]; cost: float
```

`paver/store.py` — `PaveStore` on the shared durable conn: tables
`paver_webs`, `paver_edges`, `paver_rehearsals`, plus the **node id
map** `(tenant, desk_node_id, skill_id, version_id)` — the canonical-key
translation layer the three namespaces above make mandatory — and the
**slot index** `paver_slot_index(tenant, slot, value_type, role,
node_id, side)`. Both are rebuildable projections over the registry
(M1 law), refreshed each survey, never authoritative.

`skills/index.py` — `SlotIndex.producers(slot)` / `.consumers(slot)`;
`ContractAssembler` gains optional `index=` so `_pick_producer` stops
scanning (behavior-identical ranking, pinned by parity test).

### Survey (`paver/discovery.py`)

`WebSurveyor.survey(tenant)`: builds the SlotIndex over the tenant's
contracts, emits `direct` candidate edges where `Slot.matches` holds
exactly, and *near-miss* records (same type+role, different name; same
name, convertible type) for the negotiator. Webs are connected
components anchored at nodes with external trigger doors — webhook
tokens directly; pulse schedules through a **goal → node resolution
step** (schedules fire goals, not nodes, `app.py:12777-12799`).
**Candidate edges are emitted only from producers that verifiably file
port values** — script-bodied nodes emitting the `emit_result` shape.
ActionsBody producers (whose `ExecutionOutcome`s need not carry
`evidence["result"]` at all) are excluded from auto-binding in v1; a
postcondition-evidence port mapping for them is future work, named in
the decision log — silently emitting edges whose ports can never fill
would be the exact non-determinism this barrier exists to kill.

### Negotiate and pour (`paver/negotiator.py`, `paver/adapters.py`)

`ContractNegotiator.negotiate(producer_slot, consumer_slot)` → `direct`
(exact), `mappable` (pure rename/projection → **template-generated
adapter, no model, deterministic bytes**), `convertible` (shape change →
LLM adapter). Model advice (seat `paver.match`) enters only as
candidate-pair suggestions, ProposalModel-style clamped — **advice
proposes pairs; only a passing test creates an edge**.

`author_shape_adapter` drives `NodeAuthorAgent` under seat
`paver.adapt`, context = both contracts + the producer's real last
filed port value as the test vector. Every candidate:
`screen_script` → `verify_function` **with the sampled upstream value
staged** (the F0 `files=` seam — the one cross-barrier dependency,
consumed here or landed here if W leads) + consumer-port output check →
≤2 repair rounds. Pass ⇒ the adapter lands as a real citizen:
contribute door, `provenance="synthesized"`, `needs_verification`,
drawer `src/main.py` landed transactionally. Fail ⇒ `broken` edge +
M3 negative record — the Paver never grinds the same junction.

### The Paver's loop (`paver/agent.py`)

`PaverAgent.tick(tenant, budget)`: **survey → negotiate → pour →
pre-provision → rehearse → promote**, budget-capped per tick (max
adapters, seat spend caps).

- *Pre-provision* ("code the path to full"): every gap `ScriptBody`
  in a web lacking a provided script gets one synthesized, verified,
  and **landed to its drawer** — runs become provided-script runs, so
  determinism stops depending on cache luck (the II.1 subtlety).
- *Rehearse* ("test the path to full"): assemble the web and execute
  end-to-end through `DagRouteRunner` on the sandbox backend. The
  rehearsal gate is **effect-freedom, not the verb rule**: every
  script hop runs network-severed with no egress grant (externally
  effect-free by construction, `isolation.py:7-10, 681-700`), so a
  web whose hops are all sandboxed scripts and/or read-class adapter
  actions rehearses fully; a web containing a write-class *adapter
  action* (http/cli/browser with real side effects) rehearses around
  it — that hop is verified per-node only and the edge is marked
  `paved(rehearsed=false)`, upgraded by its first real verified run.
  (The verb rule classes *every* script "run" = write-class,
  `skills/contract.py:245-250` — as a rehearsal gate it would forbid
  rehearsing exactly the code the Paver writes; the sandbox, not the
  verb, is what makes rehearsal safe.)
- *Promote*: a fully paved web registers as one `SubgraphBody` node
  with auto-bound dataflow baked into children's bindings; audit event
  `web.paved`. **This requires a body-preserving registration path**:
  today the contribute door takes a `ReusableSkill`,
  `NodeContract.to_skill` refuses non-ActionsBody
  (`skills/contract.py:303-309`), and the market library reconstructs
  listings as ActionsBody-only (`economics.py:201-216`) — so W2 names
  the nodeplace change (subgraph-preserving contribution + library
  round-trip) as a real work item, not a footnote.

### The heartbeat (`paver/routine.py`)

Parameterize `SweepScheduleStore` with `table: str = "sweep_schedule"`
(default preserves today's schema and tests byte-for-byte) and
instantiate a second store on `paver_schedule`. Same laws: enable is
the approved audited act; `claim_due` is one conditional UPDATE,
fleet-exactly-once; revoke stops the next firing cold. The tick rides
`_maybe_scheduled_sweep` with its own minute gate.

### Core engine changes (extend, don't duplicate)

- **Auto-bound dataflow at compile** (`_compile_subgraph`): for every
  derived data edge A→B where B is a `ScriptBody` and consumed slot `s`
  is unbound, inject `bindings[s] = "output://{A}/{s}"` (canonical
  producer key). Behind `wire_dataflow: bool = False` so existing
  callers are untouched.
- **Identity stamping at submission** — the real precondition for any
  mid-run piping, and a listed core change (not "wiring"): compiled
  subgraph actions carry no tenant and no owner. Extend
  `compile_with_owners` to return `owner_ids: dict[action_id,
  canonical_node_id]`, and stamp `_value_tenant` + owner onto blueprint
  actions **at submission** — the exact idiom the engine already uses
  for single-node runs (`engine.py:329-342`). A construction-time
  closure on the one shared `DagRouteRunner` cannot know the run's
  tenant (`app.py:703-704`; `execute()` receives only a RoutePlan).
- **Per-settle value piping**: `DagRouteRunner` gains a per-run pipe
  (threaded through `execute()`, not the constructor); on settle of a
  SUCCEEDED action carrying `evidence["result"]`, call
  `snapshot_outputs(tenant, payload, producer=owner)` — the same call
  `_file_run_values` makes, mid-run, idempotent (content-addressed,
  `values.py:174-210`). Fresh A-output reaches B's `bindings.json`
  within one DAG run, provenance riding the audit.
- **Contract-run completion hook**: `execute_contract` completions file
  per-child values and fire the same post-terminal notification the
  single-node path gets — without this, a paved web's own runs could
  never chain onward (`_record_function_verification` is one method
  with five call sites, and contract runs bypass all of them).
- **A market-free multi-node door**: the unreserved contract-run path
  requires market + attribution; the Paver's fast path must run on a
  personal, local-first install. Either a market-free contract
  execution mode (clearing skipped, everything else identical) or the
  per-edge slow path as the default where the market is disabled —
  decided in W4, dependency stated here.

### Trigger propagation (`paver/propagation.py`)

`WebTriggerRouter`, hooked at the post-terminal seam (now fired by both
single-node and contract runs):

- **Deferred, not in-request**: `on_filed` **enqueues** durable
  propagation work through `TransactionalOutbox` (its designed use —
  zero callers today) and returns; the lazy tick / a drain pass
  delivers. A webhook POST returns after the anchor's own run; the
  cascade never rides inside one HTTP request to `MAX_HOPS` depth.
- Fast path: anchor of a fully-paved web with standing propagation
  consent ⇒ submit one contract run of the web's registered
  SubgraphBody (parallel DAG, per-settle piping). Slow path (partially
  paved, or market-free v1): fire each paved-edge consumer as a
  single-node run with `extra_bindings={slot: "output://producer/slot"}`
  — the exact `_run_with_handoff` mechanism (`app.py:6933-6937`) minus
  the question, because consent now stands per-web.
- Bounds: `MAX_HOPS` (default 4), `MAX_FIRES_PER_TRIGGER` (default 16),
  durable dedupe on `(trigger_stamp, node_id)` via INSERT-OR-IGNORE
  claims (the pulse-occurrence pattern) — a cycle A→B→A fires each node
  once per trigger, across processes and restarts.
- Consent: `PropagationConsentStore` row per (tenant, web) — granted_by,
  revocable, sweep-schedule doctrine. Reserved/audit-hold nodes:
  propagation submits and **stops at the hold**, surfacing
  `awaiting_approval` — never routes around.

### End-state determinism chain

Trigger → anchor runs (provided script) → per-settle values file →
outbox delivers propagation → consumers run with `output://` bindings →
binder resolves exact stored bytes → provided scripts replay (no LLM
anywhere) → ports validated per hop → values + lineage + audit replay
the whole cascade. The model appears only at *paving* time, behind
screen + verify, its output frozen as drawer files before any trigger
can reach it.

## II.3 Phases

**W0 — Wire the dataflow (engine only; no agent, no LLM, no new
consent).** *Fresh data crosses one contract run.*
Changes: `orchestrator/contract.py` (auto-bind flag; `owner_ids`),
submission-time tenant/owner stamping, `orchestrator/scheduler.py`
(per-run value pipe), `nodeplace/execution.py` (per-child filing +
completion notification), the node id map, `skills/index.py`.
Tests: two-child contract, A produces `s`, B consumes `s` — B's staged
`bindings.json` holds A's *fresh* payload, provenance cites
`output://A/s`; the same logical node's values file under one canonical
key from both the contract path and the single-node path; SlotIndex
parity on `benchmarks/route_scale.py`; cycle rejection unchanged;
`wire_dataflow=False` callers byte-identical.
Done when: a hand-assembled 2-node contract run pipes fresh data A→B
with no human handoff, and both filing paths agree on the producer key.
*(Buildable today; no dependency on the other barriers.)*

**W1 — The Paver's map and heartbeat (survey + Routine; no code
written).** *An inspectable web map, refreshed on the tick.*
Changes: `paver/{contracts,store,discovery,routine}.py`;
`SweepScheduleStore` table parameterization (defaults preserved);
gateway: `paver_schedule` instance, minute-gated tick, HTTP
enable/disable/view + `GET /v1/paver/webs[/{anchor}]`, approve-gated
enable; goal→node resolution for pulse anchors.
Tests: mirror `test_sweep_schedule.py` (fires once fleet-wide; revoked
never fires); survey correctness on a synthetic library (direct edges,
near-misses, anchor detection, ActionsBody producers excluded from
candidates); existing sweep tests pass unmodified.
Done when: enabling the Paver yields a durable, inspectable web map
that refreshes on the lazy tick, with zero code authored.

**W2 — Pave direct webs (pre-provision + rehearse + promote).** *The
model bill is paid at pave time, not trigger time.*
Changes: `paver/agent.py` (tick loop, budgets); seats `paver.match`,
`paver.adapt`, `paver.build` in `seats.py`; pre-provision via the
existing synthesis ladder + verify + drawer landing; rehearsal under
the effect-freedom gate; **subgraph-preserving contribution** (the
nodeplace change: registration + library round-trip for SubgraphBody);
audit `web.paved`.
Tests: a paved web's second trigger performs **zero** synthesizer calls
(stub synthesizer un-invoked); a write-class adapter hop ⇒ per-node
verify only, `rehearsed=false`; an all-script web rehearses end-to-end
in the severed sandbox; promotion registers a SubgraphBody node visible
to `/v1/market/assemble` and it round-trips the library with its body
intact; tick budgets enforced; failed verify ⇒ `broken` edge + negative
record, no publish.
Done when: a user-triggered anchor of a fully-direct web executes the
whole web from provided scripts in one contract run, replayable.

**W3 — Adapter synthesis (the coding-agent half breaks the exact-name
wall).** *A junction becomes a tested, accountable citizen.*
Changes: `paver/negotiator.py`, `paver/adapters.py`; adapter landing
through contribute + `_land_src`; repair ≤2 rounds; consumes the
`verify_function(files=)` seam (F0; landed here if W leads).
Tests: rename near-miss ⇒ template adapter, byte-stable, no model call;
shape near-miss with a stub model ⇒ authored adapter passes verify with
the producer's real sampled value and the consumer's port check; a
candidate failing verify+repair creates **no** edge; absurd
`paver.match` advice cannot create an edge (containment, per
`test_route_finding_proof` idiom); the adapter earns its own posterior
as a citizen.
Done when: a producer/consumer pair separated by naming or shape
becomes a `paved(adapted)` edge whose adapter is a birth-verified,
drawer-landed, accountable node.

**W4 — Trigger propagation (one trigger, the whole web).** *One POST;
a deterministic, bounded, consent-gated cascade.*
Changes: `paver/propagation.py` (`WebTriggerRouter`,
`PropagationConsentStore`, durable trigger-stamp dedupe); outbox
enqueue + drain on the lazy tick; the post-terminal hook fired from
both run paths; the market-free door decision (see II.2); hold
surfacing.
Tests (loop-closure): one webhook POST on the anchor ⇒ downstream
consumers run with the fresh bound value end-to-end through real
doors, the cascade delivered by the drain, the POST returning after
the anchor alone; a cycle web fires each node once per trigger across
two processes; a reserved node halts propagation at a durable hold,
approver releases, web completes; revoking consent stops the next
propagation cold; bounds enforced; the audit chain reconstructs the
whole cascade from storage alone; a market-free install completes the
same cascade on the slow path.
Done when: "trigger one node → the paved web fires" is a single POST,
deterministic, bounded, consent-gated, replayable — on a personal
install and a market install alike.

**W5 — Gate-aware paving (after G2).** *The Paver speaks gates.*
Changes: the negotiator may propose guard edges (e.g. "only invoice
when total > 0" from SOP/near-miss evidence) and bounded retry loops
around flaky adapters — always as *candidate* gate edges, verified by
rehearsal, promoted only on pass; webs render their gates in
`/v1/paver/webs`.
Tests: a guarded web rehearses both branches under scripted evidence; a
loop-paved junction exhausts its budget loudly in rehearsal and is
refused promotion.
Done when: a paved web can carry `guard` and `loop` edges end-to-end.
*(The only W-phase with a G dependency; W0–W4 need none.)*

## II.4 Risks and invariants

- **LLMs never author routes**: edges exist only through exact
  `Slot.matches` or a *tested* adapter; advice is clamped candidates;
  no model ever writes a `WebEdge` row.
- **Consent law**: unattended paving only under the approved, audited
  enable + `autobuild_consent`; propagation opt-in per web, revocable;
  B4 ask-first stays the default for anything unpaved. Silent binding
  outside a consented web is a regression.
- **Approval friction survives propagation** — the web pauses at holds.
- **Sandbox invariants**: every Paver byte passes screen + severed
  sandbox verify; rehearsal safety derives from effect-freedom, never
  asserted; no listener, no daemon — a lazy-tick pass; PaveStore is
  local SQLite.
- **Determinism**: promotion refuses a web containing an unlanded
  ScriptBody; per-settle filing is idempotent; propagation dedupe is
  durable; blueprint cycle preflight stays intact (in-run loops arrive
  only via G, bounded; cross-run cycles bounded by trigger stamps).
- **Don't break the three existing sweeps**: `sweep_schedule` table and
  tests byte-identical; the Paver owns its own minute gate.
- **Cost containment**: per-tick budgets, seat caps, negative knowledge
  on broken junctions.

---

# Part III — Barrier three: the edge as a logic gate (G-series)

## III.1 Current state (verified)

- Edge vocabulary: `relation: Literal["before","fallback"]`,
  `provenance: Literal["sop","learned","data"]`
  (`orchestrator/state.py:121-137`; mirror `skills/contract.py:161-169`).
  No guard, no join mode, no iteration bound.
- AND-join hardwired; edge identity erased into
  `dict[str, set[str]]` before scheduling
  (`scheduler.py:172-199, 321-327`). OR-join inexpressible.
- Loops structurally refused (`scheduler.py:168-169, 210-217`);
  `derive_data_edges` deliberately emits both directions on mutual
  production so the cycle check fires. The only iteration anywhere is
  the inner loop's bounded recalc (`graph/edges.py`) and whole-route
  retry.
- The only conditional: fallback activation on
  `status in _TERMINAL_BAD` (`scheduler.py:269`), then a destructive
  in-place rewrite of dependents' dep-sets onto the repair branch
  (`scheduler.py:281-287`) — including retiring unfired fallbacks by
  writing `status[target]=SUCCEEDED` with **no outcome recorded**
  (`scheduler.py:276-279`) — a fact any evidence-reading gate must
  reckon with.
- A deterministic predicate language exists (`predicates.py`;
  `Postcondition`, `skills/models.py:44-59`), used only for node
  pass/fail.
- Success semantics assume every node runs
  (`all(effective_ok)`, `scheduler.py:395-408`); the only skip status
  is CANCELLED, conflating "branch not taken" with "cascade-cancelled".
- Idempotency keys are `{run_key}:{action_id}`; the gateway plan view
  parses `rsplit(":", 1)[-1]` (`app.py:355-359`). Trace recording joins
  outcomes back to actions by **exact key match**
  `f"{record.idempotency_key}:{action.id}"` (`scheduler.py:439-446`;
  same join in `nodeplace/execution.py:401-409`) — any iteration-key
  scheme must keep both joins whole.
- Billing note: marketplace clearing and the RunBinding commit **before
  the DAG runs** (`nodeplace/execution.py:281-342`) — "skipped children
  aren't billed" is not a property any scheduler change can deliver.
- Blueprints default `ordering="sequential"`; trace evidence promotes
  to `"graph"` only with sufficient observations
  (`adaptive.py:215-263`) — SOPs apply to sequential blueprints too.

## III.2 Target design

### Gate taxonomy

| Relation | Semantics | Gate |
|---|---|---|
| `before` (existing) | target waits for source SUCCEEDED | sequence; AND-join default |
| `fallback` (existing) | dormant repair branch on source failure | failure-OR — unchanged |
| `guard` (new) | admits iff source SUCCEEDED **and** `check(source.evidence, pointer, op, value)`; success with a failing guard **declines** | conditional; OR-split out of one source; XOR when guards are exclusive |
| `loop` (new) | back-edge tail→head with a **continue** guard + mandatory `max_iterations`; while the tail succeeds, the guard holds, and budget remains, the loop **region** re-enters with a fresh iteration index | bounded loop-with-exit |

Join mode moves to the **target**: `join: "all" | "any"` on
`ReservedAction`. AND-split is already free (fan-out). Timers and
cross-run event edges are explicitly out of scope (cross-run reactivity
is W4's trigger propagation; a per-edge `delay_s` is a later trivial
add).

**Not-taken is first-class:** `ExecutionStatus.SKIPPED` — terminal,
not-bad, not-success. Skip propagates through all-joins; a route with
SKIPPED nodes still succeeds.

### Data models (additive; defaults preserve every serialized object)

`skills/models.py`: `SKIPPED`; the guard type **is `Postcondition`
verbatim** — one predicate language, no dialect.

`orchestrator/state.py`, `BlueprintEdge` v2:

```python
relation: Literal["before", "fallback", "guard", "loop"] = "before"
guard: Postcondition | None = None   # required iff "guard"; the CONTINUE
                                     # condition iff "loop" (None = to budget)
max_iterations: int | None = None    # required, >=1, iff "loop"
```

with a model-validator enforcing the iffs — loud refusal at
construction. `ReservedAction` gains `join: Literal["all","any"] = "all"`.
`skills/contract.py`: `ContractEdge` mirrors the two fields;
`SubgraphBody` gains `joins: dict[child_id, "all"|"any"]`.

### `orchestrator/gates.py` — pure gate semantics, zero threading

Modeled on `graph/edges.py`'s pure-function pattern:

- `Admission = ADMIT | DECLINE | VETO | WAIT`;
  `admit(edge, source_status, source_evidence)` — `before`:
  SUCCEEDED→ADMIT, terminal-bad→VETO, SKIPPED→DECLINE, else WAIT;
  `guard`: SUCCEEDED+check→ADMIT, SUCCEEDED+¬check→DECLINE,
  terminal-bad→VETO, SKIPPED→DECLINE. **Named rule: guards require
  recorded evidence** — a source that is SUCCEEDED with no recorded
  outcome (today: a retired unfired fallback, `scheduler.py:276-279`)
  DECLINES its guard edges; `before` edges still admit it. The rule is
  in the Admission table and its test, not folklore.
- `dependency_edges(blueprint)` — replaces the identity-erasing
  collapse; sequential-chain synthesis still yields plain before-edges.
- `readiness(node, in_edges, join, statuses, evidence)` →
  `ready | wait | skip | cancel` — `all`: any VETO→cancel, any
  DECLINE→skip, all ADMIT→ready; `any`: first ADMIT→ready, all settled
  without ADMIT → cancel if any VETO else skip.
- `LoopSpec(head, tail, region, guard, budget)`;
  `derive_loops(blueprint)` validates each loop edge is a genuine
  back-edge, regions are disjoint or properly nested, budget ≥ 1 — and
  **refuses, by name, a fallback edge whose source or target lies
  inside a loop region** (v1): the fallback substitution's in-place
  dep-set mutation and no-outcome retirement cannot be soundly rewound
  by a region reset, so the combination is refused rather than
  half-defined.
- `structural_edges(blueprint)` = before + guard (guards ARE ordering
  edges); cycle preflight runs over these only.
- `route_verdict`: node ok iff SUCCEEDED, SKIPPED, or its whole
  fallback branch verified (existing recursion extended).

### Executor changes (`DagRouteRunner`)

- `_preflight`: cycle check over structural edges; `derive_loops`
  refusals by name; guard/loop edges on `ordering="sequential"`
  refused (the compiler never produces this — see below).
- `_run_dag`: keep `latest_evidence` per node; readiness via
  `gates.readiness`; skip settles record a SKIPPED outcome with the
  worded reason (`"not taken: guard '<name>' declined (<pointer> <op>
  <value>, observed <x>)"`).
- **Loop mechanics** (graph acyclic per iteration): `iteration:
  dict[action_id, int]`. Tail settles SUCCEEDED + continue ⇒ clear
  region statuses, re-add region to pending, bump iteration, count as
  `progressed` (the deadlock breaker must treat a reset as progress or
  it sweeps live loops — pinned by test). Budget exhausted ⇒ the tail
  settles FAILED "loop budget exhausted after N iterations" — loud,
  never silent-pass. Prior iterations' outcomes stay in the append-only
  record.
- **Iteration keys**: iteration 0 keeps the exact legacy
  `f"{key}:{action_id}"`; iteration n≥1 uses
  `f"{key}#i{n}:{action_id}"` — the plan view's `rsplit(":",1)` parse
  survives, and the latest iteration naturally wins its dict. **Both
  trace joins are updated in the same phase**: `_record_trace` and
  `_record_contract_trace` match on a key that strips the `#i{n}`
  marker, so every iteration records one honest observation under the
  same node_key — without this, iterations n≥1 silently vanish from
  posteriors and marketplace attribution (`scheduler.py:439-446`,
  `execution.py:401-409`).
- Backstop: ctor `max_total_actions: int = 1000` — the graceful-ceiling-
  inside-hard-backstop pattern from the inner loop.
- **Trace hygiene**: SKIPPED outcomes are excluded from trace stats in
  both recorders — a not-taken branch is not an observation.
- Fallback substitution and cascade-skips operate on before/guard
  admissions only; loop edges are invisible to them (and excluded from
  their regions, per the derive_loops refusal).

### Compiler, gateway, emitters

- `orchestrator/contract.py`: `connect()` passes `guard`/
  `max_iterations` through; loop endpoints must be single-exit source /
  single-entry target children (loud refusal, v1). `SubgraphBody.joins`
  compiles to entry-action `join`. **The subgraph boundary derivation
  moves to structural edges**: entries/exits are computed today from
  `relation == "before"` only (`contract.py:218-231`) — with guards as
  ordering edges, a child reached only via a guard edge must count as
  entered, or nested composition mis-wires fan-in. This is a named G2
  change, with a nested-composition test.
- Gateway `_plan_view`: renders `"skipped"` and an `iterations` count,
  additively. `/v1/runs/contract` needs no new gating — the reserved
  gate is `compile_runnable`'s ReservedActionsError
  (`execution.py:103-116`) and a guard can never un-reserve an action.
- **Sequential-mode reconciliation**: blueprints default
  `ordering="sequential"`, and SOPs apply to them — so a
  `require_guard` SOP would land a guard on a blueprint whose executor
  path G0 refuses. The rule: **applying any gate edge to a sequential
  blueprint first materializes the implicit chain as explicit
  before-edges** (a deterministic, order-preserving promotion to
  `ordering="graph"`), then adds the gate edge. The G0 preflight
  refusal then only ever fires on hand-authored inconsistency.
- Emitters: SOPs gain `require_guard: [{when, unless/if:
  Postcondition, then}]` compiled to sop-provenance guard edges beside
  `require_order` (`adaptive.py:124-153`). The assembler stays
  unchanged — node-generation §5 doctrine stands (no explicit edges
  unless order cannot be derived), amended to name gates as the sanctioned
  exception. Contract JSON and SOPs are the authoring surfaces in G;
  the author agent's `finish_node` delivers single scripts, not
  subgraphs — it gains no gate surface in this series (the Paver is the
  programmatic gate emitter, W5). Trace-induced loop edges are future
  work behind the M4 replay gate — named, not built.
- **Economics, stated honestly**: marketplace children are cleared and
  bound before execution; a guard-skip cannot un-bill. The refund/void
  path for skipped children is real economics work **deferred beyond
  G** and tracked in the decision log; until then, G2's documentation
  states that guard-skipped marketplace children were cleared at run
  start, and personal/drawer webs (no clearing) are unaffected.
- Durability: everything rides existing seams (Blueprint in RunState,
  append-only records, atomic checkpoint+outbox). **Replay honesty**:
  within-process re-execution is deterministic — gate decisions are
  pure functions of recorded evidence, and executor memoization covers
  re-entry; post-crash replay rests on external-system idempotency
  exactly as today (the in-memory memo does not survive a crash, and
  this plan does not claim it does).

## III.3 Phases

**G0 — Guard edges, joins, SKIPPED (blueprint level only).** *The
scheduler learns to not take a branch.*
Changes: `skills/models.py` (+SKIPPED), `orchestrator/state.py`
(guard relation + field, `join`, validators), new
`orchestrator/gates.py` (Admission incl. the no-evidence rule,
dependency_edges, readiness, route_verdict), `scheduler.py`
(edge-identity deps, readiness swap, skip settles + worded reasons,
verdict swap, SKIPPED excluded from traces). No compiler, gateway, or
contract changes — every existing blueprint behaves identically
(defaults), pinned by an equivalence test over the existing suite's
blueprints plus the fallback-substitution regression suite.
Tests: guard-admit runs the target; diamond `a → (b if rows>0 | c if
rows==0) → join(any)` runs exactly one branch and the route SUCCEEDS;
all-decline propagates SKIPPED through all-joins; decline+veto ⇒
CANCELLED; guard sourced on a retired unfired fallback declines by the
no-evidence rule; guard on sequential ordering refused at preflight;
full suite green.
Done when: the diamond test passes and `route_verdict` counts SKIPPED
as ok. Depends on nothing from the other barriers.

**G1 — Loop edges with termination guarantees.** *A cycle becomes a
bounded region, not a preflight error.*
Changes: `state.py` (+loop relation, `max_iterations`, validator);
`gates.py` (LoopSpec, derive_loops incl. the fallback-in-region
refusal, loop_decision, structural_edges); `scheduler.py` (preflight
over structural edges, region reset, iteration keys, `max_total_actions`,
breaker-safe progress); **both trace joins** updated for iteration
keys.
Tests: a repeat-until loop driven by scripted per-call stub evidence
exits on guard after 3 iterations with 3 distinct keys per region node
**and 3 recorded observations per node in the trace store** (the join
test); budget exhaustion ⇒ FAILED with the exact worded reason;
non-back-edge, overlapping-region, and fallback-in-region loop edges
refused by name; nested loops run; replay with a memoizing stub
reproduces an equivalent ExecutionRecord under **normalized
comparison** (fresh-minted ids and timestamps normalized — uuid4/now
fields make byte-comparison meaningless, `state.py:153`,
`models.py:164`, `scheduler.py:112,141`); deadlock-breaker regression
green.
Done when: an unbounded loop is unconstructible, a bounded loop replays
deterministically from memoized outcomes, and loop iterations count
honestly in posteriors.

**G2 — Contract vocabulary, compiler, gateway surface.** *Builders can
say it; the shell can show it.*
Changes: `skills/contract.py` (ContractEdge gate fields,
`SubgraphBody.joins`); `orchestrator/contract.py` (passthrough,
joins→entry actions, single-exit/entry loop endpoints, **boundary
derivation over structural edges**); sequential-promotion rule in the
compile path; `_plan_view` (skipped + iterations, additive).
Tests: a SubgraphBody with a guard OR-split and a loop posted to
`/v1/runs/contract` executes correctly with an honest plan view; a
nested subgraph whose child is guard-entered composes with correct
fan-in; legacy contracts compile to **normalized-identical** blueprints
(fresh action ids normalized, `contract.py:73-75`); a sequential
blueprint receiving a gate edge is promoted to explicit chain edges,
order preserved.
Done when: the same gate blueprint is reachable from a serialized
contract through the real HTTP door, and legacy compile output is
unchanged under normalization.

**G3 — Emitters + doctrine.** *SOPs and the docs speak gates.*
Changes: `skills/sop.py` + `orchestrator/adaptive.py` (`require_guard`
→ sop guard edges, riding the sequential-promotion rule);
node-generation §5 amendment reconciling gates with derived-order
doctrine; CHANGELOG; future work named: trace-induced loops behind the
M4 replay gate, the skipped-children refund path, ActionsBody
evidence-port mapping.
Tests: SOP "only send invoice if total > 0" compiles to a
sop-provenance guard edge that drives a real skip in a run — on a
default (sequential) blueprint, via the promotion rule; SOP-vs-data
contradiction still surfaces as a cycle, never silent reorder.
Done when: an SOP-authored guard skips a branch in a real run on a
fresh route with no trace history, docs land in the same commit, full
suite green.

## III.4 Risks and invariants

- **Determinism**: gate decisions are pure functions of recorded
  evidence via a never-raising checker; no LLM ever routes; any-join
  first-of affects timing, never the outcome set — replay reads the
  record, not the race.
- **Termination**: mandatory per-loop `max_iterations` + runner
  `max_total_actions`; exhaustion fails loudly; the deadlock breaker
  treats region resets as progress (pinned).
- **Posterior hygiene**: SKIPPED never enters trace stats; loop
  iterations each count once — both joins fixed with the keys, in the
  same phase.
- **Must not break**: fallback substitution and whole-repair
  `effective_ok`; SOP-contradiction-as-cycle; the reserved gate
  (`compile_runnable`); the plan-view key parse; sandboxing and
  local-first storage (gates add no I/O, no network, no storage — they
  evaluate in-process dicts).
- **Scope discipline**: guards read evidence executors already record;
  no dependency on W's dataflow piping — gates get more useful when
  payloads flow, but work today. The inner LangGraph loop is untouched.

---

## Sequencing and the loop-closure rule

Every phase ships one loop-closure test that drives the full circle
through real doors — named in each phase above.

**Independent starts, three tracks.** F0, W0, and G0 have no
dependencies on each other or on any un-landed work — they can proceed
in parallel or in any order. The cross-ties, all named where they bind:

- `verify_function(files=, bundle=)` is one shared seam: F0 lands it;
  W3 consumes it (or lands it, if W leads).
- W5 (gate-aware paving) is the only W-phase needing G (G2).
- Program nodes (F) make the strongest web anchors (webhook doors +
  one unified face over a cascade's results), but nothing in W requires
  F.

**Recommended order** (one track at a time, if serialized):
G0 → G1 → W0 → F0 → F1 → W1 → W2 → G2 → W3 → W4 → F2 → F3 → G3 → W5.
Rationale: G0/G1 are the smallest, deepest cut (scheduler-only,
massively test-pinned); W0 unlocks the determinism story everything in
W stands on; F0 cuts the shared verify seam early; the surfaces (F3,
G3, W5) land last, after the machinery they present is true.

**Metrics** (investor catalog, group `building`): program nodes born /
week and their module counts; webs surveyed → paved → promoted;
adapters authored vs refused; propagation cascades completed and their
replay verification; gate edges in live routes (guard/loop counts);
zero-synthesis trigger rate on paved webs (the "model bill paid at pave
time" number).

## Decision log

- **"Sweep" renamed to "the Paver"** — the noun is triple-booked;
  the road idiom is the house voice. (Set at proposal.)
- **Program-node trigger is explicit-request-only in v1** — a
  plan-sniffing fork either double-pays authoring or taxes every
  single-file build; implicit promotion deferred until evidence.
- **F3 view is deterministic server-rendered; tenant-authored HTML is
  a named follow-on** — three real walls (frame headers, bearer-only
  sessions, token-exfiltration via navigation) deserve a real
  credential design, not a carve-out smuggled into a phase.
- **Rehearsal gate is effect-freedom, not the verb rule** — the verb
  taxonomy classes every script write-class, which would forbid
  rehearsing exactly what the Paver builds; the severed sandbox is the
  actual safety property.
- **ActionsBody producers excluded from auto-binding v1** — their
  outcomes need not carry a result payload; edges whose ports can never
  fill are worse than no edges. Evidence-port mapping is future work.
- **Propagation is outbox-deferred** — a webhook POST must not carry a
  MAX_HOPS cascade in-request; `TransactionalOutbox` finally gets its
  caller.
- **Fallback edges inside loop regions refused in v1** — the
  substitution's in-place mutation cannot be soundly rewound by a
  region reset; refusal over half-semantics.
- **Guards require recorded evidence** — a SUCCEEDED-without-outcome
  source (retired fallback) declines its guard edges, by named rule.
- **Skipped-children refunds deferred beyond G** — clearing commits
  before execution; un-billing is an economics work item, not a
  scheduler flag.
- **Deferred / future work, in one place**: implicit program-build
  promotion; tenant-authored HTML views + scoped view credential;
  multi-operation external invocation of program nodes; per-edge
  `delay_s` and cross-run event edges (beyond W4's triggers);
  trace-induced loop edges (M4 replay gate); ActionsBody
  evidence-port mapping; skipped-children refund path; the market-free
  contract door's final form (decided in W4).
