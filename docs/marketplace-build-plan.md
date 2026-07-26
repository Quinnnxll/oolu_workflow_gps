# The embedded marketplace — build-phase plan

Status: Proposed. Scope: making OoLu a venue where **real business happens** —
goods and services discovered, negotiated, bought, sold, fulfilled, and
reconciled by agents acting for verified principals — with the **platform
taking a share of every completed transaction**.

Source of truth for *what* we are building:
[`docs/marketplace-embedding-spec.txt`](marketplace-embedding-spec.txt)
(the "OoLu Embedded Agent Marketplace" system spec: flow diagram, actors,
policy ladder, contracts, data model, acceptance tests). This document is the
**build order** for that spec on top of the machinery this repository already
has. Where the two disagree, the spec's invariants win and this plan is
corrected.

Companion reading: `docs/NODEPLACE_ROADMAP.md` (the workflow-node market and
its reward formula), `docs/REWARD_PRICING_DESIGN.md`, `docs/THREAT_MODEL.md`,
`docs/adr/0002-unified-run-state.md` (the pause/resume pattern the order state
machine inherits).

---

## 0. Two markets, one set of rails

OoLu already runs one market: the **Nodeplace**, where contributed *workflow
nodes* are discovered and run, and noders earn on verified success. The
embedded marketplace is the second market: the venue where those agents (and
their humans) **do business in the world** — buy a product, sell a service,
fill an order, get paid.

They are different products but they must share one set of rails, because the
rails are where trust lives:

- **One identity.** `identity/` sessions, tenants, and stored grants;
  `nodeplace/kyc.py` verified legal entities. A buyer agent and a seller agent
  are both delegates of a verified principal — never free-floating actors.
- **One approval seam.** Budget verdicts, the holds inbox, and
  `IdentityApprovalAuthority` already gate consequential runs. Commercial
  intents ride the same inbox, hardened with a signed intent digest.
- **One accounting spine.** The durable audit chain, the idempotency ledger,
  and append-only money ledgers. The marketplace adds a **double-entry**
  ledger as the financial source of truth; the Nodeplace earnings ledger
  becomes one client of it.
- **Two revenue streams, one book.** Nodeplace commission (`ρ` on net node
  value) and the marketplace **take rate** on transactions are both just
  postings to `marketplace_fee_revenue`. The platform's share is a ledger
  fact, not a report.

The governing rule, verbatim from the spec, is the whole design in one
sentence:

> OoLu may create typed commercial intents, but money movement and
> contractual commitment occur only after deterministic policy evaluation.

---

## 1. Non-negotiable invariants (hold in every phase)

A change that violates one of these is a release blocker.

1. **Typed intents, deterministic gates.** Language-model reasoning never sits
   inside payment authorization. Models propose; the policy engine — pure,
   deterministic rules plus risk scores — disposes.
2. **Approval binds to an immutable intent digest.** Any change to price,
   quantity, counterparty, terms, or destination invalidates the approval and
   mints a new intent. Executing anything but the exact approved digest is a
   defect, not a variance.
3. **The double-entry ledger is the financial source of truth.** Entries are
   append-only and balanced per transaction. Payment-provider state is
   reconciled *to* the ledger, never authoritative over it. Corrections are
   compensating entries, never edits.
4. **Every commercial mutation carries an idempotency key.** Replays, retries,
   and duplicate webhooks produce exactly one financial transition
   (`durable/idempotency.py` is the standing precedent).
5. **Marketplace order state is separate from payment-provider state.** The
   order state machine and the PSP's lifecycle are reconciled, not conflated.
6. **Reversible before irreversible.** Authorization before capture; escrow or
   delayed capture before settlement wherever the jurisdiction allows it.
7. **Only verified principals transact.** Buyer and seller agents are
   delegates with typed, expiring, revocable delegation records — bounded by
   amount, category, counterparty, and destination. Revocation blocks every
   unexecuted intent immediately.
8. **Complete provenance.** Every recommendation and action an agent takes is
   reconstructable from the hash-linked audit chain (`durable/audit.py`).
9. **Reputation modifies review requirements; it never overrides explicit
   spending, category, privacy, or legal constraints.**
10. **No real money on local-only infra.** Production charges and payouts
    require the PostgreSQL durable adapter and real OIDC identity — the
    existing `assert_production_identity` guard extends to every marketplace
    money path.
11. **Payment credentials never reach model context.** Tokenized methods only
    (`billing/cards.py`); sensitive-data redaction stays mandatory.
12. **Jurisdiction modules before transactions.** No transacting in a
    jurisdiction whose legal/tax module is not configured (spec
    `compliance_layer.deployment_rule`).

---

## 2. What already exists (the seams are cut)

The spec reads like a green-field system; it is not. Most of its boxes have a
landed ancestor in this repository. The build phases below are mostly
*generalizations*, not inventions.

