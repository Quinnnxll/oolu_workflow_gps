# The News agent benchmark — from member newsroom to honest reporter

Status: Proposed. Scope: one AI-generated-content benchmark, set on the
News agent. The News agent becomes a **brutally honest reporter for the
products in the OoLu marketplace**, run like a magazine company: a clear
logical decision tree of deterministic workflow nodes that scales and
evolves by replacing an outdated node with a new one and re-paving.
The Poll agent is REMOVED (landed with this document — see the
amendment in `docs/agents-expansion-plan.md` and the CHANGELOG): a
social-feed comparison game breeds superficial engagement; the
benchmark measures interest on the news posts themselves.

**The benchmark.** A news post succeeds or fails on three measured
signals from its readers, nothing else:

1. **Attention time** — how long the opened story actually held the
   reader;
2. **Complete-reading rate** — of the members who opened it, how many
   reached the end;
3. **Like count** — the explicit tap.

Every editorial decision the pipeline makes (which genre, which topic,
which sources, which final telling) must be explainable back to these
three numbers plus typed evidence — never a model's mood.

**The pipeline the benchmark demands** (the target decision tree):

```
genre desk ──► topic desk ──► research desk ──► survey desk ──► composition desk ──► publication desk
(which genre     (which topic    (gather the        (random sample      (the final post,     (push through
 are readers      inside it)      typed facts:       or direct opinion   every claim          conversation to
 interested in?)                  listings, orders,  collection, in      sourced)             matched readers;
                                  trust, lab         conversation)                            expandable block)
                                  results)
                                          └──────────── every source traceable ────────────┘
                                          └──── contributors and respondents share revenue ─┘
```

Companion reading: `docs/agents-expansion-plan.md` (the press A-phases
this grows out of), `docs/algorithm-barriers-plan.md` (nodes, gates,
SOPs, the Paver — the machinery Part III builds on),
`docs/node-generation.md` (the node-synthesis doctrine),
`docs/adr/0005-data-dividend-model.md` (the conserved contributor
split), `docs/REWARD_PRICING_DESIGN.md`.

---

## Part I — honest review of the current build

What stood when this roadmap was written — the baseline its phases
close gaps against. A phase's own **Status: LANDED** line supersedes
the rows it closes (N0 has landed: the metrics, block-persistence,
reader, and arrival rows below describe the before-state).

**The contribution spine (A1) — solid.** Members drop material into the
News thread; intake detects article-shaped text or attachments, asks at
most one question, and a plain yes publishes (`press/intake.py`,
`press/contributions.py`). Publication walks five gates (consent+license
→ bounds → taxonomy → scrub-refusal → near-duplicate flag). Attachments
now ride as TRUE bytes end to end (the blob-first `saveToDrawer` door),
with previews and lossless downloads in the thread.

**The newsroom (A2) — a selection pass, not a reporter.** Composition
reads ONLY the tenant's existing contributions
(`press/newsroom.py:340`), scores them on a deterministic three-virtue
rubric (inspiring/critical/knowledgeable, blended 0.4/0.3/0.3,
`press/standards.py:155-158`), and composes at most 3 stories per run
through the `news.compose` seat under a hard no-invention frame with a
verbatim desk fallback. Lineage weights (anchor 0.6, the rest split,
summing exactly 1.0) land in a queryable `press_story_lineage` table —
provenance is mandatory at the store's insert
(`press/newsroom.py:105-110`).

**Personalization and delivery (A2.x) — per-member, opt-in, text-only.**
`rank_edition` bends a neutral order by consented genre affinity (pull
0.5) and semantic taste (pull 1.0) with a mandatory serendipity slot
(`press/editions.py:202-252`). Delivery is a daily morning-edition pulse
that lands ONE plain-text headline list in the News thread
(`edition_message`, `press/editions.py:255-275`) plus a reminder row.

**Rewards (A4/A5) — one path, ads only.** A verified ad impression on
the News surface settles through the standing pricing pipeline over the
story's recorded lineage weights (`billing/addividend.py`). Conserved to
the micro; fraud gates; holdback accrual.

