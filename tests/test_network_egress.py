"""Gated network egress: a node reaches ONLY the hosts its human granted.

The sandbox stays severed; the honest enforcement point is the host-side
HTTP hand. Consent lives on the node's account (``network_hosts``, given
and withdrawable through the Work desk), is stamped onto the node's http
actions when a contract is prepared for execution, and is enforced by the
executor on EVERY redirect hop. Empty grant = no egress at all — fail
closed. Ad-hoc actions the user submits directly stay governed by the
machine policy alone, exactly as before.
"""

from __future__ import annotations

import httpx
import pytest
from test_contract_run import _assembled_contract, _build
from test_gateway_market import _contribute_and_publish
from test_http_gateway import _req
from test_market_assemble import TIDY

from oolu.billing import BillingService, EarningsLedger
from oolu.nodeplace import (
    NodeAccountStore,
    WorkDesk,
    compile_contract,
    stamp_egress_grants,
)
from oolu.nodeplace.accounts import MAX_NETWORK_HOSTS, normalize_network_hosts
from oolu.skills.contract import ActionsBody, NodeContract
from oolu.skills.http_adapter import HttpActionExecutor, HttpExecutionPolicy
from oolu.skills.models import ActionEvent, ExecutionStatus

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - a resolver stub


def _action(url, **params):
    params["url"] = url
    return ActionEvent(
        correlation_id="c1", adapter="http", operation="get", parameters=params
    )


