"""The small transformer in the ``ProposalModel`` seat.

route-finding-proof.md §5 reserved this socket for exactly three wins the
counting baseline cannot see:

- COLD START: a brand-new node with zero history, where name/slot
  semantics ("tax-form-pdf" ≈ "tax filing document") predict fit;
- CROSS-GOAL GENERALIZATION: what worked for one goal shape transferring
  to a structurally similar one — the parameters are shared token
  embeddings, so every goal's outcomes teach every future goal;
- CONTEXT-CONDITIONED CHOICE: the trace store is context-scoped
  (tenant), so each tenant's ranker learns ITS best providers.

Architecture — genuinely a (tiny) transformer, and deliberately
dependency-free (pure Python, ~10k parameters):

- hashed token embeddings (``buckets`` × ``dim``), trainable, seeded
  deterministically per bucket (crc32, never Python's randomized hash);
- ONE cross-attention head: the goal side (goal words + slot + context)
  forms the query; the candidate's tokens (name, consumed slots,
  description) form keys and values; softmax attention pools the
  candidate into one vector;
- a logistic head scores the pooled vector as P(this candidate serves
  this goal well).

Tokens are words plus their character trigrams, so "tax-form-pdf" and
"tax_filing.pdf" share features without any external vocabulary.

Training is online logistic SGD over the trace store's recorded runs —
the exact "execution result scoring" signal the proof names. The value
path carries the gradient (attention weights are treated as constants
per step — the classic straight-through simplification; a full backprop
through softmax buys nothing at this parameter count).

Containment is the PORT's, not ours, and is why adopting this is safe:
advice enters the assembler's Beta posterior clamped to
``DEFAULT_PROPOSAL_STRENGTH`` (worth three verified runs, no more),
unknown ids are dropped, and any exception downgrades assembly to
verified-history-only. This model can decide thin-history ties and speed
up cold starts; it can never override verified evidence.
"""

from __future__ import annotations

import math
import random
import re
import zlib
from typing import Sequence

from ..knowledge.traces import RecordedRun, TraceStore
from ..skills.contract import NodeContract, Slot
from .assembler import GoalSpec, Proposal

_WORD_RE = re.compile(r"[a-z0-9]+")

# Below this many recorded runs the ranker stays silent: an untrained
# transformer's opinion is noise, and noise is worse than no opinion.
MIN_TRAINING_RUNS = 5


def rank_tokens(text: str, *, cap: int = 48) -> list[str]:
    """Words plus their character trigrams — subword affinity with no
    external vocabulary, so renamings and separators don't hide kinship."""
    tokens: list[str] = []
    for word in _WORD_RE.findall((text or "").lower()):
        tokens.append(word)
        tokens.extend(word[i : i + 3] for i in range(len(word) - 2))
        if len(tokens) >= cap:
            break
    return tokens[:cap]


