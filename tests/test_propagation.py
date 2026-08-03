"""W4 — trigger propagation: consent, durable dedupe, and the router.

The stores are the durable spine (per-web consent, the fire-once claim);
the router is pure orchestration over injected ports — enqueue on trigger,
deliver on drain, bounded/deduped/consent-gated. All unit-testable without
a gateway.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oolu.paver import (
    FireResult,
    PropagationConsentStore,
    RouteWeb,
    TriggerClaimStore,
    WebTriggerRouter,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _conn(tmp_path):
    from oolu.durable.connection import DurableConnection

    return DurableConnection(tmp_path / "prop.db")


# --------------------------------------------------------------------------- #
# Consent store.                                                              #
# --------------------------------------------------------------------------- #
def test_consent_grants_revokes_and_regrants(tmp_path):
    store = PropagationConsentStore(_conn(tmp_path))
    assert store.stands("t1", "web-1") is False  # nothing granted
    store.grant("t1", "web-1", granted_by="quinn", now=NOW)
    assert store.stands("t1", "web-1") is True
    # Revoke stops it; re-checking stands False.
    assert store.revoke("t1", "web-1", now=NOW) is True
    assert store.stands("t1", "web-1") is False
    # Revoking an already-revoked web is a no-op (rowcount 0).
    assert store.revoke("t1", "web-1", now=NOW) is False
    # Re-granting clears the revoke.
    store.grant("t1", "web-1", granted_by="quinn", now=NOW)
    assert store.stands("t1", "web-1") is True
    # Walled: another tenant's web is unaffected.
    assert store.stands("t2", "web-1") is False
    listing = store.list("t1")
    assert len(listing) == 1 and listing[0]["web_id"] == "web-1"


# --------------------------------------------------------------------------- #
# Durable dedupe — fire-once across processes.                                #
# --------------------------------------------------------------------------- #
def test_trigger_claim_fires_once_across_processes(tmp_path):
    conn = _conn(tmp_path)
    a = TriggerClaimStore(conn)
    b = TriggerClaimStore(conn)  # a second "process" on the same connection
    assert a.claim("stamp-1", "web-1", now=NOW) is True
    # Neither the same store nor a sibling re-claims the same (stamp, target).
    assert a.claim("stamp-1", "web-1", now=NOW) is False
    assert b.claim("stamp-1", "web-1", now=NOW) is False
    # A different target under the same trigger is a fresh claim (cycle A->B).
    assert a.claim("stamp-1", "web-2", now=NOW) is True
    # A different trigger re-fires the same target.
    assert b.claim("stamp-2", "web-1", now=NOW) is True


# --------------------------------------------------------------------------- #
# Router — enqueue on trigger.                                                 #
# --------------------------------------------------------------------------- #
def _web(web_id, anchor):
    return RouteWeb(web_id=web_id, tenant="t1", anchor=anchor, node_ids=[anchor])


def _router(*, consent, webs, fire_web=None, claim=None, enqueue=None, audit=None):
    return WebTriggerRouter(
        consent=consent,
        claim=claim or (lambda stamp, target: True),
        webs_at=lambda tenant, node_id: webs,
        fire_web=fire_web or (lambda t, w, s, h: FireResult(w, "fired", fired=1)),
        enqueue=enqueue,
        audit=audit,
    )


def test_on_trigger_enqueues_only_consented_anchored_webs():
    enqueued: list = []
    granted = {"web-a"}
    router = _router(
        consent=lambda tenant, web_id: web_id in granted,
        webs=[_web("web-a", "node-1"), _web("web-b", "node-1")],
        enqueue=enqueued.append,
    )
    out = router.on_trigger("t1", "node-1", "run-9")
    assert [e.web_id for e in out] == ["web-a"]  # web-b unconsented, skipped
    assert enqueued[0]["web_id"] == "web-a"
    assert enqueued[0]["trigger_stamp"] == "node-1:run-9"  # stamp minted from run
    assert enqueued[0]["hop"] == 1


def test_on_trigger_at_max_hops_enqueues_nothing():
    router = _router(consent=lambda t, w: True, webs=[_web("web-a", "node-1")])
    assert router.on_trigger("t1", "node-1", "run-1", hop=99) == []


# --------------------------------------------------------------------------- #
# Router — deliver on drain.                                                   #
# --------------------------------------------------------------------------- #
def _payload(web_id="web-a", stamp="node-1:run-9", hop=1):
    return {
        "tenant": "t1", "web_id": web_id, "anchor": "node-1",
        "trigger_stamp": stamp, "hop": hop,
    }


def test_deliver_fires_once_then_dedupes():
    claimed: set = set()
    fires: list = []

    def claim(stamp, target):
        key = (stamp, target)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    def fire_web(tenant, web_id, stamp, hop):
        fires.append(web_id)
        return FireResult(web_id, "fired", fired=2, run_id="r")

    router = _router(consent=lambda t, w: True, webs=[], claim=claim, fire_web=fire_web)
    first = router.deliver(_payload())
    assert first.status == "fired" and first.fired == 2
    # A re-delivered message (at-least-once outbox) whose claim is taken is a no-op.
    second = router.deliver(_payload())
    assert second.status == "skipped"
    assert fires == ["web-a"]  # fired exactly once


def test_deliver_refuses_when_consent_revoked_between_enqueue_and_drain():
    fires: list = []
    router = _router(
        consent=lambda t, w: False,  # revoked since enqueue
        webs=[],
        fire_web=lambda t, w, s, h: fires.append(w) or FireResult(w, "fired"),
    )
    result = router.deliver(_payload())
    assert result.status == "no-consent"
    assert fires == []  # never fired


def test_deliver_enforces_the_hop_bound():
    router = _router(consent=lambda t, w: True, webs=[])
    result = router.deliver(_payload(hop=99))
    assert result.status == "bounded"


def test_deliver_surfaces_a_hold_without_firing_around_it():
    router = _router(
        consent=lambda t, w: True,
        webs=[],
        fire_web=lambda t, w, s, h: FireResult(w, "held", reason="reserved hop"),
    )
    result = router.deliver(_payload())
    assert result.status == "held" and "reserved" in result.reason


# --------------------------------------------------------------------------- #
# W4.1 — transient failures retry; the fan-out cap is real.                    #
# --------------------------------------------------------------------------- #
def test_transient_failure_releases_the_claim_and_retries():
    # A transient "failed" fire must RAISE DeliveryRetry with its claim
    # released, so the outbox leaves the message pending and a later drain
    # re-takes the claim and fires — the cascade is never silently lost.
    from oolu.paver import DeliveryRetry

    import pytest

    claimed: set = set()
    released: list = []
    attempts: list = []

    def claim(stamp, target):
        key = (stamp, target)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    def release(stamp, target):
        claimed.discard((stamp, target))
        released.append(target)

    def fire_web(tenant, web_id, stamp, hop):
        attempts.append(web_id)
        if len(attempts) == 1:
            return FireResult(web_id, "failed", reason="transient 503")
        return FireResult(web_id, "fired", fired=2, run_id="r")

    router = WebTriggerRouter(
        consent=lambda t, w: True,
        claim=claim,
        webs_at=lambda t, n: [],
        fire_web=fire_web,
        release=release,
    )
    with pytest.raises(DeliveryRetry):
        router.deliver(_payload())
    assert released == ["web-a"]  # the claim went back
    # The retry (a later drain) re-takes the claim and fires.
    second = router.deliver(_payload())
    assert second.status == "fired"
    assert attempts == ["web-a", "web-a"]


def test_final_delivery_settles_a_failure_without_retry():
    # attempts exhausted => final=True: the failure SETTLES (no raise, the
    # claim kept) so a permanently-broken web never retries forever.
    claimed: set = set()

    def claim(stamp, target):
        key = (stamp, target)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    router = WebTriggerRouter(
        consent=lambda t, w: True,
        claim=claim,
        webs_at=lambda t, n: [],
        fire_web=lambda t, w, s, h: FireResult(w, "failed", reason="still broken"),
        release=lambda s, t: claimed.discard((s, t)),
    )
    result = router.deliver(_payload(), final=True)
    assert result.status == "failed"
    assert ("node-1:run-9", "web-a") in claimed  # the claim stands — settled


def test_max_fires_per_trigger_is_enforced_at_both_sides():
    # Enqueue side: one trigger stages at most max_fires webs.
    enqueued: list = []
    webs = [_web(f"web-{i}", "node-1") for i in range(5)]
    router = WebTriggerRouter(
        consent=lambda t, w: True,
        claim=lambda s, t: True,
        webs_at=lambda t, n: webs,
        fire_web=lambda t, w, s, h: FireResult(w, "fired"),
        enqueue=enqueued.append,
        max_fires=3,
    )
    out = router.on_trigger("t1", "node-1", "run-1")
    assert len(out) == 3  # capped, not 5

    # Deliver side: the durable fires counter refuses past the cap.
    fired: list = []
    counter = {"n": 3}  # this trigger already fired 3 targets
    router2 = WebTriggerRouter(
        consent=lambda t, w: True,
        claim=lambda s, t: True,
        webs_at=lambda t, n: [],
        fire_web=lambda t, w, s, h: fired.append(w) or FireResult(w, "fired"),
        fires=lambda stamp: counter["n"],
        max_fires=3,
    )
    result = router2.deliver(_payload())
    assert result.status == "bounded" and fired == []
