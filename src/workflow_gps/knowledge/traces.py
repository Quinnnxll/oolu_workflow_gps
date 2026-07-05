"""Execution-trace statistics — the store that makes planning improve with use.

This is the replacement for sequence memorization (the "learn the whole verified
sequence per goal label and replay the mode" approach): instead of remembering
orderings, the store keeps small sufficient statistics that transfer between
goals and sharpen with every run:

- **Per-node Beta posteriors** of success, bucketed by a caller-defined context
  string (empty = global). Selection among alternatives is Thompson sampling
  over these posteriors — the counts are the user's own history, so route
  choice is personalized by construction and keeps exploring under drift.
  With ``recency_decay < 1`` the counts are *discounted*: every new
  observation of a node first multiplies its existing counts by the decay,
  so the posterior tracks what the node has done **lately** — a node that
  regressed last month stops looking as good as ever, and old glory decays
  into honest uncertainty that Thompson sampling then re-explores.
- **A precedence matrix**: for every ordered pair of verified steps observed in
  a trace, a directed win counter. A hard edge is derived only when the order
  is *consistent* across enough observations; pairs with no consistent order
  are parallel by default. This recovers a partial order (a DAG) from linear
  traces — the thing pairwise-adjacency counting cannot do.
- **Per-node cost EWMAs** for route cost estimates.

Everything is persisted in SQLite, so the statistics accumulate across
processes and sessions: the system grows with the user's executions without a
separate training step.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from ..persistence import Migration, migrate

_COST_EWMA_ALPHA = 0.3


@dataclass(frozen=True, slots=True)
class NodeObservation:
    """One executed step inside a trace, in completion order."""

    node_key: str
    ok: bool
    cost: float | None = None


@dataclass(frozen=True, slots=True)
class NodePosterior:
    """Beta posterior parameters for a node's success (uniform Beta(1,1) prior).

    Counts are floats: under recency decay an old observation is worth a
    fraction of a fresh one. With no decay they stay whole numbers.
    """

    successes: float
    failures: float

    @property
    def alpha(self) -> float:
        return 1.0 + self.successes

    @property
    def beta(self) -> float:
        return 1.0 + self.failures

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def observations(self) -> float:
        return self.successes + self.failures


def route_node_key(goal: str) -> str:
    """The node key under which whole-route outcomes are tracked."""
    return f"route:{goal}"


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trace_node_stats (
               node_key TEXT NOT NULL,
               context TEXT NOT NULL DEFAULT '',
               successes INTEGER NOT NULL DEFAULT 0,
               failures INTEGER NOT NULL DEFAULT 0,
               cost_ewma REAL,
               updated_at TEXT NOT NULL,
               PRIMARY KEY (node_key, context)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trace_precedence (
               first TEXT NOT NULL,
               second TEXT NOT NULL,
               wins INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (first, second)
           )"""
    )


