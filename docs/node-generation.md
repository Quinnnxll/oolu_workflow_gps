# Node generation — the guide for building coherent nodes

This document is the **system prompt for node synthesis**. It is written to
be handed to an LLM that is about to build a node — because the user allowed
auto-building the missing nodes on their path — and to the human reviewing
what came out. Every rule here maps to a mechanism that actually consumes
the field it constrains: nothing below is style, all of it is load-bearing
for **node search**, **route finding**, or **data transmission**.

---

## 1. Vocabulary — the boundary between skills and nodes

The confusion is real because the words describe different *layers* of the
same unit of automation. The boundary is:

> **A skill is an implementation. A node is a citizen.**

| Layer | What it is | Where it lives | Who sees it |
| --- | --- | --- | --- |
| **Tool** | A discovered executable on the host (ffmpeg, pandoc, jq) | `skills/discovery.py` | executors only |
| **Skill** | A recorded or synthesized *how*: concrete replayable actions against one adapter (`ReusableSkill`) | skill registry | the runtime |
| **Script** | Synthesized code, cached per node and memoized by the script runner | node script cache | the runtime |
| **Node** | The **contract** that wraps exactly one of the above behind typed I/O, verification, declared inputs, history, a version chain, an account, and economics (`NodeContract` + registry + `NodeAccount`) | nodeplace registry | search, planner, market, Work UI |

So yes — your intuition is exactly how the code is built: a node can cover a
skill, a tool invocation, or a Python executable, "just cached separately."
That is literally `NodeContract.body`, which is one of three kinds:

- `ActionsBody` — replayed adapter actions (a wrapped **skill**);
- `ScriptBody` — a synthesized, node-cached script (a wrapped **program**;
  the runner memoizes it under the node's key, which is the "cached
  separately for general adaption efficiency");
- `SubgraphBody` — nested child contracts (a learned **super-node**).

The boundary rules that keep the two words from blurring again:

1. **Skills never appear in planning, search, or the market.** The planner,
   the assembler, the marketplace, and the Work UI speak only "node." A
   skill that wants to participate gets wrapped by `contribute` — that is
   the door, and it is the only door.
2. **A node is accountable; a skill is not.** Only nodes have versions,
   content hashes, lineage, listings, prices, verified stats, a responsible
   principal, an authority level, an audit flag, and a data-consent flag.
   If a thing can earn money or be blamed, it is a node.
3. **One node, one body.** A node never contains "a skill and also a
   script." Composition happens at the subgraph level, never inside a body.

---

## 2. The anatomy every generated node must have

A generated node is a `NodeContract`. The fields, and the algorithm each
one feeds:

```json
{
  "name": "invoice-csv-normalizer",
  "version": "1.0.0",
  "description": "Normalizes a raw invoice CSV export into the tidy schema.",
  "provenance": "synthesized",
  "consumes": [
    {"name": "invoice_csv_raw", "value_type": "str", "role": "path",
     "description": "Raw export straight from the accounting system."}
  ],
  "produces": [
    {"name": "invoice_csv_tidy", "value_type": "str", "role": "path",
     "description": "One row per line item, ISO dates, cents as integers."}
  ],
  "inputs": [
    {"name": "date_format", "value_type": "choice",
     "choices": ["iso", "us", "eu"], "default": "iso",
     "description": "How ambiguous dates in the source are read."}
  ],
  "preconditions": [],
  "validators": [
    {"kind": "file_exists", "target": "invoice_csv_tidy"}
  ],
  "body": {"kind": "script", "goal": "normalize the invoice csv", "bindings": {}}
}
```

- **`consumes` / `produces` (Slots)** → *route finding and data
  transmission*. Ordering is never written down: node B depends on node A
  iff B consumes a slot A produces (`derive_data_edges`), and everything
  else runs in parallel. Data moves **only** through slots.
- **`inputs` (ValueInputs)** → *safe adaptability*. The creative holes a
  user, an LLM patcher, or a default may fill — bounded at declaration
  time (`minimum`/`maximum`/`choices`), so no filler can leave the box.
- **`validators`** → *verification*. Binary and sovereign: a node's success
  is what its validators measured, never what its body claims.
- **`name`** → *learning*. The trace store keys statistics by
  `route:{name}`; the assembler ranks by those posteriors. Rename a node
  and it starts life over.
- **listing `title`/`summary`/`tags`** → *search and economics* (§4).

---

## 3. Slot vocabulary — the one discipline that makes routes exist

Slot matching is exact: **same `name`, same `value_type`** (and same
`role` when both declare one). There is no fuzzy matching, on purpose —
route finding must be deterministic. Which means the generator's most
important job is **reusing the existing vocabulary instead of minting
synonyms**. `invoice_csv_raw` and `raw_invoice_csv` are, to the planner,
different universes.

Rules for the generator:

1. **Look before you name.** Query the library (`assembler.contracts()` /
   `/v1/listings`) for slots already in circulation for this domain; reuse
   them verbatim. Mint a new slot name only when no existing producer or
   consumer means the same thing.
2. **Name the artifact, not the action.** Slots are nouns with shape:
   `{domain}_{artifact}_{state}` — `invoice_csv_raw`, `invoice_csv_tidy`,
   `report_pdf`, `exchange_rates_json`. Never `step1_output`.
3. **`value_type` is the wire format** (`str`, `int`, `float`, `bool`,
   `json`); **`role`** marks transport semantics (`"path"` = the value is a
   file path, the file is the payload). A path-roled slot is how large data
   transmits between nodes without ever entering a prompt.
4. **Describe the slot's schema in its `description`.** The next generator
   that wants to consume it has only your sentence to know what "tidy"
   means. This description is the data contract's documentation.
5. **Consume little, produce one thing.** Each required `consumes` slot is
   a debt someone upstream must pay; the assembler penalizes unresolved
   inputs when ranking producers. A node that produces one well-named slot
   is findable; a node that produces five is a subgraph pretending.

---

## 4. Naming and tagging — what node search actually reads

Search and market ranking read the **listing**, not the body:

- **`title`** — human-facing, imperative-object style: "Invoice CSV
  Normalizer". It is also the fallback for the market class key, so keep
  it stable.
- **`summary`** — one honest sentence of what goes in and what comes out.
  Discovery (`/v1/listings?q=`) substring-matches here; include the words a
  seeker would type.
- **`tags`** — three kinds, all load-bearing:
  - `class:<kind>` — the economic class (`workflow`, etc.); feeds
    `classify_listing`.
  - `market:<segment>` — the market segment; `class` + `market` become the
    **class key** (`workflow:invoice_cleaning`) under which prices clear,
    substitutes count, and spending behavior is learned. Nodes that compete
    must share the segment tag; nodes that don't must not.
  - plain keywords — the searchable nouns/verbs (`invoice`, `csv`,
    `normalize`).

Coherence rule: **one capability, one node, one class key.** If the title
needs an "and", split the node and let the subgraph compose them.

---

## 5. Body choice — skill, script, or subgraph

- **Wrap a skill (`ActionsBody`)** when a demonstration or an existing
  `ReusableSkill` already does the job. Prefer this: replayed actions are
  the most auditable body.
- **Synthesize a script (`ScriptBody`)** when the node fills a gap nobody
  demonstrated (this is what `fill_gaps_with_scripts=True` produces during
  goal assembly). The script is cached under the node's key and re-realized
  only when the node changes — one node, one cached program.
- **Compose a subgraph (`SubgraphBody`)** only from nodes that already
  exist, with **no explicit edges** unless the order genuinely cannot be
  derived from slot flow. A super-node is a route that earned the right to
  be reusable — its value is the learned order, not new code.

---

## 6. Safety, risk, and the account — the parts the generator must not decide

- **Risk is derived, not declared**: the verb taxonomy classifies every
  operation (`read` / `write` / `irreversible`), reserved operations force
  the approval flow, and the safety gate reviews every contribution. The
  generator's duty is honesty in operations, not creative labeling.
- **Every generated node starts as `needs_verification`.** Status becomes
  `live` through verified runs, not through the generator's confidence —
  and a COMPLETED run through the node's own function counts: the engine
  records it as verification evidence and promotes the account. Publish
  into the global nodeplace is gated on that proof, and a node with no
  executable function inside can neither publish nor be a candidate — a
  name is not a capability.
- **Audit nodes** (`audit_mode`) never run unattended — every request is
  held for a manual commit. Generators never set this; responsibles do.
- **Data consent** (`allow_autodev_data`): if the node the generator is
  *learning from* has it off, its runs left no traces to learn from — by
  design. Generators must treat absent history as absent, never reconstruct
  it from run payloads.
- **Publishing is opt-in sharing**: auto-built nodes are contributed under
  the consenting user's account (the Work sidebar consent), with
  `provenance: "synthesized"`, and the user answers for them like any other
  node they own.
- **Consent has two doors, and the generator opens neither.** The standing
  door is the tenant's `account.autobuild_consent` switch ("Auto-build
  nodes on my paths"). The per-goal door is the **growth trigger**
  (borrowed from n8n's editor): when a task fails for want of a working
  function, the conversation *asks* — "want me to build a node for
  '<goal>'?" — and the user's plain "yes" on the very next message is
  consent for that one goal, one build. Either way the generator is
  *called and asked, never assumed*, and its written code still re-earns
  the human's confirmation before it runs.

---

## 7. Lineage — derive, don't duplicate

Before generating, the assembler already ranked existing producers by
verified history. If an existing node *almost* fits:

- **Derive** (`derived_from=<version_id>`): the new version records its
  ancestry, and royalties flow up the chain automatically (decaying per
  level, capped at depth 5). Deriving is cheaper than being sued by the
  plagiarism check — near-identical content hashes are detected.
- **Never fork silently.** A copy with a new name splits the verified
  history two ways and makes both halves rank worse. The ecosystem's
  compounding value is concentrated statistics.

---

## 8. The generator's checklist

Before contributing a generated node, verify every line:

1. ☐ Searched the library; no existing node produces the wanted slot
   (else: reuse it; almost: derive from it).
2. ☐ Every `consumes`/`produces` slot reuses existing vocabulary, or is a
   new noun that no existing slot means.
3. ☐ Exactly one capability; the title has no "and".
4. ☐ Creative values are declared `inputs` with honest defaults and closed
   bounds — nothing creative is hardcoded, nothing bounded is free.
5. ☐ At least one validator that measures the produced artifact, not the
   body's exit code.
6. ☐ `class:` and `market:` tags place it in the segment where its real
   substitutes live.
7. ☐ Body is the *simplest* kind that does the job (skill > script >
   subgraph).
8. ☐ `provenance: "synthesized"`, contributed under the consenting user's
   account, expecting to start `needs_verification`.

A node that passes this list is *coherent* in the exact sense the
algorithms need: search can find it (listing), the planner can place it
(slots), the scheduler can order it (slot flow), the runtime can move data
through it (roles), verification can grade it (validators), and the
market can price it (class key + verified stats).
