# OoLu Workflow Reward Marketplace — design, formula, and version-goal plan

Status: Draft for planning. Owner: OoLu. Builds on the v0.2 backend
(orchestrator, durable runtime, identity/RBAC, worker control plane, provider
adapters, desktop shell, HTTP gateway).

This document is the source of truth for **what we are building**, **how the money
math works**, and **the order we build it in**. Each milestone has a *Goal
Adherence* checklist with binary, testable criteria. Do not advance a version
until every box in its adherence checklist is true.

---

## 1. Product thesis

Users teach OoLu repetitive work as **workflows (skills)**. By default a workflow
is **private and stored locally** — OoLu automates the owner's own repetitive work
and nothing leaves the device. A user may **opt in to publish** a workflow to the
marketplace. When another user runs a published workflow and it **succeeds**, the
publisher earns a share of the commission.

Two sides of one market:
- **Publishers** supply reusable workflows and earn on verified successful use.
- **Consumers** run workflows to get work done and pay per successful use (or via a
  plan).

---

## 2. Non-negotiable invariants (hold in every version)

These are the contract. A change that violates one is a release blocker.

1. **Private by default.** Publishing is explicit and revocable. A private
   workflow's definition and data never leave the owner's storage.
2. **Secrets never leave the vault.** A published workflow is a *sanitized* skill
   (parameters, actions, constraints) — credentials are references only, scrubbed
   by the existing gate. (`knowledge/scrubbing.py`, `providers/vault.py`.)
3. **Commission only on platform-verified success.** Earnings accrue from the
   durable, hash-linked **audit log** + **execution outcomes** produced by
   **platform workers** — never from client-reported results. (`durable/audit.py`,
   `worker/` signed leases.)
4. **Metering is separate from billing.** We record immutable usage/attribution
   events; money is *computed downstream* from them. Payment providers are never
   the source of truth for what happened.
5. **Exactly-once accrual.** Every metering event is keyed by the execution's
   idempotency key; replays and retries never double-pay. (`durable/idempotency.py`.)
6. **Immutable ledgers.** The metering ledger and the earnings ledger are
   append-only; balances are *projections*, never edited in place. Corrections are
   new compensating entries (clawbacks), not mutations.
7. **Tenant isolation everywhere.** No cross-tenant read or accrual. (`identity/`.)
8. **No money on local-only infra.** Real charges/payouts require the production
   durable (PostgreSQL) and real identity (OIDC) adapters.

---

## 3. The reward formula

### 3.1 Variables

| Symbol | Meaning | Example |
| --- | --- | --- |
| `G` | Gross amount charged to the consumer for one billable execution | $0.50 |
| `C_p` | Pass-through provider/compute cost attributable to that execution (LLM tokens, sandbox compute) | $0.08 |
| `N` | **Net contribution** of the execution, `N = max(0, G − C_p)` | $0.42 |
| `ρ` (rho) | Platform commission rate on net contribution | 0.30 |
| `σ` (sigma) | Publisher share rate, `σ = 1 − ρ` | 0.70 |
| `w_i` | Attribution weight of publisher *i* in a multi-skill route | 2 of 3 |
| `μ` (mu) | Optional policy multiplier for publisher *i* (reputation/promo), `0 ≤ μ ≤ μ_max` | 1.00 |
| `H` | Holdback period before earnings become payable | 14 days |
| `R` | Reserve fraction held against chargeback/refund risk | 0.10 |
| `T` | Minimum payable balance before a payout batch is cut | $20.00 |
| `f_pp` | Payment processing fee (applied at payout/settlement, not per event) | provider-set |

### 3.2 Core per-execution split

Recover pass-through cost first, then split the net contribution:

```
N = max(0, G − C_p)                      # never pay commission on raw cost
PlatformEarning   = N × ρ
PublisherEarning  = N × σ                # σ = 1 − ρ
```

Only emitted when the execution is **SUCCEEDED** and **platform-verified**. A
failed/blocked/cancelled execution yields `PublisherEarning = 0` (see 3.6).

### 3.3 Multi-publisher routes (composition)

If a route composes several published skills, the publisher pool `N × σ` is split
by normalized weight, with the optional per-publisher multiplier:

```
weight_i      = w_i × μ_i
PublisherEarning_i = (N × σ) × (weight_i / Σ_j weight_j)
```

Default `w_i` = number of that publisher's reserved actions actually executed in
the route (so contribution tracks real work). `μ_i` defaults to 1.0; reputation
and promotions adjust it via policy, never via ad-hoc edits.

Conservation check (must always hold):

```
PlatformEarning + Σ_i PublisherEarning_i  ==  N
```

### 3.4 Settlement (period aggregation)

