"""Substance over names, and verification that actually happens.

Exit gate for the two halves of the complaint "a good name is enough to
be found, and a new node is stuck at needs-verification forever":

1. SUBSTANCE. Search and candidacy read what a node DOES: capability
   tokens derived from the function's own code (imports, definitions,
   calls, adapter words, slots) join the search index at contribute; a
   node with no executable function inside is never a candidate — not
   for routes, not for ranking, not for a paying run — and can never be
   published at all.
2. VERIFICATION. A completed run through the node's own function IS a
   verified run: it lands in the metering ledger (idempotent per run,
   with no consumer principal — self-runs never unlock rating your own
   node) and promotes the account needs_verification -> live. Publish
   into the global nodeplace is gated on that proof.
"""

from __future__ import annotations

from test_growth_trigger import GOAL, TASK_TURN, _chat, _rig, _speak_work

from oolu.durable import DurableConnection
from oolu.metering.store import MeteringLedger
from oolu.nodeplace import (
    CandidateAssembler,
    ContributionError,
    ListingStatus,
    LiveVersionStats,
    NodeAccount,
    NodeAccountStore,
    NodeplaceService,
    NodeStatus,
    RegistryStore,
    WorkDesk,
    function_capabilities,
)
from oolu.skills.contract import Slot
from oolu.skills.models import ActionEvent, ReusableSkill, SkillSignature

SCRIPT = """\
import csv
from _oolu_runtime import emit_result

def normalize_rows(rows):
    return [row for row in rows if row]

emit_result("tidy")
"""


def _skill(script: str | None = None) -> ReusableSkill:
    actions = []
    if script is not None:
        actions = [
            ActionEvent(
                correlation_id="fn",
                adapter="script",
                operation="run",
                parameters={"goal": GOAL, "script": script, "node_key": "node:x"},
            )
        ]
    return ReusableSkill(
        name="normalize invoice csv files",
        description=GOAL,
        signature=SkillSignature(application="script", adapter="script"),
        actions=actions,
    )


def _registry(tmp_path, *, verified=None):
    conn = DurableConnection(tmp_path / "registry.db")
    registry = RegistryStore(conn)
    return conn, registry, NodeplaceService(registry, verified=verified)


def _contribute(service, skill, *, title="Invoice Normalizer Supreme"):
    return service.contribute(
        noder_principal="noder-1",
        tenant_id="t1",
        skill=skill,
        semver="1.0.0",
        title=title,
        summary="the finest invoice work money can buy",
    )


# --------------------------------------------------------------------------- #
# Substance: capabilities come from the code, never the name.                  #
# --------------------------------------------------------------------------- #
def test_capabilities_derive_from_the_function_not_the_name():
    caps = function_capabilities(
        _skill(SCRIPT),
        [Slot(name="invoice_csv", value_type="path", role="input")],
        [Slot(name="result", value_type="str", role="result")],
    )
    assert "fn:script" in caps and "fn:script.run" in caps
    assert "fn:csv" in caps  # the import, straight from the code
    assert "fn:normalize_rows" in caps  # the defined function
    assert "io:invoice_csv" in caps and "io:result" in caps
    # Universal plumbing carries no meaning; a nameless shell carries none.
    assert not any("emit_result" in c for c in caps)
    assert function_capabilities(_skill(None)) == []


def test_discover_matches_the_functions_own_words(tmp_path):
    conn, registry, service = _registry(tmp_path, verified=lambda _vid: True)
    try:
        result = _contribute(service, _skill(SCRIPT))
        assert "fn:normalize_rows" in result.listing.capabilities
        service.publish(
            result.listing.listing_id, noder_principal="noder-1", tenant_id="t1"
        )
        # Found by what the code DOES — a word that appears nowhere in the
        # title, summary, or author tags.
        [found] = registry.discover("normalize_rows")
        assert found.listing_id == result.listing.listing_id
    finally:
        conn.close()


