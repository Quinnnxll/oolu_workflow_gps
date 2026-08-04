"""The observer seat (N7, doctrine 3) — models propose, the owner disposes.

Each desk node carries an OBSERVING agentic model: it reads the desk's
current readings and its non-numeric residue (subjects, member words,
refusal reasons — the things no threshold can type) and, when it finds
an issue, files a PROPOSAL with a fix plan. The laws:

- **Words plus evidence, never hands.** The observer's seat
  (``news.observe``) holds no tools and no drawer scopes; its whole
  output is one typed proposal row. It changes nothing itself.
- **The owner disposes.** A proposal waits OPEN until the owner
  approves or dismisses it at the door — the standing approval path,
  never a silent adoption. An approved proposal is the MANDATE for a
  node replacement; the actual swap is the owner publishing a
  successor desk, which the Paver re-paves and the promotion retires
  (P2). The type system enforces the swap; the proposal only argues
  for it.
- **A dead model observes nothing.** No model, no proposal — degraded
  is honest; a manufactured issue would be a defect.
- **One open proposal per desk.** The observer never grinds the same
  desk with duplicates while the owner has one on the table.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

OBSERVER_VERSION = 1

_OBSERVER_FRAME = """\
You are the observing seat on ONE desk of a member magazine's news
pipeline. You will see the desk's name and its current readings —
numbers the desk already acts on, plus non-numeric residue (subjects,
words, refusal reasons) no threshold can type. Judge whether something
is WRONG with how this desk is deciding: a bias, a blind spot, a
degenerate pattern, a reading that contradicts the residue. Hard rules:
- Evidence only: never invent a number or a member's words.
- If nothing is wrong, reply with exactly: NOTHING
- If something is wrong, reply in EXACTLY this shape, nothing else:
ISSUE: <one line naming the defect>
PLAN: <one line proposing the fix — a node replacement the owner could approve>
"""


def observe_desk(
    desk: str, reading: str, *, model
) -> tuple[str, str] | None:
    """One observation pass: the desk's reading through the seat's model
    under the hard output contract. ``None`` means no issue — a broken
    contract, a dead model, or an honest NOTHING all file nothing."""
    if model is None or not reading.strip():
        return None
    prompt = f"{_OBSERVER_FRAME}\nDesk: {desk}\nReadings:\n{reading}"
    try:
        raw = model.reply([{"role": "user", "content": prompt}])
    except Exception:  # noqa: BLE001 - a dead model observes nothing
        return None
    issue = plan = ""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.upper() == "NOTHING":
            return None
        if stripped.startswith("ISSUE:") and not issue:
            issue = stripped[len("ISSUE:") :].strip()
        elif stripped.startswith("PLAN:") and not plan:
            plan = stripped[len("PLAN:") :].strip()
    if not issue or not plan:
        return None
    return issue, plan


_PROPOSALS_SCHEMA = """CREATE TABLE IF NOT EXISTS press_desk_proposals (
    proposal_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    desk TEXT NOT NULL,
    issue TEXT NOT NULL,
    plan TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    filed_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT NOT NULL DEFAULT ''
)"""


class DeskProposalStore:
    """The proposal book: open → approved / dismissed, owner-decided."""

    def __init__(
        self, conn, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_PROPOSALS_SCHEMA)

    def file(
        self,
        *,
        tenant: str,
        desk: str,
        issue: str,
        plan: str,
        evidence: list[str] | None = None,
    ) -> str | None:
        """File one proposal — or None while the desk already has one
        OPEN (the owner judges one record at a time, never a pile)."""
        with self._conn.transaction() as db:
            row = db.execute(
                """SELECT 1 FROM press_desk_proposals
                   WHERE tenant_id = ? AND desk = ? AND status = 'open'""",
                (tenant, desk),
            ).fetchone()
            if row is not None:
                return None
            proposal_id = uuid4().hex
            db.execute(
                """INSERT INTO press_desk_proposals
                     (proposal_id, tenant_id, desk, issue, plan, evidence,
                      status, filed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'open', ?)""",
                (
                    proposal_id,
                    tenant,
                    desk,
                    issue,
                    plan,
                    json.dumps(list(evidence or [])),
                    self._clock().isoformat(),
                ),
            )
        return proposal_id

    def decide(
        self,
        proposal_id: str,
        *,
        tenant: str,
        approved: bool,
        by: str,
    ) -> dict | None:
        """The owner's decision on one OPEN proposal — consumed once."""
        status = "approved" if approved else "dismissed"
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE press_desk_proposals
                   SET status = ?, decided_at = ?, decided_by = ?
                   WHERE proposal_id = ? AND tenant_id = ?
                     AND status = 'open'""",
                (
                    status,
                    self._clock().isoformat(),
                    by,
                    proposal_id,
                    tenant,
                ),
            )
        if not (getattr(cursor, "rowcount", 0) or 0):
            return None
        return self.get(proposal_id, tenant=tenant)

    def get(self, proposal_id: str, *, tenant: str) -> dict | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT * FROM press_desk_proposals
                   WHERE proposal_id = ? AND tenant_id = ?""",
                (proposal_id, tenant),
            ).fetchone()
        return self._row(row) if row is not None else None

    def list(self, *, tenant: str, status: str | None = None) -> list[dict]:
        where = "tenant_id = ?"
        args: list = [tenant]
        if status is not None:
            where += " AND status = ?"
            args.append(status)
        with self._conn.lock:
            rows = self._conn.db.execute(
                f"""SELECT * FROM press_desk_proposals WHERE {where}
                    ORDER BY filed_at DESC, proposal_id""",
                args,
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row) -> dict:
        try:
            evidence = json.loads(row["evidence"] or "[]")
        except Exception:  # noqa: BLE001 - a bad row renders empty refs
            evidence = []
        return {
            "proposal_id": str(row["proposal_id"]),
            "desk": str(row["desk"]),
            "issue": str(row["issue"]),
            "plan": str(row["plan"]),
            "evidence": evidence,
            "status": str(row["status"]),
            "filed_at": str(row["filed_at"]),
            "decided_at": row["decided_at"],
            "decided_by": str(row["decided_by"] or ""),
        }
