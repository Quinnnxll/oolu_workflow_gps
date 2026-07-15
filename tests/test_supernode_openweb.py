"""The open web for verified Supernodes, and the org's own blocks.

A Supernode set up under the global account — a VERIFIED legal entity —
is not limited to the 8-host egress grant: its whole fleet's web stands
open, flowing down the membership chain like trust. What remains is the
org's own CHOICE, exercised exactly like a user's: which hosts to refuse
(``blocked_hosts``, enforced on every redirect hop) and which principals
not to hear from (``blocked_users``, refusing their messages in words).
"""

from __future__ import annotations

import httpx
import pytest
from test_http_gateway import _req
from test_supernode_kyc import _kyc_rig, _seed_supernode

from oolu.chat import GatewayChatTools
from oolu.durable.files import UserFileStore
from oolu.nodeplace import stamp_egress_grants
from oolu.nodeplace.accounts import (
    MAX_BLOCKED_USERS,
    normalize_blocked_hosts,
    normalize_blocked_users,
)
from oolu.skills.http_adapter import HttpActionExecutor, HttpExecutionPolicy
from oolu.skills.models import ActionEvent, ExecutionStatus

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - a resolver stub


def _action(url, **params):
    params["url"] = url
    return ActionEvent(
        correlation_id="c1", adapter="http", operation="get", parameters=params
    )


