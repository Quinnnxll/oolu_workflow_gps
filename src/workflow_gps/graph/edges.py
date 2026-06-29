r"""Conditional routing — the decisions that turn nodes into a navigation cycle.

Pure ``state -> next-node`` functions, no LangGraph import, so every branch is unit
testable. ``builder.py`` binds these to the graph's conditional edges.

THE TOPOLOGY
------------
    START -> plan -> synthesize ->? execute -> classify ->? finalize        (success)
                          |  \                       \
                          |   -> recalculate ->? ------+--> synthesize       (rewrite)
                          |                            +--> execute          (dep heal: re-run same script)
                          \-> recalculate / halt                            (no code produced)
    any unrecoverable or exhausted failure -> halt                          (surface)

Three ideas are encoded here:

  * Recalculable vs halting. A failure whose ``ErrorClass`` is not recalculable
    (auth, permission, network-denied, install-failed, resource) goes straight to
    ``halt`` — looping on it is the self-harm we designed against.

  * A semantic loop ceiling. ``max_recalcs`` halts cleanly with an "exhausted"
    reason BEFORE LangGraph's hard ``recursion_limit`` backstop trips with an opaque
    error. The recursion_limit stays as a last-resort guard; this is the graceful one.

  * Dep-heal re-runs, code-errors re-synthesize. A first ``MISSING_DEPENDENCY`` routes
    back to ``execute`` to re-run the SAME script with the dependency now installed —
    no point paying for re-synthesis when the code was fine. A *repeated* missing-dep
    (the resolver's guess isn't working) routes to ``synthesize`` instead, letting the
    model rewrite the import and giving the escalation ladder a chance to engage.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..models import ErrorClass, GraphState, GraphStatus

# Node names — the single source of truth shared with builder.py.
NODE_PLAN = "plan"
NODE_SYNTHESIZE = "synthesize"
NODE_EXECUTE = "execute"
NODE_CLASSIFY = "classify"
NODE_RECALCULATE = "recalculate"
NODE_FINALIZE = "finalize"
NODE_HALT = "halt"

# Destination sets, for builder.py to construct LangGraph path maps explicitly.
AFTER_SYNTHESIS_DESTS = frozenset({NODE_EXECUTE, NODE_RECALCULATE, NODE_HALT})
AFTER_CLASSIFY_DESTS = frozenset({NODE_FINALIZE, NODE_RECALCULATE, NODE_HALT})
AFTER_RECALCULATE_DESTS = frozenset({NODE_EXECUTE, NODE_SYNTHESIZE})


class EdgePolicy(BaseModel):
    """Loop-control thresholds for routing. Distinct from the routing matrix's
    escalation thresholds: this governs when to STOP, the matrix governs tier."""

    model_config = ConfigDict(frozen=True)

    max_recalcs: int = Field(
        default=6, description="Total recalc cycles before halting as 'exhausted'."
    )
    dep_heal_rut_threshold: int = Field(
        default=2,
        description="Repeated identical missing-dep failures past which we re-synthesize "
        "instead of re-installing.",
    )


class EdgeRouter:
    """Holds the policy and exposes the conditional-edge functions LangGraph calls."""

    def __init__(self, policy: EdgePolicy | None = None):
        self._policy = policy or EdgePolicy()

    @property
    def policy(self) -> EdgePolicy:
        return self._policy

    # --- after synthesize: did we get runnable code? ------------------ #
    def after_synthesis(self, state: GraphState) -> str:
        if state.status is GraphStatus.FAILED:
            return NODE_HALT  # a node declared a fatal infrastructure failure
        if state.plan is not None and state.plan.script:
            return NODE_EXECUTE
        # The synthesize node records a SYNTHESIS_FAILED error when it gets no code,
        # so this funnels into the same failure routing as everything else.
        return self._route_on_failure(state)

    # --- after classify: success or another failure? ------------------ #
    def after_classify(self, state: GraphState) -> str:
        if state.status is GraphStatus.FAILED:
            return NODE_HALT  # backend infrastructure failure, surfaced by the execute node
        if state.last_result is not None and state.last_result.succeeded:
            return NODE_FINALIZE
        return self._route_on_failure(state)

    # --- after recalculate: rewrite the code, or just re-run it? ------- #
    def after_recalculate(self, state: GraphState) -> str:
        err = state.latest_error
        is_missing_dep = (
            err is not None and err.error_class is ErrorClass.MISSING_DEPENDENCY
        )
        has_queued_dep = state.plan is not None and bool(
            state.plan.required_dependencies
        )
        not_a_rut = state.repeated_failure_count() < self._policy.dep_heal_rut_threshold

        if is_missing_dep and has_queued_dep and not_a_rut:
            return NODE_EXECUTE  # re-run the same script; the import will now resolve
        return NODE_SYNTHESIZE  # need fresh code (or the dep approach keeps failing)

    # --- the shared stop/continue decision ---------------------------- #
    def _route_on_failure(self, state: GraphState) -> str:
        err = state.latest_error
        if err is None:
            # Not a success yet no error captured — defensive halt rather than spin.
            return NODE_HALT
        if not err.error_class.is_recalculable:
            return NODE_HALT  # halting class: surface it
        if state.recalc_count >= self._policy.max_recalcs:
            return NODE_HALT  # budget exhausted
        return NODE_RECALCULATE
