"""Concurrency stress on the shared durable stores.

The gateway is one process today, but the stores are shared truth (two
gateway processes over one database, worker threads inside DagRouteRunner)
— so the primitives must hold under real thread contention. Invariants:
an idempotent operation executes exactly once no matter how many threads
race it; ledger dedup admits exactly one row per unique key; a hold is
decided (removed) by exactly one contender; trace statistics lose nothing.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from oolu.billing import EarningsEntry, EarningsKind, EarningsLedger
from oolu.durable.connection import DurableConnection
from oolu.durable.idempotency import IdempotencyLedger
from oolu.knowledge.traces import NodeObservation, TraceStore, route_node_key
from oolu.nodeplace.holds import PendingContractRecord, PendingContractStore

NOW = datetime(2026, 7, 1, tzinfo=UTC)
THREADS = 16


def _race(count, fn):
    """Run ``fn(i)`` on ``count`` threads through a barrier for max contention."""
    barrier = threading.Barrier(count)

    def contender(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(contender, range(count)))


def test_idempotent_run_executes_exactly_once_under_contention(tmp_path):
    conn = DurableConnection(tmp_path / "idem.db")
    idem = IdempotencyLedger(conn)
    executions = []

    def effect():
        executions.append(1)
        time.sleep(0.02)  # widen the window losers race through
        return "done"

    results = _race(THREADS, lambda i: idem.run("job", effect))
    assert len(executions) == 1  # the money property: one effect, ever
    # The winner sees the result; a loser racing DURING execution sees the
    # claim (None) — never a second execution, never an exception.
    assert set(results) <= {"done", None}
    assert "done" in results

    # After release, the key runs again — still exactly once.
    idem.release("job")
    _race(THREADS, lambda i: idem.run("job", effect))
    assert len(executions) == 2
    conn.close()


def test_ledger_dedup_admits_exactly_one_row_per_unique_key(tmp_path):
    conn = DurableConnection(tmp_path / "ledger.db")
    ledger = EarningsLedger(conn)
    entry = EarningsEntry(
        noder_principal="alice",
        event_id="evt-1",
        amount_micros=5_000_000,
        kind=EarningsKind.ACCRUAL,
        available_at=NOW,
    )
    # Same (event, noder, kind) raced by everyone: one row wins.
    outcomes = _race(THREADS, lambda i: ledger.append(entry))
    assert outcomes.count(True) == 1
    assert len(ledger.entries("alice")) == 1

    # Distinct entries raced concurrently: none are lost.
    _race(
        THREADS,
        lambda i: ledger.append(
            EarningsEntry(
                noder_principal="bob",
                event_id=f"evt-{i}",
                amount_micros=1_000_000,
                kind=EarningsKind.ACCRUAL,
                available_at=NOW,
            )
        ),
    )
    assert len(ledger.entries("bob")) == THREADS
    conn.close()


def test_a_hold_is_decided_by_exactly_one_contender(tmp_path):
    conn = DurableConnection(tmp_path / "holds.db")
    store = PendingContractStore(conn)
    store.add(
        PendingContractRecord(
            pending_id="hold-1",
            contract={"name": "x"},
            reserved=["cli/delete"],
            created_at=NOW,
        )
    )
    # Every approver clicks at once: exactly one removal succeeds — the
    # primitive both surfaces' decision paths rest on.
    outcomes = _race(THREADS, lambda i: store.remove("hold-1"))
    assert outcomes.count(True) == 1
    assert store.get("hold-1") is None

    # Adds racing a sweep: nothing corrupts, every add lands or expires.
    def add_or_sweep(i):
        if i % 4 == 0:
            return store.sweep_expired(NOW)
        store.add(
            PendingContractRecord(
                pending_id=f"hold-{i}",
                contract={"name": str(i)},
                created_at=NOW,
            )
        )
        return None

    _race(THREADS, add_or_sweep)
    listed = store.list()
    assert {r.pending_id for r in listed} == {
        f"hold-{i}" for i in range(THREADS) if i % 4 != 0
    }
    conn.close()


def test_trace_statistics_lose_nothing_under_contention(tmp_path):
    traces = TraceStore(tmp_path / "traces.db")
    runs_per_thread = 25

    def record(i):
        for run in range(runs_per_thread):
            traces.record_run(
                goal="stress",
                steps=[
                    NodeObservation(node_key="node:a", ok=True, cost=1.0),
                    NodeObservation(node_key="node:b", ok=(run % 2 == 0)),
                ],
                success=True,
                context="",
            )

    _race(8, record)
    total = 8 * runs_per_thread
    assert traces.posterior("node:a").observations == total
    b = traces.posterior("node:b")
    assert b.successes + b.failures == total
    assert traces.posterior(route_node_key("stress")).successes == total
    # Precedence counted once per verified pair: a before b on even runs.
    ab, ba = traces.precedence("node:a", "node:b")
    assert (ab, ba) == (8 * (runs_per_thread // 2 + 1), 0)
    traces.close()
