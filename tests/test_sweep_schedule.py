"""The sweep as a recurring Routine: standing consent, fleet-safe firing.

The manual sweep is approve-gated because it deletes; a schedule means
unattended firings, so the consent moves up one level: ENABLING the
Routine is the approved, audited act, every due firing runs under that
standing grant, and revoking it stops the next firing cold. On a fleet
sharing one database, the atomic claim makes many ticking hosts fire
exactly once per due interval — no coordinator, just the database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_contract_run import _grant_approver
from test_http_gateway import NOW, _req

from oolu.durable import DurableConnection
from oolu.durable.artifacts import FilesystemArtifactStore
from oolu.runtime.bundle import BundleStore
from oolu.runtime.sweep import (
    MIN_SWEEP_INTERVAL_HOURS,
    SweepScheduleStore,
)

T0 = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The store: consent that stands, a claim that fires once.                     #
# --------------------------------------------------------------------------- #
def test_enable_view_disable_roundtrip(tmp_path):
    conn = DurableConnection(tmp_path / "s.db")
    try:
        schedule = SweepScheduleStore(conn)
        assert schedule.view() is None
        view = schedule.enable(
            interval_hours=6, granted_by="steward", tenant="t1", now=T0
        )
        assert view["enabled"] is True
        assert view["interval_hours"] == 6
        assert view["granted_by"] == "steward"
        assert schedule.disable() is True
        assert schedule.view()["enabled"] is False
        assert schedule.disable() is False  # already off
    finally:
        conn.close()


def test_the_interval_has_a_floor(tmp_path):
    conn = DurableConnection(tmp_path / "s.db")
    try:
        schedule = SweepScheduleStore(conn)
        view = schedule.enable(
            interval_hours=0.01, granted_by="s", tenant="t1", now=T0
        )
        assert view["interval_hours"] == MIN_SWEEP_INTERVAL_HOURS
    finally:
        conn.close()


def test_the_claim_fires_once_per_due_interval_fleet_wide(tmp_path):
    # Two "hosts" over the SAME database file: exactly one wins each due
    # window; the interval reopens the claim.
    conn_a = DurableConnection(tmp_path / "fleet.db")
    conn_b = DurableConnection(tmp_path / "fleet.db")
    try:
        host_a = SweepScheduleStore(conn_a)
        host_b = SweepScheduleStore(conn_b)
        host_a.enable(interval_hours=1, granted_by="s", tenant="t1", now=T0)

        wins = [host_a.claim_due(T0), host_b.claim_due(T0)]
        assert wins.count(True) == 1  # one firing, whoever got there first
        # Within the interval: nobody fires again.
        later = T0 + timedelta(minutes=30)
        assert not host_a.claim_due(later) and not host_b.claim_due(later)
        # Past the interval: the claim reopens, and again only one wins.
        due = T0 + timedelta(hours=1, minutes=1)
        wins = [host_b.claim_due(due), host_a.claim_due(due)]
        assert wins.count(True) == 1
    finally:
        conn_a.close()
        conn_b.close()


def test_a_disabled_schedule_never_claims(tmp_path):
    conn = DurableConnection(tmp_path / "s.db")
    try:
        schedule = SweepScheduleStore(conn)
        assert schedule.claim_due(T0) is False  # never enabled
        schedule.enable(interval_hours=1, granted_by="s", tenant="t1", now=T0)
        schedule.disable()
        assert schedule.claim_due(T0 + timedelta(hours=2)) is False
    finally:
        conn.close()


def test_results_land_on_the_routine(tmp_path):
    conn = DurableConnection(tmp_path / "s.db")
    try:
        schedule = SweepScheduleStore(conn)
        schedule.enable(interval_hours=1, granted_by="s", tenant="t1", now=T0)
        schedule.record_result(T0, summary={"reclaimed_bytes": 7})
        assert schedule.view()["last_summary"] == {"reclaimed_bytes": 7}
        schedule.record_result(T0, error="the store went away")
        view = schedule.view()
        assert view["last_error"] == "the store went away"
        assert view["last_summary"] is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The routes: enabling IS the consent — approve authority, audited, revocable. #
# --------------------------------------------------------------------------- #
def _scheduled_rig(tmp_path):
    from test_node_hands import _grown_web_node

    app, conn, ident, desk, script_exec, agreed = _grown_web_node(tmp_path)
    app._bundle_store = BundleStore(
        conn, FilesystemArtifactStore(tmp_path / "cas")
    )
    return app, conn, ident, desk


def test_the_schedule_routes_are_approve_gated_and_audited(tmp_path):
    app, conn, ident, desk = _scheduled_rig(tmp_path)
    try:
        member = ident.token("user-1", "t1")
        refused = app.handle(
            _req(
                "POST",
                "/v1/work/bundles/schedule",
                token=member,
                body={"interval_hours": 6},
            )
        )
        assert refused.status == 403  # a member cannot grant standing consent

        _grant_approver(ident, "steward", "t1")
        steward = ident.token("steward", "t1")
        enabled = app.handle(
            _req(
                "POST",
                "/v1/work/bundles/schedule",
                token=steward,
                body={"interval_hours": 6},
            )
        )
        assert enabled.status == 200, enabled.body
        assert enabled.body["enabled"] is True
        assert enabled.body["granted_by"] == "steward"
        granted = [
            r
            for r in app._durable.audit.records()
            if r.event_type == "bundles.sweep_scheduled"
        ]
        assert len(granted) == 1

        # Revocation: the same authority; a member is refused.
        assert (
            app.handle(
                _req("DELETE", "/v1/work/bundles/schedule", token=member)
            ).status
            == 403
        )
        cleared = app.handle(
            _req("DELETE", "/v1/work/bundles/schedule", token=steward)
        )
        assert cleared.status == 200 and cleared.body["disabled"] is True
        assert app._sweep_schedule.view()["enabled"] is False
    finally:
        conn.close()


def test_the_audit_route_replays_the_sweeps_history(tmp_path):
    """GET /v1/work/bundles/audit reads the story back off the hash-chained
    log — grant, manual sweep, scheduled firing, revocation — newest first,
    behind the same hygiene:sweep authority as the other read routes."""
    from oolu.identity import AuthorityGrant, Role

    app, conn, ident, desk = _scheduled_rig(tmp_path)
    try:
        member = ident.token("user-1", "t1")
        refused = app.handle(_req("GET", "/v1/work/bundles/audit", token=member))
        assert refused.status == 403  # no hygiene:sweep, no history

        # A steward with both hats: approve authority for the writes,
        # hygiene:sweep for the reads.
        _grant_approver(ident, "steward", "t1")
        ident.store.add_role(
            Role(
                tenant_id="t1",
                name="janitor",
                permissions=frozenset({"hygiene:sweep"}),
            )
        )
        ident.store.add_grant(
            AuthorityGrant(
                tenant_id="t1",
                principal_id="steward",
                role_name="janitor",
                granted_by="x",
            )
        )
        steward = ident.token("steward", "t1")
        assert (
            app.handle(
                _req(
                    "POST",
                    "/v1/work/bundles/schedule",
                    token=steward,
                    body={"interval_hours": 6},
                )
            ).status
            == 200
        )
        assert (
            app.handle(_req("POST", "/v1/work/bundles/sweep", token=steward)).status
            == 200
        )
        app._scheduled_sweep_tick(NOW + timedelta(hours=7))
        assert (
            app.handle(
                _req("DELETE", "/v1/work/bundles/schedule", token=steward)
            ).status
            == 200
        )

        listed = app.handle(_req("GET", "/v1/work/bundles/audit", token=steward))
        assert listed.status == 200
        events = [item["event_type"] for item in listed.body["items"]]
        assert events == [  # newest first
            "bundles.sweep_unscheduled",
            "bundles.swept",
            "bundles.swept",
            "bundles.sweep_scheduled",
        ]
        fired = listed.body["items"][1]  # the scheduled firing names whose
        assert fired["scheduled"] is True  # consent it ran under
        assert fired["granted_by"] == "steward"
        manual = listed.body["items"][2]
        assert "scheduled" not in manual and manual["by"] == "steward"
        granted = listed.body["items"][3]
        assert granted["interval_hours"] == 6
        assert all("run_id" not in item for item in listed.body["items"])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The firing: a due tick sweeps under the standing consent, exactly once.      #
# --------------------------------------------------------------------------- #
def test_a_due_tick_sweeps_under_the_standing_consent(tmp_path):
    app, conn, ident, desk = _scheduled_rig(tmp_path)
    try:
        # A stale bundle no node references — what the Routine exists to reap.
        stale = app._bundle_store.freeze({"old.py": "OLD\n"})
        app._sweep_schedule.enable(
            interval_hours=1, granted_by="steward", tenant="t1", now=NOW
        )
        # Fresh blobs are grace-spared; the manifest itself is reaped, so
        # zero the blob grace via the sweep the gateway builds. The tick
        # builds its own sweep, so instead run the tick with a later NOW —
        # past the default one-hour blob grace.
        fire_at = NOW + timedelta(hours=2)
        app._scheduled_sweep_tick(fire_at)

        # The stale manifest is gone, the firing is audited as SCHEDULED
        # under the standing consent, and the Routine shows the summary.
        assert stale.bundle_id not in [
            m.bundle_id for m, _ in app._bundle_store.manifests()
        ]
        fired = [
            r
            for r in app._durable.audit.records()
            if r.event_type == "bundles.swept" and r.payload.get("scheduled")
        ]
        assert len(fired) == 1
        assert fired[0].payload["granted_by"] == "steward"
        view = app._sweep_schedule.view()
        assert view["last_summary"]["dead_manifests"] >= 1

        # The SAME due window ticks again (another host, another request):
        # the claim is already taken — no second sweep, no second audit.
        app._scheduled_sweep_tick(fire_at)
        fired = [
            r
            for r in app._durable.audit.records()
            if r.event_type == "bundles.swept" and r.payload.get("scheduled")
        ]
        assert len(fired) == 1
    finally:
        conn.close()


def test_a_revoked_routine_never_fires(tmp_path):
    app, conn, ident, desk = _scheduled_rig(tmp_path)
    try:
        app._bundle_store.freeze({"old.py": "OLD\n"})
        app._sweep_schedule.enable(
            interval_hours=1, granted_by="steward", tenant="t1", now=NOW
        )
        app._sweep_schedule.disable()
        app._scheduled_sweep_tick(NOW + timedelta(hours=2))
        assert len(app._bundle_store.manifests()) == 1  # nothing reaped
        assert app._sweep_schedule.view()["last_summary"] is None
    finally:
        conn.close()


def test_ordinary_traffic_advances_the_routine(tmp_path, monkeypatch):
    # The lazy-tick idiom: handle() runs the due-check, gated to once a
    # minute per host — the first request ticks, the next does not.
    app, conn, ident, desk = _scheduled_rig(tmp_path)
    try:
        ticks = []
        monkeypatch.setattr(
            app, "_scheduled_sweep_tick", lambda now: ticks.append(now)
        )
        app._sweep_gate = 0.0
        app.handle(_req("GET", "/v1/client-config"))
        assert len(ticks) == 1
        app.handle(_req("GET", "/v1/client-config"))
        assert len(ticks) == 1  # the minute gate held
    finally:
        conn.close()