| Spec component | Standing machinery | Gap to close |
|---|---|---|
| Commerce tool API (typed tools, idempotency, authz, audit) | `gateway/` `/v1` surface: OIDC + RBAC, request idempotency, verified replay-protected webhooks, SSE | The ~18 commerce tools as typed routes; per-tool policy evaluation |
| Typed purchase intents | `skills/commerce_intent.py` — `OrderIntent` parsed deterministically, **never invents an amount**; consent reconciled by `PaymentAuthorizationResolver` | Full `CommercialIntent` contract (offer version, digest, expiry, idempotency key); sell-side intents |
| Policy & approval engine | `nodeplace/budget.py` verdicts; `values.py` typed user values; `billing/policy.py`, `billing/guard.py` | The six-decision ladder (`auto_execute` → `deny`), benchmark checks, digest minting, approval expiration |
| Review inbox | `nodeplace/holds.py` durable pending contracts with replies; desktop/gateway inboxes; `identity/` step-up approvals | Approval preview rendered *from the digest fields*; strong/dual-control approval paths |
| Order state machine | `orchestrator/` versioned `RunState` pause/resume pattern | New `marketplace/orders.py` — the 16-state machine as durable records + outbox events |
| Catalog, listings | `nodeplace/market.py`, `store.py`, listing conventions, search/retrieval | Product/service/variant catalog with structured attributes, availability, versioned descriptions |
| RFQ & quotes | `nodeplace/quotes.py` | Multi-seller bidding, expiration, normalized comparison |
| Inventory & reservations | — (reservation *pattern* exists in durable claims, e.g. `pulse.py` INSERT-OR-IGNORE election) | New `marketplace/inventory.py`: atomic reservation, expiry, oversell prevention |
| Fulfillment & delivery evidence | `worker/` signed single-use leases; durable content-addressed object store; `pulse.py` scheduling | New `marketplace/fulfillment.py`: methods, verification evidence, escalation |
| Payment orchestration | `billing/authorization.py` (`OrderRequest`), `cards.py` (tokenized), `charging.py`, `launch.py` | PSP port with authorize/capture split, delayed capture, split payouts, idempotent webhook reconcile |
| Double-entry ledger | `billing/ledger.py` (append-only, single-entry earnings) | New balanced `ledger_transactions`/`ledger_entries` with the eight spec accounts; earnings ledger re-based onto it |
| Escrow | `billing/settlement.py` holdback `H` + reserve `R` (the accounting cousin) | Explicit `escrow_liability` postings with the spec's release conditions |
| Identity & trust | `identity/` (OIDC, tenants, grants), `nodeplace/kyc.py` (Supernode KYC), `screening.py` | KYB/sanctions provider port; payout-account verification tied to payout adapter; beneficial-owner records |
| Reputation | `nodeplace/reputation.py`, `ratings.py` (verified-run-gated) | Wire as review-requirement modifier only (invariant 9) |
| Fraud | `billing/fraud.py`, `nodeplace/plagiarism.py`, self-dealing exclusion | Commerce checks: price anomaly, duplicate order, shipping mismatch, refund abuse, collusion |
| Refunds & disputes | `billing/disputes.py`, clawback flow, `tests/test_dispute_flow.py` | Evidence collection in the object store; partial refund / replacement outcomes; adjudication states |
| Tax & invoice | The reading seat (deterministic invoice parse + human seat for the rest); `legal.py` | Tax-calculation port; invoice *generation*; per-jurisdiction module registry |
| Audit & event log | `durable/audit.py` hash-linked chain; transactional outbox | `order_events` topics; intent/approval/ledger events on the chain |
| Idempotency | `durable/idempotency.py` | Applied to every commerce tool and PSP webhook |
| Metering & the platform's share | `metering/`, `nodeplace/economics.py`, `rewards.py`, `billing/settlement.py`, `payout.py` | Take-rate fee postings; GMV/take-rate metrics |
| Currency | `currency.py` (micros discipline) | One currency at MVP; multi-currency deferred |

---

## 3. Architecture additions

New package `src/oolu/marketplace/`, plus targeted extensions to `billing/`.
Everything behind ports (SQLite local / PostgreSQL production), matching the
durable runtime's contract style.

