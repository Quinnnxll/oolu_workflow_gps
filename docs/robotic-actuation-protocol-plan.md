# The Robotic Workflow Actuation Protocol: plan review and build phases

This document reviews the "OoLu Authorized Machine Feedback Loop —
Adaptive Capability Web Building Phase Plan" (v0.2), maps it onto what
OoLu already is, consolidates its fifteen phases into a buildable
sequence, and records what the foundation laid in this repository
(`src/oolu/actuation/`) covers and deliberately does not cover.

The one-sentence verdict: the plan's architecture is sound — its two
strongest ideas (digest-bound authorization and the separation of
authentication from metrological validity) deserve to be load-bearing —
but its fifteen serial phases need consolidation around a critical
path, several of its standards citations need correcting, and most of
its "new" governance machinery already exists in OoLu under different
names and should be reused, not rebuilt.

## 1. What the plan gets right

1. **Trust is multidimensional (§4).** Identity, integrity,
   authorization, metrology, method, competence, applicability,
   independence, and recency are separate questions, and the evidence
   grades E0–E6 never replace them. This is the correct inversion:
   a cryptographically valid signature on a scientifically invalid
   measurement must stay distinguishable forever.
2. **The aggregate digest (§9, §24.4).** Binding workpiece, tool,
   fixture, frame graph, recipe, program, controller, safety
   configuration, approval bundle, and time window into one digest —
   where any mismatch blocks arming — makes drift-by-substitution
   structurally impossible instead of procedurally discouraged. This
   is the single strongest idea in the plan and it is the first thing
   the foundation code implements.
3. **The safety posture (§24, §35).** Real-time and safety loops stay
   under independently validated local controllers; the cloud proposes
   and never closes them; lease expiry prevents a new start but never
   forces an unsafe mid-cycle stop; generic inverse motion is
   prohibited as a recovery strategy; automatic restart after a safety
   trip is prohibited. Every one of these is the industrially correct
   call, and they align with how ISO 10218-1/-2:2025 and
   ISO 13849-1 partition responsibility.
4. **Negative evidence is mandatory (§24.10, invariant 29).**
   Near misses, trips, rejected parts, and failed recoveries remain
   searchable and feed routing and learning. Success-only training is
   named as a defect, not a default. OoLu's own negative-evidence
   store (`negative.py`) already embodies this.
5. **Learned behavior is envelope-bounded (§24.10, invariant 27).**
   Optimization may propose parameters only inside a validated
   envelope, and every proposal becomes a new versioned recipe that
   re-enters the approval gates. Learning never silently widens what
   was validated.

## 2. Corrections and gaps

### 2.1 Standards table errors (§2)

The plan's own rule — "a deployment cannot assume that a standard
remains current" — applies to its own table. As of this review:

| Cited | Reality |
|---|---|
| ISO/IEC 17020:2026 | Current edition is **ISO/IEC 17020:2012**; no 2026 edition exists |
| ISO 10012:2026 | Current edition is **ISO 10012:2003**; no 2026 edition exists |
| IEC 62541-14:2026 | OPC UA PubSub is **IEC 62541-14:2020** |
| ISO 10218-1/-2:2025 | Correct — published 2025, replacing the 2011 editions and absorbing ISO/TS 15066 collaborative requirements |
| ISO 13849-1:2023, ISO 13850:2015, IEC 60204-1:2016+A1:2021, ISO 12100:2010, ISO 9283:1998 | Correct |

The fix is structural, not editorial: standards belong in the
**standards registry** the plan itself specifies, as versioned entries
with status, and documents should cite registry entries — never bake
edition years into prose that will rot.

### 2.2 Underspecified areas

1. **Digest canonicalization.** The plan defines *what* the aggregate
   digest binds but not *how* the bytes are produced. Two
   implementations that disagree on key order, unicode normalization,
   or float formatting will compute different digests for the same
   job — which turns the strongest control into an interop failure.
   The foundation pins this: domain-separated SHA-256 over canonical
   JSON (sorted keys, compact separators, UTF-8, floats rejected in
   digest-bound fields).
2. **Grade-at-time-of-use.** Calibration revocation (§7.1) lowers or
   blocks evidence grades, but the plan never says what happens to
   decisions already taken on since-revoked evidence. Needed: every
   consumer records the evidence grade *at the time of use*;
   revocation flags downstream artifacts for review but never
   retroactively rewrites the decision record.
