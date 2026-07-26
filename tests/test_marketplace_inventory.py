"""M2: inventory — atomic reservations, expiry, oversell prevention.

Exit gate (docs/marketplace-build-plan.md §5 M2): two concurrent orders for
the last unit — exactly one reservation wins, the other is refused; an
expired reservation returns its stock on the next touch; a committed
reservation keeps it gone; a release racing the expiry sweep credits the
shelf exactly once.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from oolu.durable import DurableConnection
from oolu.marketplace import InventoryService

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _inventory(tmp_path):
    conn = DurableConnection(tmp_path / "durable.db")
    service = InventoryService(conn)
    service.seed("l1", 1)
    return service, conn


def test_exactly_one_contender_gets_the_last_unit(tmp_path):
    service, conn = _inventory(tmp_path)
    results: list = [None, None]

    def contend(slot: int) -> None:
        results[slot] = service.reserve(
            "l1", quantity=1, holder=f"intent-{slot}", now=NOW
        )

    threads = [threading.Thread(target=contend, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [r for r in results if r is not None]
    assert len(winners) == 1  # the guarded UPDATE decided
    assert service.available("l1", now=NOW) == 0
    conn.close()


def test_expired_reservations_release_their_stock_lazily(tmp_path):
    service, conn = _inventory(tmp_path)
    held = service.reserve(
        "l1", quantity=1, holder="intent-1", now=NOW, ttl_minutes=30
    )
    assert held is not None
    assert service.available("l1", now=NOW) == 0
    # The next touch past the TTL sweeps the dead cart.
    assert service.available("l1", now=NOW + timedelta(minutes=31)) == 1
    fresh = service.reserve(
        "l1", quantity=1, holder="intent-2", now=NOW + timedelta(minutes=31)
    )
    assert fresh is not None
    conn.close()


def test_commit_keeps_stock_gone_and_release_credits_exactly_once(tmp_path):
    service, conn = _inventory(tmp_path)
    held = service.reserve("l1", quantity=1, holder="intent-1", now=NOW)
    assert service.commit(held.reservation_id)
    # Committed units never come back — not by release, not by sweep.
    assert not service.release(held.reservation_id)
    assert service.available("l1", now=NOW + timedelta(days=1)) == 0

    service.seed("l2", 1)
    second = service.reserve("l2", quantity=1, holder="intent-2", now=NOW)
    assert service.release(second.reservation_id)
    assert not service.release(second.reservation_id)  # once means once
    assert service.available("l2", now=NOW) == 1
    conn.close()


def test_untracked_listings_are_nobodys_problem(tmp_path):
    service, conn = _inventory(tmp_path)
    assert not service.tracked("external-item")
    assert service.available("external-item", now=NOW) is None
    conn.close()
