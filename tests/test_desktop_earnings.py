"""The earnings screen: a read-only projection of the noder's ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_desktop_loopback import _call
from test_gateway_market import _build

from workflow_gps.billing import (
    EarningsEntry,
    EarningsKind,
    EarningsLedger,
    PayoutStore,
)
from workflow_gps.billing.models import PayoutBatch, PayoutStatus
from workflow_gps.desktop import DesktopService
from workflow_gps.desktop.loopback import DesktopLoopbackApp

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _entry(noder, micros, kind, *, event=None, available=None, seq=0):
    return EarningsEntry(
        noder_principal=noder,
        event_id=event,
        amount_micros=micros,
        kind=kind,
        available_at=available or (NOW - timedelta(days=10)),
        created_at=NOW - timedelta(minutes=100 - seq),  # explicit order
    )


def test_earnings_view_projects_the_ledger(tmp_path):
    app, conn, *_rest = _build(tmp_path)
    ledger = EarningsLedger(conn)
    payouts = PayoutStore(conn)
    ledger.append(_entry("me", 50_000_000, EarningsKind.ACCRUAL, event="e1", seq=1))
    ledger.append(
        _entry(  # not yet cleared: pending
            "me",
            7_000_000,
            EarningsKind.ACCRUAL,
            event="e2",
            available=NOW + timedelta(days=5),
            seq=2,
        )
    )
    ledger.append(_entry("me", 5_000_000, EarningsKind.RESERVE, seq=3))
    ledger.append(_entry("me", -45_000_000, EarningsKind.PAYOUT, event="b1", seq=4))
    ledger.append(_entry("me", -2_000_000, EarningsKind.CLAWBACK, event="e1", seq=5))
    payouts.add_batch(
        PayoutBatch(
            batch_id="b1",
            noder_principal="me",
            amount_micros=45_000_000,
            status=PayoutStatus.PAID,
            provider_ref="po_1",
        )
    )
    svc = DesktopService(
        app._durable,
        earnings_ledger=ledger,
        payout_store=payouts,
        noder_principal="me",
        clock=lambda: NOW,
    )

    view = svc.earnings()
    assert view.noder == "me"
    # cleared 50 - 2 clawed = 48; minus 5 reserved, 45 paid -> -2 owed.
    assert view.available == -2.0
    assert view.pending == 7.0
    assert view.reserved == 5.0
    assert view.lifetime_paid == 45.0
    assert [e.kind for e in view.entries] == [
        "clawback",
        "payout",
        "reserve",
        "accrual",
        "accrual",
    ]  # most recent first
    assert view.entries[0].amount == -2.0 and view.entries[0].event_id == "e1"
    (batch,) = view.batches
    assert batch.status == "paid" and batch.amount == 45.0
    conn.close()


def test_runtime_wires_earnings_by_default(tmp_path):
    """build_desktop_runtime ships the screen out of the box: honest zeros
    until the user's contributions earn — and the runtime exposes the SAME
    ledger objects, so a settlement job and the screen share one truth."""
    from workflow_gps.assembly import build_desktop_runtime

    rt = build_desktop_runtime(db_path=tmp_path / "shell.db")
    try:
        status, body = _call(DesktopLoopbackApp(rt.desktop), "GET", "/v1/earnings")
        assert status == 200
        assert body["noder"] == "local-noder"
        assert body["available"] == 0.0 and body["entries"] == []

        # Money written through the runtime's ledger shows on the screen.
        rt.earnings.append(
            _entry("local-noder", 12_000_000, EarningsKind.ACCRUAL, event="e1")
        )
        status, body = _call(DesktopLoopbackApp(rt.desktop), "GET", "/v1/earnings")
        assert body["available"] == 12.0
        assert body["entries"][0]["kind"] == "accrual"
    finally:
        rt.close()


def test_runtime_earnings_can_be_disabled(tmp_path):
    from workflow_gps.assembly import build_desktop_runtime

    rt = build_desktop_runtime(db_path=tmp_path / "shell.db", noder_principal=None)
    try:
        assert rt.earnings is None and rt.payouts is None
        status, _body = _call(DesktopLoopbackApp(rt.desktop), "GET", "/v1/earnings")
        assert status == 404
    finally:
        rt.close()


def test_earnings_over_the_loopback_and_unconfigured_404(tmp_path):
    app, conn, *_rest = _build(tmp_path)
    ledger = EarningsLedger(conn)
    ledger.append(_entry("me", 10_000_000, EarningsKind.ACCRUAL, event="e1"))
    configured = DesktopService(
        app._durable,
        earnings_ledger=ledger,
        noder_principal="me",
        clock=lambda: NOW,
    )
    status, body = _call(DesktopLoopbackApp(configured), "GET", "/v1/earnings")
    assert status == 200
    assert body["available"] == 10.0 and body["batches"] == []

    bare = DesktopService(app._durable)  # no earnings wiring
    status, _body = _call(DesktopLoopbackApp(bare), "GET", "/v1/earnings")
    assert status == 404
    conn.close()