**What is MISSING against the benchmark — the honest list:**

| Target | Current truth |
|---|---|
| Genre demand decision | None. Genres are copied from the anchor contribution (`newsroom.py:376`); intake assigns them by a hard-coded keyword table (`intake.py:220-247`). Nobody asks what readers WANT. |
| Topic decision | None. There is no topic concept at all — selection is piece-by-piece rubric scoring. |
| Information gathering | None, beyond the member shelf. The press is closed-loop BY LAW (import scan) — but the marketplace's own typed data (listings, orders, trust scores, lab results) is inside the loop and untouched by the newsroom. |
| Survey step | None. The poll floor was the only instrument and it measured the wrong thing (piece-vs-piece fun). Removed. The pairwise preference book survives (`press/pairwise.py`) with no writer yet. |
| Marketplace-product reporting | None. Explorer compares listings for BUYING; no news desk reports on products, sellers, or market movements. |
| Attention time | Nothing measures it. No open/close timestamps anywhere. |
| Complete-reading rate | Nothing. The `read` feedback signal exists server-side (weight 0.4, `editions.py:49`) and is NEVER sent by any client — nothing opens a story, so nothing finishes one. |
| Like counts | Half-exists: like/skip taps per member feed personalization (`/v1/press/stories/{id}/feedback`). There is no per-story aggregate, no editorial readback. |
| Expandable message block | None. The edition message is unaddressable plain text; blocks are never persisted (history has no block column); no full-screen or overlay machinery exists anywhere in the frontend. The unused story-detail door (`GET /v1/press/stories/{story_id}`) is the seam already pointing at "open one story". |
| Push to matched users | Half-exists: the pulse fires per subscribed member and ranking is per-member — but the News thread never re-reads (`AgentThread` fetches history once per mount), so a pushed edition is invisible until the user clicks News. No cross-member "who would want this" query. |
| Traceable research/survey sources | Story lineage is traceable and queryable — for member contributions only. No research-source or survey-source rows exist. |
| Revenue to contributors | Ad dividend over story lineage only. Survey respondents and research sources earn nothing (nothing to earn from yet). |

---

## Part II — the five laws of the honest reporter

Carried forward from the press invariants, sharpened for the benchmark.
Every phase's exit gate cites them.

1. **No invention, ever.** A number, a quote, a survey result, or a
   product fact the pipeline was not given is a thing it does not have.
   The compose contract (headline-or-fallback) extends to every desk.
2. **Provenance is mandatory and total.** Every claim in a published
   post resolves to a typed source row: a contribution, a marketplace
   record, or a survey aggregate. A post with an unresolvable claim
   cannot be stored — the `press_story_lineage` insert-refusal law,
   extended to research and surveys.
3. **Brutal honesty cuts both ways.** A product post reports the trust
   score, the return rate, and the lab result as recorded — favorable
   or not. Advertising can never buy a ranking, a topic, or a kinder
   sentence (invariant 5 unchanged); a seller who is also an advertiser
   is named as such in the post.
4. **Readers are measured with consent, in the open.** Attention time,
   completion, and likes are recorded per-member ONLY under
   `press.personalize`; editorial decisions read AGGREGATES over a
   k-anonymity floor. The data subject's erasure right outranks every
   aggregate.
5. **Survey respondents and cited contributors are principals.** A
   source that shaped a post carries attribution weight, and revenue
   attached to the post splits over those weights — the conserved
   ADR-0005 split, no new invention.

---

## Part III — the magazine company as a deterministic node web

The build directive: the News agent runs like a magazine company —
each desk a **deterministic workflow node**, the org chart a **web** of
typed producer→consumer edges, the editorial law a human-authored
**SOP** compiled into gates, and evolution = **replace the outdated
node, re-pave**. This is the platform's own machinery
(`docs/algorithm-barriers-plan.md`): slots match exactly, edges are
derived not narrated, the Paver rehearses a web end-to-end in the
severed sandbox and promotes it to one node, and one trigger fires the
whole web under revocable consent.

