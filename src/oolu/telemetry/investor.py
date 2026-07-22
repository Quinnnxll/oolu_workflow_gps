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
    """One metric's standing identity AND its contract: the key the
    ledger files under, the words the panel shows, how it is fed — and
    the matrix's contract fields (formula, owner, cadence, thresholds,
    version), so every number on the investor panel answers "defined
    how, owned by whom, healthy when"."""

    key: str  # e.g. "users.daily_active"
    label: str
    group: str  # engagement | nodes | executions | model | capital | code | seo
    unit: str = ""  # "", "users", "USD", "tokens", "minutes", "count", "%"
    source: str = "auto"  # auto | manual
    description: str = ""
    # -- the metric contract ------------------------------------------- #
    section: str = ""  # the matrix section this metric reports under
    formula: str = ""  # the business definition, in one line
    owner: str = "operator"
    update_frequency: str = "daily"  # realtime | daily | weekly | monthly
    # Health thresholds, honoring direction: "up" = higher is better
    # (warning/critical are floors), "down" = lower is better (ceilings).
    direction: str = "up"
    target: float | None = None
    warning: float | None = None
    critical: float | None = None
    version: str = "1"
    # Rides the executive summary strip.
    executive: bool = False


def metric_status(spec: MetricSpec, value: float | None) -> str:
    """ok | warning | critical | unknown — the spec's thresholds applied
    with its direction. No thresholds → ok whenever a value exists."""
    if value is None:
        return "unknown"
    if spec.warning is None and spec.critical is None:
        return "ok"
    if spec.direction == "down":
        if spec.critical is not None and value >= spec.critical:
            return "critical"
        if spec.warning is not None and value >= spec.warning:
            return "warning"
        return "ok"
    if spec.critical is not None and value <= spec.critical:
        return "critical"
    if spec.warning is not None and value <= spec.warning:
        return "warning"
    return "ok"


