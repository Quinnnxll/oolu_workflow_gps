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


def _desktop(
    tmp_path, *, executor=None, trace_store=None, authority=None, sessions=None
):
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    _seed_market(app, ident, registry)
    svc = DesktopService(
        app._durable,
        approval_authority=authority,
        market=app._market,
        price_book=app._price_book,
        contract_runner=(
            DagRouteRunner({"cli": executor}) if executor is not None else None
        ),
        attribution=attribution,
        trace_store=trace_store,
        session_manager=sessions,
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

    # explore=true Thompson-samples the picks; with a single producer per
    # slot the plan is the same — the flag just must flow through cleanly.
    status, explored = _call(
        app,
        "POST",
        "/v1/assembly/preview",
        body={"goal": "clean-the-books", "want": [TIDY], "explore": True},
    )
    assert status == 200 and explored["complete"] is True

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


def _identity_tokens(tmp_path):
    """Identity wiring that also mints raw bearer tokens (loopback tests)."""
    from datetime import UTC, datetime

    from workflow_gps.identity import (
        AuthorityGrant,
        AuthorityResolver,
        Hs256Signer,
        Hs256Verifier,
        IdentityApprovalAuthority,
        IdentityStore,
        OidcValidator,
        ProviderConfig,
        Role,
        SessionManager,
        Tenant,
    )

    secret, issuer, audience = "idp-secret", "https://idp", "wfgps"
    store = IdentityStore(tmp_path / "identity.db")
    store.add_tenant(Tenant(tenant_id="t1", name="t1"))
    store.add_role(
        Role(tenant_id="t1", name="approver", permissions=frozenset({"approve:*"}))
    )
    store.add_grant(
        AuthorityGrant(
            tenant_id="t1",
            principal_id="approver-1",
            role_name="approver",
            granted_by="admin",
        )
    )
    validator = OidcValidator(
        [
            ProviderConfig(
                issuer=issuer,
                audiences=frozenset({audience}),
                verifier=Hs256Verifier(secret),
            )
        ]
    )
    manager = SessionManager(store, validator)
    signer = Hs256Signer(secret=secret, issuer=issuer, audience=audience)
    authority = IdentityApprovalAuthority(AuthorityResolver(store))

    def token(subject):
        return signer.mint(subject=subject, tenant_id="t1", now=datetime.now(UTC))

    return authority, manager, token


def _destructive_contract():
    from workflow_gps.skills.contract import ActionsBody, NodeContract
    from workflow_gps.skills.models import ActionEvent

    return NodeContract(
        name="wipe it",
        body=ActionsBody(
            actions=[
                ActionEvent(correlation_id="c", adapter="cli", operation="delete_files")
            ]
        ),
    ).model_dump(mode="json")


def test_reserved_contract_becomes_an_approvable_inbox_task(tmp_path):
    """Not a dead end: confirming a reserved contract holds it in the inbox,
    an unauthorized session cannot release it, an authorized one runs it."""
    from test_desktop_shell import _identity

    from workflow_gps.identity.errors import AuthorizationError

    _store, authority, approver, intruder = _identity(tmp_path)
    executor = _CliExecutor(("run", "delete_files"))
    app, svc, conn, *_rest = _desktop(tmp_path, executor=executor, authority=authority)
    destructive = _destructive_contract()

    held = svc.confirm_assembly(destructive, confirm_id="click-1")
    assert held.status == "awaiting_approval"
    assert executor.calls == 0  # nothing ran unattended

    # It sits in the inbox as an approvable task, naming what is reserved.
    (item,) = svc.inbox(kind="contract-approval")
    assert item.run_id == held.run_id
    assert item.intent == "wipe it"
    assert "cli/delete_files" in item.prompt

    # A double-clicked confirm replays the hold; the inbox has ONE entry.
    again = svc.confirm_assembly(destructive, confirm_id="click-1")
    assert again.run_id == held.run_id
    assert len(svc.inbox(kind="contract-approval")) == 1

    # Over the loopback the hold is a 200 view, not a 403 refusal.
    status, body = _call(
        DesktopLoopbackApp(svc),
        "POST",
        "/v1/assembly/confirm",
        body={"contract": destructive},
    )
    assert status == 200 and body["status"] == "awaiting_approval"

    # An unauthorized session cannot release it; the hold survives.
    with pytest.raises(AuthorizationError):
        svc.approve_assembly(held.run_id, session=intruder)
    assert executor.calls == 0
    assert len(svc.inbox(kind="contract-approval")) == 2  # ours + loopback's

    # An authorized approver runs it through the shared money path.
    run = svc.approve_assembly(held.run_id, session=approver)
    assert run.status == "succeeded"
    assert executor.calls == 1
    assert all(i.run_id != held.run_id for i in svc.inbox())

    # The decision is audited with the approver's principal.
    (approved_event,) = [
        r for r in app._durable.audit.records() if r.event_type == "contract.approved"
    ]
    assert approved_event.payload["by"] == "approver-1"
    assert approved_event.payload["run_id"] == run.run_id
    assert approved_event.payload["reserved"] == ["cli/delete_files"]
    conn.close()


def test_loopback_approval_decision_end_to_end(tmp_path):
    """The UI's whole approval journey over the loopback: hold -> inbox ->
    decide with a bearer token. Bad or missing tokens never release it."""
    authority, manager, token = _identity_tokens(tmp_path)
    executor = _CliExecutor(("run", "delete_files"))
    _app, svc, conn, *_rest = _desktop(
        tmp_path, executor=executor, authority=authority, sessions=manager
    )
    loop = DesktopLoopbackApp(svc)

    status, held = _call(
        loop,
        "POST",
        "/v1/assembly/confirm",
        body={"contract": _destructive_contract()},
    )
    assert status == 200 and held["status"] == "awaiting_approval"
    pending_id = held["run_id"]

    def decide(approved, *, bearer=None, pid=pending_id):
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else None
        return _call(
            loop,
            "POST",
            f"/v1/assembly/approvals/{pid}",
            body={"approved": approved},
            headers=headers,
        )

    # No token -> 401; a garbage token -> 401; a valid but unauthorized
    # principal -> 403. The hold survives every failed attempt.
    assert decide(True)[0] == 401
    assert decide(True, bearer="garbage")[0] == 401
    assert decide(True, bearer=token("nobody"))[0] == 403
    assert executor.calls == 0
    assert len(svc.inbox(kind="contract-approval")) == 1

    # Deciding without the approved field is a 400, not a silent decline.
    status, _err = _call(
        loop,
        "POST",
        f"/v1/assembly/approvals/{pending_id}",
        body={},
        headers={"Authorization": f"Bearer {token('approver-1')}"},
    )
    assert status == 400

    # The authorized approver releases it; the run happens exactly once.
    status, run = decide(True, bearer=token("approver-1"))
    assert status == 200 and run["status"] == "succeeded"
    assert executor.calls == 1

    # Unknown (already decided) hold -> 404.
    assert decide(True, bearer=token("approver-1"))[0] == 404

    # And a decline over the loopback removes a fresh hold without running.
    _status, second = _call(
        loop,
        "POST",
        "/v1/assembly/confirm",
        body={"contract": _destructive_contract()},
    )
    status, declined = decide(False, bearer=token("approver-1"), pid=second["run_id"])
    assert status == 200 and declined["status"] == "declined"
    assert executor.calls == 1  # nothing else ran
    conn.close()


def test_loopback_approval_without_session_manager_is_not_found(tmp_path):
    authority, _manager, token = _identity_tokens(tmp_path)
    _app, svc, conn, *_rest = _desktop(
        tmp_path, executor=_CliExecutor(("run", "delete_files")), authority=authority
    )
    loop = DesktopLoopbackApp(svc)
    _status, held = _call(
        loop,
        "POST",
        "/v1/assembly/confirm",
        body={"contract": _destructive_contract()},
    )
    status, _body = _call(
        loop,
        "POST",
        f"/v1/assembly/approvals/{held['run_id']}",
        body={"approved": True},
        headers={"Authorization": f"Bearer {token('approver-1')}"},
    )
    assert status == 404  # no session manager wired: the route cannot exist
    conn.close()


def test_held_contracts_survive_a_shell_restart(tmp_path):
    """A hold is a durable commitment: a NEW service over the same durable
    store still lists it, and can decide it — recompiling once."""
    authority, manager, token = _identity_tokens(tmp_path)
    first_executor = _CliExecutor(("run", "delete_files"))
    app, first, conn, metering, attribution, audit = _desktop(
        tmp_path, executor=first_executor, authority=authority, sessions=manager
    )
    held = first.confirm_assembly(_destructive_contract())
    assert held.status == "awaiting_approval"

    # The shell restarts: a fresh service over the SAME durable connection,
    # with a fresh executor — nothing in-memory carries over.
    second_executor = _CliExecutor(("run", "delete_files"))
    reborn = DesktopService(
        app._durable,
        approval_authority=authority,
        market=app._market,
        price_book=app._price_book,
        contract_runner=DagRouteRunner({"cli": second_executor}),
        attribution=attribution,
        session_manager=manager,
    )
    (item,) = reborn.inbox(kind="contract-approval")
    assert item.run_id == held.run_id
    assert "cli/delete_files" in item.prompt

    # Deciding over the reborn shell's loopback recompiles and runs it.
    status, run = _call(
        DesktopLoopbackApp(reborn),
        "POST",
        f"/v1/assembly/approvals/{held.run_id}",
        body={"approved": True},
        headers={"Authorization": f"Bearer {token('approver-1')}"},
    )
    assert status == 200 and run["status"] == "succeeded"
    assert second_executor.calls == 1 and first_executor.calls == 0
    assert reborn.inbox(kind="contract-approval") == []
    # And the ORIGINAL service sees the durable truth too: the hold is gone.
    assert first.inbox(kind="contract-approval") == []
    conn.close()


def test_declining_a_held_contract_removes_it(tmp_path):
    from test_desktop_shell import _identity

    _store, authority, approver, _intruder = _identity(tmp_path)
    executor = _CliExecutor()
    app, svc, conn, *_rest = _desktop(tmp_path, executor=executor, authority=authority)
    held = svc.confirm_assembly(_destructive_contract())

    declined = svc.approve_assembly(held.run_id, session=approver, approved=False)
    assert declined.status == "declined"
    assert executor.calls == 0
    assert svc.inbox(kind="contract-approval") == []
    assert any(
        r.event_type == "contract.declined" and r.payload["by"] == "approver-1"
        for r in app._durable.audit.records()
    )
    # Deciding it twice is a KeyError -> 404 at any surface.
    with pytest.raises(KeyError):
        svc.approve_assembly(held.run_id, session=approver)
    conn.close()


def test_approval_still_runs_the_budget_gate(tmp_path):
    """Approval grants the RESERVED actions, not the money: the budget's
    review threshold still holds the run until acknowledged."""
    from test_desktop_shell import _identity

    _store, authority, approver, _intruder = _identity(tmp_path)
    executor = _CliExecutor(("run", "delete_files"))
    _app, svc, conn, *_rest = _desktop(tmp_path, executor=executor, authority=authority)
    # A marketplace chain (it costs money) PLUS a reserved local node.
    preview = svc.assembly_preview(goal="clean-the-books", want=[TIDY])
    hybrid = preview.contract
    hybrid["body"]["nodes"].append(_destructive_contract())

    held = svc.confirm_assembly(hybrid, review_threshold=0.000001)
    assert held.status == "awaiting_approval"  # reserved: held, not priced
    with pytest.raises(PermissionError, match="review threshold"):
        svc.approve_assembly(held.run_id, session=approver)
    assert executor.calls == 0
    assert len(svc.inbox(kind="contract-approval")) == 1  # still held

    # Acknowledged at confirm time: approval releases the whole hybrid.
    acknowledged = svc.confirm_assembly(
        hybrid, review_threshold=0.000001, review_acknowledged=True
    )
    run = svc.approve_assembly(acknowledged.run_id, session=approver)
    assert run.status == "succeeded"
    assert executor.calls == 3  # two marketplace steps + the approved one
    assert run.gross > 0  # the marketplace children committed their prices
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


def test_confirmed_runs_feed_the_trace_store_and_the_next_preview(tmp_path):
    """The desktop growth loop: every confirm sharpens the planner's own
    statistics — no separate training step."""
    from workflow_gps.knowledge.traces import TraceStore, route_node_key

    traces = TraceStore(tmp_path / "traces.db")
    _app, svc, conn, *_rest = _desktop(
        tmp_path, executor=_CliExecutor(), trace_store=traces
    )
    before = traces.posterior(route_node_key("invoice cleaner"))
    assert before.observations == 0

    preview = svc.assembly_preview(goal="clean-the-books", want=[TIDY])
    run = svc.confirm_assembly(preview.contract)
    assert run.status == "succeeded"

    for name in ("raw exporter", "invoice cleaner"):
        node = traces.posterior(route_node_key(name))
        assert (node.successes, node.failures) == (1, 0)
    goal = traces.posterior(route_node_key("clean-the-books"))
    assert goal.successes == 1

    # The next preview still assembles the (now personally proven) chain.
    again = svc.assembly_preview(goal="clean-the-books", want=[TIDY])
    assert again.complete and set(again.selected) == set(preview.selected)
    traces.close()
    conn.close()


def test_desktop_budget_gates_the_confirm_button(tmp_path):
    """The preview shows the verdict; the confirm enforces it — a review
    threshold holds the run at 403 until acknowledged, and the (possibly
    partial) linked wallet asks for review without ever refusing."""
    executor = _CliExecutor()
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    _seed_market(app, ident, registry)
    svc = DesktopService(
        app._durable,
        market=app._market,
        price_book=app._price_book,
        contract_runner=DagRouteRunner({"cli": executor}),
        attribution=attribution,
        wallet_lookup=lambda: 0.001,  # a sliver of the user's true assets
    )
    loop = DesktopLoopbackApp(svc)

    preview = svc.assembly_preview(
        goal="clean-the-books", want=[TIDY], review_threshold=0.01
    )
    assert preview.budget is not None
    assert preview.budget["needs_review"] is True
    reasons = " ".join(preview.budget["reasons"])
    assert "review threshold" in reasons and "may be partial" in reasons

    status, body = _call(
        loop,
        "POST",
        "/v1/assembly/confirm",
        body={"contract": preview.contract, "review_threshold": 0.01},
    )
    assert status == 403 and executor.calls == 0  # held for review

    status, run = _call(
        loop,
        "POST",
        "/v1/assembly/confirm",
        body={
            "contract": preview.contract,
            "review_threshold": 0.01,
            "review_acknowledged": True,
        },
    )
    assert status == 200 and run["status"] == "succeeded"
    assert executor.calls == 2  # the wallet informed; it never blocked
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