**The slot vocabulary** (the discipline in `docs/node-generation.md`
§3 — a synonym is a different universe):

```
press_genre_demand      # genre desk  → topic desk   (ranked genres + evidence)
press_topic_brief       # topic desk  → research desk (the chosen topic, typed)
press_research_bundle   # research desk → composition (typed facts + source rows)
press_survey_result     # survey desk → composition   (k-anon aggregates + source rows)
press_post_draft        # composition → publication   (post + full source table)
press_post_published    # publication's effect        (story_id + delivery receipts)
press_engagement_report # metrics spine → genre desk  (the loop closes)
```

**The decision tree is gates, not prose.** The editorial SOP compiles
to real guard/loop edges (`skills/sop.py`, `orchestrator/gates.py`):

- *"Only publish when every claim resolves to a source row"* —
  a `require_guard` on the publication desk over the composition
  desk's evidence.
- *"Only run the survey when research leaves a named open question"* —
  a guard admitting the survey desk on the research bundle's
  `open_questions` field; a survey that tests nothing never runs.
- *"Research too thin → loop back, at most 3 passes"* — a bounded
  `loop` edge with its mandatory budget; exhaustion FAILS loudly.
- *"A post naming a seller-advertiser routes through editorial hold"* —
  the reserved/hold seam: propagation halts as `awaiting_approval`,
  never routes around the human.

**The daily press run** is the pulse-anchor pattern: a schedule whose
goal names the genre-desk node marks it a web anchor; one firing runs
the whole web under the standing per-web consent, exactly once per
occurrence.

**Evolution = re-pave.** A desk that underperforms its benchmark
numbers is replaced by publishing a successor node and revoking the
old one; the web's membership changes, its signature changes, the
Paver re-bridges, re-derives the gates, REHEARSES the new web
end-to-end in the sandbox, and only a clean pass promotes. The stale
desk never limps along silently.

**Platform prerequisites (P-series)** — real gaps in today's machinery
that this build needs, named so nothing is faked:

- **P1 — the tenant SOP store.** `_paver_sops` returns `[]` today;
  production webs pave gate-free. The editorial law above cannot be
  authored by a human until this lands. (Named-deferred in
  `docs/algorithm-barriers-plan.md`.)
- **P2 — content-sensitive re-pave + retirement.** `RouteWeb.signature()`
  hashes member ids and edge slots but not child contract content: a
  same-id function edit never re-paves, and a superseded promoted node
  is never revoked. "Replace and re-pave" needs the signature to cover
  child content hashes and the promotion to retire its predecessor.
- **P3 — reserved-hold resume.** The editorial-approval halt exists;
  the approver's release does not resume the web. An approval gate that
  stalls forever is not a gate, it is a wall.
- **P4 — the News agent as web owner.** Roster agents hold "no hands,
  words only" today; the Paver's own products are stamped
  `oolu-paver`. The News desk needs its own paver principal so the
  magazine's webs are ITS standing property, listed under its name.

Phases N1–N5 are specified desk-by-desk so each desk lands FIRST as an
ordinary library/gateway stage (testable, shippable alone) and is then
contributed as a node with the slot vocabulary above — the same code,
behind a contract. The web is assembled the moment the P-series allows;
nothing in N1–N5 waits for it.

---

## Part IV — the phases

Ordering rule: the benchmark's measuring stick comes FIRST (N0) — every
later desk is judged by it, so building it last would mean building the
whole pipeline blind.

### N0 — the metrics spine and the expandable story block

**Status: LANDED** — `press/metrics.py` (`StoryMetricsStore`: one
receipt per (story, member) with max-dwell/sticky-completion upsert,
idempotent likes, the k-anonymous `aggregate`, per-member erasure wired
into account deletion); the feedback door carries `read` measurements
(`dwell_ms`, `completed`) and counts likes once; the metrics door
`GET /v1/press/stories/{id}/metrics`; blocks persist with turns
(`assistant_turns.block`, migrated in place) so the edition pulse
lands a `story` block of previews beside its words; the frontend
`StoryReader` takes the pane (the FileView pattern) with honest
dwell/scroll-completion measurement sent once on leaving, the like tap,
and the k-anonymous metrics line; the News thread re-reads on a 30 s
rhythm so a pushed edition is SEEN. Pinned by the N0 cases in
`tests/test_newsroom.py`, `tests/test_roster.py` (block round-trip),
and `Agents.test.tsx` (the reader flow).

