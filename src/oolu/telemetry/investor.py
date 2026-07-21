"""The investor metrics tracker: one catalog, one daily ledger, live.

What an investor panel needs is not a dashboard bolted to one query —
it is a CATALOG of metrics that can grow to hundreds without a schema
change, a daily SNAPSHOT ledger so every number has a time series
behind it, and one collection pass that reads the real stores the app
already writes (runs, nodes, model usage, earnings) rather than a
parallel bookkeeping that drifts.

Three kinds of metric, one table:

- **auto** — computed from the app's own stores on every collection
  pass (daily active users, node executions, token draw, capital…).
- **manual** — recorded through the API by the operator or an external
  pipeline (GitHub commits, SEO impressions, capital injections):
  sources the app cannot see but the panel must show.
- Anything future — register a spec and either wire a reader or record
  it manually; the store, history, and panel need no change.

Every value lands in ``metric_snapshots (metric, day, value)`` — the
series the monitor panel charts and the analysis report reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable


@dataclass(frozen=True)
class MetricSpec:
    """One metric's standing identity: the key the ledger files under,
    the words the panel shows, and how it is fed."""

    key: str  # e.g. "users.daily_active"
    label: str
    group: str  # engagement | nodes | executions | model | capital | code | seo
    unit: str = ""  # "", "users", "USD", "tokens", "minutes", "count"
    source: str = "auto"  # auto | manual
    description: str = ""


# The starting catalog — the shape scales to hundreds: registering a
# spec is one line, and the snapshot table never changes.
METRIC_CATALOG: tuple[MetricSpec, ...] = (
    MetricSpec(
        "users.daily_active", "Daily active users", "engagement", "users",
        description="Distinct accounts that moved work today.",
    ),
    MetricSpec(
        "users.weekly_active", "Weekly active users", "engagement", "users",
        description="Distinct accounts that moved work in the last 7 days.",
    ),
    MetricSpec(
        "engagement.avg_daily_minutes", "Avg daily use time", "engagement",
        "minutes",
        description="Mean span between an account's first and last move "
        "today — an approximation from activity stamps, honestly labeled.",
    ),
    MetricSpec("nodes.total", "Total nodes in nodeplace", "nodes", "count"),
    MetricSpec(
        "executions.daily", "Node executions today", "executions", "count"
    ),
    MetricSpec(
        "executions.total", "Node executions all-time", "executions", "count"
    ),
    MetricSpec("model.tokens_total", "Token usage all-time", "model", "tokens"),
    MetricSpec("model.calls_total", "Model calls all-time", "model", "count"),
    MetricSpec("model.spend_usd", "Model spend all-time", "model", "USD"),
    MetricSpec(
        "capital.in_app_usd", "Capital size in app", "capital", "USD",
        description="Noder balances standing in the app: available + "
        "pending + reserved earnings.",
    ),
    MetricSpec(
        "code.commits", "GitHub commits", "code", "count", source="manual",
        description="Recorded by the release pipeline or the operator.",
    ),
    MetricSpec(
        "seo.impressions", "Search impressions", "seo", "count",
        source="manual",
    ),
    MetricSpec("seo.clicks", "Search clicks", "seo", "count", source="manual"),
    MetricSpec(
        "seo.avg_position", "Avg search position", "seo", "", source="manual"
    ),
    MetricSpec(
        "capital.raised_usd", "Capital raised", "capital", "USD",
        source="manual",
    ),
)


_SCHEMA = """CREATE TABLE IF NOT EXISTS metric_snapshots (
    metric TEXT NOT NULL,
    day TEXT NOT NULL,
    value REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (metric, day)
)"""


class MetricsSnapshotStore:
    """The daily ledger every metric files into — auto and manual alike.
    One row per (metric, day); a re-collection the same day updates in
    place, so the series stays one honest point per day."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def _day(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def record(self, metric: str, value: float, *, day: str | None = None) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO metric_snapshots (metric, day, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(metric, day) DO UPDATE SET
                     value = excluded.value, updated_at = excluded.updated_at""",
                (
                    metric,
                    day or self._day(),
                    float(value),
                    self._clock().isoformat(),
                ),
            )

    def latest(self) -> dict[str, dict]:
        """Each metric's newest point: {metric: {value, day}}."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT metric, day, value FROM metric_snapshots s
                   WHERE day = (SELECT MAX(day) FROM metric_snapshots
                                WHERE metric = s.metric)"""
            ).fetchall()
        return {
            r["metric"]: {"value": r["value"], "day": r["day"]} for r in rows
        }

    def history(self, *, days: int = 90) -> dict[str, list[dict]]:
        """Every metric's series, oldest first, bounded to ``days`` points
        per metric — what the panel charts."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT metric, day, value FROM metric_snapshots"
                " ORDER BY metric, day"
            ).fetchall()
        series: dict[str, list[dict]] = {}
        for r in rows:
            series.setdefault(r["metric"], []).append(
                {"day": r["day"], "value": r["value"]}
            )
        return {m: pts[-days:] for m, pts in series.items()}


class InvestorMetricsService:
    """The collection pass: every auto metric's reader runs, the values
    land in the ledger, and the current view merges auto reads with the
    newest manual recordings. Readers are injected callables over the
    app's REAL stores — a missing reader simply leaves that metric to
    its manual door, never a fake zero."""

    def __init__(
        self,
        store: MetricsSnapshotStore,
        *,
        readers: dict[str, Callable[[], float]] | None = None,
        catalog: tuple[MetricSpec, ...] = METRIC_CATALOG,
    ) -> None:
        self._store = store
        self._readers = dict(readers or {})
        self._catalog = catalog

    @property
    def catalog(self) -> tuple[MetricSpec, ...]:
        return self._catalog

    def spec(self, key: str) -> MetricSpec | None:
        return next((s for s in self._catalog if s.key == key), None)

    def collect(self) -> dict[str, float]:
        """Run every wired reader and file today's snapshot. A reader
        that raises is skipped — one broken store must never blank the
        whole panel — and what was skipped is named in the result."""
        collected: dict[str, float] = {}
        for key, reader in self._readers.items():
            try:
                collected[key] = float(reader())
            except Exception:  # noqa: BLE001 - the panel shows the rest
                continue
        for key, value in collected.items():
            self._store.record(key, value)
        return collected

    def record_manual(self, key: str, value: float) -> MetricSpec:
        spec = self.spec(key)
        if spec is None:
            raise KeyError(f"no metric '{key}' in the catalog")
        self._store.record(key, float(value))
        return spec

    def view(self) -> dict:
        """The panel's read: every cataloged metric with its newest
        value (today's collection for auto, last recording for manual),
        grouped for display."""
        self.collect()
        latest = self._store.latest()
        items = []
        for spec in self._catalog:
            point = latest.get(spec.key)
            items.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "group": spec.group,
                    "unit": spec.unit,
                    "source": spec.source,
                    "description": spec.description,
                    "value": point["value"] if point else None,
                    "as_of": point["day"] if point else None,
                }
            )
        return {"items": items}