```
src/oolu/marketplace/
├── models.py        # Offer, ProductSpecification, CommercialIntent,
│                    # ApprovalRecord, Order — the spec's `contracts`, frozen
├── digest.py        # canonical serialization of the digest fields → SHA-256;
│                    # verify(digest, offer, policy_version) — pure
├── policy.py        # the six-decision ladder; deterministic rules + risk
│                    # scores; purchase + sales policies as typed records
├── delegation.py    # agent delegation records: scopes, limits, expiry,
│                    # revocation (blocks unexecuted intents immediately)
├── catalog.py       # products, services, variants, bundles; versioned
│                    # descriptions; availability
├── offers.py        # fixed-price + negotiated offers, versioned, signed
├── rfq.py           # request-for-quote, multi-seller bids, expiry
├── inventory.py     # atomic reserve/commit/release; reservation expiry
├── orders.py        # the 16-state order machine; durable + outbox events
├── fulfillment.py   # methods, delivery evidence, verification, escalation
└── review.py        # approval requests from digests; expiration;
                     # normal / strong / dual-control authentication

src/oolu/billing/    (extensions)
├── doubleentry.py   # accounts, balanced transactions, append-only entries:
│                    # buyer_payable, marketplace_cash, seller_payable,
│                    # escrow_liability, marketplace_fee_revenue, tax_payable,
│                    # refund_payable, dispute_reserve
├── psp.py           # PSP port: tokenize, authorize, capture, refund, split
│                    # payout; idempotent webhook reconciliation (Stripe first)
├── escrow.py        # release conditions over delivery evidence / acceptance /
│                    # timeout / milestones / dispute resolution
└── tax.py           # tax-calculation + invoice-generation ports; the
                     # jurisdiction module registry (deployment gate)
```

Gateway grows a versioned commerce surface mirroring the spec's tool list —
`/v1/market/offers`, `/v1/market/rfqs`, `/v1/market/intents`,
`/v1/market/intents/{id}/approval`, `/v1/market/orders`,
`/v1/market/orders/{id}/fulfillment`, `/v1/market/refunds`,
`/v1/market/disputes`, `/v1/market/ledger` — every mutation schema-validated,
policy-evaluated, idempotent, rate-limited, audited (the gateway already
enforces the last four for runs; commerce reuses those seams).

The flow, end to end:

```
agent ask ──► typed intent (skills/commerce_intent → marketplace/models)
         ──► policy.py evaluates (deterministic; risk signals as inputs)
               ├─ auto_execute / execute_and_notify ─► orders.py
               ├─ require_* ─► review.py ─► holds inbox ─► signed digest ─► orders.py
               └─ deny ─► back to the agent with reasons
orders.py ──► billing/psp.py authorize ─► doubleentry postings ─► confirmed
         ──► fulfillment.py ─► delivery evidence ─► billing/escrow.py release
         ──► settlement: seller_payable paid out, marketplace_fee_revenue kept
```

---

## 4. The platform's share

The take is a **ledger fact**: every completed order posts one balanced
transaction that splits the buyer's payment between the seller, the platform,
and the tax authority. No report computes revenue; the book *is* revenue.

Per completed order (single currency, MVP shape):

```
G      = buyer total (subtotal + tax + fees)
fee    = takerate × subtotal   (+ optional fixed per-order component)
postings (balanced):
    marketplace_cash        +G
    buyer_payable           −G
    seller_payable          +(subtotal − fee)
    marketplace_fee_revenue +fee
    tax_payable             +tax
    escrow_liability        (transit account while capture is held)
```

Refunds and disputes post compensating transactions (`refund_payable`,
`dispute_reserve`); nothing is ever edited. When a *node* performs the
commerce (an agent workflow bought on the Nodeplace executes a purchase), the
Nodeplace reward formula applies to the **node fee** and the marketplace take
applies to the **transaction** — two separate postings, one book, no double
counting. Conservation checks are property tests, exactly as
`NODEPLACE_ROADMAP.md` §7 does for `ρ`/`σ`.

`takerate`, fee sides (buyer/seller/both), category schedules, and floors are
decision-log items (§9) — the *mechanism* is fixed here, the *number* is not.

---

## 5. Build phases

Each phase is a tagged milestone with an exit gate and a binary Goal
Adherence checklist. Do not advance a phase until every box in its checklist
is true. Nodeplace prerequisite: real money anywhere requires the production
substrate (PostgreSQL durable adapter + JWKS OIDC) from Nodeplace P0.

### M0 — the commercial spine (intents, digests, policy — no catalog, no money)

**Status: LANDED** — `src/oolu/marketplace/`: the spec's contracts as
frozen records (`models.py`), the canonical SHA-256 digest over exactly
the spec's `intent_digest_fields` (`digest.py`), the six-decision ladder
as pure functions with the default purchase AND sales policies
(`policy.py`), typed revocable delegation (`delegation.py`), durable
tenant-scoped stores with UNIQUE-keyed idempotent intent creation
(`store.py`), the review surface's pure rules and exact-terms summaries
(`review.py`), and `MarketplaceSpine` (`service.py`) — creation,
digest-bound approval (step-up floor, dual control, single-use,
expiring), revocation that blocks unexecuted intents both at revocation
and live at authorization, and `authorize_execution` recomputing the
digest against the LIVE offer. Doors: `/v1/commerce/{policy,
delegations, intents, approvals}` — deliberately no execution door.
Gates pinned by `tests/test_marketplace_spine.py` and
`tests/test_marketplace_gateway.py`, including the import scan proving
the package touches no money path.

