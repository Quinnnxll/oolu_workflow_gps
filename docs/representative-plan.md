# The representative: a personal language model for every user

> **Status.** All three phases are built: `src/oolu/representative/`
> plus the `/v1/representative` routes, the settings-pane section, and
> in-thread drafting (✍) in the shell. The trainer worker runs on a GPU
> box with `pip install 'oolu[representative-train]'` and
> `python -m oolu.representative.trainer.worker --data ~/.oolu
> --base-model Qwen/Qwen3-4B-Instruct --vllm http://localhost:8000/v1
> --dpo`; the gateway serves trained voices when
> `OOLU_REPRESENTATIVE_VLLM` names that server. Auto mode is a setting,
> but autonomy is earned per message: enough human verdicts at a high
> sent-as-written rate, plus the gate — commitments always draft. The
> shell carries a Drafts inbox (every pending draft, decided in place)
> and per-peer overrides ("never auto-reply to my boss": mute a peer in
> their thread, or `PUT /v1/representative/peers/{peer}`). Local-first
> training is one command on the user's own machine —
> `oolu representative-train --data .oolu/unified` (with
> `representative-status` to see what's due) — messages and adapters
> never leave it.

A working plan for **representative mode** — the toggle in the chat window
that lets OoLu draft and (eventually) send replies that sound like *you*,
grounded in what you actually know and have said. This document fixes the
architecture, sketches the module layout, and lays out the build in three
phases, each shippable on its own.

## The three decisions everything else follows from

**1. Per-user LoRA adapters on one shared base — never a full model per user.**
A full 3B/7B per user is 6–15 GB of weights, hours of multi-GPU training,
and one serving instance per user: impossible economics. A LoRA adapter
(rank 16–32) is 20–100 MB, trains in ~10–20 minutes of QLoRA on a single
24 GB GPU, and a single vLLM instance serves *all* users' adapters over one
base model (`vllm serve <base> --enable-lora`, adapters loaded at runtime
and selected per request by the `model` field — e.g. `user-4f2a-v7`). Since
`config/models.yaml` already routes every tier through an OpenAI-compatible
endpoint, the representative is *just another routing tier* whose model
string happens to be per-user.

Base model: **Qwen3-4B-Instruct** (Apache 2.0 — no license terms to pass
through to users). Move to 7–8B only if drafts show *reasoning* failures
(misread multi-party threads, dropped constraints), never for style
failures.

**2. Style lives in the weights; knowledge lives in retrieval.**
Fine-tuning captures voice — sentence length, punctuation, emoji habits,
hedges, pet phrases. It is *bad* at storing facts: a 3B model fine-tuned on
someone's messages will confidently invent their opinions and commitments.
So a representative reply is a sandwich:

- **LoRA adapter** — how the user talks (trained on their outbound messages);
- **retrieval over the user's own history** — what the user knows: the k
  most similar past (inbound → the user's actual reply) exchanges plus
  standing profile facts, injected into the prompt. This is the fuzzy,
  generative generalization of the exact-match `learned_replies` table that
  already exists in `src/oolu/replies/learned.py`;
- **a persona card** in the system prompt — name, role, standing facts, and
  hard rules ("never agree to meetings or spend money — draft for approval").

**3. Draft-first, and the drafts are the flywheel.**
Representative mode starts as *drafts into the approvals inbox*, never
auto-send. Every draft outcome is labeled training signal:

- sent unedited → positive example (and the accept-rate is the product metric);
- edited before sending → a free (rejected, chosen) preference pair for a
  later DPO pass — the data nobody else has;
- discarded → negative signal for the confidence gate.

Auto-send arrives only in Phase 2, gated on per-user accept-rate and a
confidence gate that always routes commitments (money, scheduling, promises)
back to the inbox.

## What the repo already gives us

The pattern here mirrors the coming-alive review: most seams already exist.

| Need | Already built | Where |
|---|---|---|
| A place to plug a generative replier | `ReplyFallback` port — "port for a future model or human-review fallback" | `src/oolu/replies/engine.py` |
| Exact-match precedent + pairing state | `learned_replies` / `pending_messages` tables | `src/oolu/replies/learned.py` |
| Per-call model override in chat | `ChatAssistant.respond(model=...)` outranks the constructor's model | `src/oolu/chat.py` |
| PII/secret scrubbing before anything trains | scrubbing pipeline | `src/oolu/knowledge/scrubbing.py` |
| Durable background jobs with lease/heartbeat | `DurableTaskQueue` | `src/oolu/durable/queue.py` |
| Adapter artifact storage, local or S3 | `FilesystemArtifactStore`, S3 twin | `src/oolu/durable/artifacts.py`, `artifacts_s3.py` |
| Append-only SQLite schema history | `Migration` / `migrate` | `src/oolu/persistence/migrations.py` |
| Draft review surface | approvals inbox | gateway + desktop shell |
| OpenAI-compatible serving assumption | two-tier vLLM routing | `config/models.yaml`, `src/oolu/routing/matrix.py` |

New code is therefore mostly *one package* plus thin wiring.

## Module layout — `src/oolu/representative/`

Follows the established "port + deterministic default + optional adapter"
pattern (ADR-0003): every heavy dependency (embeddings, training, vLLM
adapter management) sits behind a Protocol with a model-free default, so
the base install stays light and everything is testable without a GPU.

```
src/oolu/representative/
    __init__.py       # public surface: RepresentativeService + models
    models.py         # frozen Pydantic: PersonaCard, AdapterVersion,
                      #   TrainingExample, Draft, DraftOutcome,
                      #   RepresentativeSettings, GateVerdict
    store.py          # SQLite via persistence.Migration: mode toggles,
                      #   adapter registry, draft outcomes  (schema below)
    memory.py         # retrieval over the user's past exchanges.
                      #   Port: ExchangeMemory. Default: SQLite FTS5 (BM25,
                      #   zero deps). Adapter: embedding index later.
    persona.py        # persona-card builder + system-prompt assembly:
                      #   card + retrieved few-shot exchanges + hard rules
    gate.py           # confidence gate: pure functions. Hard rules first
                      #   (commitment/money/scheduling lexicons → always
                      #   draft), then retrieval-similarity threshold,
                      #   then per-user accept-rate threshold
    engine.py         # RepresentativeEngine: retrieve → assemble prompt →
                      #   generate (ChatModel port, per-user model string) →
                      #   gate → Draft or AutoReply. Implements the
                      #   replies.engine.ReplyFallback protocol — this is
                      #   the wire-in point, not a new pipeline.
    dataset.py        # SFT dataset builder: window the user's outbound
                      #   messages into (context → reply) pairs, scrub via
                      #   knowledge.scrubbing, dedupe near-identicals,
                      #   downweight one-liners (keep them — they carry
                      #   style), emit chat-format JSONL
    serving.py        # AdapterServer port: register/evict adapters on the
                      #   inference server; model-id naming (user-{id}-v{n}).
                      #   Default: NoopAdapterServer (Phase 0 has no
                      #   adapters). Adapter: VllmAdapterServer speaking
                      #   /v1/load_lora_adapter.
    trainer/
        __init__.py
        jobs.py       # refresh policy: enqueue on durable TaskQueue when a
                      #   user crosses +N new messages or T days since the
                      #   last adapter; cold-start floor (~500 messages)
        worker.py     # leases jobs, drives dataset → sft → artifact →
                      #   registry row → serving registration. Heartbeats.
        sft.py        # QLoRA SFT runner: subprocess around a pinned
                      #   trl/unsloth script; hyperparams derived from
                      #   dataset size; retrain FROM BASE each refresh on a
                      #   rolling window (no continual updates — sidesteps
                      #   catastrophic forgetting)
        dpo.py        # Phase 2: preference pass over (edited vs sent)
                      #   draft pairs, stacked on the SFT adapter
```

Wiring outside the package (thin, mostly one call each):

- **`replies/runner.py` / channels** — pass a `RepresentativeEngine` as the
  `ReplyFallback` where the user has the mode on; the deterministic engine
  still wins first, exactly as today.
- **`gateway/app.py`** — routes: `PUT /v1/representative/mode` (off | draft |
  auto), `GET /v1/representative/drafts`, `POST /v1/representative/drafts/{id}`
  (send / edit-and-send / discard — each records a `DraftOutcome`),
  `GET /v1/representative/status` (adapter version, message count, accept
  rate). Drafts also surface through the existing approvals inbox.
- **`config.py` / `models.yaml`** — a `representative:` block: base model,
  api_base, sampling caps, gate thresholds, trainer backend
  (off | subprocess | docker), artifact root. Deserializes into
  `RepresentativeSettings` like every other section.
- **`pyproject.toml`** — training deps live in an optional extra
  (`oolu[representative-train]`); the serving/drafting path adds no
  dependencies at all.

### Data model (one SQLite store, `~/.oolu/representative.db`)

```
representative_settings(user_id PK, mode, gate_similarity_min,
                        auto_send_enabled, updated_at)

adapter_versions(user_id, version, base_model, artifact_ref, status,
                 trained_at, message_count, holdout_ppl,
                 PRIMARY KEY (user_id, version))
                 -- artifact_ref points into the ArtifactStore;
                 -- exactly one row per user has status='active'

draft_outcomes(draft_id PK, user_id, conversation_id, context_digest,
               generated_text, final_text, outcome,           -- sent |
               adapter_version, gate_score, created_at,       -- edited |
               decided_at)                                    -- discarded
```

`draft_outcomes` is simultaneously the audit log, the metric source
(accept rate), and the Phase-2 DPO dataset. Deleting a user's
representative = delete their rows + their artifacts: one per-user
artifact chain makes right-to-be-forgotten a single operation.

### Runtime flow (representative mode on)

```
inbound message
  → DeterministicReplyEngine (rules, learned exact matches — unchanged)
  → RepresentativeEngine (as ReplyFallback):
      memory.recall(user, message)            # k similar past exchanges
      persona.assemble(card, exchanges, ...)  # system prompt + few-shot
      model.chat(model="user-{id}-v{n}")      # or base model in Phase 0
      gate.judge(reply, similarity, rules)
        → PASS + auto enabled  → send, record outcome
        → otherwise            → Draft into approvals inbox
```

## The register: one voice, many registers (who you're talking to)

People don't talk to their boss and their brother the same way, and a
per-account adapter trained on a flattened corpus learns only the AVERAGE
voice. The wrong fix is per-peer adapters — most threads never reach the
cold-start floor, and the fleet explodes. The right fix is
**conditioning**: one adapter that learns many registers, told at every
step who the reply addresses.

The peer rides the whole pipeline:

- **Memory** — every exchange records who it was with (`exchanges.peer`,
  migration 4); recall boosts same-peer memories over equally similar
  cross-peer ones (`CROSS_PEER_DISCOUNT`).
- **The prompt** — "The reply is TO {peer}" plus few-shot examples labeled
  "When {peer} said …" for same-peer history, anonymous otherwise.
- **Training** — SFT examples open with a `Replying to {peer}.` system
  line and DPO prompts carry the same prefix, so the adapter learns
  peer-conditioned registers from ONE corpus: peers with history get
  their own voice, strangers fall back to the average one, and a new
  peer needs no retraining — just their name in the prompt.

## The sweep: the busy person's pass

`POST /v1/representative/sweep` drafts a reply for every friend whose
message is waiting, so the user only filters — send, edit, discard, from
the OoLu window's inline strip (the ✍ toggle at the top right) or the
Drafts inbox. Idempotent per message: a message that ever had a draft is
never drafted again, whatever the user decided, so polling costs nothing
until someone actually says something new.

## Build phases

### Phase 0 — the representative without training (ship first)

No GPU, no new dependencies, no adapters: base model + persona card +
retrieved few-shot of the user's real replies, **drafts only**. This is
most of the perceived product, and it starts the data flywheel on day one.

Build: `models.py`, `store.py`, `memory.py` (FTS5 default), `persona.py`,
`gate.py` (hard rules + similarity only), `engine.py`, gateway routes,
`ReplyFallback` wiring, the chat-window toggle. `serving.py` ships as the
Noop default.

Accept when: a user with ≥50 messages of history toggles draft mode and
gets drafts in their inbox that quote-match their retrieval context; every
send/edit/discard lands a `draft_outcomes` row; a user with the mode off is
byte-for-byte unaffected. Tests are pure-Python throughout (fake
`ChatModel`, in-memory SQLite) — same style as the existing replies tests.

### Phase 1 — the adapter pipeline (the model becomes theirs)

Build: `dataset.py`, `trainer/` (jobs, worker, sft), `serving.py`'s
`VllmAdapterServer`, the adapter registry lifecycle
(pending → trained → active → retired), and routing: when a user has an
active adapter, `engine.py` swaps the model string from the shared base to
`user-{id}-v{n}`.

Operational shape: one trainer worker leasing from the durable queue; QLoRA
on Qwen3-4B, rank 16–32, 2–3 epochs over a rolling window; retrain from
base every refresh (+200 new messages or 7 days); cold-start floor ~500
messages — below it Phase 0 behavior simply continues. Unit economics:
~10–20 min on a spot A10G/L4 ≈ $0.10–0.30 per user per refresh.

Accept when: the worker takes a user from queue → JSONL → adapter artifact
→ registry row → live vLLM registration with no manual step; held-out
perplexity of the adapter beats the base on that user's messages; a failed
job retries via the queue without wedging the user (they keep Phase-0
drafts); deleting a user removes rows + artifacts + evicts the served
adapter.

### Phase 2 — preference tuning and earned autonomy

Build: `trainer/dpo.py` (preference pass over ≥~300 edited-vs-sent pairs,
stacked on the SFT adapter), auto-send behind the full gate (per-user
accept rate over trailing window ≥ threshold AND similarity pass AND no
commitment lexicon hit — commitments *always* draft), and the local-first
option: the same trainer behind `backend: subprocess` on a user's own RTX
card, so desktop users can train their representative without their
messages ever leaving the machine (`oolu representative train`).

Accept when: DPO users' accept rate measurably beats their SFT-only
baseline; zero auto-sent messages containing commitment-lexicon hits in the
audit log; a desktop user produces a working adapter fully offline.

## Metrics

- **Accept rate** (sent-unedited / drafts decided) — *the* product metric,
  per user and cohort, straight from `draft_outcomes`.
- Holdout perplexity of adapter vs base on the user's own messages (stored
  per `adapter_versions` row) — the offline training-quality gate.
- Gate precision: fraction of auto-sends (Phase 2) later flagged/apologized
  for. Target: zero commitment escapes, ever.

## Risks and open questions

- **Parody overfitting** on small corpora — mitigated by the cold-start
  floor, downweighting one-liners, and the holdout-perplexity gate before an
  adapter goes active.
- **Adapter fleet memory** on the serving box — vLLM pages LoRAs
  CPU↔GPU; `--max-loras` bounds the hot set. Revisit LoRAX/S-LoRA only if
  concurrent-adapter count actually hurts latency.
- **Base-model upgrades invalidate every adapter** (LoRA is base-specific).
  The registry's `base_model` column makes this explicit: an upgrade is a
  fleet-wide re-enqueue, budgeted at ~$0.30/user.
- **Consent scope** — train only on the user's *outbound* text; the other
  party's words appear only as prompt context, never as completions. Needs
  a line in the privacy policy before Phase 1 ships hosted.
- Open: where the trainer GPU lives for hosted (a dedicated droplet-class
  GPU box vs. on-demand spot); whether drafts should render in the chat
  window inline as well as the inbox. Neither blocks Phase 0.
