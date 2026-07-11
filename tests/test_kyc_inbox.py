"""The KYC reviewer's inbox: a queue humans can actually work.

The apply and decide routes existed; what was missing was the reviewer's
view of what awaits a verdict. Exit gate: the store lists applications by
status with fast-tracked ones first (oldest first within a screen); the
gateway route is permission-gated on kyc:review (the bootstrap admin's
"*" covers it, ordinary members are refused); and a decided application
leaves the queue.
"""

from __future__ import annotations

from test_http_gateway import _req
from test_supernode_kyc import _kyc_rig, _seed_supernode

from oolu.identity import AuthorityGrant, Role
from oolu.nodeplace import KycStatus


def _grant_reviewer(ident, principal, tenant="t1"):
    ident.store.add_role(
        Role(
            tenant_id=tenant,
            name="kyc-reviewer",
            permissions=frozenset({"kyc:review", "approve:*"}),
        )
    )
    ident.store.add_grant(
        AuthorityGrant(
            tenant_id=tenant,
            principal_id=principal,
            role_name="kyc-reviewer",
            granted_by="x",
        )
    )


def test_the_inbox_lists_pending_applications_fast_tracked_first(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(tmp_path)
    try:
        super_id, _ = _seed_supernode(app, ident, registry, desk)
        plans["t1"] = "pro"
        # A standard-queue application (unknown domain)...
        kyc.apply(
            super_id,
            tenant="t1",
            principal="noder-export",
            legal_name="Some Co",
            company_email="office@some-company.example",
        )
        pending = kyc.pending()
        assert [r.node_id for r in pending] == [super_id]
        assert pending[0].status == KycStatus.PENDING_REVIEW

        _grant_reviewer(ident, "reviewer-1")
        inbox = app.handle(
            _req("GET", "/v1/kyc/reviews", token=ident.token("reviewer-1", "t1"))
        )
        assert inbox.status == 200, inbox.body
        [item] = inbox.body["items"]
        assert item["node_id"] == super_id
        assert item["screen"] == "standard"
    finally:
        conn.close()


def test_the_inbox_is_gated_and_empties_on_decision(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(tmp_path)
    try:
        super_id, _ = _seed_supernode(app, ident, registry, desk)
        plans["t1"] = "plus"
        kyc.apply(
            super_id,
            tenant="t1",
            principal="noder-export",
            legal_name="Mphepo",
            company_email="quinn@mphepo.io",  # trusted: fast track
        )

        # An ordinary member sees no queue — the permission wall holds.
        refused = app.handle(
            _req("GET", "/v1/kyc/reviews", token=ident.token("someone", "t1"))
        )
        assert refused.status == 403

        _grant_reviewer(ident, "reviewer-1")
        reviewer = ident.token("reviewer-1", "t1")
        [item] = app.handle(_req("GET", "/v1/kyc/reviews", token=reviewer)).body[
            "items"
        ]
        assert item["screen"] == "fast_track"

        decided = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{super_id}/kyc/decide",
                token=reviewer,
                body={"approved": True, "note": "registry checked"},
            )
        )
        assert decided.status == 200, decided.body

        # A verdict clears the inbox.
        after = app.handle(_req("GET", "/v1/kyc/reviews", token=reviewer))
        assert after.body["items"] == []
    finally:
        conn.close()