Goal: the governing rule as running code. Typed commercial intents, the
deterministic policy ladder, digest-bound approvals, and revocable agent
delegation — proven with fake offers and zero payment paths.

Deliverables:
- `marketplace/models.py` — the spec's `contracts` block as frozen records
  (`ProductSpecification`, `Offer`, `CommercialIntent`, `ApprovalRecord`).
- `marketplace/digest.py` — canonical digest over the spec's
  `intent_digest_fields`; pure verify.
- `marketplace/policy.py` — the six decisions with the spec's default
  purchase and sales policies as typed, versioned policy records; user
  limits stored via the existing typed-values store.
- `marketplace/delegation.py` — delegation records with the spec's fields;
  revocation blocks unexecuted intents.
- `marketplace/review.py` riding `nodeplace/holds.py`: approval requests
  rendered from digest fields; approval expiration; step-up (strong)
  authentication via the existing identity seam.
- Gateway routes for intents and approvals (no order execution yet).

Goal Adherence:
- [x] An intent's digest changes iff a material field changes (property test
      over the digest field set).
- [x] Approval of digest A can never execute digest B — including
      offer-version bumps (the spec's "seller changes price after approval"
      acceptance test).
- [x] Expired approvals are refused; approval reuse is refused.
- [x] Policy evaluation is a pure function of typed inputs: same inputs,
      same decision, with reasons and `policy_version` recorded.
- [x] Every decision (including `deny`) lands on the audit chain.
- [x] Revoking a delegation blocks all unexecuted intents immediately.
- [x] No code path in M0 can move money (no PSP port exists yet).

### M1 — the fixed-price market (first real dollar, first fee)

**Status: LANDED** — `billing/doubleentry.py`: the eight-account
balanced append-only ledger with replay projections (balances, GMV, the
take). `billing/psp.py`: the authorize/capture/refund/void provider port
— `StripePaymentIntents` (manual capture, vault/transport discipline)
and the pre-launch `FakePsp`. `marketplace/catalog.py`: versioned
listings, KYC-gated publication, offers minted at the listing's version
so the digest law reaches the shelf. `marketplace/orders.py`: the state
machine — authorize at confirmation, capture ONLY at acceptance with
the take-rate fee split posted once (`DEFAULT_TAKE_RATE_BPS = 500`,
interim), refunds as exact compensating transactions, provider webhooks
replaying into idempotent transitions, and `require_production_money`
in front of any live provider call. Doors: `/v1/commerce/{catalog,
listings, orders}` and per-order ship/deliver/accept/cancel/refund/
ledger. Pinned by `tests/test_marketplace_{ledger,orders,gateway}.py`.

**Phase closed** by three follow-ups: **(1) the shell Market surface**
(`desktop-app/frontend/src/components/Market.tsx` + the rebuilt shell
bundle) — Shop / Approvals / Orders / Sell over the real `/v1/commerce`
doors; buying walks offer → intent → verdict → approval → order with the
server's digest-rendered summary on the approval card, acceptance is
labeled as the capture moment, and the Sell pane is KYC-gated end to
end (vitest-pinned in `Market.test.tsx`). **(2) Live Stripe** —
`marketplace/sellerkyc`-independent: the assembly swaps
`StripePaymentIntents` in exactly when a secret key exists, order
metadata (`oolu_order_id`/`oolu_tenant`) round-trips through the
provider, and `/v1/webhooks/stripe` reconciles `payment_intent` events
into the order machine's idempotent transitions
(`tests/test_marketplace_psp.py`). **(3) Seller KYC** —
`marketplace/sellerkyc.py` on the standing KYC store and mailbox
screen: apply (personal mailboxes refused outright) → reviewer with
approve authority decides → publication opens; doors under
`/v1/commerce/seller/kyc` (+queue, +decide).

Goal: the spec's `phase_1` inside its `mvp_boundaries` — a working
fixed-price marketplace for low-risk physical goods: one currency, one
jurisdiction, verified accounts, human approval above limits, refundable
card payments — and the platform's first take-rate posting.

Deliverables:
- `marketplace/catalog.py` + `offers.py` (fixed-price only), listing flow
  reusing Nodeplace listing/review conventions; seller onboarding gated by
  the existing KYC seam.
- `marketplace/orders.py` — the full 16-state machine, durable, outbox
  events, idempotent transitions.
- `billing/psp.py` (Stripe first): tokenize, authorize → capture on
  fulfillment policy, refund; idempotent, signature-verified webhooks
  (reusing `gateway/webhooks.py` discipline).
- `billing/doubleentry.py` with the eight accounts; take-rate fee postings;
  refunds as compensating transactions.
