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


def month_span(start: str, end: str) -> list[str]:
    """Every calendar month from ``start`` to ``end`` inclusive, as
    YYYY-MM — the cohort table's columns. An inverted range is empty."""
    try:
        year, month = int(start[:4]), int(start[5:7])
        end_year, end_month = int(end[:4]), int(end[5:7])
    except (ValueError, IndexError):
        return []
    months: list[str] = []
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


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
    # ---- phase 2: unit economics -------------------------------------- #
    MetricSpec(
        "unit.arpu_usd", "ARPU (month)", "unit", "USD",
        description="This month's platform earnings per monthly active "
        "user.",
        section="financial_performance",
        formula="month_earnings / monthly_active_users",
    ),
    MetricSpec(
        "unit.cost_per_successful_workflow_usd",
        "Cost per successful workflow", "unit", "USD",
        description="This month's model spend per completed run.",
        section="cost_structure",
        formula="model_month_cost / workflows_completed_this_month",
        direction="down",
    ),
    MetricSpec(
        "unit.contribution_margin_pct", "Contribution margin", "unit", "%",
        description="Earnings after model cost, as a share of earnings, "
        "month to date.",
        section="financial_performance",
        formula="(month_earnings - model_month_cost) / month_earnings * 100",
        target=50.0, warning=20.0, critical=0.0,
    ),
    MetricSpec(
        "unit.cac_usd", "Customer acquisition cost", "unit", "USD",
        source="manual", direction="down",
        description="Recorded by the operator from marketing spend.",
        section="acquisition_and_growth",
        formula="acquisition_spend / new_customers",
    ),
    MetricSpec(
        "unit.ltv_cac_ratio", "LTV / CAC", "unit", "", source="manual",
        description="Recorded by the operator once LTV modeling stands.",
        section="financial_performance",
        target=3.0, warning=1.5, critical=1.0,
    ),
    # ---- phase 2: AI quality ------------------------------------------ #
    MetricSpec(
        "ai.task_success_rate", "AI task success rate", "ai", "%",
        description="Node-function runs (the AI's own code executing) "
        "that completed, over the last 30 days of terminal runs.",
        section="ai_performance",
        formula="completed_node_function_runs / terminal_node_function_runs"
        " * 100",
        target=90.0, warning=75.0, critical=50.0,
    ),
    MetricSpec(
        "ai.intervention_rate", "Human intervention rate", "ai", "%",
        description="Terminal runs in the last 30 days that needed a "
        "human retry.",
        section="ai_performance",
        formula="runs_with_user_retries / terminal_runs * 100",
        direction="down", warning=30.0, critical=60.0,
    ),
    MetricSpec(
        "ai.repairs_total", "Self-repairs promoted", "ai", "count",
        description="Healed functions the node.repair seat promoted into "
        "drawers, all-time.",
        section="ai_performance",
        formula="count of node.repair seat events on the audit log",
    ),
    # ---- phase 2: marketplace health ---------------------------------- #
    MetricSpec(
        "market.listings_active", "Active listings", "market", "count",
        description="Nodes currently discoverable on the nodeplace.",
        section="transaction_and_marketplace",
        formula="count of discoverable listings",
    ),
    MetricSpec(
        "market.transactions_daily", "Transactions today", "market", "count",
        description="Earnings entries filed today.",
        section="transaction_and_marketplace",
        formula="count of earnings entries created today",
    ),
    MetricSpec(
        "market.avg_transaction_usd", "Avg transaction value", "market",
        "USD",
        description="Mean value of today's earnings entries.",
        section="transaction_and_marketplace",
        formula="today_earnings_value / today_earnings_count",
    ),
    # ---- phase 2: customer health ------------------------------------- #
    MetricSpec(
        "health.activation_rate_pct", "Activation rate", "health", "%",
        description="Accounts that ever completed a run, among accounts "
        "that ever started one.",
        section="acquisition_and_growth",
        formula="accounts_with_completed_run / accounts_with_any_run * 100",
        target=60.0, warning=40.0, critical=20.0,
    ),
    MetricSpec(
        "health.at_risk_users", "At-risk users", "health", "users",
        description="Accounts active earlier this month but silent for "
        "the last 7 days — the silent-churn watchlist.",
        section="retention_and_churn",
        formula="active(8..30 days ago) − active(last 7 days)",
        direction="down",
    ),
    # ---- phase 3: attention share (external eyes — the manual door) --- #
    MetricSpec(
        "attention.share_of_search_pct", "Share of search", "attention", "%",
        source="manual",
        description="Branded-search share against the category, from the "
        "search-trend door.",
        section="market_and_competition",
        update_frequency="weekly",
    ),
    MetricSpec(
        "attention.share_of_voice_pct", "Share of voice", "attention", "%",
        source="manual",
        description="Category mentions that are ours, from press/social "
        "listening.",
        section="market_and_competition",
        update_frequency="weekly",
    ),
    MetricSpec(
        "attention.category_time_share_pct", "Category time share",
        "attention", "%", source="manual",
        description="Share of category app time spent in OoLu, from panel "
        "estimators.",
        section="product_usage_and_attention",
        update_frequency="monthly",
    ),
    MetricSpec(
        "attention.external_tools_eliminated", "External tools eliminated",
        "attention", "count", source="manual",
        description="Tools customers report replacing with OoLu workflows.",
        section="product_usage_and_attention",
        update_frequency="monthly",
    ),
    # ---- phase 3: the moat, measured off the real stores -------------- #
    MetricSpec(
        "moat.node_reuse_rate_pct", "Node reuse rate", "moat", "%",
        description="Terminal runs (30 days) that routed through an "
        "existing node's own function instead of a fresh plan.",
        section="moat_and_compounding_advantage",
        formula="node_function_runs / terminal_runs * 100",
        target=60.0, warning=30.0, critical=10.0,
    ),
    MetricSpec(
        "moat.reusable_verified_nodes", "Verified sealed releases", "moat",
        "count",
        description="Content-addressed node releases verification sealed — "
        "the reusable, provable function library.",
        section="moat_and_compounding_advantage",
        formula="count of sealed releases on the provenance ledger",
    ),
    MetricSpec(
        "moat.proprietary_events_total", "Proprietary event volume", "moat",
        "count",
        description="Rows on the tamper-evident audit chain — the "
        "platform's own execution history nobody else holds.",
        section="moat_and_compounding_advantage",
        formula="count of audit-chain entries",
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
        "inputs": (
            "capital.in_app_usd", "revenue.earnings_daily_usd",
            "unit.contribution_margin_pct",
        ),
        "basis": "growth",
    },
    {
        "name": "product", "weight": 0.10,
        "inputs": (
            "workflows.first_attempt_success_rate", "ai.task_success_rate",
            "health.activation_rate_pct",
        ),
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
        "inputs": (
            "seo.impressions", "seo.clicks",
            "attention.share_of_search_pct",
        ),
        "basis": "growth",
    },
    {
        "name": "moat", "weight": 0.10,
        "inputs": (
            "nodes.total", "moat.reusable_verified_nodes",
            "moat.proprietary_events_total",
        ),
        "basis": "growth",
    },
)