class TinyTransformerProposalModel:
    """A one-head cross-attention ranker over hashed token embeddings.

    Implements the assembler's ``ProposalModel`` protocol. ``propose``
    never raises and answers ``{}`` (no opinion) until the store holds
    enough runs to have taught it anything.
    """

    def __init__(
        self,
        store: TraceStore | None = None,
        *,
        context: str = "",
        dim: int = 16,
        buckets: int = 512,
        max_runs: int = 500,
        epochs: int = 3,
        learning_rate: float = 0.15,
    ):
        self._store = store
        self._context = context
        self._dim = dim
        self._buckets = buckets
        self._max_runs = max_runs
        self._epochs = epochs
        self._lr = learning_rate
        self._embeddings: dict[int, list[float]] = {}
        self._w = [0.0] * dim
        self._bias = 0.0
        self._trained_on = -1  # run-count signature of the last fit

    # ------------------------------------------------------------------ #
    # The tiny transformer.                                               #
    # ------------------------------------------------------------------ #
    def _embedding(self, token: str) -> list[float]:
        bucket = zlib.crc32(token.encode("utf-8")) % self._buckets
        vector = self._embeddings.get(bucket)
        if vector is None:
            seeded = random.Random(bucket)
            scale = 1.0 / math.sqrt(self._dim)
            vector = [seeded.uniform(-scale, scale) for _ in range(self._dim)]
            self._embeddings[bucket] = vector
        return vector

    def _forward(
        self, goal_tokens: Sequence[str], cand_tokens: Sequence[str]
    ) -> tuple[float, list[float], list[float], list[list[float]]]:
        """One cross-attention pass: ``(p, alphas, pooled, cand_embs)``."""
        dim = self._dim
        goal_embs = [self._embedding(t) for t in goal_tokens] or [[0.0] * dim]
        query = [
            sum(vector[j] for vector in goal_embs) / len(goal_embs)
            for j in range(dim)
        ]
        cand_embs = [self._embedding(t) for t in cand_tokens] or [[0.0] * dim]
        scale = 1.0 / math.sqrt(dim)
        logits = [
            sum(query[j] * vector[j] for j in range(dim)) * scale
            for vector in cand_embs
        ]
        peak = max(logits)
        exps = [math.exp(logit - peak) for logit in logits]
        total = sum(exps) or 1.0
        alphas = [value / total for value in exps]
        pooled = [
            sum(alpha * vector[j] for alpha, vector in zip(alphas, cand_embs))
            for j in range(dim)
        ]
        z = sum(self._w[j] * pooled[j] for j in range(dim)) + self._bias
        z = max(-30.0, min(30.0, z))
        p = 1.0 / (1.0 + math.exp(-z))
        return p, alphas, pooled, cand_embs

    def _learn(
        self, goal_tokens: Sequence[str], cand_tokens: Sequence[str], label: float
    ) -> None:
        p, alphas, pooled, cand_embs = self._forward(goal_tokens, cand_tokens)
        error = label - p
        step = self._lr * error
        old_w = list(self._w)
        for j in range(self._dim):
            self._w[j] += step * pooled[j]
        self._bias += step
        # The value path carries the gradient into the embeddings; the
        # attention weights ride along as constants (straight-through).
        for alpha, vector in zip(alphas, cand_embs):
            for j in range(self._dim):
                vector[j] += step * alpha * old_w[j]

    # ------------------------------------------------------------------ #
    # Training from the trace store's outcomes.                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _goal_side(goal_name: str, context: str, slot: Slot | None) -> list[str]:
        tokens = rank_tokens(goal_name)
        if slot is not None:
            tokens += rank_tokens(f"{slot.name} {slot.value_type}")
        if context:
            tokens += [f"ctx:{t}" for t in rank_tokens(context, cap=8)]
        return tokens[:64]

    def fit(self, runs: Sequence[RecordedRun]) -> int:
        """Train on recorded outcomes; returns how many examples it saw.
        Every step of a run is one example: the node served this goal in
        a run that succeeded (label 1) or failed (label 0)."""
        examples = 0
        for _ in range(self._epochs):
            for run in runs:
                goal_tokens = self._goal_side(run.goal, run.context, None)
                label = 1.0 if run.success else 0.0
                for key in sorted(run.step_keys()):
                    self._learn(goal_tokens, rank_tokens(key), label)
                    examples += 1
        return examples

    def _refresh(self) -> int:
        """Refit when the store grew — cheap at this parameter count.
        Returns the number of runs the model currently knows."""
        if self._store is None:
            return 0
        runs = self._store.runs(context=self._context, limit=self._max_runs)
        if len(runs) != self._trained_on:
            self._embeddings.clear()
            self._w = [0.0] * self._dim
            self._bias = 0.0
            self.fit(runs)
            self._trained_on = len(runs)
        return self._trained_on

    # ------------------------------------------------------------------ #
    # The ProposalModel protocol.                                         #
    # ------------------------------------------------------------------ #
    def propose(
        self,
        *,
        goal: GoalSpec,
        slot: Slot,
        selected: Sequence[str],
        candidates: Sequence[NodeContract],
    ) -> Proposal:
        try:
            known = self._refresh()
            if known < MIN_TRAINING_RUNS:
                return Proposal(weights={}, cost=0.0)
            goal_tokens = self._goal_side(goal.name, self._context, slot)
            weights: dict[str, float] = {}
            for candidate in candidates:
                side = " ".join(
                    [
                        candidate.name,
                        " ".join(s.name for s in candidate.consumes),
                        candidate.description or "",
                    ]
                )
                p, *_rest = self._forward(goal_tokens, rank_tokens(side))
                weights[candidate.id] = min(1.0, max(0.0, p))
            return Proposal(weights=weights, cost=0.0)
        except Exception:  # noqa: BLE001 - advice is optional by contract
            return Proposal(weights={}, cost=0.0)


class LearnedProposalStack:
    """Counts first, the transformer for what counts cannot see.

    ``TraceProposalModel``'s Beta means are direct evidence — they outrank
    the ranker wherever both have an opinion. The transformer fills ONLY
    the candidates the counting pool never saw: exactly the cold-start
    and cross-goal cases the proof reserved this seat for.
    """

    def __init__(self, counted, learned):
        self._counted = counted
        self._learned = learned

    def propose(
        self,
        *,
        goal: GoalSpec,
        slot: Slot,
        selected: Sequence[str],
        candidates: Sequence[NodeContract],
    ) -> Proposal:
        counted = self._counted.propose(
            goal=goal, slot=slot, selected=selected, candidates=candidates
        )
        unseen = [c for c in candidates if c.id not in counted.weights]
        if not unseen:
            return counted
        learned = self._learned.propose(
            goal=goal, slot=slot, selected=selected, candidates=unseen
        )
        return Proposal(
            weights={**learned.weights, **counted.weights},
            cost=counted.cost + learned.cost,
        )
