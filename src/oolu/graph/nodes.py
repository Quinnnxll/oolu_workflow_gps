"""Graph nodes — the work each step performs, wiring runtime + routing together.

Each node is a callable ``(state) -> partial-update-dict``. LangGraph merges the
dict into the state, applying the ``operator.add`` reducer to ``error_history`` (so
returning ``{"error_history": [rec]}`` appends rather than overwrites). Nodes return
only the fields they change.

The backend and gateway are injected, so the entire machine can be driven with
``StubBackend`` + ``FakeGateway`` — no Docker, no network — which is exactly how the
node/edge composition is tested before LangGraph is involved.

Infrastructure failures are handled here, not in edges: a ``GatewayError`` (model
endpoint down) or ``BackendError`` (Docker down) is fatal — the node sets
``status=FAILED`` and a reason, and the edge guard routes straight to halt. That is
distinct from a script/model that merely failed, which becomes an ``ErrorRecord`` and
flows through the recalc loop.
"""

from __future__ import annotations

import logging

from ..cache import (
    NoopScriptCache,
    ScriptCache,
    ScriptCacheSignature,
    make_script_cache_key,
)
from ..knowledge import KnowledgeClient, NoopKnowledgeClient
from ..models import (
    ErrorClass,
    ErrorRecord,
    ExecutionPlan,
    GraphState,
    GraphStatus,
    ModelTier,
)
from ..routing.gateway import Gateway, GatewayError
from ..routing.matrix import RoutingMatrix
from ..routing.prompting import PromptAssembler
from ..runtime.backend import (
    BackendError,
    ExecutionBackend,
    ExecutionRequest,
    ResourceLimits,
)
from ..runtime.dependency import classify as classify_result
from ..runtime.dependency import plan_dependency_fix, resolve

logger = logging.getLogger(__name__)