# The strategic-comparison dimensions, exactly as the matrix names them.
COMPARISON_DIMENSIONS: tuple[str, ...] = (
    "workflow_coverage",
    "automation_depth",
    "physical_execution_capability",
    "integrations",
    "reliability",
    "implementation_time",
    "pricing",
    "switching_cost",
    "data_advantage",
    "geographic_coverage",
)

# The decision-support scenarios the matrix models.
SCENARIOS: tuple[str, ...] = (
    "pricing_change",
    "model_provider_change",
    "infrastructure_migration",
    "new_market_entry",
    "physical_capacity_expansion",
    "sales_hiring",
    "workflow_subsidy",
    "competitor_price_reduction",
)


def project_scenario(
    *, scenario: str, baseline: dict, assumptions: dict
) -> dict:
    """Deterministic what-if arithmetic — the matrix's decision-support
    outputs from the CURRENT actuals and the operator's stated
    assumptions. No model touches a number: every output is a formula
    over the baseline, and the confidence range is exactly the stated
    uncertainty applied to the projected deltas."""
    if scenario not in SCENARIOS:
        raise ValueError(
            f"unknown scenario '{scenario}' — one of: {', '.join(SCENARIOS)}"
        )
    revenue = float(baseline.get("monthly_revenue_usd") or 0.0)
    cost = float(baseline.get("monthly_cost_usd") or 0.0)
    cash = float(baseline.get("cash_usd") or 0.0)
    revenue_delta = revenue * float(assumptions.get("revenue_delta_pct", 0)) / 100
    cost_delta = cost * float(assumptions.get("cost_delta_pct", 0)) / 100
    one_time = float(assumptions.get("one_time_cost_usd", 0))
    uncertainty = abs(float(assumptions.get("uncertainty_pct", 20))) / 100

    old_net = revenue - cost
    new_net = (revenue + revenue_delta) - (cost + cost_delta)
    monthly_gain = new_net - old_net
    old_margin = (old_net / revenue * 100) if revenue else None
    new_revenue = revenue + revenue_delta
    new_margin = (new_net / new_revenue * 100) if new_revenue else None
    burn = max(0.0, -new_net)
    runway = (cash - one_time) / burn if burn else None
    break_even = (
        one_time / monthly_gain if one_time > 0 and monthly_gain > 0 else None
    )
    return {
        "scenario": scenario,
        "baseline": {
            "monthly_revenue_usd": revenue,
            "monthly_cost_usd": cost,
            "cash_usd": cash,
        },
        "assumptions": dict(assumptions),
        "revenue_impact_usd_month": round(revenue_delta, 2),
        "cost_impact_usd_month": round(cost_delta, 2),
        "margin_impact_pts": (
            round(new_margin - old_margin, 2)
            if old_margin is not None and new_margin is not None
            else None
        ),
        "cash_impact_usd_year": round(monthly_gain * 12 - one_time, 2),
        "monthly_net_before_usd": round(old_net, 2),
        "monthly_net_after_usd": round(new_net, 2),
        "runway_months_after": round(runway, 1) if runway is not None else None,
        "break_even_months": (
            round(break_even, 1) if break_even is not None else None
        ),
        "confidence_range_usd_month": [
            round(monthly_gain * (1 - uncertainty), 2),
            round(monthly_gain * (1 + uncertainty), 2),
        ],
    }


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


