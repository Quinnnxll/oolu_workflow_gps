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
    script_fingerprint,
)
from ..cache.store import ScriptCache
from ..models import ErrorClass, ExecutionResult, Phase
from ..nodeplace.screening import screen_script
from ..skills.models import ActionEvent, ExecutionOutcome, ExecutionStatus
from .backend import ExecutionBackend, ExecutionRequest, ResourceLimits, WebGrant
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


class ChatModelSynthesizer:
    """Single-shot synthesis through a chat model (``reply(messages) -> str``).

    The lightweight sibling of ``GraphEngineSynthesizer`` for deployments
    without the full graph engine: one consultation with the frozen synthesis
    system prompt, code-block extraction, no navigation loop. The runner
    still verifies by execution before trusting anything, so a bad answer is
    just a failed proposal.
    """

    def __init__(self, model, *, tier: str = "chat"):
        self._model = model  # chat.ChatModel: reply(messages) -> str
        self._tier = tier

    def synthesize(self, goal: str, *, session_id: str) -> NodeSynthesis | None:
        from ..routing.gateway import extract_script
        from ..routing.prompting import DEFAULT_SYSTEM_PROMPT

        try:
            raw = self._model.reply(
                [
                    {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                    {"role": "user", "content": goal},
                ]
            )
        except Exception as exc:  # noqa: BLE001 - no model means no proposal,
            # never a crashed node.
            logger.warning("chat-model synthesis failed: %s", exc)
            return None
        script = extract_script(raw)
        if not script:
            return None
        return NodeSynthesis(script=script, tier=self._tier, model="chat-router")

    def repair(self, goal: str, script: str, error: str) -> str | None:
        """EDIT the node's failing function instead of rewriting from
        scratch: the model sees the goal, the current code, and the exact
        failure, and returns the corrected full script — which the runner
        still verifies by execution before trusting anything."""
        from ..routing.gateway import extract_script

        try:
            raw = self._model.reply(
                [
                    {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Goal:\n{goal}\n\nCurrent function:\n"
                            f"```python\n{script}\n```\n\n"
                            f"It failed with:\n{error}\n\n"
                            "Return the corrected COMPLETE script."
                        ),
                    },
                ]
            )
        except Exception as exc:  # noqa: BLE001 - a dead model repairs nothing
            logger.warning("chat-model repair failed: %s", exc)
            return None
        return extract_script(raw) or None


REPAIR_SYSTEM_PROMPT = """\
You repair the execution function of an OoLu node. You are given the
node's goal, its current Python script, and the exact failure it hit.
Edit the script to close the gap — fix the cause of THAT failure, keep
everything that already works. Same contract as before: ONE complete,
self-contained Python script in a single ```python fence that performs
the whole task in one run and calls emit_result exactly once:
    from _oolu_runtime import emit_result
The sandbox has NO host credentials and NO raw network at run time. The
ONLY way to the web is the brokered hand from the same runtime module —
    from _oolu_runtime import http_request
    answer = http_request("https://api.example.com/v1/things")
— which the host answers for the node's granted hosts alone; a refused
call returns status 0 with the reason in "error", never an exception."""


def _web_grant(action: ActionEvent) -> WebGrant | None:
    """The node's stamped egress regime, read off the action exactly the
    way the http hand reads it: ``_egress_open`` beats the allow-grant;
    a present-but-empty ``_egress_hosts`` fails closed (the broker then
    answers every call with the words to fix it); no stamp at all means
    no web hand — the exchange is never even mounted."""
    if action.parameters.get("_egress_open"):
        raw = action.parameters.get("_egress_blocked") or []
        return WebGrant(
            open_web=True,
            blocked_hosts=tuple(
                str(h).strip().lower() for h in raw if str(h).strip()
            ),
        )
    if "_egress_hosts" in action.parameters:
        raw = action.parameters.get("_egress_hosts") or []
        return WebGrant(
            hosts=tuple(str(h).strip().lower() for h in raw if str(h).strip())
        )
    return None