A periodic settlement job reads the **metering ledger** for a closed period and
produces immutable **earnings ledger** entries per publisher:

```
PeriodGross_i   = Σ over the publisher's successful events of PublisherEarning_i
Reserve_i       = PeriodGross_i × R
Available_i(t)  = Σ earnings whose (event_time + H) ≤ t  −  Reserve_i  −  Clawbacks_i  −  AlreadyPaid_i
```

### 3.5 Payout

```
if Available_i(now) ≥ T:
    payout_amount = Available_i(now) − f_pp(payout_amount)   # processor fee at payout
    issue PayoutBatch(publisher_i, payout_amount)            # via PayoutAdapter (Stripe Connect)
```

Below `T`, the balance rolls forward. Payout requires a verified payout account
(KYC) or the amount stays pending.

### 3.6 Failure, refund, dispute (clawback)

- **Failure / block / cancel:** no charge for the *skill value*; `PublisherEarning
  = 0`. (Provider cost handling is policy: absorbed by platform, or billed at cost
  with zero margin — see `PricingPolicy`.)
- **Refund / chargeback / dispute upheld:** append a **negative** earnings entry
  (clawback) referencing the original metering event id. If already paid out, the
  publisher balance goes negative and is recovered from future earnings (the
  reserve `R` exists to cushion this).
- All reversals are **new ledger entries**, preserving invariant #6.

### 3.7 Worked examples

1. **Single publisher, success.** `G=$0.50`, `C_p=$0.08`, `ρ=0.30`.
   `N=$0.42` → Platform `$0.126`, Publisher `$0.294`.
2. **Two publishers (weights 2 and 1), success.** Same `N=$0.42`, `σ=0.70`, pool
   `$0.294`. Publisher A (w=2): `$0.196`; Publisher B (w=1): `$0.098`; Platform
   `$0.126`. Sum `= $0.42` ✓.
3. **Failure.** `PublisherEarning=0`; no consumer value charge; optional cost
   recovery only.
4. **Refund after payout.** Original Publisher earning `$0.294` already paid →
   clawback entry `−$0.294`; recovered from reserve / future earnings.

---

## 4. Key data structures

New domain records, each versioned and behind ports (SQLite local / PostgreSQL
prod), reusing existing models where possible. **Ledgers are append-only;
balances/listings are projections.**

### 4.1 Registry (`marketplace/`)

| Record | Key fields | Notes |
| --- | --- | --- |
| `PublishedSkill` | `skill_id`, `owner_principal`, `tenant_id`, `visibility` (private/unlisted/public), `created_at` | Ownership + visibility. Wraps an existing `ReusableSkill`. |
| `SkillVersion` | `version_id`, `skill_id`, `semver`, `content_hash`, `sanitized_skill_json`, `license`, `published_at` | Immutable, content-addressed sanitized artifact (no secrets). |
| `Listing` | `listing_id`, `version_id`, `title`, `summary`, `tags`, `maturity_label`, `status` (draft/in_review/active/suspended) | Discovery surface; gated by review. |
| `PricingPolicy` | `policy_id`, `version_id`, `model` (per_success/subscription/free), `unit_price`, `currency`, `cost_recovery` (absorb/passthrough) | Drives `G` and `C_p` treatment. |
| `Rating` / `Review` | `subject_version_id`, `rater_principal`, `score`, `text`, `verified_run` | Only raters with a verified successful run can rate. |

### 4.2 Metering (`metering/`) — the accounting source of truth

| Record | Key fields | Notes |
| --- | --- | --- |
| `MeteringEvent` | `event_id`, `idempotency_key` (= execution key), `run_id`, `version_id`, `consumer_tenant`, `outcome`, `gross G`, `provider_cost C_p`, `audit_seq`, `occurred_at` | Append-only, one per verified billable execution; **derived from the audit log**. Unique on `idempotency_key`. |
| `AttributionRecord` | `event_id`, `publisher_principal`, `weight w_i`, `multiplier μ_i` | Per-publisher split inputs for one event. |

### 4.3 Billing (`billing/`)

| Record | Key fields | Notes |
| --- | --- | --- |
| `EarningsEntry` | `entry_id`, `publisher_principal`, `event_id?`, `amount` (+/−), `kind` (accrual/reserve/clawback/payout), `available_at`, `created_at` | Append-only earnings ledger; clawbacks are negative entries. |
| `PublisherBalance` | `publisher_principal`, `available`, `pending`, `reserved`, `lifetime_paid` | **Projection** of `EarningsEntry`. Never edited directly. |
| `PayoutBatch` | `batch_id`, `publisher_principal`, `amount`, `status`, `provider_ref`, `created_at` | Created by settlement; executed via `PayoutAdapter`. |
| `PayoutAccount` | `publisher_principal`, `provider_account_id`, `kyc_status`, `country`, `currency` | Stripe Connect (or equiv) account; KYC gate for payout. |
| `Dispute` | `dispute_id`, `event_id`, `reason`, `state`, `resolution` | Drives clawbacks. |