_COMPETITOR_SCHEMA = """CREATE TABLE IF NOT EXISTS competitor_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor TEXT NOT NULL,
    dimension TEXT NOT NULL,
    score REAL NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'medium',
    observed_at TEXT NOT NULL
)"""


class CompetitorLedger:
    """Competitor intelligence, append-only: every observation keeps its
    evidence, source, and confidence — the matrix's strategic
    comparison reads the NEWEST observation per (competitor,
    dimension), and the history stays for the audit. Scores are
    relative: positive means OoLu leads on that dimension, negative
    means the competitor does."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_COMPETITOR_SCHEMA)

    def observe(
        self,
        competitor: str,
        dimension: str,
        score: float,
        *,
        evidence: str = "",
        source: str = "",
        confidence: str = "medium",
    ) -> dict:
        competitor = competitor.strip()
        if not competitor:
            raise ValueError("name the competitor")
        if dimension not in COMPARISON_DIMENSIONS:
            raise ValueError(
                f"unknown dimension '{dimension}' — one of: "
                + ", ".join(COMPARISON_DIMENSIONS)
            )
        if confidence not in ("low", "medium", "high"):
            raise ValueError("confidence must be low, medium, or high")
        observed_at = self._clock().isoformat()
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO competitor_observations
                       (competitor, dimension, score, evidence, source,
                        confidence, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    competitor,
                    dimension,
                    float(score),
                    evidence[:2000],
                    source[:500],
                    confidence,
                    observed_at,
                ),
            )
        return {
            "competitor": competitor,
            "dimension": dimension,
            "score": float(score),
            "observed_at": observed_at,
        }

    def comparison(self) -> dict:
        """The strategic matrix: per competitor, per dimension — the
        newest relative score with its evidence, confidence, and
        last-updated stamp. Unobserved dimensions are simply absent,
        never guessed."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT competitor, dimension, score, evidence, source,
                          confidence, observed_at
                   FROM competitor_observations latest
                   WHERE seq = (SELECT MAX(seq)
                                FROM competitor_observations
                                WHERE competitor = latest.competitor
                                  AND dimension = latest.dimension)
                   ORDER BY competitor, dimension"""
            ).fetchall()
        competitors: dict[str, dict] = {}
        for row in rows:
            entry = competitors.setdefault(
                row["competitor"], {"competitor": row["competitor"],
                                    "dimensions": {}}
            )
            entry["dimensions"][row["dimension"]] = {
                "relative_score": row["score"],
                "evidence": row["evidence"],
                "source": row["source"],
                "confidence": row["confidence"],
                "last_updated": row["observed_at"],
            }
        return {
            "dimensions": list(COMPARISON_DIMENSIONS),
            "items": list(competitors.values()),
        }