def _executor(handler=None):
    handler = handler or (lambda request: httpx.Response(200, text="ok"))
    return HttpActionExecutor(
        HttpExecutionPolicy(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=PUBLIC,
    )


def _verify(kyc, super_id):
    kyc.apply(
        super_id,
        tenant="t1",
        principal="noder-export",
        legal_name="Mphepo Ltd",
        company_email="quinn@mphepo.io",
    )
    kyc.decide(super_id, reviewer="reviewer-1", approved=True)


# --------------------------------------------------------------------------- #
# The verdict: open web flows down from a VERIFIED Supernode, or not at all.   #
# --------------------------------------------------------------------------- #
def test_open_egress_takes_a_verified_supernode(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(
        tmp_path, plans={"t1": "pro"}
    )
    try:
        super_id, member_id = _seed_supernode(app, ident, registry, desk)
        # Before verification: the allow-grant regime for everyone.
        assert kyc.open_egress(super_id) is None
        assert kyc.open_egress(member_id) is None
        _verify(kyc, super_id)
        # Verified: the Supernode AND its member stand open, no blocks yet.
        assert kyc.open_egress(super_id) == ()
        assert kyc.open_egress(member_id) == ()
        # A node from nowhere stays under the grant regime.
        assert kyc.open_egress("unknown-node") is None
    finally:
        conn.close()


def test_blocked_hosts_union_down_the_chain(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(
        tmp_path, plans={"t1": "pro"}
    )
    try:
        super_id, member_id = _seed_supernode(app, ident, registry, desk)
        _verify(kyc, super_id)
        desk.update_account(
            super_id,
            principal="noder-export",
            tenant="t1",
            blocked_hosts=["tracker.example.com", "ads.example.net"],
        )
        # The org's refusals bind the whole fleet; the member's own list
        # joins the union — blocks add up, they never cancel. (The member
        # node was contributed by noder-clean, who owns its account door.)
        desk.update_account(
            member_id,
            principal="noder-clean",
            tenant="t1",
            blocked_hosts=["extra.example.org"],
        )
        assert kyc.open_egress(super_id) == (
            "ads.example.net",
            "tracker.example.com",
        )
        assert kyc.open_egress(member_id) == (
            "ads.example.net",
            "extra.example.org",
            "tracker.example.com",
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The executor: open web minus the blocks, on every hop.                       #
# --------------------------------------------------------------------------- #
def test_open_web_passes_everything_but_the_blocked_hosts():
    executor = _executor()
    ok = executor.execute(
        _action(
            "https://anywhere.example.com/data",
            _egress_open=True,
            _egress_blocked=["tracker.example.net"],
        ),
        idempotency_key="k1",
    )
    assert ok.status is ExecutionStatus.SUCCEEDED
    # The blocked host — and its subdomains — die before the network.
    for i, url in enumerate(
        ("https://tracker.example.net/x", "https://deep.tracker.example.net/x")
    ):
        blocked = executor.execute(
            _action(
                url, _egress_open=True, _egress_blocked=["tracker.example.net"]
            ),
            idempotency_key=f"k2-{i}",
        )
        assert blocked.status is ExecutionStatus.BLOCKED
        assert "blocked by this node's Supernode" in blocked.error


def test_a_redirect_into_a_blocked_host_dies_at_the_bounce():
    def handler(request):
        if request.url.host == "start.example.com":
            return httpx.Response(
                302, headers={"location": "https://tracker.example.net/pixel"}
            )
        return httpx.Response(200, text="ok")

    executor = _executor(handler)
    bounced = executor.execute(
        _action(
            "https://start.example.com/page",
            _egress_open=True,
            _egress_blocked=["tracker.example.net"],
        ),
        idempotency_key="k3",
    )
    assert bounced.status is ExecutionStatus.BLOCKED
    assert "blocked" in bounced.error


def test_open_beats_the_allow_grant_when_both_are_stamped():
    # A node that carries BOTH regimes (verified after its grant was set)
    # runs open: verification is the wider consent.
    executor = _executor()
    outcome = executor.execute(
        _action(
            "https://ungranted.example.com/x",
            _egress_hosts=["only.example.org"],
            _egress_open=True,
            _egress_blocked=[],
        ),
        idempotency_key="k4",
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# Stamping: the open regime rides the same execution-time stamp.               #
# --------------------------------------------------------------------------- #
def test_stamping_prefers_open_grants_for_verified_fleets():
    from oolu.nodeplace import compile_contract
    from oolu.skills.contract import ActionsBody, NodeContract

    contract = NodeContract(
        name="fetch-things",
        body=ActionsBody(actions=[_action("https://api.example/x")]),
    )
    compiled = compile_contract(contract)
    # The open regime stamps the open flag and the blocks — never a list
    # of allowed hosts.
    stamped = stamp_egress_grants(
        contract, compiled, {}, {contract.id: ("tracker.example.net",)}
    )
    params = stamped.blueprint.actions[0].action.parameters
    assert params["_egress_open"] is True
    assert params["_egress_blocked"] == ["tracker.example.net"]
    assert "_egress_hosts" not in params
    # A node in BOTH maps runs open: verification is the wider consent.
    both = stamp_egress_grants(
        contract,
        compiled,
        {contract.id: ("granted.example.com",)},
        {contract.id: ()},
    )
    params = both.blueprint.actions[0].action.parameters
    assert params["_egress_open"] is True
    assert "_egress_hosts" not in params
    # The original compile is untouched — stamping never mutates.
    for item in compiled.blueprint.actions:
        assert "_egress_open" not in item.action.parameters


# --------------------------------------------------------------------------- #
# The lists themselves: validated hard, mutable through the account door.      #
# --------------------------------------------------------------------------- #
def test_block_lists_are_validated_like_grants():
    assert normalize_blocked_hosts(["Ads.Example.NET", "ads.example.net"]) == (
        "ads.example.net",
    )
    with pytest.raises(ValueError, match="block list names hosts"):
        normalize_blocked_hosts(["https://ads.example.net/x"])
    with pytest.raises(ValueError, match="never grantable"):
        normalize_blocked_hosts(["127.0.0.1"])
    assert normalize_blocked_users([" zoe ", "zoe", "kai"]) == ("zoe", "kai")
    with pytest.raises(ValueError, match="at most"):
        normalize_blocked_users([f"user-{i}" for i in range(MAX_BLOCKED_USERS + 1)])


def test_the_account_door_edits_blocks_and_still_refuses_fixed_traits(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(
        tmp_path, plans={"t1": "pro"}
    )
    try:
        super_id, _member_id = _seed_supernode(app, ident, registry, desk)
        owner = ident.token("noder-export", "t1")
        saved = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{super_id}/account",
                token=owner,
                body={
                    "blocked_hosts": ["tracker.example.net"],
                    "blocked_users": ["mallory"],
                },
            )
        )
        assert saved.status == 200, saved.body
        assert saved.body["blocked_hosts"] == ["tracker.example.net"]
        assert saved.body["blocked_users"] == ["mallory"]
        # The fixed-at-creation wall stands exactly as before.
        refused = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{super_id}/account",
                token=owner,
                body={"is_supernode": False},
            )
        )
        assert refused.status == 409
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Blocking a user: the org stops hearing them, like a user blocking a user.    #
# --------------------------------------------------------------------------- #
def test_a_blocked_user_cannot_message_the_org(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(
        tmp_path, plans={"t1": "pro"}
    )
    try:
        super_id, member_id = _seed_supernode(app, ident, registry, desk)
        # Mallory legitimately answers for a node in the fleet — they can
        # REACH it. Then the org blocks them: reach is not the same as
        # being heard.
        desk.onboard_account(member_id, principal="mallory", tenant="t1")
        store = UserFileStore(conn)
        mallory_tools = GatewayChatTools(
            store, tenant="t1", principal="mallory", desk=desk
        )
        delivered = mallory_tools.deliver_message(
            "node", member_id, "status update"
        )
        assert not delivered.startswith("error:"), delivered

        desk.update_account(
            super_id,
            principal="noder-export",
            tenant="t1",
            blocked_users=["mallory"],
        )
        # The refusal walks the chain: the Supernode itself AND its member.
        assert "mallory" in desk.blocked_users_for(super_id)
        assert "mallory" in desk.blocked_users_for(member_id)
        assert "mallory" not in desk.blocked_users_for("unrelated-node")
        result = mallory_tools.deliver_message(
            "node", member_id, "let me in anyway"
        )
        assert result.startswith("error:")
        assert "blocked" in result
        # Someone the org has not blocked is still heard.
        owner_tools = GatewayChatTools(
            store, tenant="t1", principal="noder-export", desk=desk
        )
        heard = owner_tools.deliver_message(
            "node", super_id, "welcome aboard"
        )
        assert not heard.startswith("error:"), heard
    finally:
        conn.close()