3. **Simulator qualification.** Phase 6 gates physical execution on
   simulation, but nothing qualifies the simulator itself. A
   simulation service needs its own validation evidence, versioning,
   and known-divergence record, or gate 4 of §24.5 is a rubber stamp.
   Invariant 19 ("a digital twin is supporting evidence, not a safety
   authority") implies this but no phase delivers it.
4. **Compiler qualification.** The deterministic compiler (§24.1) is a
   single point of trust: a defect there converts an approved plan
   into an unapproved physical act with a valid digest. It needs a
   golden-program regression suite and reproducible builds — the plan
   should say so explicitly in Phase 7's exit gates.
5. **Operator experience.** The plan has approval tiers but no
   HMI/andon story: what the cell operator sees during preflight
   failure, hold, or containment, and how dual-control approval is
   physically performed at the cell. A4 dual control without a
   defined second channel invites approval-by-shoulder-surfing.
6. **Material genealogy.** Workpiece states are first-class, but lot
   and batch genealogy (which material lot, which supplier cert, which
   prior thermal history) appears only as an A3 trigger. For the
   automotive pilot it must be a graph node type from day one.

### 2.3 Phasing critique

Fifteen serial phases put the first physically useful loop (Phase 7)
behind six phases of infrastructure, and the science stack (Phases
10–13) behind that. Three observations:

- **Phases 0 and 6 are the same deliverable seen twice** — contracts.
  The observation contracts (Phase 0) and actuation contracts
  (Phase 6) share identity, digest, versioning, and rights machinery
  and should be one contracts package built first.
- **Phases 9 and 14 largely exist in OoLu** — the rights engine,
  consent economy, tenant walls, and marketplace are OoLu's existing
  spine, not new construction.
- **The pilot (§32) is the real sequencer.** The damper loop needs:
  contracts → registries → simulation-only routing → one qualified
  workcell → verified feedback. That is five stages, not fifteen.

## 3. What OoLu already is

As with the industrial vertical plan, much of the spec exists here
under different names — the plan should reuse these seams, not
duplicate them:

| Plan concept | OoLu today |
|---|---|
| Approval bundles, review tiers, expiring approvals | Contract holds + approval inbox; digest-bound approval path (travel desk booking already walks it) |
| "Models propose, the kernel commits" (§24.1 plane table) | The standing control philosophy; model-written code re-earns confirmation |
| Signed, hash-chained evidence lineage | `DurableAuditLog`, versioned durable schema, artifact checksums |
| Rights-controlled distribution (§16) | Egress grants, audit regimes, consent economy, tenant walls |
| Capability manifests and routing scores (§18) | Marketplace/nodeplace node contracts, `earns_its_cost` gate, route learning |
| Negative evidence retention (§24.10) | `negative.py` failure evidence; trace store raw run log |
| Identity separation (org/person/device/workload) | `identity/` package: accounts, stored grants — never token claims |
| Deterministic adapters over vendor APIs | ActionExecutor contract; two-phase install-then-sever sandbox |
| Typed action intents | `ActionEvent` adapter/operation/parameters vocabulary |

What OoLu does **not** have, and what this plan genuinely adds: typed
physical contracts (workpiece state, actuation capability, operating
envelope), the aggregate-digest arming discipline, single-use
execution leases, the actuation lifecycle with its safety-shaped
terminal states, review tiers keyed to physical consequence, and the
metrology spine (calibration, uncertainty, evidence grades).

## 4. Build phases

Consolidated from the plan's Phases 0–14 into six stages. Each stage
names the plan phases it absorbs and its exit gates.

### R0 — Protocol contracts and digests (this branch)

Absorbs: the schema slice of Phase 0 and the contract slice of
Phase 6. Pure library + tests; no I/O, no network, no physical
command surface at all.

Deliver (landed in `src/oolu/actuation/`):

- Canonical digest computation (`digests.py`): domain-separated
  SHA-256 over canonical JSON; the aggregate job digest that binds
  every execution-critical field.
- Typed contracts (`contracts.py`): action intent, actuation
  capability with declared hazards and reversibility, validated
  operating envelope with containment checks, tool instance,
  workcell version, the digest-bound actuation job, execution
  evidence bundle, and envelope-bounded parameter proposals that
  always produce a new draft recipe version requiring approval.
- Single-use execution leases (`lease.py`): nonce-bound, TTL'd,
  anti-replay; expiry prevents a new start and is explicitly not a
  stop command.
- The actuation lifecycle (`lifecycle.py`): the §24.5 state machine;
  no path from `safe_stopped` back to `executing`; arming is a gate
  that composes digest match + preflight pass + lease consumption.
- Review tiers (`review.py`): A0–A5 threshold engine; dual control
  for high consequence; safety approval separate from budget
  approval; outside-envelope ⇒ prohibited.
- Live preflight (`preflight.py`): §24.7 checks as a typed verdict;
  any failure blocks arming with a named reason.

Exit gates (each is a test in `tests/test_actuation_protocol.py`):

- Changing any bound field changes the aggregate digest
  (acceptance tests 4, 20, 21, 27).
- An expired, replayed, or previously consumed lease cannot arm
  (acceptance test 22).
- An open guard, unhealthy safety controller, or safety-config
  mismatch blocks arming locally (acceptance test 23).
- A safety stop cannot transition back to execution; recovery
  re-enters as a new proposal (acceptance test 31).
- A parameter proposal outside the validated envelope is rejected;
  one inside still creates a new version requiring approval
  (acceptance tests 32, 33).

### R1 — Evidence and observation spine (this branch)

Absorbs: the rest of Phase 0, Phase 1's manifest slice, Phase 2's
contracts. Like R0, a pure library — persistence and transport arrive
with R2's registries and R4's edge.

Deliver (landed in `src/oolu/evidence/`):

- Raw evidence manifests (`manifest.py`): content-hashed chunks with
  declared dropped-sample counts under a domain-separated Merkle
  root; canonical signing bytes reusing R0's canonicalization.
- Signed ingress (`ingress.py`): registered devices only; a
  `SignatureVerifier` seam with a stdlib HMAC baseline (device
  certificates arrive with R4 behind the same seam); verify-then-
  store with no update and no delete; idempotent resume; credential
  revocation blocks new uploads without touching history.
- Calibration (`calibration.py`): records with validity windows,
  timestamped revocation that never deletes, and named check
  failures — expiry judged at test time, revocation at check time.
- Evidence grades (`grades.py`): the E0–E6 ladder computed
  cumulatively from separately established facts, and
  grade-at-time-of-use: an append-only ledger of reliance records
  whose bases are flagged for review on revocation, never rewritten.
- Observations (`observation.py`): result with unit and uncertainty,
  processing lineage as an append-only step ledger reconstructable
  back to raw chunks, and calibration correction as the worked
  example of corrections-as-new-artifacts.
- Rights (`rights.py`): §16.1 permissions as named, separable flags
  that refuse by name; `formula_discovery` never implies
  `model_weight_training`. Durable wiring into OoLu's grant spine is
  R2 work.

Exit gates (each a test in `tests/test_evidence_spine.py`):

- A valid device with an expired calibration cannot create E2
  evidence; the ladder stops at E1 (acceptance test 1).
- A calibrated device with an invalid signature cannot create E1
  evidence — higher facts cannot skip lower rungs (acceptance
  test 2).
- Raw evidence remains unchanged after calibration correction; the
  correction is a new linked artifact (acceptance test 5).
- Every observation reconstructs its processing lineage to raw
  chunks, and a broken chain raises by name (acceptance test 6).
- Revoked device credentials block new uploads without deleting
  historical evidence (acceptance test 16).
- Unregistered devices cannot upload; a modified chunk fails
  integrity; interrupted uploads resume without duplicates (Phase 1
  exit gates).

### R2 — Registries over the durable layer (this branch)

Absorbs: Phases 1–3's registry work. The contracts get addresses:
every registry rides OoLu's `DurableConnection` (SQLite locally, the
same table contract PostgreSQL implements in production) and keeps
the protocol idioms — named refusals, history never deleted, time
injected.