class GraphNodes:
    """Bundles the injected components and exposes the node callables."""

    def __init__(
        self,
        *,
        gateway: Gateway,
        backend: ExecutionBackend,
        matrix: RoutingMatrix | None = None,
        assembler: PromptAssembler | None = None,
        limits: ResourceLimits | None = None,
        pinned_index_url: str | None = None,
        knowledge: KnowledgeClient | None = None,
        script_cache: ScriptCache | None = None,
        backend_kind: str | None = None,
        backend_image: str | None = None,
    ):
        self._gateway = gateway
        self._backend = backend
        self._matrix = matrix or RoutingMatrix()
        self._assembler = assembler or PromptAssembler()
        self._limits = limits or ResourceLimits()
        self._pinned_index_url = pinned_index_url
        self._knowledge = knowledge or NoopKnowledgeClient()
        self._script_cache = script_cache or NoopScriptCache()
        self._script_cache_enabled = script_cache is not None and not isinstance(
            script_cache, NoopScriptCache
        )
        self._backend_kind = backend_kind or type(backend).__name__
        self._backend_image = backend_image

    # --- plan: seed state, load any dependency hints ------------------ #
    def plan(self, state: GraphState) -> dict:
        hints = self._knowledge.all_dependency_hints()
        return {"dependency_hints": list(hints), "status": GraphStatus.SYNTHESIZING}

    # --- synthesize: matrix -> prompt -> gateway ---------------------- #
    def synthesize(self, state: GraphState) -> dict:
        decision = self._matrix.decide(state)
        prompt = self._assembler.build(state)

        cache_key = make_script_cache_key(
            ScriptCacheSignature(
                intent=state.intent,
                prompt_fingerprint=self._assembler.system_prompt_fingerprint,
                routing_models=(
                    self._matrix.config.fast.model,
                    self._matrix.config.reasoning.model,
                ),
                backend_kind=self._backend_kind,
                backend_image=self._backend_image,
                pinned_index_url=self._pinned_index_url,
            )
        )

        updates: dict = {
            "current_tier": decision.tier,
            "synthesis_temperature": decision.temperature,
            "cache_key": cache_key if self._script_cache_enabled else None,
        }
        if decision.escalated:
            updates["tier_escalations"] = state.tier_escalations + 1
        logger.info("synthesize: %s", decision.reason)

        # Once a cached script fails, synthesize afresh for the rest of this run.
        cached = (
            self._script_cache.get(cache_key)
            if self._script_cache_enabled and state.cache_status != "failed"
            else None
        )
        if cached is not None:
            try:
                cached_tier = ModelTier(cached.tier)
            except ValueError:
                cached_tier = decision.tier
            updates.update(
                {
                    "current_tier": cached_tier,
                    "plan": ExecutionPlan(
                        intent=state.intent,
                        script=cached.script,
                        required_dependencies=list(cached.dependencies),
                        tier=cached_tier,
                    ),
                    "status": GraphStatus.EXECUTING,
                    "cache_hit": True,
                    "cache_kind": "script",
                    "cache_status": "hit",
                }
            )
            return updates
        if not self._script_cache_enabled:
            updates["cache_status"] = "disabled"
        else:
            updates["cache_status"] = (
                "bypassed" if state.cache_status == "failed" else "miss"
            )

        try:
            result = self._gateway.complete(decision, prompt)
        except GatewayError as exc:  # fatal infrastructure failure -> halt
            logger.error("gateway failure: %s", exc)
            updates["status"] = GraphStatus.FAILED
            updates["failure_reason"] = f"model gateway error: {exc}"
            return updates

        # Per-run telemetry deltas (sum-reduced in state).
        updates["gateway_calls"] = 1
        updates["prompt_tokens"] = result.prompt_tokens
        updates["completion_tokens"] = result.completion_tokens
        updates["gateway_seconds"] = result.duration_s

        carry_deps = list(state.plan.required_dependencies) if state.plan else []
        if result.has_script:
            updates["plan"] = ExecutionPlan(
                intent=state.intent,
                script=result.script,
                required_dependencies=carry_deps,
                tier=decision.tier,
            )
            updates["status"] = GraphStatus.EXECUTING
        else:
            # No usable code: keep deps, clear the script so the edge routes to recalc,
            # and record a recalculable SYNTHESIS_FAILED (counts toward depth + rut).
            updates["plan"] = ExecutionPlan(
                intent=state.intent,
                script=None,
                required_dependencies=carry_deps,
                tier=decision.tier,
            )
            updates["error_history"] = [
                ErrorRecord.create(
                    error_class=ErrorClass.SYNTHESIS_FAILED,
                    message="model returned no usable code",
                    iteration=state.iteration,
                )
            ]
            updates["status"] = GraphStatus.RECALCULATING
        return updates

    # --- execute: build request, run the backend ---------------------- #
    def execute(self, state: GraphState) -> dict:
        plan = state.plan
        if plan is None or not plan.script:
            return {
                "status": GraphStatus.FAILED,
                "failure_reason": "execute reached with no script (routing bug)",
            }

        request = ExecutionRequest(
            script=plan.script,
            dependencies=list(plan.required_dependencies),
            pinned_index_url=self._pinned_index_url,
            limits=self._limits,
            session_id=state.session_id,
            iteration=state.iteration,
        )
        try:
            result = self._backend.run(request)
        except BackendError as exc:  # fatal infrastructure failure -> halt
            logger.error("backend failure: %s", exc)
            return {
                "status": GraphStatus.FAILED,
                "failure_reason": f"execution backend error: {exc}",
                "last_result": None,
            }
        return {
            "last_result": result,
            "backend_calls": 1,
            "backend_seconds": result.duration_s,
        }

    # --- classify: label a failure, or pass a success through --------- #
    def classify(self, state: GraphState) -> dict:
        if state.status is GraphStatus.FAILED:
            return {}  # infrastructure failure already terminal; let the edge halt
        result = state.last_result
        if result is None:
            return {
                "status": GraphStatus.FAILED,
                "failure_reason": "no execution result to classify",
            }
        record = classify_result(result, iteration=state.iteration)
        if record is None:
            return {}  # success — finalize sets the final answer
        updates = {"error_history": [record], "status": GraphStatus.RECALCULATING}
        if state.cache_hit and state.cache_kind == "script" and state.cache_key:
            self._script_cache.record_failure(state.cache_key)
            updates["cache_status"] = "failed"
        return updates

    # --- recalculate: resolve a dep, bump counters -------------------- #
    def recalculate(self, state: GraphState) -> dict:
        updates: dict = {
            "recalc_count": state.recalc_count + 1,
            "iteration": state.iteration + 1,
            "status": GraphStatus.RECALCULATING,
        }
        err = state.latest_error
        if (
            err is not None
            and err.error_class is ErrorClass.MISSING_DEPENDENCY
            and state.plan is not None
        ):
            resolution = plan_dependency_fix(err, tuple(state.dependency_hints))
            if resolution is not None:
                deps = list(state.plan.required_dependencies)
                if resolution.package_name not in deps:
                    deps.append(resolution.package_name)
                updates["plan"] = state.plan.model_copy(
                    update={"required_dependencies": deps, "phase_a_needed": True}
                )
                logger.info(
                    "recalculate: queued '%s' for %s",
                    resolution.package_name,
                    err.missing_module,
                )
        return updates

    # --- finalize: success ------------------------------------------- #
    def finalize(self, state: GraphState) -> dict:
        payload = state.last_result.contract_payload if state.last_result else None
        self._learn_from_success(state)
        updates = {"final_answer": payload, "status": GraphStatus.COMPLETED}
        if (
            self._script_cache_enabled
            and state.cache_key
            and state.plan
            and state.plan.script
        ):
            model = self._matrix.config.tier_config(state.plan.tier).model
            self._script_cache.store_success(
                state.cache_key,
                script=state.plan.script,
                dependencies=list(state.plan.required_dependencies),
                tier=state.plan.tier.value,
                model=model,
            )
            updates["cache_status"] = "hit" if state.cache_hit else "stored"
        return updates

    def _learn_from_success(self, state: GraphState) -> None:
        """Record the import->package mappings that ultimately led to success, so the
        next run resolves them from learned memory instead of guessing. Deterministic
        re-derivation via the resolver — no extra state or model fields needed."""
        if state.plan is None:
            return
        installed = set(state.plan.required_dependencies)
        seen: set[str] = set()
        for record in state.error_history:
            if (
                record.error_class is not ErrorClass.MISSING_DEPENDENCY
                or not record.missing_module
            ):
                continue
            module = record.missing_module
            if module in seen:
                continue
            seen.add(module)
            resolution = resolve(module, tuple(state.dependency_hints))
            if resolution.package_name in installed:
                self._knowledge.record_dependency_success(
                    module, resolution.package_name
                )

    # --- halt: terminal failure with a clear reason ------------------- #
    def halt(self, state: GraphState) -> dict:
        if state.failure_reason:
            reason = state.failure_reason  # infrastructure failure already explained
        else:
            err = state.latest_error
            if err is None:
                reason = "halted with no recorded error"
            elif err.error_class.is_recalculable:
                reason = (
                    f"exhausted after {state.recalc_count} recalc cycles; "
                    f"last failure: {err.error_class.value} — {err.message}"
                )
            else:
                reason = f"unrecoverable {err.error_class.value}: {err.message}"
        return {"status": GraphStatus.FAILED, "failure_reason": reason}
