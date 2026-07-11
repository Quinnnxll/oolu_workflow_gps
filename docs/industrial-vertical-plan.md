# The Industrial Vertical: from task runner to engineering truth

This plan maps the "Industrial Intelligence System for CAD, Engineering,
and 3D Production" build spec onto what OoLu already is, names the real
gaps, and orders the work. The spec's target architecture is:

```text
one shared sparse intelligence
+ internal expert routing
+ external project memory
+ deterministic engineering tools
+ transactional verification
+ small distilled workers
```

## What OoLu already is

Much of the spec's control philosophy exists here under different names:

| Spec concept | OoLu today |
|---|---|
| "Models propose, the kernel commits" | Contract holds + approval flow; model-written code re-earns confirmation |
| Semantic actions, never UI clicks | `ActionEvent` adapter/operation/parameters |
| Deterministic tool runtime + adapter contract | ActionExecutors (http/script/cli), two-phase install-then-sever sandbox |
| Ownership and permissions | Node accounts, authority levels, tenant walls, egress grants |
| Training records | Trace store's raw run log + corpus export, with failure evidence |
| "Model size selected by benchmark evidence" | `orchestrator/replay.py` and its `earns_its_cost` gate |
| Distilled workers / small models | The `ProposalModel` socket + `TinyTransformerProposalModel` + the audition |
| Hash-chained audit, versioned writes | `DurableAuditLog`, versioned durable schema, artifact checksums |

And OoLu carries what the spec lacks: a consent economy (egress grants,
audit regimes, autodev opt-out), accountability (a human answers for
every node), and real economics (verified-success payment, lineage
shares). The spec has permissions; OoLu has governance.

## The gaps, in order of leverage

1. **The Global Project Graph.** OoLu's state is runs + files. The
   spec's state is a revisioned, typed truth store: objects with
   `{id, type, revision, status, owner, parameters, relations,
   constraints, evidence, provenance}`. Nothing here holds "the
   approved design, at revision 218, with the evidence that verified
   it."
2. **The transactional patch kernel.** Holds gate whole contracts; the
   spec's kernel gates structured patches against a base revision, with
   stale-revision rejection, dependency-selected checks, and
   commit/rollback.
3. **Postconditions and the verification ladder.** Actions don't declare
   expected postconditions, and verification means "it ran," not "the
   state is what we said it would be" — with no
   component → subsystem → program ladder above it.
4. **Regression protection + critics.** Once a hard constraint passes,
   nothing protects it from a later change; and there is no critic role
   (findings-with-evidence against a proposal, distinct from rewrites).
5. **Adapter contract completeness.** Executors lack
   `observe_state`/`rollback`/`state_delta`.
6. **A real engineering hand.** No CadQuery/Blender/FEA adapters — though
   the blob file store and severed sandbox are ready for them.

## The model question, settled by architecture

The spec's own training strategy (§21, §29) defers the 300B–1T sparse
foundation model behind evidence gates: start with a frontier API
orchestrator, small local workers, adapters, an external graph, and
deterministic tools. OoLu's answer is therefore structural, not
speculative:

- **Lightweight domain adapters generalize as NODES.** A node is a
  better adapter than the spec's: it carries identity (one goal, one
  history), a responsible human, consent, verification evidence, and
  economics. The spec's "hundreds to thousands of adapters" is the
  nodeplace.
- **The small transformer is an organ, not a rival.** The replay
  harness measured its honest scope — thin-history ties and cold
  starts; counts outrank it; it can never override verified evidence.
- **Global reasoning is rented, not owned.** Hard cross-domain
  decisions go to a frontier model through the existing router —
  per decision, metered, budget-gated. Self-hosting a sparse model is
  a utilization question for later.
- **Every seat has an audition.** Any occupant of a model seat must
  beat the incumbent in the replay harness to earn its inference cost.
  The rule scales from the tiny transformer to a 700B MoE. When Level
  B/C benchmarks show the rented orchestrator failing where a trained
  model would succeed, that is the moment a foundation model earns
  consideration — not before.

## The plan

1. **Project Graph store** — durable, typed, revisioned objects with
   relations/constraints/evidence/provenance; per-project ownership;
   path-scoped read/write grants riding the same consent pattern as
   `network_hosts`. *(built: `src/oolu/graph/`)*
2. **Transactional patch kernel** — structured patches with per-object
   base revisions and a required reason; schema → permission → revision
   validation; hard-constraint protection; commit into the hash-chained
   audit or reject in words. *(built: `src/oolu/graph/kernel.py`)*
3. **Postconditions + observation** — actions declare expected
   postconditions, executors return observed state, the evaluator
   compares; failures feed the existing failure-evidence path.
   *(built: `predicates.py`, `Postcondition`/`verify_postconditions`
   in `skills/models.py`, judged at both route runners; the kernel's
   `append` op files verified observations as graph evidence)*
4. **Critic findings + regression protection breadth** — findings as
   typed evidence-backed objects; kernel-enforced protection of
   previously-passed hard constraints across dependency edges.
   *(built: `build_finding` — findings live under `issues/{target
   path}`, so critics get territory without the design; the door
   refuses findings without evidence; open BLOCKING findings gate
   status advancement to approved/released. Cross-object dependency
   breadth waits for real interfaces in step 5.)*
5. **First engineering hand: a CadQuery node** — parametric parts,
   STEP/STL into the blob store, mass properties and geometry validity
   as postconditions, running in the severed sandbox. This is where
   "adapter generalizes as node" is proven on a real domain.
6. **Level B benchmark** — a subsystem-change task through
   graph → kernel → adapter → verification, comparing router tiers
   under identical tools and budgets, so the foundation-model question
   stays permanently evidence-gated.

Steps 1–2 are the spine (a graph without the kernel is a database; a
kernel without the graph has nothing to guard). Blender/SOLIDWORKS come
after the CadQuery vertical proves the loop end to end.
