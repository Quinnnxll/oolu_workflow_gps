"""Reinforcement route learning — M5 of the memory-stack plan.

The RL rungs, in order, each gated by the last — never skipping to
online RL (capability-web §20). Everything here is a READER of stores
that already exist and a bounded advisor to seats that already decide;
nothing in this module executes, spends, or overrides a gate.

- **Rung 1 — the dataset.** ``TraceStore.record_observation`` (landed
  beside the run log it extends) keeps one row per route decision:
  context features and bucket, chosen route, node versions, outcome
  score, actual cost/latency, interventions, reuse created.
  :func:`reward` is the §20-shaped expression over that row, under the
  repo's standing bar: only verified outcomes teach — an unverified
  run has NO reward, not a negative one (its failure already counts in
  the Beta posterior; pretending to know its magnitude would be
  hallucination fuel).
- **Rung 2 — contextual bandit.** :func:`context_bucket` folds the
  features the plan names (goal class, desk shape, model manifest)
  into a canonical bucket string; passed as the ``TraceStore`` context
  it makes every existing Thompson choice (the assembler's, the route
  optimizer's) a posterior per (route, context bucket) — with the
  store's own global fallback as the cold-start floor. No new chooser:
  the bandit was always there, the context just reached it.
- **Rung 3 — learned reranker.** :class:`ObservationReranker` speaks
  the assembler's ``ProposalModel`` protocol: endorsements from mean
  verified reward in the current bucket (global fallback), candidates
  without evidence omitted, every exception downgrading to no-advice.
  It enters at ``DEFAULT_PROPOSAL_STRENGTH`` like every other advisor;
  rollback is unplugging the port. Promoted M4 skills speak here too —
  ``skills_for`` is consulted and a skill-covered candidate is
  endorsed, which is M4's route-side reader wired to a real seat.
- **Rung 4 — offline policy.** :func:`grow_corpus` widens the corpus
  exporter's JSONL with M4's promoted skills (as verified sequences)
  and M5's observations (as reward-carrying decision rows), each line
  naming its ``source``. Training happens off-box; the audition is the
  standing replay harness, and nothing bills until ``earns_its_cost``
  passes — the gate is already in code (``orchestrator/replay.py``).
- **Rung 5 — constrained exploration.** :func:`exploration_rng` is the
  only door through which exploration randomness reaches a chooser:
  OFF by default, refused outright for any irreversible action
  (structurally — no budget buys it back), refused past the risk
  budget or the spend cap. Rolling a rung back is a config change.

Config over code for every switch: :class:`RouteLearningConfig` turns
each rung off independently, and every helper honors its flag by
returning the do-nothing value (None / no advice) — the system
degrades to the frozen heuristic, never below it.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from .knowledge.corpus import build_examples
from .knowledge.traces import RouteObservation, TraceStore, route_node_key

# Exploration prices risk with the SAME weights the SOP risk budget
# uses — one vocabulary of caution, not two. An unknown risk level
# prices as a write; "irreversible" is not priced at all: it is refused.
from .orchestrator.adaptive import RISK_WEIGHTS
from .planner.vocab import DEFAULT_GOAL_BUCKETS, goal_token


class RouteLearningConfig(BaseModel):
    """The rung switches and reward weights — every OFF is a config
    change, exactly as the plan's acceptance demands. Exploration
    (rung 5) defaults OFF: randomness in a chooser is opt-in."""

    model_config = ConfigDict(frozen=True)

    observations_on: bool = True  # rung 1
    contextual_bandit_on: bool = True  # rung 2
    reranker_on: bool = True  # rung 3
    exploration_on: bool = False  # rung 5 — opt-in, never assumed

    # The §20 reward expression's weights: reward = outcome_score
    # - cost_weight*cost - latency_weight*latency
    # - intervention_weight*interventions + reuse_weight*reuse_created.
    cost_weight: float = Field(default=0.01, ge=0.0)
    latency_weight: float = Field(default=0.01, ge=0.0)
    intervention_weight: float = Field(default=0.1, ge=0.0)
    reuse_weight: float = Field(default=0.05, ge=0.0)

    # Rung 5's walls: total risk weight a route may carry and still be
    # explored, and the spend beyond which exploration stops.
    exploration_risk_budget: float = Field(default=1.0, ge=0.0)
    exploration_spend_cap: float = Field(default=0.0, ge=0.0)


DEFAULT_CONFIG = RouteLearningConfig()


# --------------------------------------------------------------------- #
# Rung 1 — the dataset's reward expression.                             #
# --------------------------------------------------------------------- #
def reward(
    observation: RouteObservation, *, config: RouteLearningConfig = DEFAULT_CONFIG
) -> float | None:
    """The §20 reward for one observation — or ``None`` for an
    unverified run. The verified-only bar is deliberate asymmetry: the
    failure already counts once, in the success posterior; assigning it
    a reward magnitude would let an unverified outcome TEACH, which is
    the one thing every memory tier refuses."""
    if not observation.success:
        return None
    return (
        float(observation.outcome_score)
        - config.cost_weight * float(observation.cost)
        - config.latency_weight * float(observation.latency)
        - config.intervention_weight * int(observation.interventions)
        + config.reuse_weight * int(observation.reuse_created)
    )


def observe_route(
    store: TraceStore,
    *,
    goal: str,
    route: str,
    features: dict | None = None,
    success: bool,
    outcome_score: float,
    cost: float = 0.0,
    latency: float = 0.0,
    node_versions: Sequence[str] = (),
    interventions: int = 0,
    reuse_created: int = 0,
    config: RouteLearningConfig = DEFAULT_CONFIG,
) -> int | None:
    """Record one route decision with its bucket derived from its
    features — the one writer seats call. Returns the row id, or None
    with rung 1 switched off (the OFF switch is a config change)."""
    if not config.observations_on:
        return None
    return store.record_observation(
        goal=goal,
        route=route,
        context_bucket=context_bucket(features or {}),
        features=features or {},
        success=success,
        outcome_score=outcome_score,
        cost=cost,
        latency=latency,
        node_versions=node_versions,
        interventions=interventions,
        reuse_created=reuse_created,
    )


# --------------------------------------------------------------------- #
# Rung 2 — context features become the bucket the bandit already keys.  #
# --------------------------------------------------------------------- #
def goal_class(goal: str, *, buckets: int = DEFAULT_GOAL_BUCKETS) -> str:
    """Free goal text folded into its bounded class token — the same
    band the node-token vocabulary conditions on, so the bandit and the
    planner agree on what 'the same kind of mission' means."""
    return goal_token(goal, buckets=buckets)


def context_bucket(features: dict) -> str:
    """A canonical, bounded bucket string from context features.

    Keys sort, values slug (lowercase, ``[a-z0-9._-]``, capped) — so the
    same context always lands the same bucket across processes, and the
    bucket stays readable in the database ("why was this chosen" should
    never require a hash table). Free-text values belong PRE-bucketed
    (``goal_class``); this function bounds each value's length but
    cannot bound a caller who feeds unbounded distinct values — the
    features the plan names (goal class, desk shape, model manifest)
    are all naturally bounded vocabularies.
    """
    parts = []
    for key in sorted(features):
        value = _slug(features[key])
        if value:
            parts.append(f"{_slug(key)}={value}")
    return "|".join(parts)


def _slug(value) -> str:
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9._-]+", "-", text).strip("-")[:48]


def contextual_choice(
    store: TraceStore,
    routes: Sequence[str],
    *,
    features: dict | None = None,
    rng: random.Random | None = None,
    config: RouteLearningConfig = DEFAULT_CONFIG,
) -> str:
    """The contextual bandit in one call: the posterior per (route,
    context bucket), Thompson-sampled with an ``rng``, greedy on the
    posterior mean without one. With rung 2 off the bucket is dropped
    and this IS the frozen context-free heuristic — the floor the plan
    says the system degrades to, never below."""
    if not routes:
        raise ValueError("no routes to choose among")
    bucket = (
        context_bucket(features or {}) if config.contextual_bandit_on else ""
    )

    def sampled(route: str) -> float:
        if rng is not None:
            return store.sample_success(route_node_key(route), bucket, rng=rng)
        return store.posterior(route_node_key(route), bucket).mean

    # Highest sample wins; then name, so ties are stable (the repo's
    # standing tiebreak everywhere a chooser must be deterministic).
    return min(routes, key=lambda route: (-sampled(route), route))


# --------------------------------------------------------------------- #
# Rung 3 — the learned reranker behind the ProposalModel port.          #
# --------------------------------------------------------------------- #
# A promoted skill's endorsement of a candidate it covers: strong
# enough to decide a thin-history tie (it enters at proposal strength,
# worth ~3 runs), weak enough that live evidence overrides it.
SKILL_ENDORSEMENT = 0.8


class ObservationReranker:
    """A ``ProposalModel`` whose opinions are mean verified rewards.

    For each candidate the reranker reads the route's observations in
    the CURRENT bucket (global fallback when the bucket is silent),
    keeps only verified rewards (rung 1's bar), and endorses on the
    normalized mean. Candidates with no evidence are omitted — no
    opinion, never a zero that reads as "advised against". A candidate
    covered by a promoted M4 skill is endorsed at least
    ``SKILL_ENDORSEMENT`` — ``skills_for`` consulted by a live seat.
    Exceptions downgrade to no-advice: the port's containment law.
    """

    def __init__(
        self,
        store: TraceStore,
        *,
        spine=None,
        tenant: str = "",
        features: dict | None = None,
        config: RouteLearningConfig = DEFAULT_CONFIG,
    ):
        self._store = store
        self._spine = spine
        self._tenant = tenant
        self._features = dict(features or {})
        self._config = config

    def propose(self, *, goal, slot, selected, candidates):
        from .orchestrator.assembler import Proposal  # local: avoid a cycle

        try:
            if not self._config.reranker_on:
                return Proposal(weights={}, cost=0.0)
            bucket = context_bucket(self._features)
            rewards: dict[str, float] = {}
            for candidate in candidates:
                # Observations key the route by its NAME (what the seat
                # chose); the route:-prefixed key space belongs to the
                # trace posteriors and the motif corpus.
                mean = self._mean_reward(candidate.name, bucket)
                if mean is not None:
                    rewards[candidate.id] = mean
            weights = _normalized(rewards)
            for candidate_id in self._skill_covered(selected, candidates):
                weights[candidate_id] = max(
                    weights.get(candidate_id, 0.0), SKILL_ENDORSEMENT
                )
            return Proposal(weights=weights, cost=0.0)
        except Exception:  # noqa: BLE001 - advice is optional by contract
            return Proposal(weights={}, cost=0.0)

    def _mean_reward(self, route: str, bucket: str) -> float | None:
        rows = self._store.observations(route=route, context_bucket=bucket)
        if not rows and bucket:
            rows = self._store.observations(route=route, context_bucket="")
        values = [
            r
            for r in (reward(row, config=self._config) for row in rows)
            if r is not None
        ]
        if not values:
            return None
        return sum(values) / len(values)

    def _skill_covered(self, selected, candidates) -> list[str]:
        """Candidate ids whose next-step placement completes or extends
        a promoted skill's motif — M4's reader, consulted at pick time."""
        if self._spine is None:
            return []
        from .skillinduction import skills_for

        covered: list[str] = []
        prior = [route_node_key(str(name)) for name in selected]
        for candidate in candidates:
            steps = [*prior, route_node_key(candidate.name)]
            if skills_for(self._spine, tenant=self._tenant, subject_steps=steps):
                covered.append(candidate.id)
        return covered


def reranker_for(
    config: RouteLearningConfig,
    store: TraceStore,
    *,
    spine=None,
    tenant: str = "",
    features: dict | None = None,
) -> ObservationReranker | None:
    """The rung's plug point: the reranker when rung 3 is on, None when
    off — and None is precisely 'unplugging the port', the rollback the
    plan promises at every rung."""
    if not config.reranker_on:
        return None
    return ObservationReranker(
        store, spine=spine, tenant=tenant, features=features, config=config
    )


# --------------------------------------------------------------------- #
# Rung 4 — the corpus grows: skills and observations join the export.   #
# --------------------------------------------------------------------- #
def grow_corpus(
    store: TraceStore,
    path,
    *,
    spine=None,
    tenant: str = "",
    limit: int = 100_000,
    only_successful: bool = False,
    config: RouteLearningConfig = DEFAULT_CONFIG,
) -> dict:
    """The corpus exporter, widened for the offline policy (rung 4).

    Three record kinds land in one JSONL, each line naming its
    ``source``: ``run`` (the standing prefix→next examples), ``skill``
    (each promoted M4 skill's steps as one verified sequence — the
    compounding the plan says makes the corpus worth learning over),
    and ``observation`` (rung 1 rows with their computed reward, None
    kept as null for unverified — a training job filters or weighs,
    this exporter never editorializes). Returns per-source counts.
    Training happens off-box; the audition is ``orchestrator/replay``
    and nothing bills until ``earns_its_cost`` passes.
    """
    runs = list(reversed(store.runs(limit=limit)))
    examples = build_examples(runs, only_successful=only_successful)
    counts = {"run": 0, "skill": 0, "observation": 0}
    with Path(path).open("w", encoding="utf-8") as sink:
        for example in examples:
            record = {
                "source": "run",
                "goal": example.goal,
                "context": example.context,
                "prefix": list(example.prefix),
                "next_node": example.next_node,
                "next_ok": example.next_ok,
                "run_success": example.run_success,
            }
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts["run"] += 1
        for skill in _promoted_skills(store, spine, tenant):
            value = skill.get("structured_value") or {}
            record = {
                "source": "skill",
                "steps": list(value.get("steps", [])),
                "support": value.get("support"),
                "contexts": list(value.get("contexts", [])),
                "provenance": list(skill.get("provenance") or []),
            }
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts["skill"] += 1
        for row in reversed(store.observations(limit=limit)):
            record = {
                "source": "observation",
                "goal": row.goal,
                "route": row.route,
                "context_bucket": row.context_bucket,
                "features": row.features,
                "success": row.success,
                "reward": reward(row, config=config),
                "cost": row.cost,
                "latency": row.latency,
                "node_versions": list(row.node_versions),
                "interventions": row.interventions,
                "reuse_created": row.reuse_created,
            }
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts["observation"] += 1
    return counts


def _promoted_skills(store: TraceStore, spine, tenant: str) -> list[dict]:
    """Every promoted skill whose motif the CURRENT corpus still
    exhibits — found by enumerating the corpus's own candidate motifs
    (``mine_candidates``) and asking the spine's scope-exact reader for
    each. No second reader, no table scan: the corpus is the export
    universe, so a skill whose evidence has entirely aged out of it has
    nothing for a training job to align against and stays home."""
    if spine is None:
        return []
    from .skillinduction import mine_candidates

    found: list[dict] = []
    seen: set[int] = set()
    for candidate in mine_candidates(store):
        motif_key = "→".join(candidate["steps"])
        for row in spine.recall(
            (tenant, f"motif:{motif_key}"), kinds=("skill",), limit=1
        ):
            if row["memory_id"] not in seen:
                seen.add(row["memory_id"])
                found.append(row)
    return found


# --------------------------------------------------------------------- #
# Rung 5 — exploration only inside the walls that already stand.        #
# --------------------------------------------------------------------- #
def exploration_rng(
    *,
    risk_levels: Sequence[str] = (),
    spent: float = 0.0,
    seed: int | None = None,
    config: RouteLearningConfig = DEFAULT_CONFIG,
) -> random.Random | None:
    """The only door exploration randomness enters a chooser through.

    Returns an ``rng`` (Thompson exploration proceeds) or ``None`` (the
    chooser stays greedy on the posterior mean — the frozen floor).
    None whenever: rung 5 is off (the default), ANY action is
    irreversible (structurally blocked — no budget buys it back), the
    route's total risk weight exceeds the risk budget, or spend has
    reached the cap. A policy violation is impossible by construction
    because the violating branch returns before an rng exists."""
    if not config.exploration_on:
        return None
    levels = [str(level) for level in risk_levels]
    if any(level == "irreversible" for level in levels):
        return None
    total_risk = sum(
        RISK_WEIGHTS.get(level, RISK_WEIGHTS["write"]) for level in levels
    )
    if total_risk > config.exploration_risk_budget:
        return None
    if config.exploration_spend_cap and spent >= config.exploration_spend_cap:
        return None
    return random.Random(seed)


# --------------------------------------------------------------------- #
# Metrics (plan §6): regret vs the frozen heuristic, cost per verified. #
# --------------------------------------------------------------------- #
def route_regret(
    chosen_rewards: Sequence[float], best_rewards: Sequence[float]
) -> float:
    """Mean shortfall of the choices actually made against the best
    fixed choice in hindsight, pairwise per decision. The plan's
    acceptance bar is this number, for the learned chooser, strictly
    below the frozen heuristic's on the same replay."""
    if len(chosen_rewards) != len(best_rewards):
        raise ValueError("regret compares the same decisions, pairwise")
    if not chosen_rewards:
        return 0.0
    gaps = [best - got for got, best in zip(chosen_rewards, best_rewards)]
    return sum(gaps) / len(gaps)


def cost_per_verified_state(observations: Sequence[RouteObservation]) -> float | None:
    """Total spend over verified outcomes — the denominator only counts
    what actually verified, because an unverified state is not a state
    the chain reaction can build on. None with nothing verified: an
    honest 'no rate yet', not an infinite or a zero."""
    verified = sum(1 for row in observations if row.success)
    if not verified:
        return None
    return sum(float(row.cost) for row in observations) / verified


def _normalized(values: dict[str, float]) -> dict[str, float]:
    """Rewards to endorsements in [0, 1], order-preserving: the best
    observed mean endorses at 1, the worst at 0 (a single value
    endorses at 1 — evidence with no rival is still evidence)."""
    if not values:
        return {}
    low, high = min(values.values()), max(values.values())
    if high == low:
        return {key: 1.0 for key in values}
    return {
        key: (value - low) / (high - low) for key, value in values.items()
    }