# The starting catalog — the shape scales to hundreds: registering a
# spec is one line, and the snapshot table never changes.
METRIC_CATALOG: tuple[MetricSpec, ...] = (
    MetricSpec(
        "users.daily_active", "Daily active users", "engagement", "users",
        description="Distinct accounts that moved work today.",
        section="product_usage_and_attention",
        formula="distinct principals with run activity in the last day",
        executive=True,
    ),
    MetricSpec(
        "users.weekly_active", "Weekly active users", "engagement", "users",
        description="Distinct accounts that moved work in the last 7 days.",
        section="product_usage_and_attention",
        formula="distinct principals with run activity in the last 7 days",
    ),
    MetricSpec(
        "users.monthly_active", "Monthly active users", "engagement", "users",
        description="Distinct accounts that moved work in the last 30 days.",
        section="product_usage_and_attention",
        formula="distinct principals with run activity in the last 30 days",
    ),
    MetricSpec(
        "users.stickiness_dau_mau", "Stickiness (DAU/MAU)", "engagement", "%",
        description="How much of the monthly base shows up on a given day.",
        section="product_usage_and_attention",
        formula="daily_active_users / monthly_active_users * 100",
        target=40.0, warning=20.0, critical=10.0,
    ),
    MetricSpec(
        "workflows.completed_daily", "Successful workflows today",
        "executions", "count",
        description="Runs that reached COMPLETED today.",
        section="workflow_automation",
        formula="runs completed today",
        executive=True,
    ),
    MetricSpec(
        "workflows.success_rate", "Workflow success rate", "executions", "%",
        description="Completed runs among today's terminal runs.",
        section="workflow_automation",
        formula="successful_workflows / completed_workflows * 100",
        target=95.0, warning=85.0, critical=70.0,
        executive=True,
    ),
    MetricSpec(
        "workflows.first_attempt_success_rate",
        "First-attempt success rate", "executions", "%",
        description="Today's completed runs that needed no retry.",
        section="workflow_automation",
        formula="first_attempt_successes / workflows_started * 100",
        target=90.0, warning=75.0, critical=50.0,
    ),
    MetricSpec(
        "revenue.earnings_daily_usd", "Net earnings today", "capital", "USD",
        description="Noder earnings accrued today across the platform.",
        section="financial_performance",
        formula="sum of earnings entries created today",
        executive=True,
    ),
    MetricSpec(
        "cost.model_month_usd", "Model spend this month", "model", "USD",
        description="Month-to-date model/API cost across every tenant.",
        section="cost_structure",
        formula="sum of tenant model usage cost for the current month",
        direction="down",
        executive=True,
    ),
    MetricSpec(
        "retention.day7_pct", "Day-7 retention", "engagement", "%",
        description="Of accounts active 7 days ago, the share active in "
        "the last day.",
        section="retention_and_churn",
        formula="active(day-7) ∩ active(today) / active(day-7) * 100",
        target=40.0, warning=20.0, critical=10.0,
        executive=True,
    ),
    MetricSpec(
        "reliability.request_success_pct", "Request success rate",
        "executions", "%",
        description="Gateway requests answered without an error since "
        "this process started — the availability proxy.",
        section="reliability_and_observability",
        formula="(requests - errors) / requests * 100",
        update_frequency="realtime",
        target=99.9, warning=99.0, critical=95.0,
        executive=True,
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


# The investor scorecard, exactly as the matrix weighs it. Each pillar
# names the metric keys that feed it; a pillar with NO data on this host
# is EXCLUDED and the remaining weights renormalize — an empty pillar is
# named, never averaged in as a fake zero.
SCORECARD_PILLARS: tuple[dict, ...] = (
    {
        "name": "growth", "weight": 0.20,
        "inputs": (
            "users.daily_active", "executions.daily",
            "revenue.earnings_daily_usd",
        ),
        "basis": "growth",  # scored on the 7-day trend
    },
    {
        "name": "retention", "weight": 0.15,
        "inputs": ("retention.day7_pct", "users.stickiness_dau_mau"),
        "basis": "status",  # scored on thresholds
    },
    {
        "name": "economics", "weight": 0.15,
        "inputs": ("capital.in_app_usd", "revenue.earnings_daily_usd"),
        "basis": "growth",
    },
    {
        "name": "product", "weight": 0.10,
        "inputs": ("workflows.first_attempt_success_rate",),
        "basis": "status",
    },
    {
        "name": "technology", "weight": 0.10,
        "inputs": (
            "workflows.success_rate", "reliability.request_success_pct",
        ),
        "basis": "status",
    },
    {
        "name": "physical_execution", "weight": 0.10,
        "inputs": (),  # no physical fleet on this platform yet — excluded
        "basis": "status",
    },
    {
        "name": "market", "weight": 0.10,
        "inputs": ("seo.impressions", "seo.clicks"),
        "basis": "growth",
    },
    {
        "name": "moat", "weight": 0.10,
        "inputs": ("nodes.total",),
        "basis": "growth",
    },
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
        grouped for display — the contract fields riding along."""
        self.collect()
        latest = self._store.latest()
        items = []
        for spec in self._catalog:
            point = latest.get(spec.key)
            value = point["value"] if point else None
            items.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "group": spec.group,
                    "unit": spec.unit,
                    "source": spec.source,
                    "description": spec.description,
                    "section": spec.section,
                    "formula": spec.formula,
                    "owner": spec.owner,
                    "update_frequency": spec.update_frequency,
                    "target": spec.target,
                    "status": metric_status(spec, value),
                    "version": spec.version,
                    "value": value,
                    "as_of": point["day"] if point else None,
                }
            )
        return {"items": items}

    # -- the executive summary ------------------------------------------ #
    def summary(self) -> dict:
        """The matrix's executive strip: each executive metric with its
        status components — actual, previous period, growth rate,
        target, and threshold status. Previous period is the day before
        the newest point in the ledger; growth is honest None when
        there is nothing to compare against."""
        self.collect()
        history = self._store.history(days=90)
        items = []
        for spec in self._catalog:
            if not spec.executive:
                continue
            points = history.get(spec.key, [])
            actual = points[-1]["value"] if points else None
            previous = points[-2]["value"] if len(points) > 1 else None
            growth = None
            if actual is not None and previous not in (None, 0):
                growth = (actual - previous) / abs(previous) * 100
            items.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "unit": spec.unit,
                    "actual": actual,
                    "previous_period": previous,
                    "growth_rate_pct": growth,
                    "target": spec.target,
                    "status": metric_status(spec, actual),
                    "owner": spec.owner,
                    "as_of": points[-1]["day"] if points else None,
                }
            )
        return {"items": items}

    # -- the weighted scorecard ----------------------------------------- #
    _STATUS_SCORE = {"ok": 100.0, "warning": 60.0, "critical": 20.0}

    def _growth_score(self, points: list[dict]) -> float | None:
        """A 7-day trend as a score: growing ≥5% → 100, flat-or-growing
        → 70, shrinking → 30. Under two points → None (no basis)."""
        if len(points) < 2:
            return None
        window = points[-8:]
        first, last = window[0]["value"], window[-1]["value"]
        if first == 0:
            return 100.0 if last > 0 else 70.0
        change = (last - first) / abs(first)
        if change >= 0.05:
            return 100.0
        if change >= 0:
            return 70.0
        return 30.0

    def scorecard(self) -> dict:
        """The matrix's weighted composite. Every pillar scores from its
        inputs (threshold status or 7-day trend, per its basis); pillars
        with NO scoreable input are EXCLUDED and named, and the weights
        renormalize over what remains — the score never averages in a
        pillar this platform cannot measure yet."""
        self.collect()
        history = self._store.history(days=90)
        pillars: list[dict] = []
        excluded: list[str] = []
        for pillar in SCORECARD_PILLARS:
            components: list[dict] = []
            for key in pillar["inputs"]:
                spec = self.spec(key)
                points = history.get(key, [])
                if spec is None or not points:
                    continue
                if pillar["basis"] == "status":
                    status = metric_status(spec, points[-1]["value"])
                    score = self._STATUS_SCORE.get(status)
                    basis = f"status:{status}"
                else:
                    score = self._growth_score(points)
                    basis = "trend:7d"
                if score is None:
                    continue
                components.append({"key": key, "score": score, "basis": basis})
            if not components:
                excluded.append(pillar["name"])
                continue
            pillars.append(
                {
                    "name": pillar["name"],
                    "weight": pillar["weight"],
                    "score": sum(c["score"] for c in components)
                    / len(components),
                    "inputs": components,
                }
            )
        total_weight = sum(p["weight"] for p in pillars)
        for p in pillars:
            p["effective_weight"] = (
                p["weight"] / total_weight if total_weight else 0.0
            )
        score = sum(p["score"] * p["effective_weight"] for p in pillars)
        return {
            "score": round(score, 1) if pillars else None,
            "pillars": pillars,
            "excluded": excluded,
        }
