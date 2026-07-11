"""Hot-path aggregates: the same answers, without walking everything.

``LiveVersionStats.version_stats`` and the desk's earnings join used to
scan every metering event (and every audit record) per question — cost
that grew with the machine's whole history. They now read through
indexes: the version's bound runs, then exactly those runs' events and
executed records. Exit gate: the indexed path agrees with a brute-force
reference on a mixed world (direct events, contract participation via
the binding index, legacy representative-only bindings, failed local
evidence, audit-side failures), and the targeted store reads behave.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oolu.durable import DurableConnection
from oolu.metering.attribution import AttributionStore
from oolu.metering.models import MeteringEvent, RunBinding
from oolu.metering.store import MeteringLedger
from oolu.nodeplace import LiveVersionStats

NOW = datetime(2026, 7, 11, tzinfo=UTC)


def _event(key, run_id, version_id=None, outcome="succeeded", cost=None, seq=0):
    return MeteringEvent(
        idempotency_key=key,
        run_id=run_id,
        version_id=version_id,
        outcome=outcome,
        provider_cost=cost,
        audit_seq=seq,
        occurred_at=NOW,
    )


def _world(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    metering = MeteringLedger(conn)
    attribution = AttributionStore(conn)
    from oolu.durable import DurableAuditLog

    audit = DurableAuditLog(conn)
    return conn, metering, attribution, audit


def _brute_force(metering, attribution, audit, version_id):
    """The old full-scan semantics, verbatim — the reference answer."""

    def touches(run_id, event_version):
        if event_version == version_id:
            return True
        binding = attribution.get_binding(run_id)
        if binding is None:
            return False
        return version_id in (binding.version_ids or [binding.version_id])

    successes = failures = 0
    costs = []
    for event in metering.events():
        if not touches(event.run_id, event.version_id):
            continue
        if event.outcome == "failed":
            failures += 1
            continue
        successes += 1
        if event.provider_cost is not None:
            costs.append(event.provider_cost)
    for record in audit.records():
        if record.event_type != "workflow.executed":
            continue
        if record.payload.get("status") == "succeeded":
            continue
        if touches(record.payload.get("run_id", ""), None):
            failures += 1
    return successes, failures, (sum(costs) / len(costs)) if costs else None


def test_indexed_stats_agree_with_the_full_scan(tmp_path):
    conn, metering, attribution, audit = _world(tmp_path)
    try:
        # Direct events against v1: two successes with costs, one local
        # failure (no binding — the audit side must never see it).
        metering.record(_event("k1", "r1", "v1", cost=0.5, seq=1))
        metering.record(_event("k2", "r2", "v1", cost=1.5, seq=2))
        metering.record(_event("k3", "r3", "v1", outcome="failed", seq=3))
        # A contract run: the event names v9 as representative, but v1
        # participated — reachable only through the binding index.
        attribution.bind(
            RunBinding(
                run_id="r4",
                version_id="v9",
                version_ids=["v9", "v1"],
                consumer_tenant="t1",
            )
        )
        metering.record(_event("k4", "r4", "v9", seq=4))
        # A legacy binding: representative only, no participation rows.
        attribution.bind(
            RunBinding(run_id="r5", version_id="v1", consumer_tenant="t1")
        )
        audit.append(
            "workflow.executed", {"run_id": "r5", "status": "failed"}
        )
        # Noise that must not count for v1 at all.
        metering.record(_event("k5", "r6", "v2", seq=5))
        audit.append("workflow.executed", {"run_id": "r6", "status": "failed"})
        audit.append("workflow.started", {"run_id": "r5"})

        stats = LiveVersionStats(
            metering=metering, audit=audit, attribution=attribution
        )
        for version in ("v1", "v2", "v9", "ghost"):
            expected = _brute_force(metering, attribution, audit, version)
            got = stats.version_stats(version)
            assert (
                got.successes,
                got.failures,
                got.provider_cost_mean,
            ) == expected, version
        # And the mixed world reads as it should: 3 successes (two
        # direct + the contract run), 2 failures (local + audit-side).
        v1 = stats.version_stats("v1")
        assert (v1.successes, v1.failures) == (3, 2)
        assert v1.provider_cost_mean == 1.0
    finally:
        conn.close()


def test_targeted_store_reads(tmp_path):
    conn, metering, attribution, audit = _world(tmp_path)
    try:
        metering.record(_event("k1", "r1", "v1", seq=1))
        event = metering.get_by_event_id(metering.events()[0].event_id)
        assert event is not None and event.idempotency_key == "k1"
        assert metering.get_by_event_id("nope") is None

        # events_for_version dedupes the direct and run-bound reads.
        assert [
            e.idempotency_key for e in metering.events_for_version("v1", ["r1"])
        ] == ["k1"]

        attribution.bind(
            RunBinding(
                run_id="r2",
                version_id="v9",
                version_ids=["v9", "v1"],
                consumer_tenant="t1",
            )
        )
        attribution.bind(
            RunBinding(run_id="r3", version_id="v1", consumer_tenant="t1")
        )
        assert sorted(attribution.version_run_ids("v1")) == ["r2", "r3"]
        assert attribution.version_run_ids("ghost") == []

        audit.append("workflow.executed", {"run_id": "r2", "status": "failed"})
        audit.append("workflow.executed", {"run_id": "r3", "status": "succeeded"})
        audit.append("workflow.cancelled", {"run_id": "r2"})
        assert sorted(
            s for s in audit.executed_statuses(["r2", "r3"]) if s
        ) == ["failed", "succeeded"]
        assert audit.executed_statuses([]) == []
    finally:
        conn.close()