- Basic shipping fulfillment + delivery confirmation; refund flow through
  the existing disputes seam (manual review per MVP).
- Buyer surfaces: commerce cards in chat (offer, approval preview, order
  tracking); seller console screens (listings, orders, payouts) on the
  existing shell.

Goal Adherence (the spec's acceptance tests, distributed):
- [x] Purchase below all thresholds auto-executes and notifies; above the
      single-order threshold, **no authorization occurs** before a recorded
      approval.
- [x] First purchase from an unknown seller requires review regardless of
      amount.
- [x] Duplicate execution request with the same idempotency key returns the
      existing order.
- [x] Duplicate PSP webhooks produce one financial transition and one
      balanced ledger transaction.
- [x] Every ledger transaction balances; account balances are pure
      projections of entries (property test).
- [x] A completed order posts the take-rate fee to
      `marketplace_fee_revenue`; GMV and take-rate metrics read from the
      ledger alone.
- [x] Refund posts a compensating transaction; the order and payment state
      machines reconcile.
- [x] Unverified sellers cannot list; card data never touches OoLu servers
      (tokenization only); model context contains no payment credentials.
- [x] Money paths are refused on local-only infra.

### M2 — negotiation, escrow, and trust (the spec's `phase_2`)

**Status: LANDED** — `marketplace/rfq.py`: typed specifications,
multi-seller quotes judged for eligibility BEFORE policy (substitutes
marked with their gaps, never awardable), normalized comparison, awards
returning the exact offer for the intent door; the seller's signed
`SalesPolicy` (durable per principal) gates submission — below the
absolute floor refuses without model discretion.
`marketplace/negotiation.py`: signed bounds, durable round budgets, a
pure violation check, and an `agree` that re-checks bounds at the
moment of commitment. `marketplace/inventory.py`: guarded-UPDATE
reservations (exactly one contender gets the last unit), lazy expiry,
commit/release exactly once. `billing/escrow.py` + the order machine:
capture into `escrow_liability` on evidenced delivery (missing evidence
files an exception and blocks acceptance; late evidence heals),
release on acceptance or the automatic timeout sweep, the spec's
direct-settlement exceptions, refunds reversing BOTH settlement legs.
`billing/tax.py`: the jurisdiction registry as a hard deployment gate
(unconfigured = no offers minted), tax estimates on catalog offers,
sequential invoices issued exactly once per completed order.
`marketplace/fraud.py`: risk facts DERIVED from the order book — spend
totals, counterparty familiarity, reputation (its one lever: the
auto-execution trust bar, property-tested to never touch an explicit
constraint), and deterministic risk signals (self-dealing, duplicates,
refund abuse, price anomaly) — explicit caller facts always win.
Doors: `/v1/commerce/rfqs` (+quotes, +award), `/v1/commerce/
sales-policy`, per-order `/evidence` and `/invoice`; the acceptance-
timeout sweep rides order-list traffic. Pinned by
`tests/test_marketplace_{rfq,inventory,escrow,trust}.py` and the
updated gateway suite.

**Phase closed** by two follow-ups: **(1) the shell surface** — the
Market screen grows a Requests pane (open a typed RFQ, compare
normalized quotes with substitutes marked by their gaps, award only
the eligible into the same intent door), escrow-aware order cards
(evidence typed on the card at delivery, the "escrow stays held"
exception named with its Attach-evidence way out, "Accept — release
escrow" vs direct capture, the invoice on completed orders), and the
seller's signed-boundary editor (the absolute floor, signed in
micros) — pinned by the vitest suite in `Market.test.tsx`.
**(2) evidence blobs** — delivery evidence supplied as content lands
in the object store content-addressed (filesystem locally, R2/S3 when
configured), so the `sha256:` ref on the order and the audit chain is
tamper-evident; a host without storage refuses content with
directions rather than storing a claim.

Goal: autonomy grows on both sides — within signed bounds. RFQ and
structured quotes, negotiation limits, escrow/delayed settlement gated on
delivery evidence, reputation and fraud scoring, tax/invoice integration.

Deliverables:
- `marketplace/rfq.py`: multi-seller bidding, quote expiry, normalized
  comparison; `offers.py` grows negotiated offers with signed bounds
  (max price / min price / max discount / max rounds / allowed terms).
- User autonomy budgets (auto-purchase, daily, monthly limits) and seller
  automation policies (auto-publish, auto-accept, floors) as typed policy
  records feeding M0's ladder.
- `marketplace/inventory.py`: atomic reservation with expiry; oversell
  prevention under concurrency.
- `billing/escrow.py`: `escrow_liability` postings; release on verified
  delivery / acceptance / timeout / dispute resolution; the spec's
  exceptions (immediate digital delivery, low-value trusted).
- Delivery-evidence capture into the object store; fulfillment escalation.
- Reputation wired as a review-requirement modifier; commerce fraud checks
  (price anomaly, duplicate order, shipping mismatch, refund abuse,
  collusion) extending `billing/fraud.py`.
- `billing/tax.py`: tax calculation + invoice generation for the launch
  jurisdiction; the jurisdiction registry as a deployment gate.

Goal Adherence:
- [x] A negotiation agent cannot accept terms outside its signed bounds;
      an offer below the seller's absolute floor is denied **without model
      discretion**.
- [x] Two concurrent orders for the last unit: exactly one reservation
      wins; the other is refused; expired reservations release stock.
- [x] Missing delivery evidence keeps escrow unreleased and opens an
      exception; verified evidence (or acceptance timeout) releases it with
      balanced postings.
- [x] Reputation never relaxes an explicit spending/category/legal
      constraint (property test over policy inputs).
- [x] Elevated fraud score forces the strong-approval path; score ≥ deny
      threshold refuses deterministically.
- [x] Every completed order has a generated invoice and a tax posting;
      transacting in an unconfigured jurisdiction is refused.
- [x] OoLu recommending a substitute outside required specifications marks
      the offer ineligible *before* policy evaluation.

### M3 — services, organizations, reconciliation (the spec's `phase_3`)

**Status: LANDED** — **Milestones**: the payment schedule rides
the Offer (digest-material: a changed schedule is a changed term, and
tranches must sum to the subtotal), `marketplace/milestones.py` keeps
each tranche's life durably, and the order machine enforces the flow —
milestone offers force escrow, the first evidenced delivery captures
the whole total, acceptance releases exactly one tranche (fees and tax
split proportionally, remainders on the final), a failure freezes the
remainder in escrow, and `refund-unreleased` resolves by returning
exactly the frozen part. **The recurring rule**
(`marketplace/recurring.py` + `spine.mint_renewal`): obligations exist
only from an AUTHORIZED recurring intent (the ladder already forced
approval); identical renewals proceed as auto-approved intents —
digest still binding, delegation still re-checked, so a revoked agent
cannot renew — and any material change refuses with the changed terms
named and re-enters policy as a new intent. **Org controls**
(`marketplace/orgcontrol.py`): payout-destination changes always take
the multi-approver path — two distinct strong approvers, self-approval
refused, and a delay window before an approved change can apply; the
owner can kill a hostile change inside the window. **Execution jobs**
(`marketplace/jobs.py`): typed dispatch carrying the order's approved
price; an acknowledgement with different terms invalidates
deterministically (the digest law, restated for the physical world);
the dispatcher port is where the worker control plane's signed leases
attach. **Reconciliation** (`marketplace/reconciliation.py`): finished
orders match against ledger, invoice, payment refs, and evidence —
matched orders close, mismatches file exceptions with the trail
attached, and a duplicate charge disputes the order itself. Doors for
all five under `/v1/commerce/...`. Pinned by
`tests/test_marketplace_{milestones,recurring,m3_ops}.py`.

**Phase closed** by three follow-ups: **(1) the worker-lease
dispatcher** (`marketplace/jobdispatch.py`) — jobs become
capability-scoped tasks on the worker control plane
(`execute:<node>`), assigned only to a worker holding that capability
under an HMAC-signed, expiring, audience-bound lease that worker alone
can verify; the lease token never rides the audit chain (a credential
is not evidence), and a dispatch with no capable worker fails LOUDLY —
the job marks failed and the caller hears it. **(2) adjudication**
(`OrderService.adjudicate` + the `/adjudicate` door behind approve
authority) — the marketplace's verdict as deterministic postings:
`replacement` re-enters fulfillment untouched, `reject` releases
frozen escrow to the seller with the split's exact remainders,
`full_refund` reverses every settlement, and `partial_refund` returns
the awarded amount from frozen escrow first and the seller's payable
for the rest — the platform's fee and the tax line stand. **(3) the
shell surfaces** — milestone schedules on order cards (deliver with
evidence, accept tranche, fail — each naming what it does to escrow)
and standing obligations with one-click lawful renewal (renew →
auto-approved intent → placement, digest law included) and
cancellation; pinned in `Market.test.tsx`, shell bundle rebuilt.

Goal: beyond parcels — services with milestones, subscriptions under the
recurring rule, organization approvals with dual control, physical execution
nodes, and books that close themselves.

Deliverables:
- Service marketplace: scheduled services, milestone payments (escrow per
  milestone), acceptance per milestone.
- Subscription management: first subscription, price increase, term
  extension, or materially changed renewal re-enters policy evaluation
  (`billing/subscription.py` grows the recurring rule).
- Organization approvals: role-based approval and dual control above the
  org limit; payout-destination changes always multi-approver + delay.
- Physical execution nodes: typed job dispatch over the existing worker
  lease seam; changed terms re-enter policy; execution evidence uploads.
- Reconciliation: match order ↔ invoice ↔ payment ↔ shipment ↔ receipt;
  duplicates and incorrect charges detected; exceptions filed to the inbox;
  dispute evidence assembled automatically from the audit chain and object
  store.

Goal Adherence:
- [x] A milestone releases only its own escrow tranche; a failed milestone
      freezes the remainder.
- [x] No recurring obligation is created or materially changed without a
      fresh policy pass; renewals inside approved terms proceed.
- [x] Above the dual-control limit, one approver is never enough;
      self-approval is refused (existing identity rule).
- [x] Payout-destination change requires the multi-approver path and a
      delay window.
- [x] A physical job's changed terms (price, schedule, scope) invalidate
      the prior approval — same digest law as purchases.
- [x] Reconciliation closes matched orders and files unmatched ones as
      exceptions; duplicate charges surface as disputes with evidence
      attached.

### M4 — the open market (the spec's `phase_4`)

**Status: LANDED** — **The A2A wire contract**
(`marketplace/protocol.py`): an external offer is the typed `Offer` the
spine already trusts, its signature an HMAC over every material field
under the announcing peer's shared secret; verification is arithmetic,
and there is deliberately no protocol beyond the domain contract.
**Federation** (`marketplace/federation.py`): peers are registered by an
operator with their jurisdiction (secrets injected at composition — the
durable records hold metadata only), suspension blocks imports
immediately, the compliance deployment gate crosses the boundary (a
peer jurisdiction with no configured module refuses import), and a
tampered or unsigned offer never reaches the intent door. What survives
import is an ordinary offer: the ladder judges it, the digest binds it
(a peer re-signing at a new price kills the approval), and the
settlement posts on OUR ledger with our take. **Sourcing**: one
specification against the local shelf and every federated import — one
normalized, eligibility-marked comparison where the local shelf is
judged by the same attribute bar as every quote. **Partners**
(`marketplace/partners.py`): partner products are offers — a financing
plan is a RECURRING offer (the ladder forces its approval, the
recurring book governs its renewals; `SimpleInterestFinancing` is the
deterministic reference adapter), an insurance policy an offer with its
own terms. Doors: `/v1/commerce/peers` (+state, +offers; registration
behind operator authority), `/v1/commerce/source`. Pinned by
`tests/test_marketplace_federation.py` and the gateway suite.

**Phase closed** by four follow-ups: **(1) the live peer wire**
(`marketplace/peerwire.py` + the `/v1/commerce/announcements` and
per-peer `/fetch` doors) — pairing is three agreed strings (two
identities, one shared secret); a host's announcements door signs its
public shelf for the asking peer, `fetch_from_peer` pulls every item
through the import door (signature, jurisdiction gate, suspension all
apply; one bad announcement never poisons the batch), and
`HttpPeerTransport` rides the providers' HTTP seam — proven by a
two-host in-process test where two real gateways trade signed offers.
**(2) HTTP partner adapters** (`marketplace/partnerwire.py`) —
financing and insurance quotes over the vault/transport seam:
documented wire shapes, typed validation (an answer that is not a
quote is an error, never a guess), asked-vs-quoted cross-checks, and
the secret minted into the header at call time only. **(3) supply
orchestration** (`marketplace/orchestration.py`) — `SupplyOrchestrator`
ranks every eligible source, mints the intent for the best one
(feeding the ladder exactly the attestation the source carries), and
falls back deterministically when a source fails at placement;
exhaustion refuses loudly and lands on the chain. **(4) the shell
surface** — "Source everywhere" in the Shop pane: one search across
the local shelf and every peer, origin chips, substitutes with their
gaps and no Buy button, and the sourced offer feeding the intent door
unchanged. Pinned across `tests/test_marketplace_federation.py` and
`Market.test.tsx`; shell bundle rebuilt.

Goal Adherence:
- [x] An external offer with a broken or missing wire signature never
      reaches the intent door.
- [x] A cross-market purchase is still a typed intent with a digest;
      changed terms kill the approval across the federation boundary.
- [x] The platform's share posts on cross-market settlements exactly as
      on local ones.
- [x] The compliance deployment gate crosses the boundary: an
      unconfigured peer jurisdiction refuses import.
- [x] Peer suspension blocks new imports immediately.
- [x] The sourcing sweep is one normalized, eligibility-marked
      comparison across every shelf — the local one included.
- [x] A financing partner's product enters as a recurring offer and
      cannot skip the ladder.

Goal: OoLu's market meets other markets. Cross-marketplace sourcing,
an agent-to-agent commercial protocol (typed offers/intents/approvals as the
wire contract), dynamic supply orchestration, financing and insurance
partners, and per-jurisdiction deployments of the compliance modules.

Its one standing law held throughout: external marketplaces and partner
protocols integrate **behind the same policy engine and ledger**; a
cross-market purchase is still a typed intent with a digest, and the
platform's share is still a posting.

---

## 6. Contract test suites (the gates, as code)

Mirroring the per-phase "exit gate as tests" practice:

- **Digest contract:** material-change ↔ digest-change equivalence; approval
  binds exactly one digest; expiry; reuse refusal.
- **Policy contract:** determinism; the six decisions; reasons +
  `policy_version` always recorded; reputation-never-overrides property.
- **Ledger invariants:** every transaction balances; append-only; balances
  are replay projections; compensating entries reverse exactly.
- **Idempotency:** every commerce mutation and every PSP webhook replays to
  the same single effect.
- **Order/PSP separation:** order machine and payment machine reconcile from
  independent state; forced webhook disorder converges.
- **Escrow:** release only on the spec's conditions; missing evidence holds.
- **Isolation & abuse:** cross-tenant refusal; self-dealing exclusion;
  delegation revocation; fraud-threshold denial.
- **MVP boundary:** prohibited categories, unverified sellers, irreversible
  payment methods, and autonomous recurring subscriptions are refused at the
  policy layer, with tests naming each exclusion.

---

## 7. Risks & compliance

- **Money movement is regulated.** Stay merchant-of-record-adjacent, never a
  money transmitter: licensed PSP (Stripe Connect or equivalent) behind
  `billing/psp.py`; raw card data never touches OoLu; KYC/KYB/sanctions via
  provider ports. Legal review before M1 flips on real charges.
- **Agent-committed contracts.** The digest + policy design exists precisely
  so a model can never commit a principal to terms no human (or no signed
  policy) authorized. Keep the prohibited-actions list
  (`capture_payment`, `release_escrow`, `change_payout_destination`, …)
  enforced at the tool layer, not by prompt.
- **Marketplace liability.** Restricted-goods controls, seller disclosures,
  consumer cancellation rights, and takedown process are compliance-layer
  deliverables in M1/M2, not afterthoughts; the jurisdiction registry is a
  hard deployment gate.
- **Fraud economics.** Take-rate revenue attracts refund abuse, collusion,
  and synthetic sellers; the reserve/holdback cushion and the M2 fraud
  checks are the containment. Fraud loss is a first-class metric from day
  one.
- **Two-market confusion.** Nodeplace earnings and marketplace settlements
  must never share accounts loosely — one double-entry book, distinct
  accounts, conservation tests on both formulas.

---

## 8. Observability

The spec's three metric families land in `telemetry/` (and the investor
panel's series idiom) as ledger- and audit-derived projections:

- **Business:** GMV, completed-order rate, AOV, take rate, refund rate,
  dispute rate, fulfillment time, seller conversion — all computed from the
  ledger and order events, never from side counters.
- **Agent:** recommendation acceptance, autonomous completion rate, human
  override rate, policy denial rate, savings against benchmark.
- **Safety:** unauthorized execution attempts, digest mismatches, duplicate
  payment attempts, fraud loss, false-positive review rate, policy bypass
  attempts — each one alarmed, because each one is an invariant with a
  counter.

---

## 9. Decision log (settle before the phase that needs them)

Before M1:
- Take-rate number and shape (flat vs category schedule; buyer/seller/both;
  fixed component). Interim: seller-side flat rate, single number.
- Launch jurisdiction + currency; PSP (Stripe Connect assumed).
- Default user limits (auto-purchase, notify, strong-approval) and their
  onboarding defaults.
- Merchant-of-record posture (platform vs seller of record) — legal input.

Before M2:
- Price benchmark source for `price_within_benchmark_percent`.
- Escrow defaults per category; acceptance-timeout length.
- Negotiation bounds vocabulary (which term variations are ever delegable).
- Tax provider (build vs integrate).

Before M3:
- Organization dual-control limit defaults; payout-change delay window.
- Milestone/escrow structures allowed per jurisdiction.
- Physical execution node certification bar.

---

## 10. Sequencing summary

1. **M0 — the commercial spine.** Intents, digests, the policy ladder,
   delegation, digest-bound approvals. *No catalog, no money; the law first.*
2. **M1 — the fixed-price market.** Catalog, orders, PSP auth/capture,
   double-entry ledger, refunds — inside the MVP boundaries. *First real
   dollar, first take-rate posting.*
3. **M2 — negotiation, escrow, trust.** RFQ, bounds, inventory, escrow on
   evidence, reputation/fraud, tax/invoice. *Autonomy grows only inside
   signed bounds.*
4. **M3 — services, organizations, reconciliation.** Milestones,
   subscriptions, dual control, execution nodes, self-closing books.
5. **M4 — the open market.** Other marketplaces, agent-to-agent protocol,
   financing — planned on real data, behind the same policy engine and
   ledger.

Money turns on last where trust turns on first: every phase ships its
deterministic gates and its ledger proofs before the next phase widens what
agents may do with them.
