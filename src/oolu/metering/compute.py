"""Measured sandbox compute, per run, per node (V6).

The isolation seam already measures wall time (``ExecutionResult
.duration_s`` — both the subprocess and the docker backends stamp it);
until now it was dropped on the gateway path, and the marketplace's
"provider cost" was the mean of its own previously recorded estimates —
a loop that, with nothing ever recorded, answered zero.

This store is the measurement's durable landing: one row per sandbox
execution, keyed by the node's own run key (``node:<skill_id>``), priced
at a declared machine rate. The books read cost from here; the
candidate economics read the measured mean from here instead of the
self-referential estimate.
"""

from __future__ import annotations

from datetime import UTC, datetime

# What one hour of sandbox wall time costs, in USD — a declared machine
# rate, deliberately modest: the point is measured-not-invented, and the
# vitality law's floor (−$5/year) is calibrated against it.
COMPUTE_RATE_PER_HOUR = 0.05

_SCHEMA = """CREATE TABLE IF NOT EXISTS compute_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    seconds REAL NOT NULL,
    cost_usd REAL NOT NULL,
    at TEXT NOT NULL
)"""


class ComputeMeterStore:
    def __init__(self, conn, *, rate_per_hour: float = COMPUTE_RATE_PER_HOUR):
        self._conn = conn
        self._rate = float(rate_per_hour)
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)
            db.execute(
                "CREATE INDEX IF NOT EXISTS compute_usage_node_idx"
                " ON compute_usage (node_key)"
            )

    def record(
        self, node_key: str, seconds: float, *, run_id: str = ""
    ) -> None:
        """One sandbox execution's measured wall time — priced at the
        declared rate, never estimated."""
        seconds = max(0.0, float(seconds))
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO compute_usage
                   (node_key, run_id, seconds, cost_usd, at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    str(node_key),
                    str(run_id or ""),
                    seconds,
                    seconds / 3600.0 * self._rate,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def node_cost(self, node_key: str, *, since: datetime | None = None) -> float:
        """Total measured compute cost for one node key, optionally
        windowed — the books' compute line."""
        with self._conn.lock:
            if since is None:
                row = self._conn.db.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) AS c"
                    " FROM compute_usage WHERE node_key = ?",
                    (str(node_key),),
                ).fetchone()
            else:
                row = self._conn.db.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) AS c"
                    " FROM compute_usage WHERE node_key = ? AND at >= ?",
                    (str(node_key), since.isoformat()),
                ).fetchone()
        return float(row["c"])

    def mean_cost(self, node_key: str) -> float | None:
        """The measured mean cost per execution — what replaces the
        self-referential provider-cost estimate in candidate economics.
        None when nothing was ever measured (honesty over invention)."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT AVG(cost_usd) AS m, COUNT(*) AS n"
                " FROM compute_usage WHERE node_key = ?",
                (str(node_key),),
            ).fetchone()
        if not row or not row["n"]:
            return None
        return float(row["m"])