def _drop(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS trace_node_stats")
    conn.execute("DROP TABLE IF EXISTS trace_precedence")


TRACE_STORE_MIGRATIONS: tuple[Migration, ...] = (Migration(up=_create, down=_drop),)


class TraceStore:
    """SQLite-backed execution-trace statistics (thread-safe)."""

    def __init__(self, path: str | Path = ":memory:", *, recency_decay: float = 1.0):
        """``recency_decay`` discounts existing counts on every new
        observation of a node (1.0 = never forget, today's default; 0.9 is
        a sensible "trust the recent past" setting — the same knob shape as
        the budget layer's behavioral profile)."""
        if not 0.0 < recency_decay <= 1.0:
            raise ValueError("recency_decay must be in (0, 1]")
        self._decay = recency_decay
        self._lock = threading.RLock()
        location = (
            str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        )
        self._db = sqlite3.connect(location, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._db, TRACE_STORE_MIGRATIONS, label="trace-store")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ #
    # Recording (called after every route execution).                     #
    # ------------------------------------------------------------------ #
    def record_run(
        self,
        *,
        goal: str,
        steps: Sequence[NodeObservation],
        success: bool,
        context: str = "",
    ) -> None:
        """Fold one executed route into the statistics.

        ``steps`` must be in completion order. Precedence is counted over every
        ordered pair of *verified* steps — noisy pairs (parallel branches whose
        completion order varies run to run) cancel out under the consistency
        threshold in :meth:`derive_edges`.
        """
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._bump(route_node_key(goal), context, success, None, now)
            for step in steps:
                self._bump(step.node_key, context, step.ok, step.cost, now)
            verified = [s.node_key for s in steps if s.ok]
            for i, first in enumerate(verified):
                for second in verified[i + 1 :]:
                    if first == second:
                        continue
                    self._db.execute(
                        """INSERT INTO trace_precedence (first, second, wins)
                           VALUES (?, ?, 1)
                           ON CONFLICT(first, second)
                           DO UPDATE SET wins = wins + 1""",
                        (first, second),
                    )
            self._db.commit()

    def _bump(
        self,
        node_key: str,
        context: str,
        ok: bool,
        cost: float | None,
        now: str,
    ) -> None:
        row = self._db.execute(
            "SELECT successes, failures, cost_ewma FROM trace_node_stats "
            "WHERE node_key = ? AND context = ?",
            (node_key, context),
        ).fetchone()
        # Discounted counting: the past fades a little on every fresh
        # observation, so the posterior tracks the node's recent self.
        # With decay 1.0 this is exact integer counting, unchanged.
        successes = (row["successes"] if row else 0) * self._decay + (1 if ok else 0)
        failures = (row["failures"] if row else 0) * self._decay + (0 if ok else 1)
        ewma = row["cost_ewma"] if row else None
        if cost is not None:
            ewma = (
                cost
                if ewma is None
                else (1 - _COST_EWMA_ALPHA) * ewma + _COST_EWMA_ALPHA * cost
            )
        self._db.execute(
            """INSERT OR REPLACE INTO trace_node_stats
               (node_key, context, successes, failures, cost_ewma, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (node_key, context, successes, failures, ewma, now),
        )

    # ------------------------------------------------------------------ #
    # Posteriors + selection.                                             #
    # ------------------------------------------------------------------ #
    def posterior(self, node_key: str, context: str = "") -> NodePosterior:
        """The Beta posterior for a node in a context bucket.

        A context bucket with no observations falls back to the global bucket,
        so a new context starts from the node's overall history rather than
        from ignorance.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT successes, failures FROM trace_node_stats "
                "WHERE node_key = ? AND context = ?",
                (node_key, context),
            ).fetchone()
            if row is None and context != "":
                row = self._db.execute(
                    "SELECT successes, failures FROM trace_node_stats "
                    "WHERE node_key = ? AND context = ''",
                    (node_key,),
                ).fetchone()
        if row is None:
            return NodePosterior(successes=0, failures=0)
        return NodePosterior(successes=row["successes"], failures=row["failures"])

    def sample_success(
        self,
        node_key: str,
        context: str = "",
        *,
        rng: random.Random | None = None,
    ) -> float:
        """One Thompson sample of the node's success probability."""
        post = self.posterior(node_key, context)
        return (rng or random).betavariate(post.alpha, post.beta)

    def expected_cost(self, node_key: str, context: str = "") -> float | None:
        with self._lock:
            row = self._db.execute(
                "SELECT cost_ewma FROM trace_node_stats "
                "WHERE node_key = ? AND context = ?",
                (node_key, context),
            ).fetchone()
            if (row is None or row["cost_ewma"] is None) and context != "":
                row = self._db.execute(
                    "SELECT cost_ewma FROM trace_node_stats "
                    "WHERE node_key = ? AND context = ''",
                    (node_key,),
                ).fetchone()
        return None if row is None else row["cost_ewma"]

    # ------------------------------------------------------------------ #
    # Structure: precedence matrix -> partial order.                      #
    # ------------------------------------------------------------------ #
    def precedence(self, first: str, second: str) -> tuple[int, int]:
        """Directed win counts (first-before-second, second-before-first)."""
        with self._lock:
            a = self._db.execute(
                "SELECT wins FROM trace_precedence WHERE first = ? AND second = ?",
                (first, second),
            ).fetchone()
            b = self._db.execute(
                "SELECT wins FROM trace_precedence WHERE first = ? AND second = ?",
                (second, first),
            ).fetchone()
        return (a["wins"] if a else 0, b["wins"] if b else 0)

    def derive_edges(
        self,
        node_keys: Sequence[str],
        *,
        min_observations: int = 3,
        min_consistency: float = 0.9,
    ) -> list[tuple[str, str]]:
        """Derive learned ``before`` edges among the given nodes.

        An edge a->b is emitted only when the pair has been observed together
        at least ``min_observations`` times AND a preceded b in at least
        ``min_consistency`` of those observations. Everything else is treated
        as parallel. The result is transitively reduced so a linear history
        yields a chain, not a clique.
        """
        keys = list(dict.fromkeys(node_keys))
        edges: set[tuple[str, str]] = set()
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                ab, ba = self.precedence(a, b)
                total = ab + ba
                if total < min_observations:
                    continue
                if ab / total >= min_consistency:
                    edges.add((a, b))
                elif ba / total >= min_consistency:
                    edges.add((b, a))
        return _transitive_reduction(edges)


def _transitive_reduction(edges: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop every edge implied by a longer path through the remaining edges."""
    successors: dict[str, set[str]] = {}
    for a, b in edges:
        successors.setdefault(a, set()).add(b)

    def reachable(start: str, goal: str, skip: tuple[str, str]) -> bool:
        stack, seen = [start], {start}
        while stack:
            node = stack.pop()
            for nxt in successors.get(node, ()):
                if (node, nxt) == skip or nxt in seen:
                    continue
                if nxt == goal:
                    return True
                seen.add(nxt)
                stack.append(nxt)
        return False

    return sorted(edge for edge in edges if not reachable(edge[0], edge[1], edge))
