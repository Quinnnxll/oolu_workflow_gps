# The agent roster — build-phase plan (News, Poll, Explorer, Travel)

Status: Proposed. Scope: growing the OoLu app from one assistant into a
**roster of agents listed below OoLu** — OoLu stays the general personal
assistant; beneath it appear **News** (member-contributed stories, selected
and composed to magazine standard, pushed by personal preference), **Poll**
(pairwise comparisons that are fun for the member and are *precise
preference-learning instruments* for the platform), **Explorer** (side-by-side
product comparison and best-buy briefs), and **Travel Plan** (Explorer's logic
extended with travel time, group availability, schedules, and budget). News
and Poll carry **affiliate advertising whose revenue is shared with the
content contributors**.

Companion reading: `docs/marketplace-build-plan.md` (the commerce rails and
the take-rate ledger), `docs/NODEPLACE_ROADMAP.md` and
`docs/REWARD_PRICING_DESIGN.md` (the verified-success reward pipeline this
plan reuses for ad revenue), `docs/adr/0005-data-dividend-model.md` (the
standing Proposed decision that contribution earns an ongoing, conserved
split — the contributor ad dividend is its first client),
`docs/personal-nodes-plan.md` (the pulse and the personal records this plan
schedules and plans against), `docs/THREAT_MODEL.md`.

---

## 0. One closed loop, four new faces

Everything these agents show is made **inside the app**. Members contribute
raw material — observations, reports, photos of the world they know, product
experiences, lab results, itineraries. The News agent edits that material
into stories; the Poll agent mines it for comparable pairs; Explorer joins it
with the marketplace's verified commerce data; Travel Plan adds the member's
own calendar and group. **No outside web search and no external content API
feeds any of it.** The loop is closed on purpose:

- **Provenance is possible.** Every rendered unit can carry the contributing
  account's profile photo and name because every unit traces to contribution
  records inside our own stores.
- **Payment is possible.** Ad revenue can be shared *fairly by contribution*
  only when contribution is measured on our own attribution rails.
- **Trust is possible.** Feedback, trust scores, and lab evidence mean
  something because they ride the platform's verified-only reputation
  machinery, not scraped claims.

Advertising money enters the loop; content does not leave it. The one
sentence that governs the economics, inherited from the Nodeplace:

> Verified events are the only thing that pays — and every split conserves
> to the micro.

Where the roster lives: the Life sidebar
(`desktop-app/frontend/src/components/Life.tsx`) already lists "who you can
talk to" — OoLu, Friends, Noder threads. The new agents are new entries in
that list, each a real conversation with its own thread, its own registered
model seat, and its own honest scope.

---

## 1. Non-negotiable invariants (hold in every phase)

A change that violates one of these is a release blocker.

1. **Closed-loop content.** Story, poll, comparison, and itinerary content is
   assembled only from records contributed and stored in the app. An import
   scan (the `tests/test_marketplace_spine.py` precedent) proves the press,
   poll, and explorer packages reach no web-search or external-content seam.
2. **Attribution is mandatory and immutable.** Every rendered content unit
   labels its source accounts — profile photo and display name — and resolves
   to durable contribution records with weights. Rendering an unattributed
   unit is a defect, not a style choice.
3. **Editorial selection is typed and explainable.** The magazine rubric
   (inspiring / critical / knowledgeable) is a versioned policy over typed
   signals; every published story records *why* it was selected. No
   self-declared quality anywhere — the `REWARD_PRICING_DESIGN.md` lesson.
4. **Ads are labeled, merged at render, never stored into the corpus.** An ad
   never masquerades as a story, poll, or comparison verdict. Removing a
   campaign removes every placement instantly, because placements are
   computed at render, not baked into stored content.
5. **Advertising can never buy a ranking.** Editorial selection, poll pair
   choice, Explorer scores, and best-buy verdicts take no input from any
   campaign. Sponsored slots are a separate, labeled surface, and the
   separation is import-scan-proven, not promised.
6. **Revenue conservation.** Each verified ad event's net splits
   `platform + Σ contributor` in exact integer micros — the existing
   `billing/pricing.py` `PricingEngine` conservation invariant with
   contributors in the noder seat. Money moves only through metering → split
   → earnings ledger → settlement; nothing in the new packages writes a
   ledger.
7. **Only verified delivery pays.** Impressions, clicks, and conversions
   become `MeteringEvent`s only after deterministic validity checks;
   `billing/fraud.py` `FraudSignals` (self-dealing, replay, velocity) extend
   to placements, so contributors farming their own content's ads earn
   nothing. Failure or fraud = no event = no earnings.
8. **Preference data is consented, scrubbed, and revocable.** Poll votes and
   reading signals are the member's; they enter learning stores only under
   the existing opt-in and scrubbing gates (`knowledge/scrubbing.py`,
   ADR-0005 §6), and revocation stops future use. Personal preference
   profiles never leave the account.
9. **Poll statistics are honest and anonymous.** Results render only real
   aggregates, only after the member votes, and only above a k-anonymity
   floor; below the floor the agent says "not enough votes yet" instead of
   leaking.
10. **Explorer verdicts are deterministic.** Same candidates, same verified
    evidence, same mode → same ranking, with the full factor breakdown
    exposed (the `ClearedPrice.notes` discipline from `nodeplace/market.py`).
11. **No real money on local-only infra.** Ad billing and contributor payouts
    require the production substrate (PostgreSQL durable adapter + real OIDC
    identity) — `require_production_money` and `assert_production_identity`
    extend to every ad money path.
12. **Reputation modifies weight; it never overrides consent, privacy, or
    the labeling laws** — the standing invariant, restated for content.
13. **The privacy promise is renegotiated in the open.** `legal.py`'s
    privacy template currently says, verbatim, "**No advertising, no sale of
    personal data**" — served publicly at `GET /v1/legal/privacy` and pinned
    by `tests/test_account_privacy.py`. Ads do not ship behind that text.
    They ship only after a versioned amendment of the legal template, an
    explicit re-consent flow, and a deliberate update of the pinning test.
    "No sale of personal data" survives the amendment untouched: matching
    uses consented signals inside the platform; advertisers never receive
    member data.

---

## 2. What already exists (the seams are cut)

Most boxes in this plan have a landed ancestor. The phases are mostly
generalizations, not inventions.

