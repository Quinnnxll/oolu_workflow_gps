"""Desktop surfacing of the assembly preview: service method + loopback route.

The desktop shell shows the same read-only plan the gateway's
``/v1/market/assemble`` computes — which nodes, what each costs, who gets
paid — through a secret-free view-model, without ever moving the price book.
"""

from __future__ import annotations

from test_desktop_loopback import _call
from test_gateway_market import _build
from test_market_assemble import TIDY, _seed_market

from workflow_gps.desktop import DesktopService
from workflow_gps.desktop.loopback import DesktopLoopbackApp


def _desktop(tmp_path):
    app, conn, ident, registry, *_rest = _build(tmp_path)
    _seed_market(app, ident, registry)
    svc = DesktopService(app._durable, market=app._market, price_book=app._price_book)
    return app, svc, conn


def test_assembly_preview_view_carries_prices_payouts_and_the_contract(tmp_path):
    app, svc, conn = _desktop(tmp_path)
    view = svc.assembly_preview(goal="clean-the-books", want=[TIDY])

    assert view.complete is True
    assert set(view.selected) == {"raw exporter", "invoice cleaner"}
    assert view.missing == []
    assert len(view.steps) == 2
    for step in view.steps:
        assert step.gap is False
        assert step.price is not None and step.price > 0
        assert step.price_notes  # the clearing forces, human-readable
        assert step.payouts and all(p.amount > 0 for p in step.payouts)
    payees = {p.noder for step in view.steps for p in step.payouts}
    assert payees == {"noder-export", "noder-clean"}
    assert view.estimated_gross_total > 0
    assert view.platform_margin_preview > 0
    # The contract crossing the loopback is the runnable artifact itself.
    assert view.contract is not None and view.contract["body"]["kind"] == "subgraph"
    # Previewing is read-only: the market reference never moved.
    assert app._price_book.reference("workflow:invoice_cleaning") is None
    conn.close()


def test_assembly_preview_reports_gaps_honestly(tmp_path):
    _app, svc, conn = _desktop(tmp_path)
    unicorn = {"name": "unicorn", "value_type": "path"}

    honest = svc.assembly_preview(goal="impossible", want=[unicorn])
    assert honest.complete is False
    assert honest.contract is None
    assert honest.missing == ["unicorn"]

    filled = svc.assembly_preview(goal="stretch", want=[unicorn], fill_gaps=True)
    assert filled.complete is True
    assert filled.gap_filled == ["unicorn"]
    (step,) = filled.steps
    assert step.gap is True and step.kind == "script"
    conn.close()


def test_loopback_route_serves_the_preview(tmp_path):
    _app, svc, conn = _desktop(tmp_path)
    app = DesktopLoopbackApp(svc)

    status, body = _call(
        app,
        "POST",
        "/v1/assembly/preview",
        body={"goal": "clean-the-books", "want": [TIDY]},
    )
    assert status == 200, body
    assert body["complete"] is True
    assert body["contract"] is not None
    assert {s["name"] for s in body["steps"]} == {"raw exporter", "invoice cleaner"}

    status, _err = _call(app, "POST", "/v1/assembly/preview", body={"goal": "x"})
    assert status == 400  # want is required

    status, _err = _call(
        app,
        "POST",
        "/v1/assembly/preview",
        body={"goal": "x", "want": [{"no-name": True}]},
    )
    assert status == 400  # a bad slot fails loudly, not with a 500
    conn.close()


def test_shell_without_market_returns_not_found(tmp_path):
    gateway, conn, *_rest = _build(tmp_path)
    svc = DesktopService(gateway._durable)  # no market economics configured
    app = DesktopLoopbackApp(svc)
    status, _body = _call(
        app,
        "POST",
        "/v1/assembly/preview",
        body={"goal": "anything", "want": [TIDY]},
    )
    assert status == 404
    conn.close()
