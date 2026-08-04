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

**The desk doctrine, amended (2026-08): deterministic route, thinking
insides.** The first cut of N1/N2 made the desks deterministic end to
end and paid for it with a real defect: a tie-broken trial slot locks
onto the same cold genre forever, and an unlucky early record never
earns a re-test — cumulative suboptimal choice. The amendment splits
the determinism law along its true seam:

1. **The ROUTE is deterministic.** Which desk feeds which, the slot
   contracts, the gates, the SOP edges — exact matching, no model, no
   draw. Unchanged.
2. **Inside a desk, sampled decisions are legitimate — and recorded.**
   Where evidence is a posterior, the desk THOMPSON-SAMPLES it (cold
   start explores by construction; wide posteriors keep earning
   re-tests; tight ones converge). Every reading records its draw
   seed, so a sampled decision replays exactly from its stored inputs
   plus its stored seed: auditable stochasticity, not forbidden
   stochasticity. Landed in N1 v2 (the completion posterior) and
   N2 v2 (the slate's exploration slot over topic kinds).
3. **Inside a desk, model judgment over non-numeric data is
   legitimate — behind the observer seat.** Each desk node carries an
   OBSERVING agentic model: it reads the desk's readings and its
   non-numeric residue (member words, subjects, refusal logs, the
   things no threshold can type), and when it finds an issue it files
   a PROPOSAL with a fix plan. The proposal is words plus evidence
   refs; the OWNER's approval — the standing approval path, never a
   silent adoption — turns it into a node replacement and a re-pave.
   Models propose, the owner disposes, the type system enforces.
   (The observer seat builds with N7/P4 — named here, not faked.)
4. **Fully deterministic work opts OUT of the route.** Work that needs
   and only needs deterministic operation — certain, specific data in,
   one right answer out (a rollup, a schema migration, a normalizer) —
   is library code inside a desk, not a routed node. Node-hood is for
   work that carries uncertainty worth observing; giving a pure
   function a node's costume wastes an observer on nothing.

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

**Status: LANDED (v2)** — `press/demand.py`: `rank_demand`
(engagement 0.5 / interest 0.3 / supply 0.2, `DEMAND_VERSION = 2`),
every rank carrying its factor breakdown and raw evidence. **v2
amendment (the desk doctrine):** the completion evidence is a Beta
posterior and the desk THOMPSON-SAMPLES it — cold start explores by
construction, unlucky records keep earning re-tests, no cumulative
lock-in; every reading records its draw seed so the sampled decision
replays exactly; below the reader floor the row is flagged `explored`
(ranked on a draw, honestly named); `rng=None` gives the deterministic
posterior-mean reading. `GenreDemandStore` holds the standing reading
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
- The decision: a ranking with named factors (the Explorer brief's
  discipline — every factor shown, the breakdown stored), refreshed on
  the pulse. Exploration is principled: the completion posterior is
  Thompson-sampled (the desk doctrine), so cold genres explore by
  construction and no early record locks the order in — with every
  reading's draw seed recorded.
- Output: `press_genre_demand` — ranked genres, each with its evidence
  breakdown, queryable and rendered in the News thread on ask
  ("genres" answers with the chips AND the current demand reading).

Exit gate: a reading replays exactly from its recorded inputs plus its
recorded draw seed; every rank carries its breakdown; a genre below the
reader floor is flagged as exploring and says so; cold start explores
and strong evidence converges (both statistically pinned); no model
call anywhere in the decision.

### N2 — the topic desk and the marketplace beat

**Status: LANDED (v2)** — `press/topics.py` (`TOPIC_VERSION = 2`):
four deterministic miners over typed rows (the gateway hands them in;
the press package imports no marketplace module and the import scan
holds): `mine_price_moves` (the
discount FACT past a 10% floor), `mine_trust_bands` (order-book trust
in the concern/proven bands, only with a real book behind the number),
`mine_measured_gaps` (lab vs verified-review factors splitting ≥0.35,
both sides with real evidence — a neutral no-evidence factor never
manufactures a gap), `mine_clusters` (independent voices agreeing via
the corroboration machinery, anchored on the first telling, never
twinned). Law 3 from birth: the gateway stamps advertiser/promoted
flags onto the beat rows (`_beat_rows`) and every candidate carries
its named disclosure before selection. `select_topics` blends demand
(N1's standing reading) 0.4 / evidence 0.35 / freshness 0.25 with the
breakdown stored. **v2 amendment (the desk doctrine):** the last slate
slot is an exploration draw — a Thompson sample over the topic kinds
waiting outside the slate, from each kind's served/engaged book
(`press_topic_kind_stats`; the engaged side's writer arrives with N4).
The chosen row is flagged `explored`, the reading records its draw
seed, and `rng=None` keeps the pure ranked slate. `TopicBriefStore`
refuses a brief without facts (provenance mandatory) and replaces the
slate whole. Doors:
`GET /v1/press/topics`; "topics"/"slate"/"beat" in the News thread
speaks the slate with every disclosure; the edition pulse re-mines the
beat after the demand refresh. Pinned by `tests/test_topic_desk.py`
and the N2 case in `tests/test_newsroom.py`.

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

**Status: LANDED** — `press/surveys.py`: `compose_survey` derives ONE
question deterministically from a topic brief (a corroborated cluster
asks "which telling serves the reader better?" with the two pieces as
options — the `telling` kind; every other brief asks "is this worth a
full story?" — the `editorial` kind, fixed typed options); the desk
tick (on the pulse) closes expired questions (48 h TTL) and opens ONE
survey at a time for the top not-yet-surveyed brief — research, not a
feed. The random sample is bounded (12), drawn from the consented
edition subscribers with a RECORDED seed (the desk doctrine), and the
question block lands in each sampled member's News thread with the
honest "why am I seeing this"; any member volunteers through
`GET /v1/press/surveys` or the "surveys" ask. The answer laws:
consent first (off → unrecorded, and the door says so), one answer
once (idempotent; changing refused), the reveal above `SURVEY_K_FLOOR`
only (counts, never a name), erasure removes answers AND sample rows
so aggregates honestly shrink. A consented `telling` answer writes the
member's own `press_pairwise` row — the book's first writer since the
poll floor closed; the DPO export door needed no change. The typed
SOURCE ROW (`SurveyDesk.result_row`: survey id, topic key, question,
sample size, floored aggregate) stands ready for N4 to cite, and the
respondent set is retained pseudonymously for N6's split. Pinned by
`tests/test_survey_desk.py` and the survey case in `Agents.test.tsx`.

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

**Status: LANDED** — `press/composer.py`: `compose_story_parts`
consumes exactly the survey's BRIEF SNAPSHOT (the research bundle as
it stood when the question opened — the slate replaces whole, so the
survey carries its own copy), the survey's floored result row, and the
cited contributions resolved live. The seat's model voices the post
under the hard HEADLINE:/PROSE: contract over numbered sources only; a
broken contract or a dead model falls back to the DESK POST — the
typed facts rendered plainly with the survey line, publishable on a
host with no brain. The disclosure is appended by the desk and stored
on the story row — law 3 survives verbatim, never entrusted to the
model. `StoryStore` grows the source table (`press_story_sources`, one
provenance surface: the rendered "Sources" section IS these rows) and
the N4-extended refusal law: no lineage AND no sources → no stored
post; contribution sources still produce lineage shares summing
exactly to 1.0 (the dividend keeps paying), while a pure-market post
stores with sources and no lineage. The pulse composes every closed
survey's untold topic BEFORE the edition assembles; the reader renders
the disclosure line and the expandable Sources section; and the kind
book's ENGAGED side gains its named writer — a consented completed
read of a topic post teaches the exploration draws. Pinned by
`tests/test_composer.py`.

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

**Status: LANDED** — `press/editions.py` grows `match_edition`
(`MATCH_FLOOR`): a consented member's morning holds the posts whose
BENT score (the rubric selection bent by their affinity and taste)
clears the bar — not a fixed digest — with the serendipity slot
surviving the threshold (the best story outside the leaning may take
the last slot even below the bar; tastes never fully close). A member
without consented signals keeps the neutral digest they subscribed to.
Delivery receipts land in the benchmark store (`press_deliveries` on
`StoryMetricsStore`): exactly-once per (post, member) by key, so a
morning never repeats a story and a morning with nothing new says so
honestly (no block, the plain words). The engagement report IS the
benchmark aggregate — one store, pushed → opened → finished → liked —
with `pushed` riding both aggregate shapes and per-genre evidence
(`GenreEvidence.pushed`), read by the genre desk next cycle.
Deliveries ride account erasure. Also fixed here: a topic post's
breakdown gains `selection` (its slate score), so N4 posts rank and
match like any story instead of at zero. Pinned by
`tests/test_publication.py`.

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
7. **The desk doctrine (amended 2026-08).** The route between desks
   is deterministic; INSIDE a desk, Thompson-sampled decisions (seed
   recorded, replayable) and observer-model judgment over non-numeric
   data (proposal → owner approval → re-pave) are legitimate; work
   that is fully deterministic over certain, specific data opts out of
   the route as library code. Landed as N1 v2 / N2 v2.
8. **Node-first is the architecture, not a precondition.** Each desk
   ships as an ordinary testable stage and is contributed as a node
   when the P-series lands; the slot vocabulary is fixed NOW so the
   later paving is a survey, not a rewrite.