Goal: the three benchmark signals exist, honestly measured, and the
story is finally something a member can OPEN.

- **The story message block.** A fifth block kind
  `{ kind: "story", items: [...] }` in the two block unions; the
  edition pulse writes it. Blocks must survive reload: a `block` column
  on `assistant_turns` (the `files` column's migration pattern,
  landed with this commit, is the template). The edition message keeps
  its text form for old clients; the block rides beside it.
- **Expand to full screen.** The block renders headline + first lines;
  tapping expands to the full story — the app's existing top-level
  view-swap pattern (the Market precedent), not a popped window. The
  unused story-detail door becomes the client's `pressStoryDetail`.
- **Attention time + completion.** Opening a story stamps `opened_at`;
  leaving it stamps `closed_at` and the furthest scroll position.
  Attention time = the dwell; complete-read = reaching the end marker.
  Recorded per-member under `press.personalize` (the existing feedback
  door grows `read` payloads: `{signal: "read", dwell_ms, completed}`);
  the dead `read` signal finally fires from the real reading surface.
- **Aggregates with a floor.** A new `press_story_metrics` read:
  opens, likes, mean attention, completion rate — rendered to
  EDITORIAL decisions only above a k-anonymity floor (law 4). The
  per-story like count becomes real.
- **Arrival.** The News thread gains a 30-second history refresh so a
  pushed edition is seen without a manual click. (The OoLu thread's
  standing 30-second interval polls only reminders — the history
  re-read is new here.)

Exit gate: a story pushed on the pulse renders as an expandable block
on every device (history reload included); opening/finishing/liking it
produces exactly the three benchmark numbers in the aggregate read;
consent off → nothing per-member is written and the door says so;
erasure removes the member's rows and the aggregate honestly shrinks.

### N1 — the genre desk (which genre are people interested in)

**Status: LANDED** — `press/demand.py`: `rank_demand`, the
deterministic, model-free ranking (engagement 0.5 / interest 0.3 /
supply 0.2, versioned `DEMAND_VERSION`), every rank carrying its factor
breakdown and raw evidence; the reader floor gates the engagement
factor (below it the factor is honestly absent); ONE bounded trial
slot promotes the best unevidenced genre to second place,
deterministically. `GenreDemandStore` holds the standing reading
(whole-replacement, never a mixed vintage) and the anonymous interest
book — no principal column exists by schema.
`StoryMetricsStore.genre_evidence` rolls N0's signals up per genre
(distinct readers; per-member rows never leave the store). Doors: the
reading at `GET /v1/press/genres/demand` (computed fresh from recorded
inputs, recorded on read); "genres" in the News thread answers with
the chips AND the demand line; a named stream is an anonymous tap plus
that stream's standing in words; the edition pulse refreshes the
reading — the benchmark loop's first closure. Pinned by
`tests/test_genre_demand.py` and the N1 case in `tests/test_newsroom.py`.

Goal: the first editorial decision reads evidence, not keyword hints.

- Inputs (typed, all existing or from N0): per-genre engagement
  aggregates (attention/completion/likes from `press_story_metrics`),
  genre-affinity distributions across consented members, contribution
  supply per genre, and the genre-chips asks members tap in the News
  thread (landed with this commit — the chips re-homed from the poll).
- The decision: a deterministic ranking with named factors (the
  Explorer brief's discipline — every factor shown, the breakdown
  stored), refreshed on the pulse. Exploration is principled: an
  under-evidenced genre gets a bounded trial slot, on evidence, never
  on an editor's hunch.
- Output: `press_genre_demand` — ranked genres, each with its evidence
  breakdown, queryable and rendered in the News thread on ask
  ("genres" answers with the chips AND the current demand reading).

