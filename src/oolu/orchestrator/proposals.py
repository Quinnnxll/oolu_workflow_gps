"""A model ranks candidate producers — advice priced, parsed, and distrusted.

``GatewayProposalModel`` implements the assembler's ``ProposalModel`` seam
over the same ``Gateway`` the synthesis engine already uses, so one model
deployment serves both jobs. Three properties keep the advice honest:

1. ADVISORY BY CONSTRUCTION. The completion parses defensively — an answer
   with no readable weights is simply "no opinion", never an error — and
   the assembler folds whatever survives into the Beta posterior as
   pseudo-observations that verified history washes out. A dead endpoint
   raises ``GatewayError``, which the assembler downgrades to
   verified-history-only assembly: the model advises the marketplace, it
   can never take it down.

2. METERED. Every call records into a ``ModelCallMeter`` and its price
   rides the returned ``Proposal``, so a plan that needed advice is
   honestly dearer than one that did not — budgets judge the sum.

3. BOUNDED. At most ``max_candidates`` candidates (the strongest by
   verified history) ride the prompt, and the completion budget is small:
   ranking is a cheap fast-tier job, never a reasoning-tier essay. The
   system message is a frozen constant, so prefix caches stay warm across
   every proposal the assembler ever asks for.
"""

from __future__ import annotations

import json
import re
from typing import Sequence

from ..billing.model_calls import ModelCallMeter
from ..knowledge.traces import RecordedRun, TraceStore, route_node_key
from ..models import ModelTier
from ..routing.gateway import Gateway, extract_script
from ..routing.matrix import RoutingConfig, RoutingDecision, default_routing_config
from ..routing.prompting import AssembledPrompt
from ..skills.contract import NodeContract, NodeStats, Slot
from .assembler import GoalSpec, Proposal

PROPOSAL_PURPOSE = "assembly.proposal"

# Frozen — the cacheable prefix of every proposal prompt.
PROPOSAL_SYSTEM_PROMPT = """\
You advise the workflow assembler of OoLu. Given a goal, the slot \
that needs a producer, and a list of candidate workflow nodes with their \
platform-verified history, you weigh which candidates are most likely to \
produce the slot successfully for this goal.

Rules:
- Judge fit for the goal; the platform already accounts for the verified \
statistics, so use them only to break ties in fit.
- Your answer is advice, not a decision: weight 1 means strongly \
recommended, 0 means strongly advised against, and omitting a candidate \
means no opinion.

Output format:
- Reply with EXACTLY ONE fenced ```json block containing a single JSON \
object that maps candidate id (copied exactly from the list) to a number \
between 0 and 1. No prose outside the block."""

_JSON_SPAN_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_weights(text: str | None) -> dict[str, float]:
    """Pull ``{id: weight}`` out of a completion, tolerating prose and fences.

    Accepts a bare JSON object of id -> number or one wrapped as
    ``{"weights": {...}}``; weights clamp to [0, 1] and non-numeric entries
    are dropped. Returns ``{}`` for anything unusable — a proposal is
    advice, and unreadable advice is no advice.
    """
    if not text:
        return {}
    for blob in _json_candidates(text):
        try:
            data = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        inner = data.get("weights", data)
        if not isinstance(inner, dict):
            continue
        weights: dict[str, float] = {}
        for key, value in inner.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            weights[key] = min(1.0, max(0.0, float(value)))
        if weights:
            return weights
    return {}


def _json_candidates(text: str):
    yield text.strip()
    fenced = extract_script(text)  # falls back to any fenced block, json included
    if fenced:
        yield fenced.strip()
    span = _JSON_SPAN_RE.search(text)
    if span:
        yield span.group(0)


