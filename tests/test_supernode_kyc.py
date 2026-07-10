"""Issue 5: Supernode KYC — screened applications, paid verification,
a global trust-ranking multiplier.

The walls under test: the screen refuses personal mailboxes outright and
fast-tracks trusted company domains; applying takes a Supernode, its
owner, and a paying plan; a human with approve authority decides; the
multiplier flows down membership chains (never stacking) and lands in
marketplace ranking through the candidate assembler.
"""

from __future__ import annotations

import pytest
from test_http_gateway import _req
from test_work_desk import _desk_build

from oolu.nodeplace import (
    VERIFIED_TRUST_MULTIPLIER,
    KycScreen,
    KycService,
    KycStatus,
    KycStore,
    OwnershipError,
    SubscriptionRequired,
    screen_company_email,
)

TRUSTED = frozenset({"mphepo.io"})


def test_the_screen_sorts_the_queue_and_refuses_personal_mailboxes():
    # A free mailbox can never anchor a legal entity: refused outright.
    with pytest.raises(ValueError, match="personal mailbox"):
        screen_company_email("ceo@gmail.com", trusted_domains=TRUSTED)
    with pytest.raises(ValueError, match="not a mailbox"):
        screen_company_email("not-an-address", trusted_domains=TRUSTED)
    # A trusted company domain (e.g. Google-verified workspace): fast lane.
    screen, note = screen_company_email(
        "quinn@mphepo.io", trusted_domains=TRUSTED
    )
    assert screen is KycScreen.FAST_TRACK and "trusted" in note
    # Anything else: the standard review queue — never auto-verified.
    screen, note = screen_company_email(
        "office@some-company.example", trusted_domains=TRUSTED
    )
    assert screen is KycScreen.STANDARD


def _kyc_rig(tmp_path, plans=None, *, global_service=True):
    # KYC binds only on the GLOBAL service, so the rig defaults to one;
    # the edge test flips the flag off.
    from oolu.gateway import GatewayConfig

    app, conn, ident, registry, *_rest, desk, _ = _desk_build(
        tmp_path, config=GatewayConfig(global_service=global_service)
    )
    plans = plans if plans is not None else {}
    kyc = KycService(
        KycStore(conn),
        accounts=desk._accounts,
        plan_for=lambda tenant: plans.get(tenant, "free"),
        trusted_domains=TRUSTED,
    )
    app._kyc = kyc
    return app, conn, ident, registry, desk, kyc, plans


def _seed_supernode(app, ident, registry, desk):
    from test_contract_run import _seed_chain

    exporter, cleaner = _seed_chain(app, ident, registry)
    super_id = registry.get_version(exporter).node_id
    member_id = registry.get_version(cleaner).node_id
    desk.create_account(
        super_id, principal="noder-export", tenant="t1", is_supernode=True
    )
    desk.create_account(
        member_id,
        principal="noder-export",
        tenant="t1",
        supernode_id=super_id,
        authority_level=2,
    )
    return super_id, member_id