Exit gate: the ranking is reproducible from its recorded inputs; every
rank carries its breakdown; a genre with zero evidence ranks by the
exploration rule and says so; no model call anywhere in the decision.

### N2 — the topic desk and the marketplace beat

Goal: inside the chosen genre, decide WHAT to report — and open the
marketplace's own typed records to the newsroom as first-class sources.

- **The beat.** A read-only research surface over what the platform
  already records: listings and their categories, price movements,
  order volumes, verified-buyer feedback, order-book trust scores,
  member lab results, RFQ activity. All inside the closed loop — the
  import-scan law does not move. No web search.
- **Topic mining, deterministic.** Topic candidates are typed events:
  a price moved past a threshold, a trust score crossed a band, a
  product's feedback diverged from its rating, contribution clusters
  formed around a subject (the corroboration machinery reused above
  the piece level). Each candidate carries its evidence rows.
- **Topic selection** blends demand (N1), evidence weight, and
  freshness with named factors; the choice and its breakdown are
  stored (`press_topic_brief`).
- **The honesty seam (law 3).** A topic involving an active advertiser
  or the seller of a promoted listing is flagged IN the brief — the
  disclosure travels with the topic from birth.

Exit gate: every topic brief resolves to typed marketplace/contribution
evidence; an advertiser-adjacent topic carries its disclosure flag; the
selection breakdown is stored and reproducible; the import scan still
holds over the press package.

### N3 — the survey desk (ask the members, honestly)

Goal: the pipeline's one instrument for what records cannot tell —
reader opinion — rebuilt on the News desk, without the poll floor's
social-feed shape.

- **Two collection modes, both in conversation.** (a) *Random sample*:
  the survey desk draws a bounded random sample of consented members
  and lands ONE question block in their News thread (single question,
  typed answer options, an honest "why am I seeing this"). (b) *Direct
  opinion collection*: any member can volunteer an answer to the open
  survey from the News thread ("surveys" lists what is open).
- **Consent and the floor.** Participation requires
  `press.personalize`; results aggregate above the k-anonymity floor
  (the K_FLOOR discipline, reborn where it belongs); no individual
  answer is ever rendered or exported; answers ride account erasure.
- **The pairwise book gains its writer.** A survey answer that is a
  choice between two tellings writes the member's own
  `press_pairwise` row (the store that outlived the poll floor —
  `press/pairwise.py`) under the same consent; the DPO export door
  needs no change.
- **Traceable by construction.** A survey aggregate used in a post is
  a source row: survey id, question, sample size, the aggregate — and
  the respondent set is retained (pseudonymous, erasable) for the
  revenue split in N6.
- Output: `press_survey_result`.

Exit gate: a survey question renders as a block and one answer per
member is idempotent; below the floor the aggregate refuses to render;
consent off → unsampled and unrecordable; erasure removes the member's
answers and honestly shrinks aggregates; a survey source row resolves
from any post that cites it.

### N4 — the composition desk (the final post, every claim sourced)

Goal: sum research + survey into the post — the reporter's voice, the
notary's records.

