"""The atomic memory spine — M0 of the memory-stack plan.

Phase 5 of the context-harness arc proved a pattern on one seat: the
BuildLedger's durable rows with provenance, and supersession instead of
deletion. This module promotes that pattern to the PLATFORM's memory
contract (docs/memory-stack-plan.md §M0; the Adaptive Capability Web's
§17.2 atomic memory): one table, every tier's records in one shape —
type, statement, structured value, scope, validity, confidence,
verification state, provenance, supersession.

Three laws are structural, not conventional:

- **Admission is earned.** A record claiming ``observed`` or better
  MUST carry provenance (event ids that resolve on durable storage —
  the audit chain, a ledger row); only ``proposed`` may arrive bare,
  and it never outranks what evidence backs. This is the write-back
  admission policy the harness plan enforced for builds, generalized.
- **Supersession is a WHERE clause.** ``recall`` excludes superseded
  and expired rows in SQL — a corrected value cannot re-enter an
  executable context by oversight, because the exclusion is not a
  caller's discipline, it is the query's shape.
- **History is never erased.** Correction stamps ``superseded_by``;
  the row, its provenance, and its mistakes stay auditable forever.

Writers bridge, never fork: the BuildLedger dual-writes its lessons
here (its own rows remain its provenance), and every future writer
names its seat and its admission state before its first row. The one
reader is ``recall`` — scope-walled, validity-filtered, ranked by the
shared retrieval scorer, budget-capped by the caller.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .retrieval import score as _score

_SCHEMA = """CREATE TABLE IF NOT EXISTS memory_spine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    structured_value TEXT,
    scope_ids TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    confidence REAL NOT NULL,
    verification_state TEXT NOT NULL,
    provenance TEXT NOT NULL,
    source_seat TEXT NOT NULL DEFAULT '',
    superseded_by INTEGER,
    created_at TEXT NOT NULL
)"""

# The capability-web ladder: evidence promotes a record rightward,
# never a rewrite.
VERIFICATION_STATES = ("proposed", "observed", "reproduced", "verified", "rejected")

# States that assert something HAPPENED must prove where they saw it.
_EVIDENCE_STATES = ("observed", "reproduced", "verified")


class MemorySpine:
    """The one atomic-memory table, on the same connection every other
    promise rides."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)
            db.execute(
                "CREATE INDEX IF NOT EXISTS memory_spine_scope_idx"
                " ON memory_spine (memory_type, superseded_by)"
            )

    # ------------------------------------------------------------------ #
    def admit(
        self,
        memory_type: str,
        statement: str,
        *,
        scope_ids: list[str] | tuple[str, ...],
        verification_state: str = "proposed",
        provenance: list[str] | tuple[str, ...] = (),
        confidence: float = 0.5,
        structured_value: dict | None = None,
        source_seat: str = "",
        valid_until: str | None = None,
        supersedes: list[int] | tuple[int, ...] = (),
    ) -> int:
        """One record onto the spine; returns its row id. Raises on a
        record that breaks the admission law — a memory admitted wrong
        is worse than no memory at all."""
        if verification_state not in VERIFICATION_STATES:
            raise ValueError(f"unknown verification state {verification_state!r}")
        if verification_state in _EVIDENCE_STATES and not provenance:
            raise ValueError(
                f"a {verification_state!r} memory must carry provenance — "
                "evidence states are earned, never asserted"
            )
        if not str(statement or "").strip():
            raise ValueError("a memory with no statement remembers nothing")
        if not scope_ids:
            raise ValueError("an unscoped memory pollutes every context")
        stamp = datetime.now(UTC).isoformat()
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT INTO memory_spine
                   (memory_type, statement, structured_value, scope_ids,
                    valid_from, valid_until, confidence, verification_state,
                    provenance, source_seat, superseded_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    memory_type,
                    statement.strip(),
                    json.dumps(structured_value, ensure_ascii=False, default=str)
                    if structured_value is not None
                    else None,
                    json.dumps(sorted(str(s) for s in scope_ids)),
                    stamp,
                    valid_until,
                    max(0.0, min(1.0, float(confidence))),
                    verification_state,
                    json.dumps([str(p) for p in provenance]),
                    source_seat,
                    stamp,
                ),
            )
            row_id = int(cursor.lastrowid)
            for old_id in supersedes:
                db.execute(
                    "UPDATE memory_spine SET superseded_by = ?"
                    " WHERE id = ? AND superseded_by IS NULL",
                    (row_id, int(old_id)),
                )
        return row_id

    def supersede_scope(
        self,
        scope_ids: list[str] | tuple[str, ...],
        *,
        memory_type: str,
        by: int,
    ) -> int:
        """Close the book on every open record of one type in one exact
        scope — the publish-beats-warnings rule, generalized. Returns
        how many records the correction covered."""
        scoped = json.dumps(sorted(str(s) for s in scope_ids))
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE memory_spine SET superseded_by = ?
                   WHERE memory_type = ? AND scope_ids = ?
                     AND superseded_by IS NULL AND id != ?""",
                (int(by), memory_type, scoped, int(by)),
            )
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ #
    def recall(
        self,
        scope_ids: list[str] | tuple[str, ...],
        query: str = "",
        *,
        kinds: tuple[str, ...] = (),
        limit: int = 3,
    ) -> list[dict]:
        """The standing records for a scope — superseded and expired
        rows excluded by the QUERY, ranked by the shared retrieval
        scorer when a query rides, newest-first otherwise. Every row
        answers where it came from."""
        scoped = json.dumps(sorted(str(s) for s in scope_ids))
        now = datetime.now(UTC).isoformat()
        with self._conn.transaction() as db:
            rows = db.execute(
                """SELECT id, memory_type, statement, structured_value,
                          confidence, verification_state, provenance,
                          source_seat, created_at
                   FROM memory_spine
                   WHERE scope_ids = ? AND superseded_by IS NULL
                     AND verification_state != 'rejected'
                     AND (valid_until IS NULL OR valid_until > ?)
                   ORDER BY id DESC LIMIT 200""",
                (scoped, now),
            ).fetchall()
        records = [
            {
                "memory_id": int(r[0]),
                "memory_type": str(r[1]),
                "statement": str(r[2]),
                "structured_value": json.loads(r[3]) if r[3] else None,
                "confidence": float(r[4]),
                "verification_state": str(r[5]),
                "provenance": json.loads(r[6] or "[]"),
                "source_seat": str(r[7]),
                "created_at": str(r[8]),
            }
            for r in rows
            if not kinds or str(r[1]) in kinds
        ]
        if query:
            records.sort(
                key=lambda m: _score(query, m["statement"]), reverse=True
            )
        return records[: max(0, int(limit))]

    def provenance(self, memory_id: int) -> list[str]:
        """Where one memory came from — empty only for ``proposed``."""
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT provenance FROM memory_spine WHERE id = ?",
                (int(memory_id),),
            ).fetchone()
        return json.loads(row[0]) if row else []
