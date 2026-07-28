"""Explorer decisions — inferred modes, and comparisons that end (A6.1).

Two laws, both against paralysis:

1. **The mode is read, not asked.** Nobody picks value/balanced/proven/
   measured from a menu: the member's own words weigh first (an
   instruction like "cheapest" or "lab numbers" names the lens), their
   LAST decided comparison's mode second, and "balanced" stands when
   nothing speaks. Deterministic — the same words, the same history,
   the same lens.
2. **A comparison is a runtime with a deadline.** One open comparison
   per member (a new one supersedes the old — never a pile), and every
   comparison expires on its own after DECISION_TTL_HOURS (a 1–3 day
   band): an undecided comparison lapses honestly instead of standing
   forever — no decision paralysis, no system overload.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Callable

DECISION_TTL_HOURS = 48  # the default deadline — inside the 1–3 day band
TTL_MIN_HOURS, TTL_MAX_HOURS = 24, 72

# What a member's own words name — checked in order, first hit wins.
_MODE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("value", ("cheap", "cheapest", "price", "budget", "value", "deal")),
    ("proven", ("proven", "trusted", "trust", "review", "reviews", "reliable")),
    ("measured", ("measured", "lab", "benchmark", "numbers", "tested", "data")),
    ("balanced", ("balanced", "overall", "all round", "all-round")),
)


def infer_mode(instruction: str = "", *, last_mode: str | None = None) -> str:
    """The lens, read from the member: their words outrank their
    history; their history outranks the default."""
    lowered = f" {str(instruction or '').lower()} "
    for mode, hints in _MODE_HINTS:
        if any(f" {hint}" in lowered for hint in hints):
            return mode
    return last_mode or "balanced"


_SCHEMA = """CREATE TABLE IF NOT EXISTS explorer_comparisons (
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    query TEXT NOT NULL,
    mode TEXT NOT NULL,
    listing_ids TEXT NOT NULL,
    state TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, principal)
)"""


class ComparisonStore:
    """The one live comparison per member — durable, superseding, and
    self-expiring. ``standing`` sweeps lazily: an expired row answers as
    "expired" exactly once, then rests."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def open(
        self,
        *,
        tenant: str,
        principal: str,
        query: str,
        mode: str,
        listing_ids: list[str],
        ttl_hours: int = DECISION_TTL_HOURS,
    ) -> dict:
        ttl = min(max(int(ttl_hours), TTL_MIN_HOURS), TTL_MAX_HOURS)
        now = self._clock()
        row = {
            "query": query,
            "mode": mode,
            "listing_ids": listing_ids,
            "state": "open",
            "opened_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=ttl)).isoformat(),
        }
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO explorer_comparisons
                     (tenant_id, principal, query, mode, listing_ids,
                      state, opened_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, principal) DO UPDATE SET
                     query = excluded.query, mode = excluded.mode,
                     listing_ids = excluded.listing_ids,
                     state = excluded.state,
                     opened_at = excluded.opened_at,
                     expires_at = excluded.expires_at""",
                (
                    tenant,
                    principal,
                    query,
                    mode,
                    json.dumps(listing_ids),
                    "open",
                    row["opened_at"],
                    row["expires_at"],
                ),
            )
        return row

    def standing(self, *, tenant: str, principal: str) -> dict | None:
        with self._conn.lock:
            r = self._conn.db.execute(
                "SELECT * FROM explorer_comparisons"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
        if r is None:
            return None
        row = {
            "query": r["query"],
            "mode": r["mode"],
            "listing_ids": json.loads(r["listing_ids"]),
            "state": r["state"],
            "opened_at": r["opened_at"],
            "expires_at": r["expires_at"],
        }
        if (
            row["state"] == "open"
            and datetime.fromisoformat(row["expires_at"]) <= self._clock()
        ):
            # The deadline holds by itself — lapse, honestly, once.
            self._set_state(tenant, principal, "expired")
            row["state"] = "expired"
        return row

    def close(self, *, tenant: str, principal: str, state: str = "decided") -> bool:
        current = self.standing(tenant=tenant, principal=principal)
        if current is None or current["state"] != "open":
            return False
        self._set_state(tenant, principal, state)
        return True

    def last_decided_mode(self, *, tenant: str, principal: str) -> str | None:
        row = self.standing(tenant=tenant, principal=principal)
        return row["mode"] if row is not None and row["state"] == "decided" else None

    def _set_state(self, tenant: str, principal: str, state: str) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE explorer_comparisons SET state = ?"
                " WHERE tenant_id = ? AND principal = ?",
                (state, tenant, principal),
            )