def _staged_files(action: ActionEvent) -> dict[str, str]:
    """The node's own files riding the action — programs and data the
    backend stages next to the script. Shape-checked only; the backend
    enforces the path and size walls."""
    raw = action.parameters.get("files")
    if not isinstance(raw, dict):
        return {}
    return {str(name): str(content) for name, content in raw.items()}


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
      - ``bindings`` (optional dict): the node's resolved slot values;
      - ``script`` (optional): a pre-written script the planner supplies
        (e.g. the LLM rebuild). It is a PROPOSAL like any synthesis —
        executed through this backend and classified before it is trusted,
        reported, or cached; a failing provided script falls through to
        single-node re-synthesis exactly like a stale cache entry.
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
        bundle_resolver=None,  # (bundle_id) -> PreparedBundle | None
    ):
        self._backend = backend
        self._cache = cache
        self._synthesizer = synthesizer
        self._limits = limits or ResourceLimits()
        self._pinned_index_url = pinned_index_url
        self._backend_kind = backend_kind or type(backend).__name__
        self._backend_image = backend_image
        self._environment = environment_fingerprint
        # The seam to the bundle layer: an action carrying a ``bundle`` id
        # is resolved to a packed ``PreparedBundle`` (cache-first) and
        # staged in one archive. No resolver -> the inline ``files`` path
        # stands, so a minimal install (or a test) needs nothing extra.
        self._bundle_resolver = bundle_resolver
        self._completed: dict[str, ExecutionOutcome] = {}

    def capabilities(self) -> frozenset[str]:
        return frozenset({"run"})

    def cancel(self, idempotency_key: str) -> None:
        """Backend runs are bounded by their own resource limits."""

    # ------------------------------------------------------------------ #
    def cache_key(
        self,
        node_key: str,
        bindings: dict[str, Any],
        *,
        script: str | None = None,
    ) -> str:
        # ``script`` is the PROVIDED function, when there is one: its
        # fingerprint joins the key so an edited function is never
        # shadowed by the cache of the code it replaced.
        return make_node_script_cache_key(
            NodeScriptSignature(
                node_key=node_key,
                bindings_fingerprint=bindings_fingerprint(bindings),
                environment_fingerprint=self._environment,
                backend_kind=self._backend_kind,
                backend_image=self._backend_image,
                pinned_index_url=self._pinned_index_url,
                script_fingerprint=script_fingerprint(script),
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
        provided = action.parameters.get("script")
        # The tree's identity joins the key: an edited bundle (new
        # bundle_id) re-verifies rather than replaying against a tree it
        # was never run with.
        bundle_id = action.parameters.get("bundle")
        node_key = f"{node_key}#bundle:{bundle_id}" if bundle_id else node_key
        key = self.cache_key(
            node_key, bindings, script=str(provided) if provided else None
        )
        # The node's stamped egress regime and its own staged files ride the
        # action into EVERY backend run below — cached replay, provided,
        # repaired, and resynthesized alike.
        web = _web_grant(action)
        files = _staged_files(action)
        # The exact-value channel: the node's resolved inputs ride into
        # the sandbox as DATA (./bindings.json), so the function reads
        # the values the runtime bound — never literals the model
        # retyped, never values it imagined.
        if bindings:
            import json as _json

            files = {
                **files,
                "bindings.json": _json.dumps(
                    bindings, ensure_ascii=False, sort_keys=True, default=str
                ),
            }
        # A large tree rides as a content-addressed bundle: resolve its id
        # to a packed archive (cache-first) once, here, and hand it to every
        # backend run below instead of a per-file dict.
        bundle = None
        if bundle_id and self._bundle_resolver is not None:
            bundle = self._bundle_resolver(str(bundle_id))

        # --- hit path: replay the memoized script, no synthesis paid ----- #
        cached = self._cache.get(key)
        stale_error: str | None = None
        if cached is not None:
            result = self._run_script(
                cached.script,
                list(cached.dependencies),
                idempotency_key,
                web=web,
                files=files,
                bundle=bundle,
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

        # --- provided path: a planner-supplied script is a proposal ------ #
        if provided:
            # Missing imports heal in-place (bounded): the one recalculable
            # failure a correct script can legitimately hit on this backend.
            deps: list[str] = []
            for _ in range(3):
                result = self._run_script(
                    str(provided),
                    deps,
                    idempotency_key,
                    web=web,
                    files=files,
                    bundle=bundle,
                )
                record = classify(result)
                if record is None:
                    self._cache.store_success(
                        key,
                        script=str(provided),
                        dependencies=deps,
                        tier="provided",
                        model=str(action.parameters.get("model", "provided")),
                    )
                    return self._outcome(
                        action,
                        idempotency_key,
                        ExecutionStatus.SUCCEEDED,
                        started,
                        evidence={
                            "cache": "provided",
                            "cache_key": key,
                            "result": result.contract_payload,
                        },
                    )
                if (
                    record.error_class is ErrorClass.MISSING_DEPENDENCY
                    and record.missing_module
                    and record.missing_module not in deps
                ):
                    deps.append(record.missing_module)
                    continue
                break
            stale_error = (
                f"provided script failed verification: {record.error_class.value}"
            )
            logger.info("provided script failed for %s (%s)", node_key, stale_error)

            # --- edit path: the model REPAIRS the node's own function ---- #
            # Bounded: the model sees the exact failure, edits the code,
            # and the edit is verified by execution before it is trusted
            # or cached — the loop closes the gap or says it could not.
            repairer = getattr(self._synthesizer, "repair", None)
            if repairer is not None:
                current = str(provided)
                failure_words = (
                    f"{record.error_class.value}: "
                    + (result.stderr or result.stdout or "")[-800:]
                )
                for attempt in range(1, 3):
                    edited = repairer(
                        render_node_goal(str(goal), bindings),
                        current,
                        failure_words,
                    )
                    if not edited or edited.strip() == current.strip():
                        break
                    deps = []
                    result = self._run_script(
                        edited,
                        deps,
                        idempotency_key,
                        web=web,
                        files=files,
                        bundle=bundle,
                    )
                    record = classify(result)
                    if record is None:
                        # The repaired function is the node's function now:
                        # cached under the FAILING code's key (so this exact
                        # provided script heals on replay) AND under its own
                        # fingerprint (so once the healed code is promoted
                        # into the drawer, its runs hit the cache at once).
                        self._cache.store_success(
                            key,
                            script=edited,
                            dependencies=deps,
                            tier="repaired",
                            model="chat-router",
                        )
                        self._cache.store_success(
                            self.cache_key(node_key, bindings, script=edited),
                            script=edited,
                            dependencies=deps,
                            tier="repaired",
                            model="chat-router",
                        )
                        return self._outcome(
                            action,
                            idempotency_key,
                            ExecutionStatus.SUCCEEDED,
                            started,
                            evidence={
                                "cache": "repaired",
                                "cache_key": key,
                                "repair_rounds": attempt,
                                # The healed code itself — the channel the
                                # gateway promotes into the node's drawer
                                # (src/main.py) through the node.repair
                                # seat, AFTER the run: the run itself never
                                # mutates files mid-flight.
                                "repaired_script": edited,
                                "result": result.contract_payload,
                            },
                        )
                    current = edited
                    failure_words = (
                        f"{record.error_class.value}: "
                        + (result.stderr or result.stdout or "")[-800:]
                    )
                stale_error = (
                    "the function failed and repair could not close the "
                    f"gap: {record.error_class.value}"
                )

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
            synthesis.script,
            list(synthesis.dependencies),
            idempotency_key,
            web=web,
            files=files,
            bundle=bundle,
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
        self,
        script: str,
        dependencies: list[str],
        session_id: str,
        *,
        web: WebGrant | None = None,
        files: dict[str, str] | None = None,
        bundle=None,
    ) -> ExecutionResult:
        # The antivirus screen at the last gate: no script — provided,
        # synthesized, repaired, or replayed from cache — reaches the
        # backend without passing it. Defense in depth behind the sandbox,
        # so a hostile pattern is refused as a failed run, never executed.
        flags = screen_script(script)
        if flags:
            return ExecutionResult(
                phase=Phase.EXECUTE,
                exit_code=1,
                stderr="refused by the safety screen: " + "; ".join(flags),
                contract_ok=False,
            )
        return self._backend.run(
            ExecutionRequest(
                script=script,
                dependencies=dependencies,
                pinned_index_url=self._pinned_index_url,
                limits=self._limits,
                session_id=session_id,
                web=web,
                files=dict(files or {}),
                bundle=bundle,
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