### 4.4 Ports (so providers/stores stay swappable)

- `RegistryStore`, `MeteringLedger`, `EarningsLedger`, `BalanceProjection`
  (SQLite + PostgreSQL adapters).
- `PricingEngine` (pure: event → `(N, PlatformEarning, {PublisherEarning_i})`).
- `PayoutAdapter` (Stripe Connect adapter; sandbox/remote-mock for tests).
- `FraudSignals` (pluggable anti-abuse checks).

### 4.5 Mapping to what already exists

| Need | Existing building block |
| --- | --- |
| Verified "it ran and succeeded" | `durable/audit.py`, `ExecutionRecord` outcomes |
| Exactly-once accrual | `durable/idempotency.py` |
| Trustworthy execution | `worker/` signed, single-use leases; isolation policy |
| Safe shareable unit | `skills/` records; credential refs only |
| Identity / tenancy / authority | `identity/` |
| Async API + webhooks + RBAC | `gateway/` |

---

## 5. Architecture additions

```
                +-------------------- HTTP gateway (/v1/marketplace, /v1/earnings) ----+
                |                                                                       |
   consumer --> run workflow --> durable audit + execution outcome (verified) ---------+
                                              |
                                              v
                              metering/ : derive MeteringEvent (idempotent, attributed)
                                              |
                                              v
                              billing/  : PricingEngine -> EarningsEntry (accrual)
                                              |   (settlement job, holdback H, reserve R)
                                              v
                              billing/  : PayoutBatch -> PayoutAdapter (Stripe Connect)

   publisher --> marketplace/ : publish (opt-in) -> SkillVersion (sanitized) + Listing + PricingPolicy
```

New modules: `workflow_gps/marketplace/`, `workflow_gps/metering/`,
`workflow_gps/billing/`. New gateway routes under `/v1/marketplace`,
`/v1/listings`, `/v1/earnings`, `/v1/payout-accounts`, `/v1/disputes`.

---

## 6. Build process — versions P0 → P2

Versioning continues from `v0.2.0`. Each version is a tagged milestone with an
**exit gate** and a **Goal Adherence** checklist.

### P0 — `v0.3.0` "Production substrate" (prerequisite; no money)

Goal: be able to run real users at multi-process scale, and start *recording*
metering events from the audit trail — without charging anyone.

Deliverables:
- PostgreSQL adapters for the durable runtime ports (`codex/durable-runtime` prod
  adapter).
- Real OIDC asymmetric (RS256/ES256, JWKS) `SignatureVerifier` and a real
  `HttpTransport` for provider adapters.
- Frontend chat gateway app on the existing HTTP gateway.
- `metering/` schema + idempotent derivation of `MeteringEvent` from the audit log
  (recording only; no pricing, no money).

Exit gate / Goal Adherence:
- [ ] API + worker run as separate processes on PostgreSQL; restart loses/dupes
      nothing (existing durability tests pass on the PG adapter).
- [ ] A real IdP token verifies via JWKS; HS256 is rejected in production config.
- [ ] A non-developer completes a workflow end to end via the frontend.
- [ ] Every verified successful execution produces exactly one `MeteringEvent`
      (idempotent; replay/retry does not duplicate).
- [ ] No pricing, charging, or payout code path exists yet.

### P1 — `v0.4.0` "Publish & meter" (supply side + accounting, display-only money)

Goal: publishers can share workflows; verified usage is attributed and earnings
are *computed and shown* — still no real payments.

Deliverables:
- `marketplace/`: opt-in publish flow, `SkillVersion` (sanitized, content-hashed),
  `Listing`, visibility, licensing, basic discovery/search.
- Publish-time review + safety gate (sandboxed, reserved-action/approval rules
  mandatory for marketplace skills).
- `metering/`: `AttributionRecord` (multi-publisher weights).
- `billing/` (display-only): `PricingEngine` + `EarningsEntry` accrual ledger +
  `PublisherBalance` projection (no payout adapter wired).
- Reputation/quality signals (`μ` inputs), verified-run-gated ratings.

Exit gate / Goal Adherence:
- [ ] Publishing is opt-in and revocable; private workflows never appear in the
      registry and never leave local storage.
