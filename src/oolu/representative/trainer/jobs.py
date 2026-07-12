"""When a voice is due for a refresh, and putting that on the queue.

The policy is deliberately boring: enough corpus to train at all (the
cold-start floor), then refresh when the user has said enough NEW things
or the active adapter has simply aged out. The sweep is idempotent — the
enqueue key carries the exchange count, so an unchanged corpus can never
queue twice, and every retrain is FROM BASE on the rolling corpus.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..dataset import COLD_START_FLOOR
from ..store import RepresentativeStore

TRAIN_TASK_KIND = "representative.train"

# A refresh is due after this many new exchanges since the active adapter…
REFRESH_NEW_EXCHANGES = 200
# …or when the active adapter is simply this old.
REFRESH_AGE_S = 7 * 24 * 3600.0


def refresh_reason(
    store: RepresentativeStore,
    scope: str,
    *,
    now: float | None = None,
    floor: int = COLD_START_FLOOR,
) -> str | None:
    """Why this scope should train now — or None to leave it alone."""
    count = store.exchange_count(scope)
    if count < floor:
        return None
    active = store.active_adapter(scope)
    if active is None:
        return f"first adapter ({count} exchanges)"
    fresh = count - int(active["message_count"])
    if fresh >= REFRESH_NEW_EXCHANGES:
        return f"{fresh} new exchanges since v{active['version']}"
    trained_at = float(active["trained_at"] or 0.0)
    age = (now if now is not None else time.time()) - trained_at
    if age >= REFRESH_AGE_S:
        return f"v{active['version']} is {age / 86400:.0f} days old"
    return None


def sweep(
    store: RepresentativeStore,
    queue,
    *,
    now: float | None = None,
    floor: int = COLD_START_FLOOR,
    clock: Callable[[], float] = time.time,
) -> list:
    """Enqueue a training task for every scope that is due. Returns the
    tasks touched (existing ones on an unchanged corpus, thanks to the
    idempotency key — never a duplicate)."""
    moment = now if now is not None else clock()
    tasks = []
    for scope in store.scopes():
        reason = refresh_reason(store, scope, now=moment, floor=floor)
        if reason is None:
            continue
        count = store.exchange_count(scope)
        tasks.append(
            queue.enqueue(
                TRAIN_TASK_KIND,
                {"scope": scope, "reason": reason, "exchange_count": count},
                idempotency_key=f"{TRAIN_TASK_KIND}:{scope}:{count}",
            )
        )
    return tasks
