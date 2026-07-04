# Reward & pricing system — design

Status: Implemented (`nodeplace/market.py`, `nodeplace/rewards.py`,
`nodeplace/quotes.py`, `nodeplace/economics.py`, gateway `/v1/market/*`).
Builds on the existing money stack and the Nodeplace roadmap's invariants;
supersedes the `Reward_system.txt` prototype.

## 1. What the prototype got right, and what had to change

The prototype introduced the right *vocabulary*: node pricing classes,
a cost vector, damped reference prices with movement bands, quote modes,
subscription coverage vs pass-through invoices, and difficulty/scarcity-aware
contributor payouts. All of that survives here. Five things could not:

1. **It paid contributors at quote time.** Payouts were computed inside
   `quote()` — before anything ran. The roadmap's invariant #3 is
   *commission only on platform-verified success*, and the repo already has
   the machinery to honor it (metering deriver → billing split → earnings
   ledger → settlement). The new system computes **shares and multipliers**
   at plan time and hands them to that pipeline via `RunBinding`; money moves
   only when a verified success event exists.
2. **Its bonus math could overspend the pool.** `contributor_pool x
   difficulty_bonus x scarcity_bonus x quality_bonus` exceeds the pool as
   soon as the product of bonuses exceeds 1 — payouts were unbounded relative
   to revenue. Here, bonuses become the `NoderShare.multiplier`, and the
   billing `PricingEngine` **normalizes weight x multiplier within the pool**
   with integer-micros conservation. A multiplier changes a noder's slice,
   never the size of the pie (`test_full_split_conserves...`).
3. **It ranked on self-declared quality.** `Node.quality`, `trust`, and
   `success_probability` were whatever the noder typed — instantly gameable.
   Ranking here uses only **platform-verified statistics**: the Beta
   posterior of metered successes and the ratings-derived reputation mu
   (which itself requires a verified run to rate). A new node starts at the
   neutral 0.5 posterior and must *earn* rank with real runs.
4. **Its reference prices lived in a dict.** The `DampingBook` forgot
   everything on restart, so the band that is supposed to prevent price
   shocks reset with every process. `PriceBook` persists references in
   SQLite (same pattern as the trace store).
5. **Its budget warnings overwrote each other.** Three checks wrote to one
   field; only the last survived. Warnings are now a list; every exceeded
   limit is reported.

## 2. Price formation (`market.py`)

The noder's `PricingPolicy.unit_price` stays the **ask**. Four deterministic,
explainable forces turn an ask into a **cleared price** (each clearing
returns the full breakdown in `ClearedPrice.notes`):

| Force | Rule | Why |
| ----- | ---- | --- |
| Cost floor | `>= automation_cost x (1 + min_margin)` | nobody is subsidized into negative-margin work |
| Competition pull | target `= ask x (1 - competition x sensitivity)`; `competition = n_eff / (n_eff + 4)` counts quality-comparable substitutes, saturating | crowded commodity classes converge to utility pricing; scarce professional supply keeps pricing power (sensitivity 0.60 commodity / 0.30 workflow / 0.05 professional) |
| Value anchor | `<= max_value_share x` (minutes saved x hourly rate) | automation must stay visibly cheaper than doing the work by hand (35% share; 60% for professional work) |
| Damping | EMA toward the target, clamped to a per-class band per 30-day period, from a **persisted** reference | no price shocks in either direction; commodities may fall fast (-20%/period) but rise slowly (+8%/period) |

**Regulated pass-through is exempt from everything.** Government fees,
monopoly audits, and company invoices clear at face value on their own
invoice lines, carry no platform commission, and are never marked up. That
line is bright and testable (`test_regulated_fees_pass_through_untouched`).

## 3. Route economics (`market.py`: `utility`, `rank_candidates`)

Per step, candidates are scored by **verified quality per retry-adjusted
dollar**:

```
effective_price = (cleared_price + external_invoice) / p(success)
utility = (p(success) x reputation)^w_q x (difficulty x scarcity x liability)^0.2
          / (effective_price^w_p x latency_penalty^w_l)
```

- `p(success)` is the Beta-posterior mean of *metered* runs — the same
  number the personal planner's `TraceStore` keeps, so local and marketplace
  routing share one notion of reliability.
- The retry adjustment makes flaky-but-cheap honest: a node that fails half
  its runs effectively costs double, and the budget projection charges the
  plan the same way.
- Quote modes reweight the exponents (budget 1.45/0.85 price/quality …
  certified 0.55/1.60), so "cheapest that works" and "most proven, price is
  secondary" are both one enum away (`test_budget_mode_prefers_cheap...`).

## 4. Noder rewards (`rewards.py`)

A noder's slice of the contributor pool is `weight x multiplier`, where the
multiplier is a product of bounded, **non-gameable** factors
(`RewardBreakdown` exposes each one):