class GatewayProposalModel:
    """Asks the routing gateway's model to weigh candidate producers."""

    def __init__(
        self,
        gateway: Gateway,
        *,
        config: RoutingConfig | None = None,
        tier: ModelTier = ModelTier.FAST,
        meter: ModelCallMeter | None = None,
        max_candidates: int = 8,
        max_tokens: int = 512,
    ):
        self._gateway = gateway
        self._config = config or default_routing_config()
        self._tier = tier
        self._meter = meter or ModelCallMeter()
        self._max_candidates = max_candidates
        self._max_tokens = max_tokens

    @property
    def meter(self) -> ModelCallMeter:
        """The long-run ledger of what this model's advice has cost."""
        return self._meter

    def propose(
        self,
        *,
        goal: GoalSpec,
        slot: Slot,
        selected: Sequence[str],
        candidates: Sequence[NodeContract],
    ) -> Proposal:
        shortlist = self._shortlist(candidates)
        result = self._gateway.complete(
            self._decision(), self._prompt(goal, slot, selected, shortlist)
        )
        charge = self._meter.record(PROPOSAL_PURPOSE, result)
        return Proposal(weights=parse_weights(result.raw_text), cost=charge.cost)

    # ------------------------------------------------------------------ #
    def _shortlist(self, candidates: Sequence[NodeContract]) -> list[NodeContract]:
        """The strongest few by verified history — a bounded prompt, and the
        thin-history long tail (where the model's prior matters least per
        token) stays neutral rather than bloating every call."""

        def strength(contract: NodeContract) -> tuple:
            stats = contract.stats or NodeStats()
            return (-stats.success_mean, contract.name)

        return sorted(candidates, key=strength)[: self._max_candidates]

    def _decision(self) -> RoutingDecision:
        tier_cfg = self._config.tier_config(self._tier)
        return RoutingDecision(
            tier=tier_cfg.tier,
            model=tier_cfg.model,
            api_base=tier_cfg.api_base,
            temperature=tier_cfg.base_temperature,
            top_p=tier_cfg.top_p,
            top_k=tier_cfg.top_k,
            max_tokens=min(self._max_tokens, tier_cfg.max_tokens),
            extra_params=dict(tier_cfg.extra_params),
            reason="assembly proposal",
        )

    def _prompt(
        self,
        goal: GoalSpec,
        slot: Slot,
        selected: Sequence[str],
        shortlist: Sequence[NodeContract],
    ) -> AssembledPrompt:
        lines = [f"Goal: {goal.name}", f"Producing slot: {_render_slot(slot)}"]
        lines.append(
            "Already selected: " + (", ".join(selected) if selected else "nothing yet")
        )
        lines.append("Candidates:")
        for contract in shortlist:
            lines.extend(_render_candidate(contract))
        return AssembledPrompt(
            messages=[
                {"role": "system", "content": PROPOSAL_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(lines)},
            ],
            prefix_len=1,
        )


def _render_slot(slot: Slot) -> str:
    parts = [f"{slot.name} (type: {slot.value_type}"]
    if slot.role:
        parts.append(f", role: {slot.role}")
    parts.append(")")
    rendered = "".join(parts)
    if slot.description:
        rendered += f" — {slot.description}"
    return rendered


class TraceProposalModel:
    """The baseline learned planner: proposes from the caller's own runs.

    Reads the trace store's raw run log LIVE on every contested pick (so
    each executed contract immediately informs the next assembly) and
    judges candidates against the most specific evidence pool available —
    the same class-first shape as the budget layer's behavioral profiles:

    1. runs of THIS goal;
    2. else runs sharing at least one already-selected node (co-selection:
       "when these nodes were in a plan, what ran alongside them");
    3. else all recorded runs.

    Within the pool a candidate's weight is the Beta mean of the runs it
    appeared in — appearing in verified successes endorses it, appearing
    in failures counts against it — and a candidate the pool never saw
    gets NO opinion rather than a bad one. Free and local (cost 0).

    This is deliberately the modest end of the ``ProposalModel`` seam: a
    sequence checkpoint (Mamba/SSM via the routing gateway or ONNX) later
    implements the same protocol, trains on the same corpus
    (``knowledge.corpus.export_jsonl``), and must beat this baseline in
    the replay harness to earn its inference cost. That harness exists:
    ``orchestrator.replay`` (run it: ``python benchmarks/proposal_replay.py``;
    the gate is ``earns_its_cost``).
    """

    def __init__(self, store: TraceStore, *, context: str = "", max_runs: int = 500):
        self._store = store
        self._context = context
        self._max_runs = max_runs

    def propose(
        self,
        *,
        goal: GoalSpec,
        slot: Slot,
        selected: Sequence[str],
        candidates: Sequence[NodeContract],
    ) -> Proposal:
        runs = self._store.runs(context=self._context, limit=self._max_runs)
        pool = self._evidence_pool(runs, goal, selected)
        weights: dict[str, float] = {}
        for candidate in candidates:
            key = route_node_key(candidate.name)
            appeared = [run for run in pool if key in run.step_keys()]
            if not appeared:
                continue  # never seen here: no opinion, not a bad one
            wins = sum(1 for run in appeared if run.success)
            losses = len(appeared) - wins
            weights[candidate.id] = (1.0 + wins) / (2.0 + wins + losses)
        return Proposal(weights=weights, cost=0.0)

    @staticmethod
    def _evidence_pool(
        runs: list[RecordedRun], goal: GoalSpec, selected: Sequence[str]
    ) -> list[RecordedRun]:
        goal_runs = [run for run in runs if run.goal == goal.name]
        if goal_runs:
            return goal_runs
        selected_keys = {route_node_key(name) for name in selected}
        if selected_keys:
            companions = [run for run in runs if selected_keys & run.step_keys()]
            if companions:
                return companions
        return runs


def _render_candidate(contract: NodeContract) -> list[str]:
    stats = contract.stats or NodeStats()
    cost = f"{stats.cost_ewma:.4g}" if stats.cost_ewma is not None else "unmeasured"
    consumes = ", ".join(s.name for s in contract.consumes) or "nothing"
    lines = [
        f"- id: {contract.id}",
        f"  name: {contract.name}",
        f"  verified: {stats.successes} successes / {stats.failures} failures",
        f"  cost: {cost}",
        f"  consumes: {consumes}",
    ]
    if contract.description:
        lines.append(f"  description: {contract.description}")
    return lines
