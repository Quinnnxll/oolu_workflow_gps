"""The member's pairwise preference book — "this member chose A over B".

Born on the poll floor (A3) and OUTLIVING it: the ``press_pairwise``
table was always the generic store — the source column names who wrote
the row so any future instrument shares one book in the DPO trainer's
own vocabulary. The poll floor is gone (the Poll agent is removed); the
book, and the member's right to export and erase it, stand. Its next
WRITER is the reader-survey desk (news-agent-benchmark-roadmap N3) —
today nothing writes it, and the export honestly answers empty.

Every row is written ONLY under the member's ``press.personalize``
consent (checked at the doors), scrub-checked again at export (defense
in depth), and rides account erasure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Callable

from ..knowledge.scrubbing import scrub

# The generic pairwise preference store — in the DPO trainer's own
# vocabulary, with the source named so story feedback and future survey
# instruments share one table.
_PAIRWISE_SCHEMA = """CREATE TABLE IF NOT EXISTS press_pairwise (
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    source TEXT NOT NULL,
    prompt TEXT NOT NULL,
    chosen TEXT NOT NULL,
    rejected TEXT NOT NULL,
    genres TEXT NOT NULL,
    at TEXT NOT NULL
)"""


class PairwiseStore:
    """Members' pairwise preferences — consent-gated at the doors,
    per-member, scrub-checked again at export (defense in depth)."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_PAIRWISE_SCHEMA)

    def record(
        self,
        *,
        tenant: str,
        principal: str,
        source: str,
        prompt: str,
        chosen: str,
        rejected: str,
        genres: tuple[str, ...] | list[str] = (),
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO press_pairwise
                     (tenant_id, principal, source, prompt, chosen,
                      rejected, genres, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tenant,
                    principal,
                    source,
                    prompt,
                    chosen,
                    rejected,
                    json.dumps(list(genres)),
                    self._clock().isoformat(),
                ),
            )

    def export(
        self, *, tenant: str, principal: str, limit: int = 10_000
    ) -> list[dict]:
        """The member's OWN pairs in the DPO dataset shape — scrubbed
        once more on the way out, empty or degenerate rows dropped (the
        `build_dpo_dataset` discipline)."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT prompt, chosen, rejected FROM press_pairwise
                   WHERE tenant_id = ? AND principal = ?
                   ORDER BY at DESC LIMIT ?""",
                (tenant, principal, int(limit)),
            ).fetchall()
        pairs: list[dict] = []
        for row in rows:
            prompt = scrub(str(row["prompt"]))
            chosen = scrub(str(row["chosen"]))
            rejected = scrub(str(row["rejected"]))
            if not (prompt.strip() and chosen.strip() and rejected.strip()):
                continue
            if chosen.strip() == rejected.strip():
                continue
            pairs.append(
                {"prompt": prompt, "chosen": chosen, "rejected": rejected}
            )
        return pairs

    def erase(self, *, tenant: str, principal: str) -> int:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM press_pairwise"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)