| Factor | Range | Signal |
| ------ | ----- | ------ |
| Reputation | 0.25–2.0 | ratings mu (verified raters only) |
| Verified reliability | 0.5–1.5 | Beta mean of metered success; exactly 1.0 with no evidence |
| Scarcity | 1.0–1.5 | `1 + 0.5/(1 + comparable substitutes)` |
| Maintenance | 0.7–1.0 | decays after ~a year unmaintained; updating restores it |
| Commodity decay | 0.35–1.0 | commodities in crowded classes converge to parity; other classes exempt |

Clamped to `[0.1, 4.0]` overall — and since billing normalizes within the
pool, the clamp is legibility, not safety; conservation is structural.

**Class-aware commission** (`commission_rate`): the platform's take is
lowest where supply is hardest to attract — professional 20%, workflow 30%,
commodity 35%, regulated pass-through 0% — further reduced (never below 10%)
by a scarcity bonus. This is the supply-side lever: the marketplace pays
best, in both slice and commission, for contributing what it *lacks*.

**Lineage royalties** (`lineage_shares`): a node derived from other nodes
shares each verified success upstream with geometric decay (level *n* holds
`0.35^n` of the executing noder's weight), normalized so royalties are carved
out of the pool, never added on top. Derivation becomes a passive income
stream for upstream noders — the incentive to publish *composable* nodes,
which is exactly what the DAG planner wants to consume. Expressed as plain
`NoderShare`s, so attribution, disputes, clawback, and settlement all work
unchanged.

**The bridge** (`build_run_binding`): bind run → economics *before*
execution; the metering deriver turns the binding into accruals only for
verified successes. Failure = no event = no earnings; clawback and the
settlement reserve handle post-hoc reversals. Nothing in the new modules
writes to a ledger.

## 5. Consumer quoting (`quotes.py`)

`QuoteEngine.quote()` renders the chosen route the way a consumer needs to
see it:

- **Coverage split**: commodity/workflow steps are plan-covered (line shows
  the value delivered, amount 0.00); regulated and professional steps are
  outside-plan lines at face value, per vendor. `total_user_due_now` is
  exactly the sum of outside-plan lines — nothing hidden.
- **Budget projection** charges the plan the retry-adjusted expected cost,
  and every exceeded limit (budget, CLI quota, API quota) appears as its own
  warning.
- **Payout previews**: each step shows what every noder in its lineage would
  earn *if the run verifies*, computed with the same commission and
  multiplier math settlement will use — labeled as a forecast. Noders see
  their incentive before anyone runs anything; consumers see where their
  money goes; the ledger stays untouched until verification.

## 6. Live assembly and the gateway surface

`nodeplace/economics.py` is the seam between records and economics.
`CandidateAssembler.assemble(query)` joins, per active public listing:

- the **registry** (version, ask from the pricing policy, owning noder);
- the **metering ledger** — verified successes and their *measured* provider
  cost (which becomes the candidate's cost vector);
- the **audit log** — failed `workflow.executed` records mapped to versions
  through run bindings, so failure counts are real too (`LiveVersionStats`);
- the **rating store** — the reputation mu from verified raters;
- substitute counts computed per class key across the assembled set.

Market classification rides on listing tags: `class:<node class>` (default
`workflow`) and `market:<segment>` produce class keys like
`commodity:file_conversion_csv_xlsx`. The contribute endpoint accepts a
`pricing` object so a noder sets the ask at contribution time.

Two authenticated gateway routes expose it:

- `GET /v1/market/candidates?q=&mode=&days_elapsed=` — assembled candidates
  ranked by mode-weighted utility, each with its cleared-price breakdown,
  reward signals, and current reward multiplier. **Read-only**: prices are
  previewed with `commit=False`, so browsing never moves the price book.
- `POST /v1/market/quotes` — a full `WorkflowQuote` (coverage lines,
  warnings, payout previews) from live data; steps name a discovery query
  each, the plan is optional (a documented default applies). Quotes are
  previews by default (`commit_prices: false`); nothing here writes a ledger
  — money still moves only through the metering deriver on verified success.

And the loop closes at run submission: `POST /v1/runs` with a
`node_version_id` assembles that version's live economics, clears the price
(committing — a real run moves the market reference), and binds the run to
its noder shares (`build_run_binding`) inside the idempotent submit. From
there the standing pipeline does the rest: audit-verified success -> metering
event -> billing split -> earnings ledger -> settlement. A version without an
active public listing is refused, so a revoked node can never be bound to a
paying run. The end-to-end test drives submit -> verified execution ->
derive -> accrual and asserts micro-conservation on the way through.

## 7. Incentive properties (all under test)

- A better-rated, more-reliable noder earns a larger slice of the same pool.
- Scarce supply earns more and pays lower commission; crowded commodities
  decay to parity but never to zero.
- An unmaintained node's multiplier drifts down; shipping an update restores it.
- A new node cannot buy rank with claims — only verified runs move its posterior.
- Flaky nodes lose routes even at low sticker prices (retry-adjusted cost).
- Prices cannot shock (damping band), cannot dip below cost, cannot exceed a
  fraction of the value created, and regulated fees pass through untouched.
- Every split conserves to the micro: noders + platform = net, always.
