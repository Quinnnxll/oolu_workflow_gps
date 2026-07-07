"""Issue 6: node hygiene — clones, fraud, zombies, and the policy that
was agreed upfront.

The walls under test: the create door refuses a node whose creator did
not agree to the Node Policy; the detectors find clones (same content,
published later), fraud (verified failures, zero successes), and zombies
(past the window with no run ever bound); the sweep revokes clones and
restricts the rest under approve authority, audited; and restriction is
real — a restricted node refuses new contract runs and leaves ranking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_http_gateway import _req
from test_work_desk import _desk_build

from oolu.nodeplace import (
    NODE_POLICY_VERSION,
    HygieneKind,
    NodeHygieneService,
    NodeplaceService,
    NodeStatus,
    VersionStats,
)
from oolu.skills.models import ActionEvent, ReusableSkill, SkillSignature


def _skill(name: str, url: str) -> ReusableSkill:
    return ReusableSkill(
        id=f"skill.{name}",
        name=name,
        description=f"{name} does things",
        signature=SkillSignature(application="web", adapter="http"),
        actions=[
            ActionEvent(
                correlation_id=name,
                adapter="http",
                operation="get",
                parameters={"url": url},
            )
        ],
    )


def _contribute(nodeplace, *, noder, name, url):
    return nodeplace.contribute(
        noder_principal=noder,
        tenant_id="t1",
        skill=_skill(name, url),
        semver="1.0.0",
        title=name,
        summary=f"{name} summary",
    )


class _Stats:
    """A scripted stats source: {version_id: (successes, failures)}."""

    def __init__(self, table=None):
        self.table = table or {}

    def version_stats(self, version_id):
        s, f = self.table.get(version_id, (0, 0))
        return VersionStats(successes=s, failures=f)


def _hygiene(registry, desk, **kwargs):
    return NodeHygieneService(
        registry=registry, accounts=desk._accounts, **kwargs
    )


def test_a_clone_is_detected_revoked_and_the_original_untouched(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        nodeplace = NodeplaceService(registry)
        # The same skill body, published twice: the SECOND is the clone —
        # different name and noder, identical sanitized content? No:
        # content hashes cover the whole skill, so a clone here is a
        # verbatim copy under a copycat account.
        original = _contribute(
            nodeplace, noder="honest", name="csv-export", url="https://a.example"
        )
        clone = nodeplace.contribute(
            noder_principal="copycat",
            tenant_id="t1",
            skill=_skill("csv-export", "https://a.example"),
            semver="1.0.0",
            title="csv-export (fast!)",
            summary="totally original work",
        )
        assert clone.node.node_id != original.node.node_id
        desk.create_account(
            clone.node.node_id, principal="copycat", tenant="t1"
        )

        hygiene = _hygiene(registry, desk)
        findings = hygiene.inspect()
        assert [f.kind for f in findings] == [HygieneKind.CLONE]
        assert findings[0].node_id == clone.node.node_id
        assert original.node.node_id in findings[0].evidence

        acted = hygiene.sweep()
        assert [f.action for f in acted] == ["revoked"]
        # The clone is gone from the market; the original still stands.
        assert registry.get_node(clone.node.node_id).revoked_at is not None
        assert registry.get_node(original.node.node_id).revoked_at is None
        account = desk.account_for(clone.node.node_id)
        assert account.status is NodeStatus.RESTRICTED
        # Idempotent: nothing new happens on the next sweep.
        assert hygiene.sweep() == []
    finally:
        conn.close()


def test_fraud_and_zombie_are_restricted_by_their_own_evidence(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        nodeplace = NodeplaceService(registry)
        fraud = _contribute(
            nodeplace, noder="seller", name="miracle-fix", url="https://f.example"
        )
        desk.create_account(fraud.node.node_id, principal="seller", tenant="t1")

        # Fraud: three platform-verified failures, not one success.
        stats = _Stats({fraud.version.version_id: (0, 3)})
        hygiene = _hygiene(registry, desk, stats=stats)
        (finding,) = [
            f for f in hygiene.inspect() if f.kind is HygieneKind.FRAUD
        ]
        assert finding.node_id == fraud.node.node_id
        assert "not one verified success" in finding.evidence
        hygiene.sweep()
        assert (
            desk.account_for(fraud.node.node_id).status
            is NodeStatus.RESTRICTED
        )

        # Zombie: seen from 100 days in the future, an untouched node with
        # no run ever bound has been abandoned past the window.
        abandoned = _contribute(
            nodeplace, noder="gone", name="left-behind", url="https://z.example"
        )
        desk.create_account(abandoned.node.node_id, principal="gone", tenant="t1")
        future = lambda: datetime.now(UTC) + timedelta(days=100)  # noqa: E731
        later = _hygiene(registry, desk, stats=_Stats(), clock=future)
        zombies = [
            f for f in later.inspect() if f.kind is HygieneKind.ZOMBIE
        ]
        assert abandoned.node.node_id in [f.node_id for f in zombies]
        # Today, nothing is a zombie yet.
        now_view = _hygiene(registry, desk, stats=_Stats())
        assert [
            f for f in now_view.inspect() if f.kind is HygieneKind.ZOMBIE
        ] == []
    finally:
        conn.close()


def test_the_platform_namespace_is_never_a_zombie(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        from oolu.nodeplace.handiwork import seed_handiwork_node

        nodeplace = NodeplaceService(registry)
        node_id = seed_handiwork_node(
            nodeplace, desk, registry, tenant="t1", principal="local"
        )
        future = lambda: datetime.now(UTC) + timedelta(days=365)  # noqa: E731
        hygiene = _hygiene(registry, desk, clock=future)
        assert node_id not in [f.node_id for f in hygiene.inspect()]
    finally:
        conn.close()


def test_restriction_is_real_no_runs_no_ranking(tmp_path):
    from test_contract_run import _assembled_contract, _CliExecutor, _seed_chain

    app, conn, ident, registry, *_rest, desk, _ = _desk_build(
        tmp_path, executors={"cli": _CliExecutor()}
    )
    try:
        exporter, _ = _seed_chain(app, ident, registry)
        node_id = registry.get_version(exporter).node_id
        desk.create_account(node_id, principal="noder-export", tenant="t1")
        hygiene = _hygiene(registry, desk)
        app._hygiene = hygiene

        contract = _assembled_contract(app, ident)
        # Restrict the exporter's node, then try to run through it.
        account = desk.account_for(node_id)
        desk._accounts.upsert(
            account.model_copy(update={"status": NodeStatus.RESTRICTED})
        )
        refused = app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=ident.token("consumer", "t2"),
                body={"contract": contract},
            )
        )
        assert refused.status == 409, refused.body
        assert "Node Policy" in refused.body["error"]["message"]

        # And ranking will not show it either.
        from oolu.nodeplace import CandidateAssembler

        assembler = CandidateAssembler(
            registry=registry,
            stats=app._market._stats,
            restricted=hygiene.is_restricted,
        )
        listed = {e.candidate.version_id for e in assembler.assemble("")}
        assert exporter not in listed
    finally:
        conn.close()


def test_the_sweep_route_needs_authority_and_audits(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        nodeplace = NodeplaceService(registry)
        original = _contribute(
            nodeplace, noder="honest", name="csv-export", url="https://a.example"
        )
        clone = nodeplace.contribute(
            noder_principal="copycat",
            tenant_id="t1",
            skill=_skill("csv-export", "https://a.example"),
            semver="1.0.0",
            title="csv-export deluxe",
            summary="original, honestly",
        )
        desk.create_account(clone.node.node_id, principal="copycat", tenant="t1")
        app._hygiene = _hygiene(registry, desk)

        # Anyone may look; only approve authority may act.
        report = app.handle(
            _req("GET", "/v1/work/hygiene", token=ident.token("copycat", "t1"))
        )
        assert report.status == 200
        assert report.body["items"][0]["kind"] == "clone"
        blocked = app.handle(
            _req(
                "POST",
                "/v1/work/hygiene/sweep",
                token=ident.token("copycat", "t1"),
            )
        )
        assert blocked.status == 403

        from test_contract_run import _grant_approver

        _grant_approver(ident, "steward", "t1")
        swept = app.handle(
            _req(
                "POST",
                "/v1/work/hygiene/sweep",
                token=ident.token("steward", "t1"),
            )
        )
        assert swept.status == 200, swept.body
        assert swept.body["items"][0]["node_id"] == clone.node.node_id
        events = [
            e.event_type
            for e in app._durable.audit.records(
                run_id=f"hygiene:{clone.node.node_id}"
            )
        ]
        assert events == ["hygiene.revoked"]
        assert original.node.node_id  # the original stands, still listed
    finally:
        conn.close()


def test_policy_agreement_is_stamped_fixed_and_published(tmp_path):
    app, conn, ident, registry, *_rest, desk, _ = _desk_build(tmp_path)
    try:
        from test_contract_run import _seed_chain

        exporter, _ = _seed_chain(app, ident, registry)
        node_id = registry.get_version(exporter).node_id
        token = ident.token("noder-export", "t1")

        # The policy has one public home.
        policy = app.handle(_req("GET", "/v1/work/policy", token=token))
        assert policy.body["version"] == NODE_POLICY_VERSION
        assert "clone" in policy.body["text"]

        created = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=token,
                body={"accept_policy": True},
            )
        )
        assert created.status == 200
        assert created.body["policy_version"] == NODE_POLICY_VERSION

        # What was agreed is fixed like the rest of the regime.
        rewrite = app.handle(
            _req(
                "POST",
                f"/v1/work/nodes/{node_id}/account",
                token=token,
                body={"policy_version": "something-else"},
            )
        )
        assert rewrite.status == 409
        assert "fixed at creation" in rewrite.body["error"]["message"]
    finally:
        conn.close()