- **The source table is the contract.** Composition consumes exactly
  `press_research_bundle` + `press_survey_result` + the cited
  contributions. The compose frame (no-invention, fallback-on-broken
  contract — `newsroom.py`'s law, extended) requires every factual
  sentence to map to a source row; the desk fallback is the typed facts
  rendered plainly.
- **Lineage grows two row types.** `press_story_lineage` (or a sibling
  table joined to it) records research sources (marketplace record
  refs) and survey sources (survey ids) beside contributor weights —
  one queryable provenance surface per post. The insert-refusal law
  covers all three: no full source table, no stored post.
- **Brutal honesty rendered.** The post template carries the
  disclosure flag from N2 verbatim, the unfavorable numbers unedited,
  and a "Sources" section the reader can expand — every row tappable
  to its record (a contribution opens the piece; a survey opens the
  aggregate; a marketplace record opens the listing view).

Exit gate: a post with an unresolvable claim cannot be stored; the
rendered post's source table matches the stored lineage rows exactly;
the model-down path still produces a publishable desk post; the
disclosure flag survives from topic brief to rendered post.

### N5 — the publication desk (push to the readers it fits)

Goal: the post reaches matched readers in conversation, as the N0
block, and the loop closes.

- **Matching.** The existing per-member machinery is the matcher:
  `rank_edition`'s affinity + semantic taste decides whether THIS post
  clears THIS member's bar (a threshold on the bent score), instead of
  a fixed 5-story digest for subscribers only. The pulse enumeration
  (every consented member's standing schedule) is the fan-out; the
  serendipity slot survives so tastes never fully close.
- **The receipt.** Delivery writes a per-post, per-member receipt row
  (pushed / opened / finished / liked — N0's signals keyed by post),
  which is exactly `press_engagement_report`: the genre desk (N1)
  reads it next cycle. The benchmark loop is closed.
- **The thread is live.** N0's refresh makes arrival visible; the
  reminder ring stays as the cross-thread nudge.

Exit gate: a post lands only with members whose consented signals clear
the threshold (plus the serendipity slot), as an expandable block;
receipts are exactly-once per (post, member); the engagement report
over receipts is the same numbers the benchmark aggregate shows.

### N6 — the revenue loop (sources get paid)

Goal: revenue attached to a post flows back over the FULL source table.

- The ad dividend's split input widens: contributor lineage weights
  (standing), plus survey-respondent shares (the survey's retained
  respondent set splits a fixed survey tranche evenly), plus any
  research-source member (a member whose lab result or verified review
  anchored a claim). One conserved split through the standing
  `PricingEngine` — ADR-0005's discipline, no parallel pipeline.
- Every payout row cites the source row it pays for — the provenance
  surface and the ledger agree to the micro.

Exit gate: a settled post's payouts sum exactly to the revenue less the
named commission; every payout resolves to a source row; an erased
member's future shares stop while history stays balanced; nothing pays
twice (idempotent settlement, the A5 law).

### N7 — the web assembled (the magazine company stands)

Goal: with P1–P4 landed, the desks become the paved web.

- Each desk contributed as a node under the News agent's principal,
  slots per Part III; the survey confirms the derived edges match the
  pipeline above; the editorial SOP compiles to the gates; the Paver
  rehearses and promotes; the morning pulse anchors the daily run.
- The benchmark becomes the evolution law: a desk whose replacement
  rehearses better numbers on the same recorded inputs (the replay
  discipline) is swapped in by re-pave; the old node is revoked, never
  left limping.

Exit gate: one trigger runs genre→topic→research→survey→composition→
publication end to end with gates enforced; an editorial hold halts
and RESUMES on release (P3); replacing one desk re-paves and re-
rehearses without touching the others; the promoted web's gates render
in words on `/v1/paver/webs`.

---

## Part V — decisions taken with this document

1. **The Poll agent is removed, not parked.** Code, seat, card, doors,
   UI, tables (via a durable migration). Rationale: the benchmark
   measures interest on posts; a comparison game measures fun with an
   instrument bolted on, and breeds superficial content.
2. **The pairwise preference book survives** in `press/pairwise.py`,
   consented, exportable, erasable — the survey desk (N3) is its next
   writer. The DPO export door stands unchanged.
3. **Privacy v3** narrows v2 (News surface only) with the version bump
   and re-acceptance the repo's own invariant 13 demands. The members
   license drops the poll use the same way — promises narrow, never
   silently widen.
4. **The genre chips re-home to the News desk** — the smallest standing
   piece of N1, landed with this commit.
5. **Metrics before desks** (N0 first): a pipeline judged by numbers
   nobody measures yet would be built blind.
6. **The closed loop does not open.** "Research" means the platform's
   own typed records — the import-scan law is untouched in every phase.
7. **Node-first is the architecture, not a precondition.** Each desk
   ships as an ordinary testable stage and is contributed as a node
   when the P-series lands; the slot vocabulary is fixed NOW so the
   later paving is a survey, not a rewrite.
