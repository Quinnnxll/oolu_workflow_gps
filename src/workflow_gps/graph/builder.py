"""Graph assembly — the compiled navigation engine and its public entry point.

Wires the seven nodes and the conditional edges into a LangGraph ``StateGraph``,
compiles it with a checkpointer, and exposes ``WorkflowGPS.run(intent)``.

The topology (see ``edges.py`` for the routing rationale):

    START -> plan -> synthesize -->(execute | recalculate | halt)
                     execute -> classify -->(finalize | recalculate | halt)
                     recalculate -->(execute | synthesize)
                     finalize -> END
                     halt -> END

Two safety layers stack here: the semantic ceiling in ``EdgePolicy`` (halts cleanly
as "exhausted") sits inside LangGraph's hard ``recursion_limit`` backstop, which is
set comfortably above the semantic ceiling so the graceful halt always wins.
"""

from __future__ import annotations

import logging
import uuid

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from ..models import GraphState, GraphStatus, ModelTier
from ..routing.gateway import Gateway
from ..routing.matrix import RoutingMatrix
from ..routing.prompting import PromptAssembler
from ..runtime.backend import ExecutionBackend, ResourceLimits
from ..telemetry import RunMetrics
from .edges import (
    AFTER_CLASSIFY_DESTS,
    AFTER_RECALCULATE_DESTS,
    AFTER_SYNTHESIS_DESTS,
    NODE_CLASSIFY,
    NODE_EXECUTE,
    NODE_FINALIZE,
    NODE_HALT,
    NODE_PLAN,
    NODE_RECALCULATE,
    NODE_SYNTHESIZE,
    EdgePolicy,
    EdgeRouter,
)
from .nodes import GraphNodes

logger = logging.getLogger(__name__)


class WorkflowResult(BaseModel):
    """The outcome of one navigation, summarized from the final graph state."""

    model_config = ConfigDict(frozen=True)

    success: bool
    answer: dict | None = None
    failure_reason: str | None = None
    status: GraphStatus
    final_tier: ModelTier
    recalc_count: int
    tier_escalations: int
    attempts: int
    metrics: RunMetrics = Field(default_factory=RunMetrics)


class WorkflowGPS:
    """The assembled engine. Construct once with a gateway + backend, then ``run``."""

    def __init__(
        self,
        *,
        gateway: Gateway,
        backend: ExecutionBackend,
        matrix: RoutingMatrix | None = None,
        assembler: PromptAssembler | None = None,
        edge_policy: EdgePolicy | None = None,
        limits: ResourceLimits | None = None,
        pinned_index_url: str | None = None,
        hint_provider=None,
        knowledge=None,
        recursion_limit: int | None = None,
        checkpointer=None,
    ):
        # hint_provider retained for backward compatibility; knowledge supersedes it.
        self._nodes = GraphNodes(
            gateway=gateway, backend=backend, matrix=matrix, assembler=assembler,
            limits=limits, pinned_index_url=pinned_index_url, knowledge=knowledge,
        )
        self._router = EdgeRouter(edge_policy)
        # Backstop comfortably above the semantic ceiling (~4 supersteps per recalc).
        self._recursion_limit = recursion_limit or (self._router.policy.max_recalcs * 6 + 12)
        self._checkpointer = checkpointer if checkpointer is not None else MemorySaver()
        self._graph = self._build()

    # --- assembly ----------------------------------------------------- #
    def _build(self):
        g = StateGraph(GraphState)

        n = self._nodes
        g.add_node(NODE_PLAN, n.plan)
        g.add_node(NODE_SYNTHESIZE, n.synthesize)
        g.add_node(NODE_EXECUTE, n.execute)
        g.add_node(NODE_CLASSIFY, n.classify)
        g.add_node(NODE_RECALCULATE, n.recalculate)
        g.add_node(NODE_FINALIZE, n.finalize)
        g.add_node(NODE_HALT, n.halt)

        r = self._router
        g.add_edge(START, NODE_PLAN)
        g.add_edge(NODE_PLAN, NODE_SYNTHESIZE)
        g.add_conditional_edges(NODE_SYNTHESIZE, r.after_synthesis, _identity_map(AFTER_SYNTHESIS_DESTS))
        g.add_edge(NODE_EXECUTE, NODE_CLASSIFY)
        g.add_conditional_edges(NODE_CLASSIFY, r.after_classify, _identity_map(AFTER_CLASSIFY_DESTS))
        g.add_conditional_edges(NODE_RECALCULATE, r.after_recalculate, _identity_map(AFTER_RECALCULATE_DESTS))
        g.add_edge(NODE_FINALIZE, END)
        g.add_edge(NODE_HALT, END)

        return g.compile(checkpointer=self._checkpointer)

    # --- public entry point ------------------------------------------- #
    def run(self, intent: str, *, session_id: str | None = None) -> WorkflowResult:
        session_id = session_id or uuid.uuid4().hex
        initial = GraphState(intent=intent, session_id=session_id)
        config = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": self._recursion_limit,
        }
        raw = self._graph.invoke(initial, config)
        final = GraphState.model_validate(raw)
        return WorkflowResult(
            success=final.status is GraphStatus.COMPLETED,
            answer=final.final_answer,
            failure_reason=final.failure_reason,
            status=final.status,
            final_tier=final.current_tier,
            recalc_count=final.recalc_count,
            tier_escalations=final.tier_escalations,
            attempts=final.iteration + 1,
            metrics=RunMetrics(
                gateway_calls=final.gateway_calls,
                prompt_tokens=final.prompt_tokens,
                completion_tokens=final.completion_tokens,
                total_tokens=final.prompt_tokens + final.completion_tokens,
                gateway_seconds=final.gateway_seconds,
                backend_calls=final.backend_calls,
                backend_seconds=final.backend_seconds,
            ),
        )

    @property
    def compiled_graph(self):
        """The compiled LangGraph, e.g. for ``.get_graph().draw_mermaid()`` diagrams."""
        return self._graph


def _identity_map(destinations) -> dict:
    """LangGraph path_map: our edge functions return node names directly."""
    return {dest: dest for dest in destinations}
