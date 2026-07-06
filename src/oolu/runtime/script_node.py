"""Script-bodied nodes: node-granular caching + single-node re-synthesis.

This is the third node body kind from the planning review (actions | script |
sub-graph), and the replacement for whole-script recalculation economics:

- ``NodeScriptRunner`` is an ``ActionExecutor`` (adapter ``"script"``), so a
  synthesized-code step slots into a ``DagRouteRunner`` blueprint next to
  replayed CLI actions.
- Scripts are memoized **per node**: the cache key is the node key + the
  slot-binding fingerprint + the environment fingerprint — never the parent
  intent — so the same sub-task recurring inside different workflows hits the
  same entry (`cache.signature.NodeScriptSignature`).
- On a hit, the cached script runs straight on the execution backend: no
  gateway call, no synthesis, one sandbox run.
- On a miss — or when a cached script stops working (environment drift) —
  only *this node* is re-synthesized through the graph engine's full
  recalculating navigation loop; the rest of the workflow's cached nodes are
  untouched. The stale entry records its failure, and the fresh script
  replaces it on verified success.

**Verification is the runner's own.** Whatever the synthesizer returns is
executed through THIS backend and classified before it is trusted, reported,
or cached — a synthesis nobody watched run never enters the cache. Scripts
run with an empty environment (the backend never inherits host env), and
bindings are rendered into the synthesis goal as explicit values, so a
cached script is a closed artifact: same key, same behaviour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from ..cache.signature import (
    NodeScriptSignature,
    bindings_fingerprint,
    make_node_script_cache_key,
)
from ..cache.store import ScriptCache
from ..models import ExecutionResult
from ..skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus
from .backend import ExecutionBackend, ExecutionRequest, ResourceLimits
from .dependency import classify

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NodeSynthesis:
    """One synthesized script for one node, plus its provenance."""

    script: str
    dependencies: tuple[str, ...] = ()
    tier: str = "fast"
    model: str = "unknown"


@runtime_checkable
class ScriptSynthesizer(Protocol):
    """Produces a candidate script for a node goal (or None when it cannot).

    The runner verifies every synthesis by executing it before trusting it,
    so a synthesizer only proposes — it never vouches.
    """

    def synthesize(self, goal: str, *, session_id: str) -> NodeSynthesis | None: ...


class GraphEngineSynthesizer:
    """Single-node re-synthesis through the graph engine's navigation loop.

    Drives a ``OoLu`` run for just this node's goal — the engine pays
    for the model call and navigates its own recalculation ladder (dependency
    healing, tier escalation) — then reads the winning script back from the
    engine's own script cache via the run's cache key. The engine must
    therefore be constructed WITH a script cache; without one there is
    nothing to read back and synthesis reports failure rather than guessing.
    """

    def __init__(self, engine: Any, script_cache: ScriptCache):
        self._engine = (
            engine  # OoLu; typed loosely to avoid the langgraph import
        )
        self._cache = script_cache

    def synthesize(self, goal: str, *, session_id: str) -> NodeSynthesis | None:
        result = self._engine.run(goal, session_id=session_id)
        if not getattr(result, "success", False) or not getattr(
            result, "cache_key", None
        ):
            return None
        entry = self._cache.get(result.cache_key)
        if entry is None:
            logger.warning(
                "engine succeeded but its cache has no script for %s",
                result.cache_key,
            )
            return None
        return NodeSynthesis(
            script=entry.script,
            dependencies=tuple(entry.dependencies),
            tier=entry.tier,
            model=entry.model,
        )


def render_node_goal(goal: str, bindings: dict[str, Any]) -> str:
    """The exact synthesis prompt for a bound node — deterministic, so the
    same node + bindings always asks the same question."""
    if not bindings:
        return goal
    lines = [goal, "", "Bindings (use these exact values):"]
    for name in sorted(bindings):
        lines.append(f"- {name} = {bindings[name]!r}")
    return "\n".join(lines)


class NodeScriptRunner:
    """The ``ActionExecutor`` for script-bodied nodes (adapter ``"script"``).

    Action parameters:
      - ``goal`` (required): what this node must accomplish;
      - ``node_key`` (optional): stable identity for caching — defaults to
        the goal, but a planner should pass its own node key so renames of
        the surrounding workflow do not orphan the cache;
      - ``bindings`` (optional dict): the node's resolved slot values.
    """

    name = "script"

    def __init__(
        self,
        backend: ExecutionBackend,
        cache: ScriptCache,
        *,
        synthesizer: ScriptSynthesizer | None = None,
        limits: ResourceLimits | None = None,
        pinned_index_url: str | None = None,
        backend_kind: str | None = None,
        backend_image: str | None = None,
        environment_fingerprint: str = "",
    ):
        self._backend = backend
        self._cache = cache
        self._synthesizer = synthesizer
        self._limits = limits or ResourceLimits()
        self._pinned_index_url = pinned_index_url
        self._backend_kind = backend_kind or type(backend).__name__
        self._backend_image = backend_image
        self._environment = environment_fingerprint
        self._completed: dict[str, ExecutionOutcome] = {}

    def capabilities(self) -> frozenset[str]:
        return frozenset({"run"})

    def cancel(self, idempotency_key: str) -> None:
        """Backend runs are bounded by their own resource limits."""

    # ------------------------------------------------------------------ #
    def cache_key(self, node_key: str, bindings: dict[str, Any]) -> str:
        return make_node_script_cache_key(
            NodeScriptSignature(
                node_key=node_key,
                bindings_fingerprint=bindings_fingerprint(bindings),
                environment_fingerprint=self._environment,
                backend_kind=self._backend_kind,
                backend_image=self._backend_image,
                pinned_index_url=self._pinned_index_url,
            )
        )

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        if idempotency_key in self._completed:
            return self._completed[idempotency_key]
        outcome = self._execute(action, idempotency_key)
        self._completed[idempotency_key] = outcome
        return outcome

    def _execute(self, action: ActionEvent, idempotency_key: str) -> ExecutionOutcome:
        started = datetime.now(UTC)
        goal = action.parameters.get("goal")
        if action.operation != "run" or not goal:
            return self._outcome(
                action,
                idempotency_key,
                ExecutionStatus.BLOCKED,
                started,
                error="script actions need operation 'run' and a 'goal' parameter",
            )
        bindings = dict(action.parameters.get("bindings") or {})
        node_key = str(action.parameters.get("node_key") or goal)
        key = self.cache_key(node_key, bindings)

        # --- hit path: replay the memoized script, no synthesis paid ----- #
        cached = self._cache.get(key)
        stale_error: str | None = None
        if cached is not None:
            result = self._run_script(
                cached.script, list(cached.dependencies), idempotency_key
            )
            record = classify(result)
            if record is None:
                self._cache.store_success(
                    key,
                    script=cached.script,
                    dependencies=list(cached.dependencies),
                    tier=cached.tier,
                    model=cached.model,
                )
                return self._outcome(
                    action,
                    idempotency_key,
                    ExecutionStatus.SUCCEEDED,
                    started,
                    evidence={
                        "cache": "hit",
                        "cache_key": key,
                        "result": result.contract_payload,
                    },
                )
            # Environment drift: the world moved under a proven script. Count
            # the failure and fall through to single-node re-synthesis.
            self._cache.record_failure(key)
            stale_error = f"cached script failed: {record.error_class.value}"
            logger.info("node cache stale for %s (%s)", node_key, stale_error)

        # --- miss / repair path: re-synthesize THIS node only ------------ #
        if self._synthesizer is None:
            return self._outcome(
                action,
                idempotency_key,
                ExecutionStatus.FAILED,
                started,
                error=stale_error or "no cached script and no synthesizer configured",
                evidence={
                    "cache": "stale" if stale_error else "miss",
                    "cache_key": key,
                },
            )
        synthesis = self._synthesizer.synthesize(
            render_node_goal(str(goal), bindings), session_id=idempotency_key
        )
        if synthesis is None:
            return self._outcome(
                action,
                idempotency_key,
                ExecutionStatus.FAILED,
                started,
                error="synthesis produced no usable script",
                evidence={
                    "cache": "stale" if stale_error else "miss",
                    "cache_key": key,
                },
            )

        # Verify with OUR backend before trusting, reporting, or caching.
        result = self._run_script(
            synthesis.script, list(synthesis.dependencies), idempotency_key
        )
        record = classify(result)
        if record is not None:
            return self._outcome(
                action,
                idempotency_key,
                ExecutionStatus.FAILED,
                started,
                error=f"synthesized script failed verification: {record.error_class.value}",
                evidence={"cache": "miss", "cache_key": key},
            )
        self._cache.store_success(
            key,
            script=synthesis.script,
            dependencies=list(synthesis.dependencies),
            tier=synthesis.tier,
            model=synthesis.model,
        )
        return self._outcome(
            action,
            idempotency_key,
            ExecutionStatus.SUCCEEDED,
            started,
            evidence={
                "cache": "resynthesized" if stale_error else "miss",
                "cache_key": key,
                "result": result.contract_payload,
            },
        )

    # ------------------------------------------------------------------ #
    def _run_script(
        self, script: str, dependencies: list[str], session_id: str
    ) -> ExecutionResult:
        return self._backend.run(
            ExecutionRequest(
                script=script,
                dependencies=dependencies,
                pinned_index_url=self._pinned_index_url,
                limits=self._limits,
                session_id=session_id,
            )
        )

    @staticmethod
    def _outcome(
        action: ActionEvent,
        idempotency_key: str,
        status: ExecutionStatus,
        started: datetime,
        *,
        error: str | None = None,
        evidence: dict | None = None,
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=str(action.parameters.get("skill_id", "script-node")),
            status=status,
            evidence=dict(evidence or {}),
            error=error,
            started_at=started,
            completed_at=datetime.now(UTC),
        )