Deliver (landed in `src/oolu/registries/`):

- Standards registry (`standards.py`): versioned editions with
  status; currency is a query, registering a newer edition
  supersedes the old in the same transaction, withdrawal is a
  status — the structural fix for §2.1's citation rot.
- Machine and sensor registry (`machines.py`): status walls
  (quarantine/retire without deletion) and `find_instruments` —
  selection by measurand, calibrated-range containment, and
  achievable uncertainty, never by machine name; calibration
  validity stays owned by R1's registry through a predicate seam.
- Method registry (`methods.py`): the version lifecycle as a
  transition whitelist (`developing → validated → approved`,
  terminal `superseded`/`withdrawn`); only an approved version
  authorizes.
- Workcell registry (`workcells.py`): workcell versions with
  commissioning status, tools with wear and inspection state,
  fixtures, and coordinate-frame calibrations kept as history; the
  live frame-graph digest computed over each frame's current
  calibration; `release_check` (worn/failed/overdue tools,
  unqualified cell) and `live_state` — registry truth plus edge
  observation, composed for R0's preflight.
- Authorized test jobs (`testjobs.py`): the §9 authorization digest
  over exactly the plan's bound fields; a job citing an unapproved
  method version is stored quarantined with a named reason, so the
  attempt remains evidence.
- Durable rights (`rights_store.py`): R1's rights contract persisted
  with timestamped revocation, closing the seam promised there.

Exit gates (each a test in `tests/test_registries.py`):

- A test using an unauthorized method version is quarantined
  (acceptance test 3).
- Instruments are selected by range and uncertainty, never by
  machine name; quarantine and expired calibration wall the query
  (acceptance test 17, registry level).
- A changed controller configuration invalidates the test-job
  digest (acceptance test 4, test-job flavor).
- A tool-wear limit or failed/overdue tool inspection blocks the
  next job; an unqualified workcell blocks release (acceptance
  test 28).
