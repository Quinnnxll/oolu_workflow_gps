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


def test_payout_account_onboarding_flow(tmp_path):
    """Onboard -> pending KYC blocks payouts -> processor verifies -> the
    shell mirrors it on the next read and persists the refresh."""
    from workflow_gps.billing import FakePayoutAdapter, KycStatus

    app, conn, *_rest = _build(tmp_path)
    adapter = FakePayoutAdapter(kyc=KycStatus.PENDING)
    svc = DesktopService(
        app._durable,
        earnings_ledger=EarningsLedger(conn),
        payout_store=PayoutStore(conn),
        payout_adapter=adapter,
        noder_principal="me",
        clock=lambda: NOW,
    )
    loop = DesktopLoopbackApp(svc)

    # Not-onboarded is a rendered state, never an error.
    status, body = _call(loop, "GET", "/v1/payout-account")
    assert status == 200
    assert body["onboarded"] is False and body["kyc_status"] == "not_onboarded"

    status, account = _call(
        loop, "POST", "/v1/payout-account", body={"country": "PT", "currency": "eur"}
    )
    assert status == 201
    assert account["kyc_status"] == "pending"
    assert account["payouts_enabled"] is False  # blocked until KYC verifies
    assert account["country"] == "PT" and account["currency"] == "eur"
    account_id = account["provider_account_id"]

    # Idempotent: an account is an external resource, never minted twice.
    _status, again = _call(loop, "POST", "/v1/payout-account", body={})
    assert again["provider_account_id"] == account_id
    (event,) = [
        r for r in app._durable.audit.records() if r.event_type == "payout.onboarded"
    ]
    assert event.payload["provider_account_id"] == account_id

    # KYC verifies on the processor's side; the shell mirrors it on the
    # next read — and persists the refreshed status in the store.
    adapter.accounts[account_id] = adapter.accounts[account_id].model_copy(
        update={"kyc_status": KycStatus.VERIFIED}
    )
    _status, refreshed = _call(loop, "GET", "/v1/payout-account")
    assert refreshed["kyc_status"] == "verified"
    assert refreshed["payouts_enabled"] is True
    assert svc._payout_store.get_account("me").kyc_status is KycStatus.VERIFIED
    conn.close()


def test_payout_account_configuration_gates(tmp_path):
    app, conn, *_rest = _build(tmp_path)
    # No payout store at all: the whole surface is a 404.
    bare = DesktopService(app._durable)
    status, _body = _call(DesktopLoopbackApp(bare), "GET", "/v1/payout-account")
    assert status == 404
    # A store without an adapter can SHOW state but cannot onboard.
    view_only = DesktopService(
        app._durable, payout_store=PayoutStore(conn), noder_principal="me"
    )
    loop = DesktopLoopbackApp(view_only)
    status, body = _call(loop, "GET", "/v1/payout-account")
    assert status == 200 and body["onboarded"] is False
    status, _body = _call(loop, "POST", "/v1/payout-account", body={})
    assert status == 404  # onboarding needs the processor adapter
    conn.close()


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