| Plan component | Standing machinery | Gap to close |
|---|---|---|
| The agent list | `Life.tsx` `Selection` union (lines 36–42) + sidebar + render guards; the committed shell bundle under `src/oolu/gateway/frontend/shell/` | Four new variants/buttons/guards; a small typed roster registry; rebuild + recommit the bundle |
| Metered model call-sites | `seats.py` `SEATS` registry (9 seats, `chat.turn` … `node.review`): purpose, charge, reads/writes, hands, consent key, audited | New seats `news.compose`, `poll.pair`, `explore.brief`, `travel.plan` |
| Per-agent conversations | `social.py` `AssistantHistoryStore` — one thread per account, `kind IN ("user","assistant","run")` | An `agent` column (default `oolu`) so each roster agent holds its own durable thread |
| Contributor byline | `identity/models.py:105` `display_name`; `settings_node.py` `account.display_name`; the generated avatar (`avatar.ts`, `OoLuAvatar.tsx`) | A real profile **photo**: content-addressed blob (`durable/artifacts.py`), upload door, settings surface; one byline component |
| Content storage | `durable/files.py` `UserFile` (tenant+owner-walled), content-addressed artifact store, hash-linked audit chain (`durable/audit.py`) | A **published**, author-attributed `ContentContribution` record with license, consent, and genre keys — today no "publish to other members" object exists at all |
| Publication gates | `knowledge/scrubbing.py` (`scrub`, `is_safe_to_store`); `nodeplace/plagiarism.py` `similarity`; `nodeplace/sanitize.py`, `safety.py` | Wire them in front of publication; near-duplicate credit; refusal messages |
| Provenance & lineage | `memoryspine.py` admission law (provenance or stay `proposed`); `episodes.py` ("an episode nobody can trace is a story, not a memory"); `nodeplace/provenance.py` commit chains | Story → contribution lineage records with weights — the attribution set ad revenue later splits over |
| Editorial ranking inputs | `retrieval.py` (`LexicalEmbedder`, the one shared scorer); `routelearning.py` rerankers; ratings gated on verified runs | The magazine rubric as a typed, versioned policy; per-story selection reasons |
| Preference signals | `representative/dataset.py` `preference_pairs` + `trainer/dpo.py` (a working chosen/rejected → DPO pipeline); `knowledge/traces.py` `TraceStore` (Thompson sampling); `values.py` | A **generic pairwise preference store** ("member chose A over B") — today preference pairs are representative-scoped only |
| The feedback door | `POST /v1/runs/{run_id}/feedback` exists but is a stub — `GatewayApp._feedback` writes one audit line and returns 202 | Make it a real preference write; every roster agent gets a signal channel almost free |
| Delivery / push | The reminder channel (agent speaks as its own message; `_fire_morning` precedent), `pulse.py` schedules with durable exactly-once claims, SSE/WS events | Edition delivery on a pulse schedule per member; true mobile/web push is out of scope for this plan (poll-based delivery holds at this scale) |
| Advertising | **Nothing.** Confirmed absent — no campaign, placement, impression, or affiliate machinery anywhere; two standing *prohibitions* (`legal.py` "No advertising…", `chat.py` "one offer per chore, not a campaign") | The whole `adhouse/` package — and the legal amendment gate (invariant 13) before any of it renders |
| Advertiser identity & funds | `marketplace/sellerkyc.py` (KYC-gated seller principals); `billing/doubleentry.py`; the PSP port | Campaign records; funding as a real charge; unspent budget as a liability account |
| Contributor revenue share | `metering/` (`NoderShare`, `RunBinding`, `AttributionRecord`, `MeteringEvent`); `billing/pricing.py` conservation; `billing/policy.py` knobs (ρ=0.30, H=14d, R=0.10, W=90d, T=$20); `SettlementService`, `DisputeService`, `FraudSignals`; ADR-0005 (`DataPool`, `DataContribution`, three-way split — Proposed, no code yet) | Content items as earning principals; the ad-event deriver; ADR-0005 landed with content lineage as its first client |
| Comparison engine | `marketplace/rfq.py` — normalized `QuoteRecord`s, eligibility with named gaps, "compares like with like"; `catalog.py` `Listing` versioning; `policy.py` `price_benchmark_micros` + discount ceiling; trust derived from the durable order book (`PurchaseFacts`) | Verified-buyer product reviews; a browsable trust score; lab-evidence attachments; a discount **fact** on listings; `ComparisonSet` + best-buy briefs |
| Purchases from a brief | The full commerce spine: typed intents, digest law, policy ladder, approvals, orders | Nothing — Explorer hands off to it unchanged |
| Scheduling | `pulse.py` (`Schedule` with `tz_offset_minutes`, occurrence math, durable claims); `reminders.py`; the starter-shelf `calendar` node (P1: files what it is given) | A real calendar record model — **no `Event`/`TimeSlot` type exists anywhere**; group free-busy intersection |
| Inter-agent coordination | `handoffs.py` — typed batons, "agents cooperate through the STACK, never through each other's transcripts" | Travel ↔ Explorer ↔ Poll handoffs |
| Budgets | `nodeplace/budget.py` (hard cap, review threshold, learned comfort ceiling); settings keys `budget.*` | A trip budget envelope reusing the same ceilings |

---

## 3. Architecture additions

New packages `src/oolu/press/`, `src/oolu/adhouse/`, `src/oolu/explorer/`,
plus a small roster registry and targeted extensions. Everything behind the
same durable ports (SQLite local / PostgreSQL production).

```
src/oolu/roster.py           # AgentCard registry: id, name, seat prefix,
                             # thread kind, scope line — the list below OoLu

src/oolu/press/
├── contributions.py         # ContentContribution: frozen, author-attributed,
│                            # licensed, consented, genre-keyed, media by
│                            # sha256 ref; publish/supersede; scrub+screen gate
├── standards.py             # the magazine rubric: typed, versioned editorial
│                            # policy — inspiring/critical/knowledgeable scores
│                            # over typed signals, selection reasons recorded
├── newsroom.py              # composition: contribution set → Story with
│                            # lineage weights (the news.compose seat)
├── editions.py              # per-member ranked edition; pulse-scheduled
│                            # delivery as the News agent's own messages
└── polls.py                 # PollPair mining, idempotent votes, k-anonymous
                             # aggregates, genre switch (the poll.pair seat)

src/oolu/adhouse/
├── campaigns.py             # advertiser (seller-KYC principal) funds a
│                            # Campaign: budget micros, flight, taxonomy
│                            # targeting, creative, affiliate offer ref
├── matching.py              # deterministic, explainable second-price
│                            # placement auction; consent-aware; capped
├── placement.py             # merge-at-render, the labeling law, frequency
│                            # caps, placement provenance token
└── delivery.py              # verified impression/click/conversion events
                             # (viewability, dedupe, velocity) → metering

src/oolu/explorer/
├── evidence.py              # verified-buyer reviews, browsable trust score,
│                            # lab reports (content-addressed, attributed)
├── comparisons.py           # ComparisonSet: normalized attribute matrix over
│                            # listings and RFQ rows
├── bestbuy.py               # deterministic mode-weighted scoring →
│                            # BestBuyBrief with factor breakdown
└── travel.py                # TravelPlan: constraints (window, group
                             # free-busy, schedule, budget) → itineraries

src/oolu/records/calendar.py # Event/TimeSlot records + free-busy reads —
                             # fulfills the starter-shelf calendar node's
                             # promise; Travel's prerequisite

billing/ + metering/ (extensions, per ADR-0005)
                             # content items / pools as earning principals;
                             # the ad-event deriver; the extended conserved
                             # split — no new payments rail
```

The flow, end to end:

```
member publishes ─► contributions.py (scrub ─► screen ─► license ─► lineage byline)
newsroom.py: rubric selects ─► Story (lineage weights recorded)  ──┐
polls.py: comparable pair ─► vote ─► k-anon stats ─► preference event ─┤
                                                                   ▼
editions.py: rank by consented preference ─► pulse ─► News/Poll thread
                                                                   │
adhouse: campaign ─► match (deterministic) ─► placement AT RENDER ─┘
placement viewed/clicked/converted ─► delivery.py verified event
    ─► metering ─► PricingEngine split (platform + Σ contributors, conserved)
    ─► earnings ledger ─► settlement (H, R, T; disputes claw back)

explorer: evidence + catalog ─► ComparisonSet ─► BestBuyBrief ─► buy?
    ─► the standing commerce spine (intent ─► digest ─► approval ─► order)
travel: calendar/free-busy + budget + group poll ─► itinerary ─► bookable legs
    ─► the same spine; agents coordinate via handoffs.py batons
```

---

## 4. The ad economy: buying, matching, merging, sharing

Four concerns, deliberately separated:

**Buying (demand).** An advertiser — a KYC-verified seller principal, the
`marketplace/sellerkyc.py` gate reused — funds a **Campaign**: budget in
micros, flight window, targeting (genre/topic keys from the same taxonomy
that classifies contributions), creative, and the affiliate offer it points
at (an existing marketplace offer/listing, so the digest law and the
take-rate posting already govern the commerce behind the click). Campaign
funding is a real charge on the standing billing rails; the unspent balance
is a **liability** on the double-entry book, not revenue.

**Matching.** A deterministic matcher scores eligible campaigns against a
content unit and a viewer: topical fit (content taxonomy × campaign
targeting), viewer-consented preference fit, frequency caps, and bid — and
*nothing from the editorial side flows back* (invariant 5). Matching is
explainable: each placement records its factor breakdown, the
`ClearedPrice.notes` discipline. Second-price clearing keeps bids honest;
the persisted `PriceBook` damping pattern keeps rates from shocking.

**Merging.** Placement happens at render time: the story or poll card and
the ad slot compose in the surface layer, the ad visibly labeled, the
affiliate link carrying the placement's provenance token. The stored corpus
never contains ad copy (invariant 4). Placements are capped per edition and
per session — the magazine rule: ads live *between* the stories, never
inside the prose.

**Sharing (the contributor dividend).** A verified delivery event —
impression (viewability-checked), click (deduplicated, velocity-gated), or
conversion (the affiliate order reached a paid state on our own order
machine) — derives one immutable `MeteringEvent` keyed by the placement's
idempotency key. The split rides the existing pipeline with contributors in
the noder seat:

```
net_micros == platform_micros + Σ contributor_micros      (exact, integer)
```