def test_applying_takes_a_supernode_its_owner_and_a_paying_plan(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(tmp_path)
    try:
        super_id, member_id = _seed_supernode(app, ident, registry, desk)

        # A plain member node is not a legal entity's anchor.
        with pytest.raises(ValueError, match="for Supernodes"):
            kyc.apply(
                member_id,
                tenant="t1",
                principal="noder-export",
                legal_name="Mphepo Ltd",
                company_email="quinn@mphepo.io",
            )
        # A stranger cannot apply on someone else's Supernode.
        with pytest.raises(OwnershipError):
            kyc.apply(
                super_id,
                tenant="t1",
                principal="stranger",
                legal_name="Mphepo Ltd",
                company_email="quinn@mphepo.io",
            )
        # The reviewing work rides on the subscription fee: free is refused.
        with pytest.raises(SubscriptionRequired, match="paying plan"):
            kyc.apply(
                super_id,
                tenant="t1",
                principal="noder-export",
                legal_name="Mphepo Ltd",
                company_email="quinn@mphepo.io",
            )
        plans["t1"] = "pro"
        record = kyc.apply(
            super_id,
            tenant="t1",
            principal="noder-export",
            legal_name="Mphepo Ltd",
            company_email="quinn@mphepo.io",
        )
        assert record.status is KycStatus.PENDING_REVIEW
        assert record.screen is KycScreen.FAST_TRACK
        # One application at a time; the screen's verdict is stored.
        with pytest.raises(ValueError, match="already under review"):
            kyc.apply(
                super_id,
                tenant="t1",
                principal="noder-export",
                legal_name="Mphepo Ltd",
                company_email="quinn@mphepo.io",
            )
    finally:
        conn.close()


def test_the_multiplier_flows_down_the_chain_and_never_stacks(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(
        tmp_path, plans={"t1": "enterprise"}
    )
    try:
        super_id, member_id = _seed_supernode(app, ident, registry, desk)
        kyc.apply(
            super_id,
            tenant="t1",
            principal="noder-export",
            legal_name="Mphepo Ltd",
            company_email="quinn@mphepo.io",
        )
        # Nothing multiplies until a human verifies.
        assert kyc.trust_multiplier(super_id) == 1.0
        assert kyc.trust_multiplier(member_id) == 1.0

        kyc.decide(super_id, reviewer="reviewer-1", approved=True)
        # The Supernode itself, and every node under it, carries the
        # global trust multiplier — once, never compounded.
        assert kyc.trust_multiplier(super_id) == VERIFIED_TRUST_MULTIPLIER
        assert kyc.trust_multiplier(member_id) == VERIFIED_TRUST_MULTIPLIER
        # A node from nowhere stays neutral.
        assert kyc.trust_multiplier("unknown-node") == 1.0
    finally:
        conn.close()


def test_verified_trust_lands_in_marketplace_ranking(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(
        tmp_path, plans={"t1": "pro"}
    )
    try:
        from oolu.nodeplace import CandidateAssembler

        super_id, member_id = _seed_supernode(app, ident, registry, desk)
        assembler = CandidateAssembler(
            registry=registry,
            stats=app._market._stats,
            trust=kyc.trust_multiplier,
        )
        before = {
            e.candidate.version_id: e.candidate.reputation
            for e in assembler.assemble("")
        }
        kyc.apply(
            super_id,
            tenant="t1",
            principal="noder-export",
            legal_name="Mphepo Ltd",
            company_email="quinn@mphepo.io",
        )
        kyc.decide(super_id, reviewer="reviewer-1", approved=True)
        after = {
            e.candidate.version_id: e.candidate.reputation
            for e in assembler.assemble("")
        }
        member_versions = [
            v.version_id for v in registry.list_versions(member_id)
        ]
        boosted = [
            vid for vid in member_versions if vid in before and vid in after
        ]
        assert boosted, "the member node should be listed"
        for vid in boosted:
            assert after[vid] == pytest.approx(
                before[vid] * VERIFIED_TRUST_MULTIPLIER
            )
    finally:
        conn.close()


def test_the_kyc_routes_screen_gate_decide_and_audit(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(tmp_path)
    try:
        super_id, _ = _seed_supernode(app, ident, registry, desk)
        owner = ident.token("noder-export", "t1")

        # A personal mailbox never even becomes an application.
        refused = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{super_id}/kyc",
                token=owner,
                body={
                    "legal_name": "Mphepo Ltd",
                    "company_email": "quinn@gmail.com",
                },
            )
        )
        assert refused.status == 400
        assert "personal mailbox" in refused.body["error"]["message"]

        # Free plan: 402 — the fee funds the reviewing work.
        gated = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{super_id}/kyc",
                token=owner,
                body={
                    "legal_name": "Mphepo Ltd",
                    "company_email": "quinn@mphepo.io",
                },
            )
        )
        assert gated.status == 402

        plans["t1"] = "pro"
        applied = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{super_id}/kyc",
                token=owner,
                body={
                    "legal_name": "Mphepo Ltd",
                    "company_email": "quinn@mphepo.io",
                    "registration_no": "MW-2026-001",
                },
            )
        )
        assert applied.status == 201, applied.body
        assert applied.body["screen"] == "fast_track"

        # Deciding takes approve authority; the owner alone has none.
        blocked = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{super_id}/kyc/decide",
                token=owner,
                body={"approved": True},
            )
        )
        assert blocked.status == 403

        from test_contract_run import _grant_approver

        _grant_approver(ident, "reviewer-1", "t1")
        decided = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{super_id}/kyc/decide",
                token=ident.token("reviewer-1", "t1"),
                body={"approved": True, "note": "registry checked"},
            )
        )
        assert decided.status == 200, decided.body
        assert decided.body["status"] == "verified"
        assert decided.body["multiplier"] == VERIFIED_TRUST_MULTIPLIER

        status = app.handle(
            _req("GET", f"/v1/work/nodes/{super_id}/kyc", token=owner)
        )
        assert status.body["application"]["status"] == "verified"
        assert status.body["trust_multiplier"] == VERIFIED_TRUST_MULTIPLIER

        # Both moments are in the audit trail.
        events = [
            e.event_type
            for e in app._durable.audit.records(run_id=f"kyc:{super_id}")
        ]
        assert events == ["kyc.applied", "kyc.decided"]
    finally:
        conn.close()


def test_edge_installs_owe_no_kyc_and_no_subscription(tmp_path):
    # The desktop and private-network hosts are Edge: a Supernode there
    # serves its own people, not the global ecosystem — no KYC policy, no
    # paying-plan gate. Only the Global service enforces either.
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(
        tmp_path, global_service=False
    )
    try:
        super_id, _ = _seed_supernode(app, ident, registry, desk)
        owner = ident.token("noder-export", "t1")

        # The status says so, and the UI hides the whole block on it.
        status = app.handle(
            _req("GET", f"/v1/work/nodes/{super_id}/kyc", token=owner)
        )
        assert status.status == 200
        assert status.body["required"] is False
        assert status.body["application"] is None

        # Applying is refused as unnecessary — 409, never a plan nag (402).
        refused = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{super_id}/kyc",
                token=owner,
                body={
                    "legal_name": "Mphepo Ltd",
                    "company_email": "quinn@mphepo.io",
                },
            )
        )
        assert refused.status == 409
        assert "Edge install" in refused.body["error"]["message"]
        assert kyc.status_for(super_id) is None  # nothing was stored
    finally:
        conn.close()


def test_the_global_service_reports_kyc_as_required(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(tmp_path)
    try:
        super_id, _ = _seed_supernode(app, ident, registry, desk)
        status = app.handle(
            _req(
                "GET",
                f"/v1/work/nodes/{super_id}/kyc",
                token=ident.token("noder-export", "t1"),
            )
        )
        assert status.body["required"] is True
    finally:
        conn.close()