def _executor(handler=None, resolver=PUBLIC):
    handler = handler or (lambda request: httpx.Response(200, text="ok"))
    return HttpActionExecutor(
        HttpExecutionPolicy(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=resolver,
    )


# --------------------------------------------------------------------------- #
# The executor: the grant is a second wall inside the machine policy.          #
# --------------------------------------------------------------------------- #
def test_a_granted_host_and_its_subdomains_pass():
    executor = _executor()
    for i, url in enumerate(
        ("https://api.example/x", "https://v2.api.example/x")
    ):
        outcome = executor.execute(
            _action(url, _egress_hosts=["api.example"]),
            idempotency_key=f"k{i}",
        )
        assert outcome.status is ExecutionStatus.SUCCEEDED, outcome.error


def test_an_ungranted_host_is_blocked_before_the_network():
    hits = []

    def never(request):
        hits.append(request)
        return httpx.Response(200)

    executor = _executor(never)
    outcome = executor.execute(
        _action("https://elsewhere.example/x", _egress_hosts=["api.example"]),
        idempotency_key="k1",
    )
    assert outcome.status is ExecutionStatus.BLOCKED
    assert "granted hosts" in outcome.error
    assert hits == []


def test_an_empty_grant_fails_closed():
    executor = _executor()
    outcome = executor.execute(
        _action("https://api.example/x", _egress_hosts=[]),
        idempotency_key="k1",
    )
    assert outcome.status is ExecutionStatus.BLOCKED
    assert "no network grant" in outcome.error


def test_no_grant_key_means_the_machine_policy_alone_governs():
    executor = _executor()
    outcome = executor.execute(
        _action("https://anywhere.example/x"), idempotency_key="k1"
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED


def test_a_redirect_to_an_ungranted_host_dies_at_the_bounce():
    def bouncing(request):
        if request.url.host == "api.example":
            return httpx.Response(
                302, headers={"location": "https://exfil.example/sink"}
            )
        return httpx.Response(200, text="leaked")

    executor = _executor(bouncing)
    outcome = executor.execute(
        _action("https://api.example/x", _egress_hosts=["api.example"]),
        idempotency_key="k1",
    )
    assert outcome.status is ExecutionStatus.BLOCKED
    assert "granted hosts" in outcome.error


# --------------------------------------------------------------------------- #
# The grant's canonical form: bare public hostnames, a short list.             #
# --------------------------------------------------------------------------- #
def test_grants_normalize_to_deduped_lowercase_hostnames():
    assert normalize_network_hosts(None) == ()
    assert normalize_network_hosts(["API.Example", "api.example", ""]) == (
        "api.example",
    )


@pytest.mark.parametrize(
    "bad",
    [
        ["https://api.example"],  # a grant names a host, not a URL
        ["api.example/path"],
        ["api.example:8080"],
        ["*.example.com"],  # subdomains are already covered
        ["localhost"],
        ["127.0.0.1"],  # the machine's own network is never grantable
        ["nodots"],
        "api.example",  # a string is not a list
    ],
)
def test_grant_refusals_are_loud(bad):
    with pytest.raises(ValueError):
        normalize_network_hosts(bad)


def test_a_grant_is_a_short_reviewable_list():
    too_many = [f"h{i}.example" for i in range(MAX_NETWORK_HOSTS + 1)]
    with pytest.raises(ValueError, match="at most"):
        normalize_network_hosts(too_many)


# --------------------------------------------------------------------------- #
# Stamping: registered children carry consent; ad-hoc actions do not.          #
# --------------------------------------------------------------------------- #
def test_stamping_marks_registered_children_and_spares_adhoc_ones():
    contract = NodeContract(
        name="fetch-things",
        body=ActionsBody(
            actions=[_action("https://api.example/x"), _action("https://b.example/y")]
        ),
    )
    compiled = compile_contract(contract)
    untouched = stamp_egress_grants(contract, compiled, {})
    assert untouched is compiled  # no registered children: nothing changes

    stamped = stamp_egress_grants(
        contract, compiled, {contract.id: ("api.example",)}
    )
    for item in stamped.blueprint.actions:
        assert item.action.parameters["_egress_hosts"] == ["api.example"]
    assert stamped.owners == compiled.owners
    # The original compile is untouched — stamping never mutates.
    for item in compiled.blueprint.actions:
        assert "_egress_hosts" not in item.action.parameters


def test_an_empty_grant_is_still_stamped_so_the_executor_fails_it_closed():
    contract = NodeContract(
        name="fetch-things",
        body=ActionsBody(actions=[_action("https://api.example/x")]),
    )
    stamped = stamp_egress_grants(
        contract, compile_contract(contract), {contract.id: ()}
    )
    assert stamped.blueprint.actions[0].action.parameters["_egress_hosts"] == []


# --------------------------------------------------------------------------- #
# The whole wall, end to end: contribute, run blocked, consent, run again.     #
# --------------------------------------------------------------------------- #
def _egress_build(tmp_path):
    executor = _executor()
    app, conn, ident, registry, metering, attribution, audit = _build(
        tmp_path, executors={"http": executor}
    )
    desk = WorkDesk(
        registry=registry,
        accounts=NodeAccountStore(conn),
        billing=BillingService(EarningsLedger(conn)),
        metering=metering,
        attribution=attribution,
        audit=audit,
    )
    app._desk = desk
    return app, conn, ident, registry, desk


def _seed_http_node(app, ident, registry):
    version_id = _contribute_and_publish(
        app,
        ident,
        registry,
        name="invoice fetcher",
        noder="noder-fetch",
        price=0.10,
        consumes=[],
        produces=[TIDY],
        actions=[
            {
                "correlation_id": "c",
                "adapter": "http",
                "operation": "get",
                "parameters": {"url": "https://api.example/report"},
            }
        ],
    )
    return version_id, registry.get_version(version_id).node_id


def test_a_node_reaches_nothing_until_its_human_consents(tmp_path):
    app, conn, ident, registry, desk = _egress_build(tmp_path)
    try:
        version_id, node_id = _seed_http_node(app, ident, registry)
        contract = _assembled_contract(app, ident)

        # No grant yet: the run happens, but the node's fetch fails closed.
        blocked = app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=ident.token("consumer", "t2"),
                body={"contract": contract},
            )
        )
        assert blocked.status == 200, blocked.body
        assert blocked.body["status"] != "succeeded"
        assert any(
            "no network grant" in (o.get("error") or "")
            for o in blocked.body["outcomes"]
        )

        # The responsible creates the account (agreeing to the Node Policy
        # upfront) and then grants the exact host through the desk door.
        created = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=ident.token("noder-fetch", "t1"),
                body={"accept_policy": True},
            )
        )
        assert created.status == 200, created.body
        granted = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=ident.token("noder-fetch", "t1"),
                body={"network_hosts": ["api.example"]},
            )
        )
        assert granted.status == 200, granted.body
        assert granted.body["network_hosts"] == ["api.example"]

        ok = app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=ident.token("consumer", "t2"),
                body={"contract": contract},
            )
        )
        assert ok.status == 200, ok.body
        assert ok.body["status"] == "succeeded", ok.body["outcomes"]

        # Consent changes its mind: withdrawing closes the wall again.
        withdrawn = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=ident.token("noder-fetch", "t1"),
                body={"network_hosts": []},
            )
        )
        assert withdrawn.status == 200
        assert withdrawn.body["network_hosts"] == []
        again = app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=ident.token("consumer", "t2"),
                body={"contract": contract},
            )
        )
        assert again.body["status"] != "succeeded"
    finally:
        conn.close()


def test_a_bad_grant_is_refused_in_words_at_the_door(tmp_path):
    app, conn, ident, registry, desk = _egress_build(tmp_path)
    try:
        _version_id, node_id = _seed_http_node(app, ident, registry)
        created = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=ident.token("noder-fetch", "t1"),
                body={"accept_policy": True},
            )
        )
        assert created.status == 200, created.body
        resp = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=ident.token("noder-fetch", "t1"),
                body={"network_hosts": ["https://api.example/path"]},
            )
        )
        assert resp.status == 400
        assert "bare hostname" in resp.body["error"]["message"]
    finally:
        conn.close()
