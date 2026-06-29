# ADR-0002: Unified orchestrator run state

- Status: Accepted
- Date: 2026-06-29

## Context

Workflow-GPS grew as independent vertical slices: a self-healing code-synthesis
graph, a portable skill core with a Requirement and Constraint Compiler, a
safety-gated skill runtime, a knowledge layer, and reply channels. Each slice is
contract-tested in isolation, but there is no single runtime that drives a task
from intake all the way through execution, monitoring, and learning while a human
stays in control at the right moments.

The `codex/unified-orchestrator` branch connects those slices into one end-to-end
flow:

```text
intake
-> guided clarification
-> semantic grounding
-> route optimization
-> human-control evaluation
-> confirmation or approval wait
-> execution
-> outcome monitoring
-> automatic recovery or incident escalation
-> finalization and route learning
```

Two properties are non-negotiable for this runtime:

1. A workflow can pause for clarification, confirmation, approval, or an incident,
   and resume later — possibly in a different process — without losing state.
2. No code path reaches execution without passing every preflight control
   (requirements ready, route not excluded, human control satisfied, and every
   action's capability available).

## Decision

Define **one versioned, serializable run state** — `RunState` — as the single
source of truth for a workflow, and drive it with a deterministic phase machine
(`WorkflowOrchestrator`).

### Run state

- `RunState` carries `schema_version` (`ORCHESTRATOR_SCHEMA_VERSION`) and is a
  plain Pydantic model that round-trips losslessly through
  `model_dump_json()` / `model_validate_json()`. Persistence and transport never
  see anything but this serialized form.
- Every phase records its output as a typed sub-record on the state
  (`RequirementBrief`, `CompilationResult`, `SemanticGrounding`, `RoutePlan`,
  `HumanControlDecision`, `ConfirmationRecord`, `ApprovalRecord`s,
  `ExecutionRecord`, `MonitorReport`, `Incident`s, `FeedbackRecord`).
- An append-only `history` of `PhaseTransition`s makes the path auditable.
- A nullable `pause: PauseToken` is the *only* signal that the runtime is waiting
  on a human. While set, the machine does not advance.

### Phase machine

- `step()` executes exactly the phase named by `state.phase`. Phases advance only
  by completing their own gate, so there is no API that jumps to execution.
- Pause/resume is explicit: a phase that needs human input sets `pause` and
  returns; `resume()` validates the supplied `ResumeInput` against the pause kind,
  folds the input into the state, clears the pause, and lets the machine continue.
- Supported pauses are clarification, confirmation, approval, and incident.

### Preflight is re-derived, not trusted

`EXECUTION` calls a single hard guard, `assert_execution_preflight()`, that
re-derives every gate from the recorded sub-records *every time it runs* —
including on a post-incident retry. A hand-constructed state that claims
`phase == EXECUTION` without recorded approvals, with an excluded route, or with
an action whose capability the executor lacks, raises `PreflightError` instead of
executing. Authority therefore comes from recorded decisions, never from the
phase label or caller text.

### Deployment-neutral ports

Each stage is a `Protocol` port (intake, grounding, route optimization, human
control, execution, monitoring, recovery, feedback). The branch ships
deterministic, fully-offline default adapters that compose the existing skill
core (the Requirement and Constraint Compiler, the `ActionExecutor` contract, and
`ExecutionOutcome`s). Durable, networked, or model-backed adapters can replace
them later without touching the run state or the phase machine.

## Alternatives considered

### Extend the LangGraph `GraphState` instead of a new run state

Rejected. `GraphState` is tuned for the cache-stable-prefix code-synthesis loop
and is not a general workflow record. Overloading it would entangle prompt-cache
concerns with human-control and approval state.

### Enforce preflight once, at the phase boundary

Rejected. A single boundary check can be bypassed by resuming, retrying, or
constructing a state directly. Re-deriving the guard inside the execution phase is
the only way to make "no execution without controls" structurally true.

### Keep slices independent and orchestrate from the CLI

Rejected. Orchestration logic in the CLI is untestable as a unit and cannot offer
durable pause/resume. The run state and machine belong in the library.

## Consequences

- One serializable object describes a workflow at any instant; pause/resume and
  durability reduce to storing and reloading it.
- Schema migrations apply to the run state from the first persisted workflow
  (versioned via the shared `persistence` runner).
- Execution safety is a property of the state, not of any single call site.
- More records and ports exist before the durable runtime and identity branches,
  but those branches can swap adapters without reshaping the core.

## Migration impact

The orchestrator run-state store is new and versioned through the shared
`PRAGMA user_version` migration runner. The existing script cache, learned-reply,
knowledge, and skill schemas are unchanged.
