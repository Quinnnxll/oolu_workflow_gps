# Model seats — one discipline for every model consultation

## The problem this solves

The platform consults language models from many places: the conversation
(`chat.py`), the node-function author (`author_node_function`), the script
synthesizer and repairer (`runtime/script_node.py`), the planning intaker
and route chooser, the post-retry rebuilder (`orchestrator/rebuild.py`),
and the representative. Any of these may be answered by a *different*
model on any given day — the tenant's pasted Anthropic key, an OpenAI
key, the hosted subscription brain, or a local model — and the model in
the chair can change between two calls without anything else changing.

That interchangeability is a feature. It becomes a hazard only when the
things that must NOT change with the model are defined implicitly, per
call site: which files the call may touch, which tools it holds, what it
is responsible for producing, and which consent, meter, and audit cover
it. The concrete failure that motivated this design: node building
"succeeded" — the model planned the function, the node was created — but
**no source file ever appeared**, because no call site owned the duty of
materializing the model's output into the node's drawer. The function
lived only inside the version's JSON snapshot; the drawer's `src/` folder
was read at run time but never written at build time. Nothing enforced
the seat's full job description, so part of the job silently didn't
exist.

## The design

A **seat** (`oolu/seats.py`) is the standing definition of one model
call site — everything that stays constant when the model changes:

| Field | Question it answers | Example (`node.build`) |
| --- | --- | --- |
| `purpose` | What do the meter, usage books, and audit log book this under? | `node.build` |
| `charge` | What is this call responsible for producing? | judge the goal executable, write the function |
| `reads` / `writes` | Which drawer paths may it touch? | reads `src/`, `lessons/`; writes `src/` |
| `hands` | Which tools may the call site expose to it? | none (it writes code, it doesn't act) |
| `consent_key` | Which settings switch must a door have checked? | `account.autobuild_consent` |
| `audited` | Do its acts land on the audit log? | yes |

The registry (`SEATS`) is the whole table — one place to read who sits
where, and one vocabulary shared with the model router's metering
purposes (`chat.turn`, `plan.intake`, `plan.route`, `plan.synthesize`,
`plan.rebuild`, …), so governance and accounting agree on names.

Two mechanisms enforce the parts that are enforceable today:

- **`DeskFiles`** — the uniform file hand. A seated call writes files
  only through it; it is bound to one node's drawer, refuses any path
  outside the seat's declared scopes (and any path that tries to escape
  the drawer), records every write, and will not even open a
  consent-gated seat without the caller's attestation that the consent
  door was passed. Whatever model sits down, its reach is the seat's.
- **The audit event** — every seated write lands on the hash-chained
  audit log as `model.seat` (purpose, tenant, principal, node, files
  written), so "which model call put this file here" is answerable from
  storage alone.

Governance is therefore a chain, and each link already has an owner:

```text
consent (settings switch / explicit user yes, checked at the DOOR)
  -> seat (scope of files + hands + charge, enforced at the SEAT)
    -> meter (tokens + cost booked under the seat's purpose, in the ROUTER)
      -> audit (the act on the hash-chained log, appended by the GATEWAY)
        -> verification (the output re-earns trust by execution, in the RUNTIME)
```

The last link matters most: a seat never *trusts* a model. Everything a
seated model writes still passes the same walls as any other code — the
static safety screen, the network-severed sandbox, verified-by-execution
before caching, human confirmation for model-written code. Seats bound
what a call can *reach*; verification decides what its output is *worth*.

## The node's function lives in its drawer

The bug fix that anchors this architecture: building a node now
**materializes the authored function as `src/main.py`** in the node's
own drawer, written through the `node.build` seat. From then on the file
is the function's home:

- runs resolve the function drawer-first — `src/main.py`, when present,
  *is* the script (the version's JSON snapshot is the fallback for a
  deleted drawer copy);
- editing the file edits the node — the script cache keys on the
  function's own fingerprint, so an edit takes effect on its next run
  and still re-earns trust by verified execution;
- other `src/` files stage into the sandbox beside it, so a node is a
  small program, not one string.

The in-run repair loop keeps its discipline and completes its circle.
Mid-run, `node.repair`'s runtime half still touches no files: it edits,
verifies by execution, and caches — under the failing code's key (so
the exact broken script heals on replay) and under the healed code's
own fingerprint (so the promoted file's runs hit a warm cache). The
healed code rides the outcome evidence (`repaired_script`), and **after
the run completes** the gateway performs the promotion the seat
reserved: `src/main.py` is rewritten with the healed code through the
`node.repair` seat — scope-checked, audited as `model.seat`, exactly
once per run, and only for the node-function action itself, never for
some other script the route carried. A failed repair promotes nothing.

## Seated today / to be seated

| Call site | Seat | Status |
| --- | --- | --- |
| Node-function author (build doors) | `node.build` | **seated** — writes `src/main.py` via `DeskFiles`, audited |
| Chat conversation | `chat.turn` | declared — tools already walled by `ChatTools` (tenant/owner walls); listed hands are the contract |
| Script synthesizer | `plan.synthesize` | declared — no file access by design; sandbox-verified |
| In-run repair | `node.repair` | **seated** — file-silent mid-run; the gateway promotes the healed code into `src/main.py` after a COMPLETED run, via `DeskFiles`, audited |
| Planning intake / route | `plan.intake` / `plan.route` | declared — read-only by design |
| LLM rebuild | `plan.rebuild` | declared — consent enforced in `LLMRouteRebuilder` |
| Representative drafts | `rep.draft` | declared — drafts only; the human's send is the send |

"Declared" means the seat's terms are in the registry and already hold
in practice through each site's existing walls; migrating a site to
*seated* means routing its file writes through `DeskFiles` and its acts
onto the audit log, exactly as `node.build` now does. New model call
sites MUST start from a seat: pick the purpose, declare the four
answers, and only then write the prompt.
