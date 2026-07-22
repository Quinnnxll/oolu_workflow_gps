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