- The contributor pool for a placement is the **attribution set of the
  content it ran against**: the story's cited contributions with the weights
  recorded at composition time, or the poll pair's object contributors.
  "Fairly weighed for contribution" is thus a recorded fact, not an
  editorial mood — the same `w_i × μ_i` normalization `billing/pricing.py`
  already conserves, with bounded multipliers from verified engagement and
  reputation exactly as `nodeplace/rewards.py` bounds today ("multipliers
  redistribute, never inflate").
- This is ADR-0005 made real, with content lineage as the first client:
  contributions are the `DataContribution` analogue, individual contributors
  are the first earning principals, and cooperatives (`DataPool`) follow
  once individual attribution is proven.
- Settlement holdback `H`, reserve `R`, minimum payout `T`, dispute clawback
  via compensating entries, and fraud exclusion are the standing
  `SettlementService` / `DisputeService` / `FraudSignals` machinery — ad
  earnings are just another source flowing into the same per-account
  earnings ledger and the same `/v1/earnings` surface.
- Conversions additionally post the normal marketplace take on the purchase
  itself. Two postings, one book, no double counting — the
  `marketplace-build-plan.md` §4 rule.

The platform's ad commission (`α` on ad net), second-price floors, and the
impression/click/conversion price weights are decision-log items (§8) — the
mechanism is fixed here, the numbers are not.

---

## 5. Build phases

Each phase is a milestone with an exit gate and a binary Goal Adherence
checklist. Do not advance a phase until every box in its checklist is true.
Real money anywhere requires the production substrate (PostgreSQL durable
adapter + JWKS OIDC) — the standing Nodeplace P0 prerequisite.

### A0 — the roster and the byline (no content, no money)

**Status: LANDED** — `roster.py`: the four `AgentCard`s and the lean
roster turn (the card IS the boundary — a model-less or model-down host
answers from the card, never dies). `seats.py`: the `news.compose`,
`poll.pair`, `explore.brief`, `travel.plan` seats, so every roster model
call is metered per (tenant, purpose) through the standing
`_tenant_model` discipline. `social.py`: the `agent` column on
`assistant_turns` (in-place migration, old rows stay OoLu's; the
messenger cap falls per thread), and `ProfilePhotoStore` — the byline's
face, published to the account's own tenant, erased with the account.
Doors: `GET /v1/roster`, `/v1/chat` carrying `{"agent": ...}` into the
agent's own thread, `/v1/chat/history?agent=`, and the profile trio
(`GET /v1/profiles/{username}`, `GET .../photo`, `POST|DELETE
/v1/profile/photo`). Surface: the roster renders below OoLu in the Life
sidebar; each agent opens its own `AgentThread` (words only — no runs,
no tools); the `Byline` component and the Settings photo picker preview
the byline exactly as peers see it; the shell bundle is rebuilt and
committed. Pinned by `tests/test_roster.py` (registry/seat coverage,
per-thread isolation and caps, pre-roster migration, photo flow
end-to-end, model-less roster turns) and the roster case in
`Life.test.tsx`. Account export gained the all-threads read
(`agent=None`) and the photo; erasure removes both.

Goal: four agents listed below OoLu, each a real, separately-threaded,
seat-metered conversation; contributor identity renderable everywhere as
photo + name.

Deliverables:
- `roster.py` — the typed `AgentCard` registry (id, name, seat prefix,
  thread kind, one-line scope); `Life.tsx` gains an "Agents" sidebar section
  below OoLu: four `Selection` variants, four buttons, four render guards;
  the committed shell bundle rebuilt (`npm run build:shell`).
- `seats.py` — `news.compose`, `poll.pair`, `explore.brief`, `travel.plan`
  seats with purpose, charge, reads/writes, hands, consent key, audited —
  so every roster model call is metered and consent-gated like the nine
  existing seats.
- `social.py` — an `agent` column on `assistant_turns` (migration, default
  `"oolu"`), so each agent holds its own durable cross-device thread and
  existing history is untouched.
- The profile photo: `account.photo` as a content-addressed blob
  (`durable/artifacts.py`), an upload door, the settings surface entry; the
  generated `OoLuAvatar` blob stays as the fallback for accounts without
  one. One shared byline component (photo + `display_name`) used by every
  later surface.
- Each agent answers within its honest scope from day one (Explorer can
  already read the commerce catalog; News/Poll say what they will become).

Goal Adherence:
- [ ] The roster renders below OoLu; each agent opens its own thread and
      turns are tagged by agent; the existing OoLu thread is intact after
      migration.
- [ ] Every roster model call goes through its registered seat — audited,
      metered, consent-gated (no direct provider calls; grep-proven).
- [ ] An account can set a photo; the byline component renders photo + name;
      accounts without a photo fall back to the generated avatar.
- [ ] Shell bundle rebuilt and committed; a `Life.test.tsx` pin covers the
      roster the way `Market.test.tsx` pins the market surface.

### A1 — the contribution spine (publish, attribute, weigh)

**Status: LANDED** — `src/oolu/press/`: the taxonomy as typed, versioned
records (`taxonomy.py`, twelve genres including the commerce-adjacent
`products` and `results` that seed Explorer's evidence), and
`contributions.py` — the frozen `ContentContribution`, the stated
license as a named, versioned record (`oolu-members-1`, terms and all),
and `PressDesk.publish` walking the gate in order: consent + license →
words and bounds → taxonomy → the scrub gate (refusals name each leak —
"an email address, an API key" — and the platform never rewrites the
author's words) → the neighbor check (`plagiarism.similarity` over the
tenant's live pieces; a near-duplicate is FLAGGED with the original
credited, never refused). Media rides as drawer refs (`sha256:` blob
addresses), never copies. Unpublishing supersedes — a WHERE clause, the
memoryspine law — and both acts land on the tamper-evident audit chain.
Doors: `/v1/press/genres` (taxonomy + licenses, so consent renders the
server's own terms), `GET/POST /v1/press/contributions`, detail,
`/unpublish`, and per-attachment media streaming (the publication is the
consent that crosses the drawer wall; a deleted file is honestly gone).
Surface: the press panel at the head of the News thread — the shelf with
bylines, genre chips speaking the taxonomy's labels, the retelling
credit, the contribute form with the license terms inline, and unpublish
on your own pieces only. Pinned by `tests/test_press.py` (every gate, the
supersession law, the tenant wall, the audit-chain verify, and the
import scan proving `press/` reaches no outside-content seam) and
`Agents.test.tsx`.

Goal: members can publish content into the app under a stated license;
every contribution is scrubbed, screened, genre-keyed, author-attributed,
durable, and revocable-forward.

Deliverables:
- `press/contributions.py` — `ContentContribution` (frozen): author
  principal, tenant, genre/topic keys (one taxonomy, shared later with
  campaign targeting), text and media by `sha256:` refs, license, consent
  flags, timestamps. Publication walks the gate: `knowledge/scrubbing.py`
  scrub → safety screen → `nodeplace/plagiarism.py` near-duplicate check
  (duplicates flagged with the original credited) → provenance row on the
  audit chain.
- The genre taxonomy as typed, versioned records (the `class:`/`market:`
  listing-tag precedent from `nodeplace/economics.py`).
- Doors: `GET/POST /v1/press/contributions`, contribution detail, unpublish.
  Unpublishing supersedes (the `memoryspine.py` WHERE-clause law) — future
  compositions exclude it; history is never erased.
- The contribution card renders the byline; the Life "News" thread offers
  publish from the file drawer (`durable/files.py` refs, never copies).

Goal Adherence:
- [ ] Publishing without consent + license is refused; scrub failures refuse
      loudly with directions, not silently drop.
- [ ] Every stored contribution resolves to its author and renders the
      byline (photo + name).
- [ ] Near-duplicates are flagged and credit the original.
- [ ] Unpublish excludes from all future selection without erasing history;
      the audit chain verifies end to end.
- [ ] Import scan: `press/` reaches no web-search or external-content seam.

### A2 — the newsroom (stories to magazine standard, pushed by preference)

**Status: LANDED** — `press/standards.py`: the rubric as a versioned
policy (RUBRIC_VERSION 1) over typed signals — novelty and recency
(inspiring), independent-author corroboration on CONTENT-token agreement
(critical; stopword Jaccard was tried and rejected — strangers must not
"agree" on the word *the*), depth and attached evidence (knowledgeable) —
with every selection recording its full factor breakdown.
`press/newsroom.py`: `Story` with **lineage weights recorded at
composition time** in a queryable `press_story_lineage` table (anchor
0.6, corroborators and near-dup credits splitting the rest, summing to
exactly 1.0 — the set A5 pays); provenance-mandatory storage (a story
with no lineage refuses to store); composition through the
`news.compose` seat under a hard no-invention frame, with a
contract-checked parse and a verbatim desk fallback — degraded is
honest, invented is a defect; idempotent runs (told pieces never
retell). `press/editions.py`: the `PreferenceStore` (unary like/read/
skip signals, bounded per-genre affinity, erasable), `rank_edition`
(neutral rubric order; bounded affinity pull under consent; the
serendipity slice holding the last slot against a strong leaning), and
the `EDITION_PULSE_GOAL` sentinel. Consent: the `press.personalize`
setting, **off by default** — a learning-off member gets the neutral
edition and their taps answer `recorded: false` honestly. Doors:
`GET /v1/press/stories` (the edition, `personalized` named),
story detail (reasons on demand), story feedback, the newsroom crank
(`POST /v1/press/newsroom/run`, audited per story), and the
morning-edition schedule door riding the pulse; `_fire_edition` lands
the edition as News's own thread message with the skipped count named,
plus a reminder ping. Preference rows ride account erasure. Pinned by
`tests/test_newsroom.py` (rubric independence, lineage conservation,
provenance law, model contract + fallback, two-members-two-orderings,
the serendipity property, the pulse fire end-to-end) and the story
cases in `Agents.test.tsx`.

**Amended (A2.1)** — three decisions revise the landed surface without
touching the rubric or the lineage law. (1) *The scoring is the house's
own*: the factor breakdown stays recorded and versioned but no longer
rides the member-facing wire — the "reasons on demand" panel is gone;
the order speaks for itself. (2) *Semantic taste joins genre affinity*
(`press/personalize.py`): under the same one consent switch, the
edition is additionally bent by the member's own semantics — the words
of stories they tapped (👍 attracts likes-alike and stores the story's
words next to the signal; ⏭ repels) and the member's OWN turns in
their OoLu and News threads (attraction only — talking about a subject
is interest, never a skip). Similarity is the one `retrieval.py`
scorer; the bend is bounded (`SEMANTIC_PULL`), composes with the genre
pull in the open, and the serendipity slice still holds the last slot.
Taste rows ride the same erasure as every preference signal. (3)
*Multimedia rides the contribution into the story*: upload straight
from the device (blob door first, inline fallback), the media-type
table grown (webp/webm/quicktime/m4a/ogg/wav), and a story's payload
names its lineage's attachments by reference — served through the
press media door with true bytes (inline data-URL rows decoded),
honestly absent once the author unpublishes.

**Amended (A2.2)** — the contribute form is gone: the conversation IS
the door (`press/intake.py`). A member drops whatever they have into
the News thread — article-shaped text, a photo, a clip, a song (📎 on
the composer; attachments ride the chat message as drawer refs through
the same wall the publish door keeps) — and the DESK detects it, the
way OoLu spots a buildable chore (the n8n growth-trigger law applied
to publishing). The desk reviews in conversation: one gathering
question when the piece is thin (what happened, where, when), a loud
named refusal when the text would leak (dropped — fixed words arrive
fresh, never folded onto leaky ones), the retelling credit named up
front — then a standing publish offer that RENDERS the license terms.
The member's plain "yes" on the very next message publishes through
the same `PressDesk.publish` gate and the same audit voice as the
manual door (which remains, API-only); a plain "no" drops it; anything
else withdraws the offer — consent detached from the question it
answered is not consent. Whether the piece is worth composing stays
the RUBRIC's decision: the newsroom runs after publication and the
desk reports honestly which way it went. Detection, titles, genres,
and every desk sentence are deterministic functions of the material;
the seat's model keeps the ordinary News conversation. One standing
draft per member, durable (`press_intake`), spent atomically. Pinned
by `tests/test_press_intake.py` end to end.

Goal: News selects and composes stories from contributions only, to a
typed editorial rubric, and delivers each member a personal edition.

Deliverables:
- `press/standards.py` — the rubric as a versioned policy over typed
  signals: novelty against the corpus (`retrieval.py` similarity),
  corroboration (independent contributions agreeing), source diversity,
  verified engagement, recency. The three magazine virtues are named
  scores — *inspiring* (resonance signals), *critical* (corroboration floor
  and counter-source presence), *knowledgeable* (depth/coverage) — and every
  selection records its factor breakdown and policy version.
- `press/newsroom.py` — composition on the `news.compose` seat: a selected
  contribution set becomes a `Story` with **lineage weights recorded at
  composition time** (the attribution set A5 pays). Story cards carry the
  headline, the prose, and the byline of every cited contributor.
- `press/editions.py` — the per-member edition: ranking blends rubric score
  with consented preference affinity; learning-off members get the neutral
  edition. Delivery rides the reminder-channel pattern (`_fire_morning`
  precedent) on a per-member pulse schedule ("morning edition"), arriving
  as the News agent's own messages; a serendipity slice is reserved in
  every edition (the bandit-exploration house pattern) so preference
  ranking never collapses into an echo chamber.
- Wire the dormant feedback door: `GatewayApp._feedback` becomes a real
  preference write (read / skip / like per story), landing in the generic
  preference store A3 formalizes.

Goal Adherence:
- [ ] A story with no resolvable contribution lineage cannot publish —
      provenance-mandatory, the `episodes.py` law.
- [ ] Every published story records why it was selected (factor breakdown +
      rubric version); the reasons render on demand.
- [ ] Two members with different consented preferences receive different
      edition orderings; a learning-off member receives the neutral edition.
- [ ] Editions arrive on schedule as News's own thread messages; a missed
      window catches up once and names the skipped count (pulse discipline).
- [ ] The serendipity slice is present in every edition (property test).
- [ ] Import scan holds.

### A3 — the poll floor (fun on the surface, instruments underneath)

**Status: LANDED** — `press/polls.py`: comparable-pair mining on the
content-token band (same genre, distinct authors, agreement in the
comparable band, near-duplicates excluded — one piece twice is no
comparison), never re-minting a standing pair; `PollPair` with both
contributors' bylines; one durable idempotent vote per member per pair
(a replay changes nothing; a change of heart is a 409 — the first vote
is the vote). The honesty laws in `PollDesk`: nothing reveals before
your own vote, and below `K_FLOOR` the answer is "not enough votes yet"
with no counts — not even a total — property-tested at the exact
boundary. Genre switch is the query parameter; no genre = a seedable
Thompson draw over per-genre served/voted engagement. The **generic
pairwise preference store** (`press_pairwise`) landed: consented votes
write `{prompt, chosen, rejected}` in the DPO trainer's exact shape,
exported per-member (scrubbed once more on the way out, degenerate rows
dropped) at `GET /v1/press/preferences/export`; the same consented vote
records the engagement signal that measurably reorders that member's
OWN edition (end-to-end test through the preference store). The
aggregate always counts the vote; only the learning is consent-gated,
and votes + pairs ride account erasure. Doors: `/v1/press/polls/next`,
`/{id}/vote`, `/{id}/stats`, the export. Surface: the poll panel
heading the Poll thread — genre chips as the stream switch, two
byline-carrying sides, tap to vote, the server's reveal words verbatim.
Pinned by `tests/test_polls.py` and the poll cases in `Agents.test.tsx`.

Goal: pairwise polls mined from the same corpus; vote → real aggregate;
manual genre switch; every vote a typed, consented preference event that
teaches both the editions and the models.

Deliverables:
- `press/polls.py` — comparable-pair mining: two objects from the same
  genre/class key within a similarity band (`retrieval.py`), both rendered
  with their contributors' bylines. `PollPair`, one idempotent vote per
  member per pair, durable.
- The honesty laws: aggregates render only after the member votes and only
  above a k-anonymity floor — below it, "not enough votes yet." Genre
  switch changes the pair stream immediately; genre scheduling explores via
  Thompson sampling (`TraceStore` pattern) so fun genres surface and stale
  ones retire.
- The **generic pairwise preference store** — the "member chose A over B"
  table the codebase lacks: written by polls and by A2's feedback door,
  read by edition ranking, and exported (consent-gated, scrubbed) in the
  `preference_pairs` shape `representative/trainer/dpo.py` already trains
  on. Poll is thereby *specific for reinforcement learning* by
  construction: every pair is a labeled preference in the exact format the
  standing DPO pipeline consumes.
- Doors: `GET /v1/press/polls/next`, `POST /v1/press/polls/{id}/vote`,
  `GET /v1/press/polls/stats`, genre switch.

Goal Adherence:
- [ ] Results are invisible until the member votes; below-floor aggregates
      refuse to render (property test at the floor boundary).
- [ ] A vote is one durable idempotent preference event; replaying the
      request changes nothing.
- [ ] Revoking learning consent stops future use of a member's events; the
      export path passes scrubbing.
- [ ] Seeded votes measurably reorder that member's edition (end-to-end
      test through the preference store).
- [ ] Exported pairs validate against the DPO dataset shape.
- [ ] Both poll objects render their contributor bylines.

### A4 — the ad house (buying, matching, merging — display-only money)

**Status: LANDED** — **the legal gate first**: `legal.py` carries
privacy VERSION 2 (`LEGAL_VERSIONS`), amending §2 and adding §7 —
sponsored placements on News/Poll only, always labeled, matched inside
the platform, revenue shared with contributors, and "no sale of
personal data" untouched; `LegalAcceptanceStore` records explicit,
version-named acceptance (monotone; a legal-basis record retained like
the audit chain); the pinning test (`test_account_privacy.py`) was
updated deliberately in the same commit, and decision-log item 2 is
resolved conservatively: ads are DEFAULT-OFF for every account until it
accepts v2 — declining costs nothing but the ads. `src/oolu/adhouse/`:
`campaigns.py` (seller-KYC-gated at the door; scrub-gated creative;
taxonomy targeting on the one genre vocabulary; display-only
`charged_micros` recognition), `matching.py` (a pure deterministic
second-price pick — same inputs, same placement, same factor breakdown;
bounded consented-affinity pull; paused/spent/frequency-capped
campaigns never place), `placement.py` (placements are computed served
occurrences, never baked into content — deactivation removes every
placement on the next render; `SPONSORED_LABEL` is structural; the
placement_id is the provenance token; per-viewer day cap), and
`delivery.py` (impression/click behind dedupe, click-needs-impression,
viewer-binding, and velocity gates; `forecast_split` conserving
`platform + Σ contributors == price` to the micro — the A5 dry run —
and `preview_earnings` labeled `forecast: true`, never a balance).
Funding is a ledger fact posted BY THE GATEWAY on the double-entry
book: two new accounts (`advertiser_payable`, `ad_budget_liability`),
cash against standing liability, trial-balanced in the end-to-end test;
the adhouse package itself imports no money path and `press/` imports
no adhouse — both walls import-scan-proven. Doors: `/v1/legal/consent`
(GET/POST), `/v1/adhouse/campaigns` (+status), `/v1/press/ads` (the
render-time merge with the consent gate first), placement
impression/click, `/v1/adhouse/preview`. Surface: the `AdSlot` between
the stories and after the poll pair — consent card until acceptance,
then the labeled sponsored card with impression/click delivery. Pinned
by `tests/test_adhouse.py` and the ad cases in `Agents.test.tsx`.

Goal: campaigns fund, match, and render against News and Poll — behind the
renegotiated legal seam, always labeled, with earnings previews only.

Deliverables:
- **The legal gate, first.** The `legal.py` privacy template amended as a
  versioned policy change: ads on News/Poll surfaces only, matched on
  consented signals inside the platform, revenue shared with contributors,
  **no sale of personal data** (unchanged). Re-consent flow for existing
  accounts; `tests/test_account_privacy.py` updated deliberately, in the
  same commit, as the record of the renegotiation. No adhouse surface
  renders before this lands.
- `adhouse/campaigns.py` — advertiser = seller-KYC principal; funding is a
  real charge on the billing rails; unspent budget is a liability account
  on `billing/doubleentry.py`; flight windows, taxonomy targeting,
  creative, affiliate offer ref (a live marketplace offer, so the click
  lands on digest-governed commerce).
- `adhouse/matching.py` — the deterministic second-price auction over
  eligible campaigns: taxonomy fit × consented preference fit × caps ×
  bid; full factor breakdown per placement; personalization-off members
  get contextual-only (taxonomy) matching or nothing.
- `adhouse/placement.py` — merge at render into edition and poll cards;
  the label law; per-edition and per-session caps; the placement
  provenance token on the affiliate link.
- `adhouse/delivery.py` — typed delivery events with validity checks
  (impression viewability, click dedupe, velocity gates), metered
  **display-only**: earnings previews on the contributor's desk, no ledger
  writes (the Nodeplace P1 "display-only money" precedent).

Goal Adherence:
- [ ] The amended legal text is served and versioned; existing accounts see
      the re-consent flow; the pinning test asserts the new promise.
- [ ] No placement renders unlabeled; deactivating a campaign removes every
      placement on the next render.
- [ ] `press/` and `explorer/` import nothing from `adhouse/`
      (import scan — invariant 5 as code).
- [ ] Matching is reproducible: same campaigns, consent state, and content
      → same placement and breakdown.
- [ ] Budget accounting balances: charges, liability, and spend reconcile
      on the double-entry book.
- [ ] No ledger-writing code path exists in `adhouse/` (import scan on
      money paths); previews are labeled forecasts (`QuoteEngine`
      discipline: "a quote is a forecast, not a ledger entry").

### A5 — the contributor dividend (real money, conserved)

**Status: LANDED** — `billing/addividend.py`: `AdDividendService`,
ADR-0005's first client with individual contributors as earning
principals (pools follow once individual attribution is proven). A
verified impression settles ONCE through the standing pipeline: the
same `PricingEngine` (contributors in the noder seat, α in the rho
seat — conservation structural), the same `EarningsLedger` with
holdback `H`, so the standing settlement (`R`, `T`, `W`) and the
standing `DisputeService` clawback (reserve-first, shortfall as debt —
tested on an ad event end-to-end) work UNCHANGED; an ad event is just
`ad:<placement_id>`. Fraud before money: the standing self-dealing
exclusion hardened to the ad law (a viewer who is the ONLY contributor
settles nothing at all — `no_arms_length_share`), replay rejection via
the idempotency ledger, velocity already gated at delivery.
Multipliers redistribute, never inflate: bounded engagement (a
verified click) and injected reputation μ, clamped to `mu_max`,
normalized in the pool. Recognition is a balanced posting on the ONE
book: `ad_budget_liability` shrinks by the net, α lands in
`marketplace_fee_revenue`, the pool in the new `contributor_payable`
account — the A4 funding, the A5 recognition, and an ordinary order
fee trial-balance to zero with no double counting (the §4 rule as a
test). `require_production_money` guards every settle: the service
refuses local infra, and the gateway's `POST /v1/adhouse/settle` door
relays the refusal as a named 403, never a silent skip (proven after a
full real A4 flow). Earnings surface: `/v1/earnings/entries` labels
every row by source (`ads` / `nodeplace`); balances ride the standing
holdback/threshold maths; payout stays behind the standing KYC-verified
payout account. Wired in the production assembly over the shared
ledger and book. Pinned by `tests/test_ad_dividend.py` (conservation
property across random prices, replay, both self-dealing shapes,
multiplier clamp and redistribution, the local-infra guard at service
and door, the one-book balance, the standing clawback, and the source
labels).

Goal: verified ad events pay contributors through the standing pipeline —
ADR-0005 lands, with content lineage as its first client.

Deliverables:
- The ADR-0005 build: contributions/contributors as earning principals
  (individuals first; `DataPool` cooperatives after individual attribution
  is proven); lineage records written only from verified signals.
- The ad-event deriver: verified delivery event → `MeteringEvent`
  (idempotency key = placement occurrence) → `PricingEngine` split with
  contributors in the noder seat — weights are the story/poll lineage
  weights × bounded multipliers (verified engagement, reputation μ,
  `mu_max` clamp), platform commission `α` per the decision log —
  conservation property-tested exactly as `tests/test_rewards.py` does.
- Settlement reuse end to end: holdback `H`, reserve `R`, threshold `T`,
  risk window `W` from `billing/policy.py`; disputes claw back reserve-first
  via compensating entries; `FraudSignals` extended with self-placement
  exclusion (a contributor's own views/clicks of ads on their content never
  meter) and coordinated-engagement velocity checks.
- Conversions post the affiliate order's normal marketplace take
  separately — two postings, one book.
- Contributor earnings appear on the standing `/v1/earnings` surface,
  labeled by source; payout requires the standing KYC-verified payout
  account.

Goal Adherence:
- [ ] `net == platform + Σ contributor` to the micro, property-tested
      across impression, click, and conversion events.
- [ ] Self-dealing produces no event; replays double-count nothing;
      velocity abuse throttles (fraud tests extended).
- [ ] An advertiser dispute claws back contributor shares reserve-first via
      compensating entries; shortfalls become debt future accruals repay.
- [ ] Earnings render with source labels; below-threshold balances roll
      forward exactly as node earnings do.
- [ ] No ad money moves on local-only infra
      (`require_production_money` on every path).
- [ ] Conversion events never double-count with the marketplace take
      (one-book test, the §4 rule).

### A6 — the explorer desk (compare, then the best buy)

**Status: LANDED** — `src/oolu/explorer/`: `evidence.py` with the three
missing evidence types: **verified-buyer reviews** (`ReviewDesk` — a
review needs a finished order (`accepted`/`completed`) by its OWN buyer
for THAT listing, self-reviews are refused by the standing self-dealing
law, one voice per member per product), the **browsable trust score**
(`trust_from_book` — the PurchaseFacts derivations made a persistent
explainable read model: finished/refunded/disputed from the durable
order book, trouble weighing double, no history sitting at the honest
neutral), and **lab evidence** (`LabReport` — a LIVE press contribution
in the `results` genre, attached only by its own author, so byline,
provenance, and dividend-eligibility are structural; broader
certification is decision-log item 6). The catalog gained
`list_price_micros` so the **discount is a fact or nothing** — no
policy ceiling ever renders as a "was" price (`discount_fact`).
`comparisons.py`: the RFQ discipline generalized — one normalized
matrix, ineligible rows kept with named gaps. `bestbuy.py`: four modes
(`value`/`balanced`/`proven`/`measured`) as declared weight sets over
five factors, absent evidence at the neutral 0.5, deterministic ranking
with ties on listing_id, every ranked row carrying its full factor
breakdown AND the stance that weighed it. Followed interests ride the
pulse (`explorer.brief:<category>:<mode>` sentinel) and land as
Explorer's own thread messages. Buying is a handoff: the end-to-end
test walks the brief's winner through the STANDING spine — listing
offer → intent → policy verdict — unchanged. The walls hold
structurally: explorer imports no adhouse (the A4 scan covers the
package the moment it exists) and no outside-content seam. Doors:
`/v1/explorer/compare`, `/v1/explorer/reviews` (GET/POST),
`/v1/explorer/lab` (GET/POST), `/v1/explorer/interests`. Surface: the
Explorer panel — mode chips, category, the winner card with reasons on
demand, the matrix rows with evidence summaries and the discount fact,
the follow toggle. Pinned by `tests/test_explorer.py` and the explorer
case in `Agents.test.tsx`.

Goal: side-by-side comparisons over verified in-app data — price, discount,
customer feedback, trust, lab performance — and deterministic best-buy
briefs for followed interests.

Deliverables:
- `explorer/evidence.py` — the three missing evidence types:
  **verified-buyer reviews** (rating requires a finished order on
  `marketplace/orders.py` — the `nodeplace/ratings.py` verified-run gate
  applied to commerce; self-reviews refused by the standing self-dealing
  derivation); a **browsable trust score** (the `PurchaseFacts` order-book
  derivations — spend history, dispute rate, fulfillment record — made a
  persistent, explainable read model per seller/listing); **lab evidence**
  (test reports as content-addressed, provenance-required, contributor-
  attributed records — contributions in their own right, byline-labeled
  and dividend-eligible; who may certify is a decision-log item riding the
  `nodeplace/accounts.py` authority-level precedent).
- Catalog extension: `list_price_micros` so discount is a displayable
  **fact** (current vs list), not only the policy ceiling.
- `explorer/comparisons.py` — `ComparisonSet`: candidates from catalog
  search or an RFQ round, normalized into one attribute matrix (the
  `rfq.py` "compare like with like" engine generalized; ineligible rows
  keep their named gaps).
- `explorer/bestbuy.py` — deterministic mode-weighted scoring (price,
  discount, feedback μ, trust, lab performance; modes as one enum, the
  `QuoteMode` pattern) → `BestBuyBrief` with the full factor breakdown.
  Followed interests get pulse-scheduled briefs in Explorer's thread.
- Buying hands off to the standing commerce spine unchanged: intent →
  digest → policy ladder → approval → order.

Goal Adherence:
- [ ] Only verified-buyer feedback enters scores; a review without a
      finished order is refused; self-reviews are refused.
- [ ] Same candidates + evidence + mode → same ranking; every brief exposes
      its breakdown.
- [ ] Sponsored slots, if present on the Explorer surface, are labeled and
      provably absent from ranking inputs (import scan).
- [ ] A best-buy purchase walks the digest-bound approval path unchanged.
- [ ] Lab evidence renders its contributor byline and resolves its
      provenance.
- [ ] Discounts render only where a real list-price fact exists — no
      manufactured "was" prices.

### A7 — the travel desk (Explorer's logic plus time, people, and budget)

**Status: LANDED** — **the prerequisite first**: `src/oolu/records/`
closes the survey's hard wall with `calendar.py` — the codebase's first
`CalendarEvent`/interval types: durable, tenant-scoped, owner-walled
events; `busy_intervals` whose RETURN TYPE is the privacy wire shape
(merged (start, end) blocks — titles have no field to leak through);
`common_free` for the group's shared slots; and `FreeBusyGrants` —
sharing is a standing per-peer, revocable grant, and reading without
one refuses by name (silence is never treated as free time).
`explorer/travel.py`: `TravelConstraints` (window, nights, party,
budget envelope) and `plan_trip` — travel candidates from in-app supply
(the `travel` category; thin at first, and the module says so: v1 is a
planner over real constraints first), scored with the bestbuy factor
arithmetic, with **broken constraints as walls, not weights**: an
over-budget or no-common-window candidate lands in `infeasible` with
the violated constraint spelled out ("over budget by 80.00 USD for 2
travellers") and never outranks a feasible plan. Group choice rides A3
unchanged (destination pieces in the `travel` genre poll under the
standing honesty laws); composition is typed data end to end — the
import scan proves `explorer/` and `records/` touch no chat, social, or
assistant-history seam. Booking rides the standing spine unchanged (the
declined-approval/reservation-release discipline is the marketplace's
own pinned law); `POST /v1/travel/confirm` asks the durable order book
whether YOUR order stands finished and lands the trip as calendar
events (source `trip`) — a pending order books no calendar. Calendar
events and grants ride account export and erasure. Doors:
`/v1/records/calendar` (GET/POST/DELETE), `/v1/records/freebusy/grants`
(GET/POST), `/v1/travel/plan`, `/v1/travel/confirm`. Surface: the
Travel panel — the plan form, feasible plans ranked with reasons on
demand, infeasible ones with their named violations, and the member's
own calendar beneath. Deferred, named: multi-leg itineraries and
inter-leg travel time. Pinned by `tests/test_travel.py` (the record
model, the no-titles interval shape, grant semantics, the
constraints-as-walls property, brief determinism, consent-by-name at
the door, the SECRET-title privacy sweep over the whole brief, the
trip-on-calendar flow, and the no-transcript scan) and the travel case
in `Agents.test.tsx`.

Goal: itinerary comparison under real constraints — travel time, group
availability, schedules, and budget — with group decisions made through
Poll and bookings through the commerce spine.

Deliverables:
- **The calendar record model, first** (the survey's hard wall — no
  `Event`/`TimeSlot` type exists): `records/calendar.py` with timezone-aware
  events and free-busy reads, fulfilling the starter-shelf calendar node's
  P2 promise so OoLu and Travel share one calendar.
- Group availability: consented free-busy **intersection** across chosen
  friends (`FriendshipStore` peers) — sharing reveals busy/free intervals
  only, never event contents.
- `explorer/travel.py` — the `TravelPlan` constraint model: date window,
  party, budget envelope (riding `nodeplace/budget.py` ceilings), schedule
  conflicts, inter-leg travel time. Candidate itineraries compose from
  in-app supply — marketplace and federated-partner listings where
  bookable, contributed travel content for the knowledge layer — and score
  deterministically with the `bestbuy.py` engine plus the time/feasibility
  terms. Infeasible plans are marked with the named violated constraint,
  never silently ranked.
- Group choice through Poll: destination and itinerary pairs run as A3
  polls scoped to the group, inheriting the honesty laws.
- Coordination with Explorer and Poll via `handoffs.py` typed batons —
  never shared transcripts.
- Bookable legs purchase through the digest-bound approval path; the trip
  itself lands as calendar events on confirmation.

Goal Adherence:
- [ ] Free-busy sharing is opt-in per group and reveals busy/free only
      (privacy test on the wire shape).
- [ ] An itinerary violating a hard constraint is marked infeasible with
      the reason; it never outranks a feasible plan.
- [ ] Group polls follow A3's laws (vote-before-reveal, k-floor, one vote
      per member).
- [ ] Bookings walk the approval path; a declined approval cancels cleanly
      with reservations released (`inventory.py` discipline).
- [ ] Travel composes via typed handoffs; the import scan shows no
      transcript sharing.
- [ ] Confirmed trips appear on the member's calendar records.

---

## 6. Sequencing and the loop-closure rule

```
A0 roster/byline ─► A1 contributions ─► A2 newsroom ─► A3 polls ─► A4 ad house ─► A5 dividend
        └────────────────────────────► A6 explorer ──────────────────────────────► A7 travel
```

- **The spine is strictly ordered**: A0 → A1 → A2 → A3 → A4 → A5. Polls
  need the corpus; ads need two live surfaces to place against; money needs
  display-only previews proven first (the Nodeplace P1 → P2 precedent).
- **A6 runs as a parallel track** after A0 — Explorer's data comes from the
  marketplace, not the press corpus. Its evidence records (reviews, lab
  reports) become dividend-eligible only once A5 exists; nothing in A6
  waits for it.
- **A7 is last** and additionally gated on the calendar record model — the
  one genuinely green-field prerequisite this plan has.
- **The loop-closure rule** (from `personal-nodes-plan.md`): a phase is not
  done when its code lands; it is done when its Goal Adherence checklist is
  fully true and the surface proves it — a member can *do the thing* end to
  end. Do not open the next phase on a partially-true checklist.
- **Deliberate deferrals**, named so their absence is a decision and not an
  oversight: real mobile/web push (editions ride the standing poll-based
  reminder channel; push is its own plan), external channel delivery of
  editions (the Telegram `ChannelAdapter` seam is ready when wanted),
  multi-currency ad billing (one currency at MVP, the marketplace rule),
  and `DataPool` cooperatives (individuals first).

---

## 7. Metrics (into the standing investor catalog, group `building`)

- **A1**: contributions published/week; active contributors; scrub/screen
  refusal rate (with reasons).
- **A2**: editions delivered; story open + read-through rate (the feedback
  door); rubric-version stability; serendipity-slice engagement.
- **A3**: votes/day; pairs above the k-floor; preference events exported;
  edition-reorder lift from poll signals.
- **A4**: funded campaigns; fill rate; placement label-render rate (must be
  100%); re-consent completion.
- **A5**: contributor payout total; conservation-check failures (must be 0);
  fraud exclusions; dispute clawbacks.
- **A6**: comparison sets built; briefs opened; brief → approved-order
  conversion; verified-review count.
- **A7**: plans drafted; group polls run; feasible-plan rate; bookings
  completed; calendar events created.

---

## 8. Risks, open questions, and the decision log

**Risks to hold in view:**

- **The legal renegotiation is the largest single risk.** Shipping ads
  reverses a public, test-pinned promise. Mitigation is invariant 13 plus a
  product stance to decide below: whether ads are default-off for accounts
  created under the old promise until they re-consent.
- **Editorial liability.** The platform composes stories from member
  content. Mitigations: the corroboration floor for *critical* claims,
  the correction path as supersession (never silent edits), a report door
  on every story, and takedown that excludes forward while preserving the
  audit trail.
- **Echo chambers.** Preference-ranked editions converge. The serendipity
  slice is mandatory (A2), and its size is a decision-log item, not a knob
  buried in code.
- **Poll gaming.** Coordinated voting skews both the public stats and the
  learning signal. Mitigations: one verified principal one vote, velocity
  gates, the k-floor, and exclusion of flagged rings from exported
  preference data.
- **Cold-start supply.** A closed loop means empty at birth. Mitigations:
  the contribution surface ships before the newsroom (A1 before A2), and
  Explorer's parallel track gives the roster immediate utility from
  existing marketplace data.
- **Travel supply is thin at first.** In-app bookable inventory limits
  early itineraries to what sellers and federated partners list. The plan
  says so honestly: Travel v1 is a *planner over real constraints* first, a
  booking engine to the extent supply exists.

**Decision log (numbers and stances to fix before their phase):**

1. The ad commission `α`, price weights per event type, and second-price
   floors (A4/A5).
2. Ads default-off or default-on for pre-amendment accounts (A4).
3. The k-anonymity floor value for poll stats (A3).
4. The serendipity-slice fraction of an edition (A2).
5. The corroboration floor for *critical*-scored stories (A2).
6. Lab-evidence certification: who may attach, and whether an audit-flagged
   authority level is required (A6).
7. Contributor multiplier factor set and bounds for the ad split (A5) —
   start from the `nodeplace/rewards.py` factor table, drop what does not
   transfer.
8. `DataPool` governance and the member-weight function, inherited from
   ADR-0005's open questions (post-A5).
9. Whether editions may later deliver over external channels (Telegram
   adapter) and what the byline/label laws require there (post-A2).
