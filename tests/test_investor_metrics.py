"""The investor metrics tracker: one catalog, one daily ledger, live.

Exit gate: the catalog view reads every metric with its newest value —
auto metrics collected off the app's real stores, manual ones through
an approved, audited door — the daily ledger keeps one honest point
per (metric, day) for the history charts, a broken reader never blanks
the panel, and every route is walled like the other operator screens.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity.models import AuthorityGrant, Role
from oolu.telemetry.investor import (
    InvestorMetricsService,
    MetricsSnapshotStore,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def test_the_ledger_keeps_one_honest_point_per_day(tmp_path):
    conn = DurableConnection(tmp_path / "m.db")
    tick = {"now": NOW}
    store = MetricsSnapshotStore(conn, clock=lambda: tick["now"])
    store.record("users.daily_active", 3)
    store.record("users.daily_active", 5)  # same day: updated, not doubled
    tick["now"] = NOW + timedelta(days=1)
    store.record("users.daily_active", 8)
    series = store.history()["users.daily_active"]
    assert [p["value"] for p in series] == [5.0, 8.0]
    assert store.latest()["users.daily_active"]["value"] == 8.0
    conn.close()


def test_a_broken_reader_never_blanks_the_panel(tmp_path):
    conn = DurableConnection(tmp_path / "m.db")
    store = MetricsSnapshotStore(conn)

    def boom():
        raise RuntimeError("store down")

    service = InvestorMetricsService(
        store, readers={"nodes.total": lambda: 7, "users.daily_active": boom}
    )
    collected = service.collect()
    assert collected == {"nodes.total": 7.0}
    view = {i["key"]: i for i in service.view()["items"]}
    assert view["nodes.total"]["value"] == 7.0
    assert view["users.daily_active"]["value"] is None  # honest absence
    # Manual metrics ride the same ledger.
    service.record_manual("code.commits", 412)
    view = {i["key"]: i for i in service.view()["items"]}
    assert view["code.commits"]["value"] == 412.0
    assert view["code.commits"]["source"] == "manual"
    conn.close()


def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        metrics_store=MetricsSnapshotStore(conn),
    )
    ident.store.add_role(
        Role(
            tenant_id="t1",
            name="metrics",
            permissions=frozenset({"metrics:view", "approve:metrics.record"}),
        )
    )
    ident.store.add_grant(
        AuthorityGrant(
            tenant_id="t1",
            principal_id="operator",
            role_name="metrics",
            granted_by="root",
        )
    )
    return gateway, conn, ident


def test_the_routes_are_walled_and_read_the_real_books(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        walled = gw.handle(
            _req("GET", "/v1/platform/metrics", token=ident.token("user-1", "t1"))
        )
        assert walled.status == 403

        # Real activity enters the auto metrics.
        submitted = gw.handle(
            _req(
                "POST", "/v1/runs",
                token=ident.token("operator", "t1"), body={"intent": "tidy"},
            )
        )
        assert submitted.status in (200, 201, 202), submitted.body

        view = gw.handle(
            _req("GET", "/v1/platform/metrics", token=ident.token("operator", "t1"))
        )
        assert view.status == 200, view.body
        items = {i["key"]: i for i in view.body["items"]}
        assert items["executions.total"]["value"] >= 1
        assert items["users.daily_active"]["value"] >= 1
        assert items["seo.impressions"]["value"] is None  # manual, unfed

        # The manual door: approved, audited, refused on unknown keys.
        recorded = gw.handle(
            _req(
                "PUT", "/v1/platform/metrics/code.commits",
                token=ident.token("operator", "t1"), body={"value": 900},
            )
        )
        assert recorded.status == 200, recorded.body
        unknown = gw.handle(
            _req(
                "PUT", "/v1/platform/metrics/not.a.metric",
                token=ident.token("operator", "t1"), body={"value": 1},
            )
        )
        assert unknown.status == 404
        events = [
            e for e in gw._durable.audit.records()
            if e.event_type == "metrics.recorded"
        ]
        assert events[-1].payload["metric"] == "code.commits"

        # The snapshot tick and the charted history.
        tick = gw.handle(
            _req(
                "POST", "/v1/platform/metrics/snapshot",
                token=ident.token("operator", "t1"),
            )
        )
        assert tick.status == 200 and tick.body["collected"]
        history = gw.handle(
            _req(
                "GET", "/v1/platform/metrics/history",
                token=ident.token("operator", "t1"),
            )
        )
        assert history.status == 200
        assert history.body["series"]["code.commits"][-1]["value"] == 900.0
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# Phase 1 of the panel matrix: contract, executive summary, scorecard.  #
# --------------------------------------------------------------------- #
def test_metric_status_honors_direction_and_thresholds():
    from oolu.telemetry.investor import MetricSpec, metric_status

    up = MetricSpec(
        "x", "X", "g", "%", target=95.0, warning=85.0, critical=70.0
    )
    assert metric_status(up, 96.0) == "ok"
    assert metric_status(up, 80.0) == "warning"
    assert metric_status(up, 60.0) == "critical"
    assert metric_status(up, None) == "unknown"
    down = MetricSpec(
        "y", "Y", "g", "USD", direction="down", warning=100.0, critical=500.0
    )
    assert metric_status(down, 50.0) == "ok"
    assert metric_status(down, 200.0) == "warning"
    assert metric_status(down, 900.0) == "critical"
    # No thresholds: a present value is simply ok.
    assert metric_status(MetricSpec("z", "Z", "g"), 1.0) == "ok"


def test_the_executive_summary_speaks_the_status_components(tmp_path):
    conn = DurableConnection(tmp_path / "m.db")
    tick = {"now": NOW}
    store = MetricsSnapshotStore(conn, clock=lambda: tick["now"])
    service = InvestorMetricsService(store)
    # Two days of history for one executive metric.
    store.record("workflows.success_rate", 90.0)
    tick["now"] = NOW + timedelta(days=1)
    store.record("workflows.success_rate", 99.0)
    summary = service.summary()
    by_key = {i["key"]: i for i in summary["items"]}
    line = by_key["workflows.success_rate"]
    assert line["actual"] == 99.0
    assert line["previous_period"] == 90.0
    assert round(line["growth_rate_pct"], 1) == 10.0
    assert line["target"] == 95.0
    assert line["status"] == "ok"
    # A headline metric with no points answers honestly.
    empty = by_key["retention.day7_pct"]
    assert empty["actual"] is None and empty["status"] == "unknown"
    # Only executive metrics ride the strip.
    assert "users.weekly_active" not in by_key
    conn.close()


def test_the_scorecard_renormalizes_over_what_exists(tmp_path):
    conn = DurableConnection(tmp_path / "m.db")
    tick = {"now": NOW}
    store = MetricsSnapshotStore(conn, clock=lambda: tick["now"])
    service = InvestorMetricsService(store)
    # Technology pillar: status basis, both inputs green.
    store.record("workflows.success_rate", 99.0)
    store.record("reliability.request_success_pct", 99.95)
    # Growth pillar: an 8-day rising series for one input.
    for offset in range(8):
        tick["now"] = NOW + timedelta(days=offset)
        store.record("users.daily_active", 10 + offset * 2)
    card = service.scorecard()
    names = {p["name"]: p for p in card["pillars"]}
    assert names["technology"]["score"] == 100.0
    assert names["growth"]["score"] == 100.0  # ≥5% over the window
    # No physical fleet, no seo recordings, no retention data → named.
    assert "physical_execution" in card["excluded"]
    assert "market" in card["excluded"]
    # Weights renormalize over the pillars that HAVE data.
    total = sum(p["effective_weight"] for p in card["pillars"])
    assert abs(total - 1.0) < 1e-9
    assert card["score"] is not None
    conn.close()


def test_summary_and_scorecard_routes_are_walled(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        for path in (
            "/v1/platform/metrics/summary",
            "/v1/platform/metrics/scorecard",
        ):
            walled = gw.handle(
                _req("GET", path, token=ident.token("user-1", "t1"))
            )
            assert walled.status == 403, path
            answered = gw.handle(
                _req("GET", path, token=ident.token("operator", "t1"))
            )
            assert answered.status == 200, (path, answered.body)
        summary = gw.handle(
            _req(
                "GET", "/v1/platform/metrics/summary",
                token=ident.token("operator", "t1"),
            )
        )
        # The strip carries the matrix's status components.
        (first, *_rest) = summary.body["items"]
        for field in ("actual", "previous_period", "growth_rate_pct",
                      "target", "status", "owner"):
            assert field in first
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# Phase 2: unit economics, AI quality, cohorts, market, health.         #
# --------------------------------------------------------------------- #
def test_month_span_walks_calendar_months():
    from oolu.telemetry.investor import month_span

    assert month_span("2025-11", "2026-02") == [
        "2025-11", "2025-12", "2026-01", "2026-02",
    ]
    assert month_span("2026-03", "2026-03") == ["2026-03"]
    assert month_span("2026-05", "2026-03") == []  # inverted: empty
    assert month_span("garbage", "2026-03") == []


def test_phase2_metrics_read_the_real_books(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        # One real completed run: activation and AI books have a basis.
        gw.handle(
            _req(
                "POST", "/v1/runs",
                token=ident.token("operator", "t1"), body={"intent": "tidy"},
            )
        )
        view = gw.handle(
            _req(
                "GET", "/v1/platform/metrics",
                token=ident.token("operator", "t1"),
            )
        )
        items = {i["key"]: i for i in view.body["items"]}
        # The runner completed, so every account that started activated.
        assert items["health.activation_rate_pct"]["value"] == 100.0
        assert items["health.at_risk_users"]["value"] == 0.0
        # No human retries anywhere: intervention is honestly zero.
        assert items["ai.intervention_rate"]["value"] == 0.0
        # No earnings books on this host: unit economics stay blank —
        # a missing store is a blank metric, never a fake zero.
        assert items["unit.arpu_usd"]["value"] is None
        # The manual CAC door exists with its contract riding along.
        assert items["unit.cac_usd"]["source"] == "manual"
        assert items["unit.cac_usd"]["section"] == "acquisition_and_growth"
    finally:
        conn.close()


def test_cohorts_answer_from_the_run_books_and_are_walled(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        walled = gw.handle(
            _req(
                "GET", "/v1/platform/metrics/cohorts",
                token=ident.token("user-1", "t1"),
            )
        )
        assert walled.status == 403
        gw.handle(
            _req(
                "POST", "/v1/runs",
                token=ident.token("operator", "t1"), body={"intent": "tidy"},
            )
        )
        answered = gw.handle(
            _req(
                "GET", "/v1/platform/metrics/cohorts",
                token=ident.token("operator", "t1"),
            )
        )
        assert answered.status == 200
        (cohort,) = answered.body["items"]
        assert cohort["size"] == 1
        # The signup month itself is retention point M0 at 100%.
        assert cohort["retention"][0]["offset"] == 0
        assert cohort["retention"][0]["pct"] == 100.0
        # The columns walk to the CURRENT month, one point per month.
        months = [p["month"] for p in cohort["retention"]]
        assert months == sorted(set(months))
    finally:
        conn.close()


def test_the_scorecard_gains_phase2_inputs(tmp_path):
    from oolu.telemetry.investor import SCORECARD_PILLARS

    pillars = {p["name"]: p for p in SCORECARD_PILLARS}
    assert "ai.task_success_rate" in pillars["product"]["inputs"]
    assert "health.activation_rate_pct" in pillars["product"]["inputs"]
    assert "unit.contribution_margin_pct" in pillars["economics"]["inputs"]


# --------------------------------------------------------------------- #
# Phase 3: competitors, moat, scenario modeling, the automated report.  #
# --------------------------------------------------------------------- #
def test_competitor_observations_build_the_strategic_comparison(tmp_path):
    from oolu.telemetry.investor import CompetitorLedger

    conn = DurableConnection(tmp_path / "c.db")
    try:
        ledger = CompetitorLedger(conn)
        ledger.observe(
            "AutomateCo", "automation_depth", 1.5,
            evidence="no verified physical hand", confidence="high",
        )
        ledger.observe("AutomateCo", "pricing", -1.0, confidence="low")
        # A newer observation supersedes; the history stays underneath.
        ledger.observe(
            "AutomateCo", "pricing", -0.5, evidence="they raised prices"
        )
        comparison = ledger.comparison()
        (entry,) = comparison["items"]
        assert entry["dimensions"]["pricing"]["relative_score"] == -0.5
        assert entry["dimensions"]["pricing"]["evidence"] == (
            "they raised prices"
        )
        assert entry["dimensions"]["automation_depth"]["confidence"] == "high"
        # Only the matrix's dimensions are speakable.
        import pytest

        with pytest.raises(ValueError, match="unknown dimension"):
            ledger.observe("X", "vibes", 1.0)
        with pytest.raises(ValueError, match="low, medium, or high"):
            ledger.observe("X", "pricing", 1.0, confidence="certain")
    finally:
        conn.close()


def test_scenario_projection_is_deterministic_arithmetic():
    from oolu.telemetry.investor import project_scenario

    projected = project_scenario(
        scenario="pricing_change",
        baseline={
            "monthly_revenue_usd": 1000.0,
            "monthly_cost_usd": 600.0,
            "cash_usd": 10_000.0,
        },
        assumptions={
            "revenue_delta_pct": 20,
            "cost_delta_pct": 5,
            "one_time_cost_usd": 300,
            "uncertainty_pct": 25,
        },
    )
    assert projected["revenue_impact_usd_month"] == 200.0
    assert projected["cost_impact_usd_month"] == 30.0
    assert projected["monthly_net_before_usd"] == 400.0
    assert projected["monthly_net_after_usd"] == 570.0
    # margin: 40% -> 47.5% = +7.5 pts
    assert projected["margin_impact_pts"] == 7.5
    # one-time 300 / monthly gain 170
    assert projected["break_even_months"] == 1.8
    lo, hi = projected["confidence_range_usd_month"]
    assert (lo, hi) == (127.5, 212.5)
    import pytest

    with pytest.raises(ValueError, match="unknown scenario"):
        project_scenario(
            scenario="wishful_thinking", baseline={}, assumptions={}
        )


def test_phase3_doors_answer_and_are_walled(tmp_path):
    gw, conn, ident = _host(tmp_path)
    try:
        # Competitor door: approved recording, then the comparison view.
        recorded = gw.handle(
            _req(
                "PUT", "/v1/platform/competitors",
                token=ident.token("operator", "t1"),
                body={
                    "competitor": "AutomateCo", "dimension": "reliability",
                    "score": 1.0, "evidence": "public outage log",
                    "confidence": "medium",
                },
            )
        )
        assert recorded.status == 200, recorded.body
        walled = gw.handle(
            _req(
                "GET", "/v1/platform/competitors",
                token=ident.token("user-1", "t1"),
            )
        )
        assert walled.status == 403
        comparison = gw.handle(
            _req(
                "GET", "/v1/platform/competitors",
                token=ident.token("operator", "t1"),
            )
        )
        assert comparison.status == 200
        (entry,) = comparison.body["items"]
        assert entry["dimensions"]["reliability"]["relative_score"] == 1.0

        # A run gives the moat and scenario baselines something real.
        gw.handle(
            _req(
                "POST", "/v1/runs",
                token=ident.token("operator", "t1"), body={"intent": "tidy"},
            )
        )
        scenario = gw.handle(
            _req(
                "POST", "/v1/platform/metrics/scenario",
                token=ident.token("operator", "t1"),
                body={
                    "scenario": "model_provider_change",
                    "assumptions": {"cost_delta_pct": -30},
                },
            )
        )
        assert scenario.status == 200, scenario.body
        assert scenario.body["scenario"] == "model_provider_change"
        assert "baseline" in scenario.body

        report = gw.handle(
            _req(
                "GET", "/v1/platform/metrics/report",
                token=ident.token("operator", "t1"),
            )
        )
        assert report.status == 200
        markdown = report.body["markdown"]
        assert "# OoLu — Investor Report" in markdown
        assert "## Executive summary" in markdown
        assert "## Cohort retention" in markdown
        assert "vs AutomateCo" in markdown
        # Moat metrics ride the catalog with real readers.
        view = gw.handle(
            _req(
                "GET", "/v1/platform/metrics",
                token=ident.token("operator", "t1"),
            )
        )
        items = {i["key"]: i for i in view.body["items"]}
        assert items["moat.node_reuse_rate_pct"]["value"] is not None
        assert items["moat.proprietary_events_total"]["value"] > 0
    finally:
        conn.close()