- A coordinate-frame recalibration invalidates programs authorized
  against the previous frame digest — end to end: armable before,
  preflight-refused after, with both calibrations retained as
  history (acceptance test 27).

### R3 — Simulation-only routing (this branch)

Absorbs: Phases 5–6's routing. A physical goal becomes a ranked set
of compatible nodes, a digest-bound plan, a qualified simulation, and
an approval bundle — and nothing more; no module in the package can
emit a machine command.

Deliver (landed in `src/oolu/actuation_routing/`):

- Capability nodes (`nodes.py`): measurement and actuation nodes —
  one contract for robots and specialized machines alike (Phase 6's
  second gate), differing only in declared effects, constraints,
  hazards, and registry bindings. Nodes carry routable facts; they
  cannot bring their own scoring.
- The router (`router.py`): the raw-command wall rejects
  servo-or-drive vocabulary (joint targets, axis velocities, G-code,
  PWM) at the boundary by name — acceptance test 19 at the routing
  layer; hard exclusions with named reasons (wrong family,
  unproducible desired state, incompatible input, irreversible
  without disposition path, stale safety case, unqualified workcell
  via a seam to R2); and the §18.1 score returned as a full
  breakdown, term by term, so a routing choice is auditable.
- Simulation (`simulation.py`): the simulator is equipment (review
  gap 2.2.3) — versioned qualification with a validated check-family
  scope, evidence refs, and revocation; simulation records must name
  the exact workcell, tools, fixtures, frame-graph digest, recipe,
  parameters, and envelope (Phase 6's third gate) or report the
  missing binding by name.
- Plan assembly (`planner.py`): `build_plan` binds a routed
  candidate under containment — parameters inside the validated
  envelope, recipe validated against that same envelope, disposition
  paths mandatory for irreversible effects, qualified workcell — and
  the plan digest (`oolu-actuation-plan/v1`) binds every choice;
  `ready_for_compilation` is the terminal gate checking plan,
  qualified simulation, review tier, and approvals together.
- Approval bundles (`approvals.py`): durable and digest-bound on the
  house law — approval of digest A never releases digest B, each
  bundle spends exactly once, expired approvals are dead, dual
  control means two distinct people (one person holding both roles
  is one signature), and prohibited tiers (A0/A5) cannot even open a
  bundle. Safety roles only; budget approval stays in the
  marketplace's own flow.

Exit gates (each a test in `tests/test_actuation_routing.py`):

- A physical goal routes to compatible actuation nodes without any
  command being issued; the decision is ids, scores, and named
  exclusions (Phase 6 gates 1–2).
- Raw servo/drive vocabulary is rejected at the API boundary by
  name (acceptance test 19).
- Every simulation identifies exact configuration versions, and an
  unqualified or revoked simulator cannot support review (Phase 6
  gate 3; gap 2.2.3).
- Irreversible actions without declared disposition paths are
  excluded from routing and refused at plan assembly (Phase 6
  gate 4).
- A high-consequence job waits for dual-control approval; the
  bundle spends exactly once and binds one digest (acceptance
  test 18).
- The full walk — intent → route → plan → simulate → review →
  approve → release — succeeds once and refuses a second release;
  a failed or foreign simulation refuses without spending the
  bundle.

### R4 — Edge, compiler, and one qualified workcell

Absorbs: Phases 4 and 7. Deliver: edge gateway, deterministic
compiler with golden-program regression suite (gap 2.2.4), signed
program artifacts, the lease service over HTTP, evidence ingress,
command/evidence channel separation, reduced-energy and first-piece
modes, and the pilot cell (§32). This is the first stage requiring
hardware and likely dedicated edge packages. Exit gates: Phase 7's
gates plus acceptance tests 19–26, 29 against the pilot cell.

### R5 — Verified feedback and the science stack

Absorbs: Phases 8, 10–13. Deliver: state-delta qualification,
process-capability learning inside envelopes, dataset builder,
formula discovery sandbox, replication and approval lifecycle,
closed-loop experiment proposals. Exit gates: Phases 8 and 10–13's
gates; acceptance tests 9–14, 30–35.

Rights distribution (Phase 9) and federation (Phase 14) ride OoLu's
existing consent economy and cross-tenant work respectively, and
harden incrementally across R1–R5 rather than as standalone stages.

## 5. Invariants adopted now

The foundation code enforces, from day one, the invariants that can be
enforced by a library (plan §35): raw evidence immutability by
construction (frozen models), digest binding of every physical
command's authorization (21), envelope containment for learned
proposals (27), no automatic restart after a safety trip (26), lease
single-use (22), and the plane separation — nothing in this package
can emit a servo command, by design (18). The remaining invariants
bind services built in R1–R5 and are restated in their exit gates.
