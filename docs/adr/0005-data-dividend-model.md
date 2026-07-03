# ADR-0005: Collective data dividends

- Status: Proposed
- Date: 2026-07-03

## Context

Non-developers use the gateway as a communication app but do real work through it.
If they consent to the agent learning from their (scrubbed) working data, that data
lets the platform synthesize, verify, and register **automated functions** (skills
and, once contributed, nodeplace nodes). Those functions earn: a verified execution
is metered and priced, and the money is split among the node authors who supplied
the capability.

Today that split rewards **authorship** but not the **data** that made a learned
function possible. When a *group* of people pools their working data so the global
agent can learn, they have no claim on the profit the resulting functions generate.
This ADR plans a **data-dividend model**: pooled data contributors earn an ongoing
dividend from the functions their data helped create, without ever exposing the raw
data.

The machinery to build on already exists:

- **Metering** derives one immutable `MeteringEvent` per verified success, keyed by
  the execution idempotency key (replay/retry never double-counts).
- **Attribution** (`AttributionStore`) records run→node bindings and per-contributor
  `AttributionRecord`s carrying a weight `w_i` and multiplier `μ_i`.
- **Pricing** splits net revenue in exact integer micro-units with a hard
  conservation invariant: `platform_micros + Σ noder_micros == net_micros`, shares
  proportional to normalized `w_i·μ_i`.
- **Settlement** reserves `R`, holds back below a minimum `T`, pays out per period,
  and rolls the remainder forward. **Disputes** claw back via compensating entries.
  **FraudSignals** exclude self-dealing, replays, and velocity abuse.
- **Scrubbing** is the enforceable gate that removes secrets/PII before anything is
  learned or stored; the sandbox severs the network during execution.

The dividend model is therefore mostly an **extension of attribution and the reward
split**, not a new payments system.

## Decision

Introduce **data pools** as first-class earning principals and extend the reward
split with a **data-dividend tranche**, so a function's net revenue is conserved
across the platform, its authors, and the data pools whose contributions the
function was learned from.

### 1. Data pools (cooperatives)

A **DataPool** is a set of members who opt in — explicitly, revocably, under a
stated license — to contribute scrubbed working data for learning. A pool is an
attribution principal (it can hold a balance and receive payouts). Membership is
Sybil-resistant (identity-gated, one verified principal per membership).

### 2. Lineage: which data trained which function

A new **DataContribution** record links a pool to a learned artifact
(`pool_id → node_version`, with a contribution weight). It is the data-side analogue
of `AttributionRecord` and is written only from verified signals — a pool earns a
lineage edge to a function only when its contributed data measurably improved a
function that later earns. Lineage is provenance metadata; it never stores the data
itself, only the fact and the weight of the contribution.

### 3. Extended reward split (conservation preserved)

Extend `PricingResult`/`PricingEngine` so a verified event's net splits three ways:

```
net_micros == platform_micros + Σ author_micros + Σ pool_micros
```

- `Σ author_micros` is today's noder split (by normalized `w_i·μ_i`).
- `Σ pool_micros` is the new **data-dividend tranche**: a policy fraction `δ` of the
  net (or of the author tranche) distributed across the contributing pools by their
  lineage weight.
- The conservation invariant is unchanged in spirit — it simply gains a third term,
  still bit-exact in integer micro-units, remainder assigned to the platform so the
  sum is exact.

### 4. Pool-internal distribution

A pool's dividend is split among its members by a **member contribution weight** =
`volume × quality × recency`, where *quality* is measured by verified usefulness
(did this member's data improve a function that actually earns?), not by raw volume.
The principled version is a marginal-contribution / Shapley-style attribution over
verified-earning functions; v1 ships proportional-by-verified-usefulness and leaves
the Shapley refinement as a later step.

### 5. Cadence, reserves, clawback, anti-gaming — reuse, don't rebuild

- **Cadence/threshold:** dividends accrue per verified earning event and pay out per
  period above a minimum, below rolls forward — the existing `SettlementService`
  pattern, with pools as payout accounts.
- **Reserve/clawback:** hold a reserve `R`; a dispute/refund on an underlying
  function claws back proportionally via the existing compensating-entry path.
- **Anti-gaming:** reuse `FraudSignals` (self-dealing exclusion, replay rejection,
  velocity throttle) and add **data-quality gating** — junk or duplicated data earns
  no lineage weight because usefulness is measured by verified impact, not volume.

### 6. Consent, licensing, privacy (non-negotiable)

- Learning is **opt-in and revocable** per surface; off by default.
- Data is **scrubbed before the model or any store** (`knowledge/scrubbing`); the
  member contributes the *shape* of the work, never the sensitive values. The
  learning step operates on aggregated/scrubbed signals (differential-privacy or
  aggregation for the global-learning path is an open question below).
- The dividend is computed from **earnings and lineage**, never from re-exposing the
  data. Revocation stops future use; a member's residual claim on already-learned
  functions is governed by the pool license.
- Money-movement remains refused on local-only infra (invariant #8): dividends pay
  out only on the production PostgreSQL + asymmetric-identity substrate.

## Consequences

- The change is concentrated in **attribution + pricing + settlement**, which are
  already contract-tested with exact conservation; the risk is bounded to extending
  a proven split, not inventing a payments rail.
- A new, durable incentive appears: people are paid an ongoing dividend for
  contributing to a commons the agent learns from, aligned with the platform's
  "verified success is the only thing that pays" principle.
- New surfaces to design and gate: pool membership/governance, the `DataContribution`
  lineage store, the `δ` policy, and the member-weight function.

## Open questions

- **Global-learning privacy:** what exactly crosses the boundary for the "global
  agent learns" step — scrubbed demonstrations only, or aggregated/DP gradients?
- **`δ` policy:** fixed platform-wide, per-pool negotiated, or a function of how much
  a pool's data actually reduced synthesis cost / raised success rate?
- **Marginal attribution cost:** Shapley over verified-earning functions is
  principled but expensive; what is the acceptable approximation?
- **Residual claims on revocation:** how long does a member's claim on an
  already-learned earning function persist after they leave the pool?
- **Cross-pool overlap:** when the same capability is learnable from several pools,
  how is lineage weight divided without double-paying?
