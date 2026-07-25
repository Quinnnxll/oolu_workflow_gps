# Conversational building — phases for the non-developer's node

Status: Proposed. Scope: the build experience and the node's own
completeness — the four standing problems reported from live use:

1. OoLu (and the agent in a node's interact window) asks NON-DEVELOPERS
   technical questions — file formats, APIs, schemas — they cannot
   answer.
2. The philosophy is conversation-first: inputs and outputs move through
   conversation; when a form appears it is a STRICT VALUE CHECK, never a
   questionnaire.
3. The authored function does not land in the node's ``src`` reliably.
4. A built node is not self-contained: it does not keep its own input
   and output data, and cannot demonstrably hand its information to the
   next node.

Companion reading: `docs/context-harness-plan.md` (the build door, birth
gate, and review seat this plan hardens), `docs/memory-stack-plan.md`
(the spine/graph/episodes the data phases ride), `docs/node-bundles.md`
(the drawer that becomes the node's home).

---

## 1. The laws this plan builds under

- **Mechanisms are the builder's problem; values are the user's.** A
  format, an API, an endpoint, a schema is a DECISION the building seat
  makes, defaults, and names in its receipt — never a question to a
  human who came to get work done. The only questions a human ever
  hears name a value in THEIR world: which folder, which account, what
  date range, what it should be called.
- **Ground-up by default.** When nothing on the desk answers, the
  default is to BUILD — sensible mechanism defaults, revisable later in
  words ("make it a spreadsheet instead") — not to interrogate until
  the user gives up.
- **A form is a value check.** Where the app renders a form, it renders
  the node's declared inputs exactly: typed fields, plain-word labels,
  validation in words. A form never asks anything a conversation could
  not have asked; conversation can always fill what the form would.
- **A node is a complete unit.** Contract + function + its own record
  of what went in and what came out. Verify-by-execution, the birth
  gate, and the review seat stay exactly as they stand — this plan adds
  completeness, never a bypass.

## 2. What already exists (the seams are cut, not yet aligned)

| Need | Standing machinery |
|---|---|
| Typed asks | `orchestrator/intake.py` clarification: parameters with `options`/`suggested_values`, the human selects — the SHAPE of a value-ask, today unfiltered (an LLM may ask anything, including mechanisms) |
| Value forms | `skills/inputs.py`: `inputs_manifest` (typed, qualified names) + `bind_inputs` (refuses undeclared placeholders) — the strict-check seam, today unlabeled for humans |
| IO declaration | `chat.py` `NODE_FUNCTION_PROMPT` `IO:` line (`parse_node_io_checked` refuses broken declarations) — the builder already DECIDES io; nothing yet forbids upstream conversation from off-loading that decision to the user |
| The function's home | `_build_function_node` writes `src/main.py` via the seat-walled `DeskFiles` + birth commit; `_drawer_function` makes the FILE the run's first read — today the write is best-effort (`if self._files is not None`), silent when the seat refuses, and nothing reconciles drawer↔version drift |
| Run inputs | `bindings.json` staged into the sandbox each run — consumed, then GONE; nothing lands in the node's drawer |
| Run outputs | `emit_result` → route slot flow (`derive_data_edges`) — values chain in-route, in memory; the node keeps no record of what it produced |
| Relations | M1 temporal graph: publishes land `consumes`/`produces` edges — structure without run-level data provenance |
| Attention | `node.build_failed` + the operator inbox (issue 5) — where any new loud failure belongs |

---

## 3. The phases

### Phase B0 — the interrogation budget: questions become value-asks

**Status: LANDED** — `src/oolu/plainlanguage.py`: the mechanism lexicon
with its readers (`mechanism_terms`, `mechanism_questions` — only
QUESTION sentences spend the budget; a receipt states its decisions
freely) and the wall that RESOLVES instead of hiding
(`default_mechanism_parameters`: first suggestion, else first option,
bound `DERIVED` with the question retired; nothing to bind → demoted to
optional — a mechanism nobody can ask about must never block a run).
Wired at intake (`ModelBackedIntaker`), stated as prompt law on all
three conversational surfaces (chat, intake, the interact window), and
the build receipt now names its decisions as revisable in plain words
(`tests/test_plain_language.py` — the acceptance build spends zero
mechanism questions). Remaining: none for B0; B1 relabels the asks that
survive.

*No mechanism ever reaches a human as a question.*

- A **plain-language gate** on every question surface (intake
  clarification, the chat model's prompts, the interact window's
  prompt): a question to a human must name a value in their world.
  Enforced two ways, belt and braces:
  - **Prompt law**: the chat, intake, and interact system prompts state
    it — formats, APIs, endpoints, schemas, auth are the builder's to
    decide and default; ask only which/what/when/who-shaped value
    questions.
  - **Lexical wall**: a question matching the mechanism lexicon
    (format, API, endpoint, schema, JSON, CSV/PDF-as-a-choice, token,
    header, protocol, …) is not shown — the build proceeds on the
    builder's default and the receipt names it ("I'll read it as a
    spreadsheet; say the word to change that").
- **The build receipt**: every publish already reports in words; it
  gains one line per defaulted mechanism, so the decision is visible
  and revisable ("revise …" already exists) without ever having been a
  question.
- Acceptance: the growth-rig build transcript for a standard goal
  contains ZERO questions matching the mechanism lexicon; a build with
  an unanswerable mechanism choice proceeds on a named default; the
  named default is revisable in one sentence.

### Phase B1 — values through conversation; forms as strict checks

**Status: LANDED (core)** — `Slot` and `ValueInput` carry ``label`` +
``example``; the `IO:` declaration authors them at birth (a
mechanism-flavored input label REFUSES at the gate naming the words
that tripped; a missing label is DECIDED from the humanized name —
the B0 law applied to this gate itself, defaulting over refusing —
`plain_label`). The labels ride the listing's consumes, the market
library, and the assembly preview's ``inputs`` (with type, bounds,
choices, required, example), so every surface asks with the builder's
words. `validate_user_inputs` is the ONE strict check every surface
shares — unknown keys refuse (nothing smuggles an undeclared ask),
type-invalid values refuse in words using the plain label, choices
name their set — wired at the contract-run door (which previously let
user values bypass validation entirely), and the operator console's
route preview renders the manifest verbatim as a typed form
(`tests/test_plain_language.py`, `tests/test_market_assemble.py`).
Remaining: the chat loop asking a node's missing input values one at
a time with these labels (today conversation fills the orchestrator's
clarification asks, which B0 keeps plain; the node-run value chat
rides B3's stored-io seam).

*One ask surface — the declared inputs — fillable by words, checkable
by form.*

- **Plain-word labels at birth**: the `IO:` declaration gains
  `label` and `example` per input/output — authored by the builder in
  the user's language ("Which folder are the invoices in?" /
  "~/Invoices") — refused at the gate when missing or
  mechanism-flavored (the B0 lexicon, reused).
- **Conversation fills the manifest**: a build or run that needs values
  asks them ONE at a time in chat, mapped onto the declared inputs
  (`bind_inputs` stays the single binder); the model never invents an
  input the contract does not declare.
- **The form is the manifest**: where a surface renders a form (the
  app's task pane, the interact window), it renders `inputs_manifest`
  verbatim — typed fields, the plain-word labels, type/range validation
  answered in words ("that needs to be a folder on this machine") — and
  nothing else. A submitted form and a conversation fill the same
  binder; neither can smuggle an undeclared ask.
- Acceptance: every declared input is fillable by conversation alone;
  the form refuses a type-invalid value in words; no label in a
  standard build matches the mechanism lexicon.

### Phase B2 — the function lands in src, transactionally

*The drawer copy is the node; a publish that cannot land it says so.*

- ``src/main.py`` joins the publish TRANSACTION: written before the
  registry row is final; a write refusal (seat wall, missing file
  store) either fails the publish in words or — on hosts genuinely
  without a file store — records the miss loudly (`node.build_failed`
  Grade attention in the operator inbox), never a silent divergence.
- **Fingerprint reconciliation**: the birth commit already fingerprints
  the function; every run compares the drawer copy's fingerprint to
  the version it resolves, repairs a missing drawer copy FROM the
  version (the reverse already half-exists in `_drawer_function`), and
  audits any drift it healed.
- **Revise writes the same home**: the revise path lands its edit in
  `src/main.py` under the same transaction, so file and version can
  never disagree for longer than one run.
- Acceptance: after every publish, the drawer holds `src/main.py`
  fingerprint-equal to the version snapshot; a forced write failure
  surfaces in the inbox; deleting the drawer copy heals from the
  version on the next run, audited.

### Phase B3 — the self-contained node: its data lives with it

*What went in and what came out are the node's own records.*

- Every run lands two files in the node's drawer, under the run's id:
  the resolved inputs it executed with (the staged bindings, scrubbed
  by the corpus discipline — no secrets) and the outputs it emitted.
  The drawer becomes the node's complete story: contract, function,
  and every verified run's io.
- **The standing result**: the newest verified outputs project into a
  "last result" card (M1's projection law — derived, never stored as
  truth), readable in the interact window ("what did you produce
  last?") and by the router.
- Retention is the drawer's own: the frozen-tree store and sweep
  already govern node files; run io ages the same way.
- Acceptance: after a run, the node's drawer holds that run's inputs
  and outputs verbatim (scrubbed); the interact window answers "what
  did you produce last" from the drawer alone; a node exported as a
  bundle carries its own io history.

### Phase B4 — hand-off: outputs flow to the next node

*The route's data movement is durable, inspectable, and reusable.*

- **Run-level provenance on the M1 edges**: when a route moves a value
  from producer to consumer, the `produces`/`consumes` edges gain the
  run's citation — "which value moved along which edge, when" is one
  query, answered with run ids.
- **Standing outputs become offered defaults**: when a node runs alone
  and a declared input's slot is produced by another node on the desk,
  the newest standing output is OFFERED as the default — in words, in
  the conversation or pre-filled in the form, confirmed by the human,
  never silently bound.
- **The chain is visible**: the run detail (and the interact window)
  can show the hand-off — what this node received, from which node's
  run, and what it passed on.
- Acceptance: a two-node route answers "what value moved between you,
  in which run" from the stores; running the downstream node alone
  offers the upstream's standing output as a default and binds it only
  on a yes.

---

## 4. Sequencing and the loop-closure rule

B0 → B1 → B2 → B3 → B4. B0 and B1 are the conversation face (B1 needs
B0's lexicon for its labels); B2 is independent and URGENT (a
reliability defect, not a feature); B3 needs B2 (the drawer must be
trustworthy before it becomes the node's record); B4 rides B3's stored
outputs and M1's edges.

The loop-closure rule, inherited: each phase ships with one test that
drives the full circle through the real doors — a build (or run)
produces the phase's artifact, a LATER surface demonstrably consumes
it, and the consumption changes what a human sees (a question not
asked, a form that checks, a file that heals, a default that offers).

## 5. Metrics (into the standing audition/metrics surfaces)

- `mechanism_questions_per_build` — target 0, by the B0 lexicon, over
  the growth-rig transcripts.
- `value_asks_per_build` — visible, expected small; the honest cost of
  conversation-first.
- `src_divergence_rate` — drawer fingerprint ≠ version at run time;
  target 0 after B2, every heal audited.
- `runs_with_stored_io` — share of verified runs whose node drawer
  holds both files; target 100% after B3.
- `handoff_inspectability` — share of route hand-offs answerable with
  run-cited provenance; trends to 100% after B4.
