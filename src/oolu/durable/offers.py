"""Standing growth offers, durable across restarts and processes.

The growth trigger asks a question in the conversation — "say yes and
I'll build/run it" — and the very NEXT message answers it. That promise
must not depend on which process serves the next message, or on the host
staying up in between: the offer is one small durable row, keyed to the
person it was made to, holding exactly what was offered.

One offer per person (a new offer replaces the old — the newest question
is the one on the table), consumed atomically by ``pop`` so an answer is
spent exactly once even with concurrent turns.
"""

from __future__ import annotations

from datetime import UTC, datetime

_SCHEMA = """CREATE TABLE IF NOT EXISTS growth_offers (
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    kind TEXT NOT NULL,
    goal TEXT NOT NULL,
    original_goal TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant, principal)
)"""


class GrowthOfferStore:
    """The standing offer per person: (kind, goal, original_goal).

    ``kind`` is the gateway's vocabulary — "build" builds the goal,
    "reuse" runs a near-match node's own goal, "build_distinct" builds
    after the user said the near-match is different work. The store keeps
    words, never behavior: what an answer does stays in the gateway."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def put(
        self,
        tenant: str,
        principal: str,
        *,
        kind: str,
        goal: str,
        original_goal: str,
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO growth_offers
                   (tenant, principal, kind, goal, original_goal, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant, principal) DO UPDATE SET
                     kind = excluded.kind,
                     goal = excluded.goal,
                     original_goal = excluded.original_goal,
                     created_at = excluded.created_at""",
                (
                    tenant,
                    principal,
                    kind,
                    goal,
                    original_goal,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get(self, tenant: str, principal: str) -> tuple[str, str, str] | None:
        """The standing offer, unspent — for tests and inspection."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT kind, goal, original_goal FROM growth_offers"
                " WHERE tenant = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
        if row is None:
            return None
        return (row["kind"], row["goal"], row["original_goal"])

    def pop(self, tenant: str, principal: str) -> tuple[str, str, str] | None:
        """Take the offer off the table — read and delete in ONE
        transaction, so a consent is spent exactly once."""
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT kind, goal, original_goal FROM growth_offers"
                " WHERE tenant = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "DELETE FROM growth_offers WHERE tenant = ? AND principal = ?",
                (tenant, principal),
            )
            return (row["kind"], row["goal"], row["original_goal"])