- [ ] A published `SkillVersion` contains no secrets (secret-hygiene scan passes).
- [ ] Tenant A runs Tenant B's published skill; verified success creates an
      attributed `MeteringEvent` and an `EarningsEntry`.
- [ ] `PlatformEarning + Σ PublisherEarning_i == N` for every event (property
      test, incl. multi-publisher).
- [ ] Failure/block/cancel accrues zero publisher earning.
- [ ] Earnings ledger is append-only; balance is a pure projection.
- [ ] Cross-tenant earnings access is refused.

### P2 — `v0.5.0` "Monetize" (real money)

Goal: consumers are charged; publishers are paid out; refunds/disputes claw back
correctly; abuse is contained.

Deliverables:
- `PayoutAdapter` (Stripe Connect): consumer billing + publisher payouts; KYC/tax.
- Settlement job: holdback `H`, reserve `R`, minimum payout `T`, `PayoutBatch`.
- Refund/chargeback/`Dispute` → clawback flow.
- Trust & safety / anti-fraud: similarity/plagiarism detection, fake-success
  detection, abuse throttling (`FraudSignals`).
- Gateway routes: `/v1/earnings`, `/v1/payout-accounts`, `/v1/disputes`, webhooks
  for processor events (verified, replay-protected — reuse `gateway/webhooks.py`).

Exit gate / Goal Adherence:
- [ ] A consumer is charged `G` for a verified successful execution; a publisher's
      `Available` balance increases by `PublisherEarning_i` after holdback `H`.
- [ ] A payout above `T` settles to a KYC-verified account via the adapter; below
      `T` rolls forward.
- [ ] A refund/chargeback posts a compensating clawback; paid-out negatives are
      recovered from reserve/future earnings; ledgers stay append-only.
- [ ] No charge or payout occurs on local-only infra (production-adapter guard).
- [ ] Self-dealing (publisher running own skill to farm commission) and replayed
      "successes" are detected and excluded.
- [ ] Processor webhooks are signature-verified and replay-protected.
- [ ] Cross-tenant + concurrent-load money tests pass; no double-pay, no lost
      accrual under retries/restarts.

---

## 7. Contract test suites (the gates, as code)

Mirror the existing per-branch "exit gate as tests" practice:

- **Metering contract:** idempotent accrual; one event per verified success;
  derived only from audit + verified outcomes; never from client claims.
- **Formula property tests:** conservation (`platform + Σ publisher == N`),
  non-negativity, multi-publisher split correctness, failure → zero.
- **Ledger invariants:** append-only; balance == replay of entries; clawback
  reverses exactly.
- **Isolation/abuse:** cross-tenant denied; self-dealing excluded; replay/dup
  rejected.
- **Payout contract:** holdback/reserve/threshold honored; KYC gate; refund
  clawback; processor webhook verify + replay protection.

---

## 8. Risks & compliance

- **Regulatory:** handling money = money-transmission / KYC / AML / tax
  (1099/VAT). **Do not build payments in-house** — use Stripe Connect (or equiv)
  behind `PayoutAdapter`; never touch raw card data.
- **Marketplace liability:** shared workflows act on others' behalf. Keep Docker
  isolation + reserved-action/approval gates **mandatory** for marketplace skills;
  require publish review; clear ToS and takedown process.
- **IP / plagiarism:** content-hash + similarity detection; ownership records;
  DMCA-style dispute path.
- **Fraud:** commission only on platform-verified success; exclude self-dealing;
  velocity/anomaly checks; reserve `R` and holdback `H` cushion reversals.
- **Privacy:** private-by-default; sanitized publish; export/delete already exist
  (`durable/maintenance.py`, desktop export/delete).

---

## 9. Decision log (open questions to settle before P2)

- Pricing model default: per-success vs subscription vs hybrid; who bears `C_p`.
- Platform rate `ρ` and any tiered/volume schedule.
- Holdback `H`, reserve `R`, minimum payout `T` initial values per region.
- Multi-publisher weighting default (`w_i` = reserved-action count vs declared).
- Reputation multiplier `μ` policy and bounds `μ_max`.
- Payout provider and supported countries/currencies for launch.

---

## 10. Sequencing summary

1. **P0 `v0.3.0`** — PostgreSQL + real OIDC/HTTP transport + frontend + metering
   recording. *Ship to real users; start accruing metering data.*
2. **P1 `v0.4.0`** — publish/registry + attribution + earnings (display-only).
   *Prove the loop with no money at risk.*
3. **P2 `v0.5.0`** — pricing + payments/payouts + disputes + anti-fraud. *Turn on
   real money once the accounting is proven.*

Do not start P2 until P1's adherence checklist is fully green, and do not start P1
until P0's is. Money turns on last, on infrastructure and accounting that are
already trusted.
