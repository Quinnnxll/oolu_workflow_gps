"""Desktop surfacing of the assembly flow: preview + the confirm button.

The desktop shell shows the same read-only plan the gateway's
``/v1/market/assemble`` computes — which nodes, what each costs, who gets
paid — through a secret-free view-model, without ever moving the price book.
Confirming runs the previewed contract through the same shared money path
as ``POST /v1/runs/contract``: committed prices, one aggregate binding,
earnings only on platform-verified success.
"""

from __future__ import annotations

import pytest
from test_contract_run import _CliExecutor
from test_desktop_loopback import _call
from test_gateway_market import _build
from test_market_assemble import TIDY, _seed_market

from workflow_gps.desktop import DesktopService
from workflow_gps.desktop.loopback import DesktopLoopbackApp
from workflow_gps.metering.deriver import MeteringDeriver
from workflow_gps.orchestrator import DagRouteRunner


def _desktop(tmp_path, *, executor=None):
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    _seed_market(app, ident, registry)
    svc = DesktopService(
        app._durable,
        market=app._market,
        price_book=app._price_book,
        contract_runner=(
            DagRouteRunner({"cli": executor}) if executor is not None else None
        ),
        attribution=attribution,
    )
    return app, svc, conn, metering, attribution, audit


def test_assembly_preview_view_carries_prices_payouts_and_the_contract(tmp_path):
    app, svc, conn, *_rest = _desktop(tmp_path)
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
    _app, svc, conn, *_rest = _desktop(tmp_path)
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
    _app, svc, conn, *_rest = _desktop(tmp_path)
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


# --------------------------------------------------------------------------- #
# The confirm button: run what was previewed, on the shared money path.        #
# --------------------------------------------------------------------------- #
def test_confirm_runs_the_previewed_contract_and_binds_the_money(tmp_path):
    executor = _CliExecutor()
    app, svc, conn, metering, attribution, audit = _desktop(tmp_path, executor=executor)
    preview = svc.assembly_preview(goal="clean-the-books", want=[TIDY])
    assert preview.contract is not None

    run = svc.confirm_assembly(preview.contract, confirm_id="click-1")
    assert run.status == "succeeded"
    assert len(run.steps) == 2 and executor.calls == 2
    assert run.gross > 0
    assert run.noders == ["noder-clean", "noder-export"]

    # Confirming commits prices (previewing never did) and binds the run.
    assert app._price_book.reference("workflow:invoice_cleaning") is not None
    binding = attribution.get_binding(run.run_id)
    assert binding is not None and binding.gross == run.gross
    assert binding.consumer_tenant == "local"
    assert abs(sum(s.weight for s in binding.shares) - 1.0) < 1e-9

    # The audit event it appended is exactly what the deriver pays from.
    events = MeteringDeriver(audit, metering, attribution).derive()
    event = next(e for e in events if e.run_id == run.run_id)
    assert event.gross == run.gross

    # A double-clicked confirm replays the first result; nothing runs twice.
    again = svc.confirm_assembly(preview.contract, confirm_id="click-1")
    assert again.run_id == run.run_id and executor.calls == 2
    conn.close()


def test_confirm_refuses_reserved_contracts(tmp_path):
    from workflow_gps.skills.contract import ActionsBody, NodeContract
    from workflow_gps.skills.models import ActionEvent

    _app, svc, conn, *_rest = _desktop(tmp_path, executor=_CliExecutor())
    destructive = NodeContract(
        name="wipe it",
        body=ActionsBody(
            actions=[
                ActionEvent(correlation_id="c", adapter="cli", operation="delete_files")
            ]
        ),
    ).model_dump(mode="json")

    with pytest.raises(PermissionError, match="reserved"):
        svc.confirm_assembly(destructive)

    # And over the loopback the same refusal is a 403, not a crash.
    status, body = _call(
        DesktopLoopbackApp(svc),
        "POST",
        "/v1/assembly/confirm",
        body={"contract": destructive},
    )
    assert status == 403 and "reserved" in body["error"]
    conn.close()


def test_loopback_confirm_route_end_to_end(tmp_path):
    executor = _CliExecutor()
    _app, svc, conn, *_rest = _desktop(tmp_path, executor=executor)
    app = DesktopLoopbackApp(svc)

    status, preview = _call(
        app,
        "POST",
        "/v1/assembly/preview",
        body={"goal": "clean-the-books", "want": [TIDY]},
    )
    assert status == 200 and preview["contract"] is not None

    status, run = _call(
        app,
        "POST",
        "/v1/assembly/confirm",
        body={"contract": preview["contract"], "confirm_id": "ui-1"},
    )
    assert status == 200, run
    assert run["status"] == "succeeded" and run["gross"] > 0

    status, replay = _call(
        app,
        "POST",
        "/v1/assembly/confirm",
        body={"contract": preview["contract"], "confirm_id": "ui-1"},
    )
    assert status == 200 and replay["run_id"] == run["run_id"]
    assert executor.calls == 2  # the replay never re-executed

    status, _err = _call(app, "POST", "/v1/assembly/confirm", body={})
    assert status == 400  # a contract object is required
    conn.close()


def test_confirm_without_runner_returns_not_found(tmp_path):
    _app, svc, conn, *_rest = _desktop(tmp_path)  # market yes, runner no
    status, _body = _call(
        DesktopLoopbackApp(svc),
        "POST",
        "/v1/assembly/confirm",
        body={"contract": {"name": "x", "body": {"kind": "script", "goal": "g"}}},
    )
    assert status == 404
    conn.close()