def test_an_empty_active_listing_is_never_a_candidate(tmp_path):
    conn, registry, service = _registry(tmp_path)
    try:
        real = _contribute(service, _skill(SCRIPT), title="honest worker")
        empty = _contribute(service, _skill(None), title="Miracle Everything AI")
        # Force both listings active at the store level — bypassing the
        # publish gate on purpose: even a listing that somehow went active
        # must not rank on its name alone.
        for listing in (real.listing, empty.listing):
            registry.update_listing(
                listing.model_copy(update={"status": ListingStatus.ACTIVE})
            )
        assembler = CandidateAssembler(
            registry=registry,
            stats=LiveVersionStats(metering=MeteringLedger(conn)),
        )
        candidates = assembler.assemble("")
        assert [c.title for c in candidates] == ["honest worker"]
        # And the paid-run door is closed too.
        assert assembler.assemble_version(empty.version.version_id) is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Publish: a name is not a capability, and proof precedes the nodeplace.       #
# --------------------------------------------------------------------------- #
def test_publish_refuses_a_node_with_no_function(tmp_path):
    conn, registry, service = _registry(tmp_path, verified=lambda _vid: True)
    try:
        result = _contribute(service, _skill(None))
        try:
            service.publish(
                result.listing.listing_id,
                noder_principal="noder-1",
                tenant_id="t1",
            )
            raise AssertionError("an empty node must not publish")
        except ContributionError as exc:
            assert "a name is not a capability" in str(exc)
    finally:
        conn.close()


def test_publish_requires_a_verified_run(tmp_path):
    verified = {"answer": False}
    conn, registry, service = _registry(
        tmp_path, verified=lambda _vid: verified["answer"]
    )
    try:
        result = _contribute(service, _skill(SCRIPT))
        try:
            service.publish(
                result.listing.listing_id,
                noder_principal="noder-1",
                tenant_id="t1",
            )
            raise AssertionError("an unverified node must not publish")
        except ContributionError as exc:
            assert "needs verification first" in str(exc)
        # The proof arrives (a verified run) and the same door opens.
        verified["answer"] = True
        active = service.publish(
            result.listing.listing_id, noder_principal="noder-1", tenant_id="t1"
        )
        assert active.status is ListingStatus.ACTIVE
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The verification loop, end to end: build -> run -> verified -> live ->      #
# publishable.                                                                 #
# --------------------------------------------------------------------------- #
def test_a_personal_run_verifies_the_node_and_goes_live(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        metering = MeteringLedger(conn)
        app._metering = metering
        _speak_work(app, [TASK_TURN])
        _chat(app, ident, "please tidy up my invoice files")

        agreed = _chat(app, ident, "yes")
        reply = agreed.body["reply"]
        assert "VERIFIED" in reply and "live" in reply, reply

        # The evidence is real: one metering event, keyed to the node's
        # version, idempotent per run — and carrying NO consumer principal,
        # so a self-run never unlocks rating your own node.
        [event] = metering.events()
        assert event.idempotency_key.startswith("node-verify:")
        assert event.version_id is not None
        assert event.consumer_principal is None
        assert not metering.verified_run(event.version_id, "user-1")

        # The account moved: needs_verification -> live, from local use.
        [entry] = desk.overview(principal="user-1", tenant="t1")
        assert entry.status == "live"

        # And the proven node passes the publish gate into the nodeplace.
        registry = app._nodeplace._store
        stats = LiveVersionStats(metering=metering)
        gated = NodeplaceService(
            registry,
            verified=lambda vid: stats.version_stats(vid).successes > 0,
        )
        listing = registry.listing_for_version(event.version_id)
        active = gated.publish(
            listing.listing_id, noder_principal="user-1", tenant_id="t1"
        )
        assert active.status is ListingStatus.ACTIVE
    finally:
        conn.close()


def test_mark_verified_touches_only_needs_verification(tmp_path):
    conn = DurableConnection(tmp_path / "accounts.db")
    try:
        accounts = NodeAccountStore(conn)
        desk = WorkDesk(registry=None, accounts=accounts)
        accounts.upsert(NodeAccount(node_id="n1", responsible="alice"))
        promoted = desk.mark_verified("n1")
        assert promoted is not None and promoted.status is NodeStatus.LIVE
        # An errored node is never silently healed by a passing run.
        accounts.upsert(
            NodeAccount(node_id="n2", responsible="alice", status=NodeStatus.ERROR)
        )
        stays = desk.mark_verified("n2")
        assert stays is not None and stays.status is NodeStatus.ERROR
        # And a node with no account at all is a quiet no-op.
        assert desk.mark_verified("ghost") is None
    finally:
        conn.close()
