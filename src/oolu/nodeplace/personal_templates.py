"""The starter shelf — P1 of the personal-nodes plan.

Seven personal nodes every new account receives, so day one is never
an empty desk: Calendar, Tasks, Reminders, Automation Trigger, Stock,
Cashflow, and Invoice Scan. The SHELF is standard; the CONTENT is
personal — the drawers start empty and fill with each person's own
events, tasks, goods, and money.

Two convictions, inherited from the org templates:

- **Deterministic functions at birth.** No model writes a starter
  function and no model spend happens at seeding: each script below is
  curated, reviewed, and deterministic — reliability by construction.
  A user who wants more says "revise …" and the standing building
  doors grow it, audited, through the same gates as any node.
- **A starter node is an ordinary node.** Born through the contribute
  door with declared io and plain-word labels (B1), its function in
  ``src/main.py`` (B2), its run io in its drawer (B3), its hand-offs
  offered and cited (B4). Nothing in the runtime knows the word
  "starter" — only the seeding pass does, and its ledger below makes
  that pass happen exactly once per person: a deleted starter is
  respected forever, never re-seeded.

P1 ships each node's ESSENTIAL starting function: file what the run
was given as a structured record. P2 and P3 grow the real
record-keeping (drawer rows, projections, charts) on this foundation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Callable

from pydantic import BaseModel, ConfigDict


class StarterInput(BaseModel):
    """One declared input, asked in the user's own words (B1)."""

    model_config = ConfigDict(frozen=True)

    name: str
    value_type: str = "str"
    label: str
    example: str = ""


class StarterSpec(BaseModel):
    """One shelf node: a name, one responsibility, one function."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    # ONE sentence: what this node answers for on the person's desk.
    responsibility: str
    # The executable sentence — the node's goal, registry summary, and
    # the exact words a P0 schedule fires it by.
    goal: str
    inputs: tuple[StarterInput, ...]


def starter_script(spec: StarterSpec) -> str:
    """The node's essential function: deterministic, self-contained —
    read the bound values, file them as a structured record, emit it.
    No model writes this; the shelf IS the plan. The node grows from
    here ("revise …" rewrites it through the standing doors)."""
    record = {"kind": spec.key, "kept_at": ""}
    for item in spec.inputs:
        record[item.name] = ""
    payload = json.dumps(json.dumps(record, ensure_ascii=False))
    return (
        f'"""{spec.name} — {spec.responsibility}"""\n'
        "import json\n"
        "from datetime import datetime\n"
        "from _oolu_runtime import emit_result\n"
        "\n"
        "# The starter function: keep what this run was given as a\n"
        "# structured record. Deterministic by design — grown later by\n"
        "# revising the node through the standing building doors.\n"
        "try:\n"
        "    with open('bindings.json') as handle:\n"
        "        values = json.load(handle)\n"
        "except (FileNotFoundError, ValueError):\n"
        "    values = {}\n"
        f"RECORD = json.loads({payload})\n"
        "for name in list(RECORD):\n"
        "    if values.get(name):\n"
        "        RECORD[name] = str(values[name])\n"
        "RECORD['kept_at'] = datetime.now().isoformat()\n"
        "emit_result({'record': RECORD})\n"
    )


# --------------------------------------------------------------------------- #
# The shelf: seven nodes, reviewed words, plain asks.                          #
# --------------------------------------------------------------------------- #
STARTER_SHELF: tuple[StarterSpec, ...] = (
    StarterSpec(
        key="calendar",
        name="Calendar",
        responsibility=(
            "Keeps your events and appointments, and answers what's on."
        ),
        goal="keep my calendar of events and appointments",
        inputs=(
            StarterInput(
                name="entry",
                label="What should go on the calendar?",
                example="dentist Tuesday 3pm",
            ),
        ),
    ),
    StarterSpec(
        key="tasks",
        name="Tasks",
        responsibility=(
            "Keeps your task list and answers what is still open."
        ),
        goal="keep my task list and what is still open",
        inputs=(
            StarterInput(
                name="task",
                label="What needs doing?",
                example="send the quote to Alex",
            ),
        ),
    ),
    StarterSpec(
        key="reminders",
        name="Reminders",
        responsibility=(
            "Keeps what to nudge you about, and when."
        ),
        goal="keep my reminders and when to nudge me",
        inputs=(
            StarterInput(
                name="reminder",
                label="What should I remind you about, and when?",
                example="call the bank tomorrow at 9",
            ),
        ),
    ),
    StarterSpec(
        key="trigger",
        name="Automation Trigger",
        responsibility=(
            "Keeps your standing rhythms — what runs daily, weekly, "
            "monthly, yearly."
        ),
        goal="keep my automation rhythms and what they fire",
        inputs=(
            StarterInput(
                name="rhythm",
                label="When should it run, in plain words?",
                example="every day at 9, run my invoice node",
            ),
        ),
    ),
    StarterSpec(
        key="stock",
        name="Stock",
        responsibility=(
            "Keeps your goods in and out, and answers what's on the "
            "shelf."
        ),
        goal="keep my stock of goods in and out",
        inputs=(
            StarterInput(
                name="movement",
                label="What moved in or out, and how many?",
                example="received 40 boxes of paper",
            ),
        ),
    ),
    StarterSpec(
        key="cashflow",
        name="Cashflow",
        responsibility=(
            "Keeps your money in and out, and draws the picture over "
            "time."
        ),
        goal="keep my cashflow of money in and out",
        inputs=(
            StarterInput(
                name="entry",
                label="What money moved, in or out?",
                example="invoice paid, 1200 in, July 3",
            ),
        ),
    ),
    StarterSpec(
        key="invoice_scan",
        name="Invoice Scan",
        responsibility=(
            "Turns a photographed or scanned invoice into rows on a "
            "sheet."
        ),
        goal="turn a scanned invoice into rows on a sheet",
        inputs=(
            StarterInput(
                name="invoice_file",
                label="Which file is the invoice?",
                example="invoice-042.pdf",
            ),
        ),
    ),
)


# --------------------------------------------------------------------------- #
# The seeding ledger: once per person, forever.                                #
# --------------------------------------------------------------------------- #
_LEDGER_SCHEMA = """CREATE TABLE IF NOT EXISTS starter_shelf (
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    seeded_at TEXT NOT NULL,
    nodes TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (tenant_id, principal)
)"""


class StarterLedger:
    """One row per person: the seeding happened. The INSERT-OR-IGNORE
    claim makes the pass exactly-once even when two sign-ins race —
    and because the row never leaves, a deleted starter node is never
    resurrected by a later sign-in."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None):
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_LEDGER_SCHEMA)

    def claim(self, tenant: str, principal: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO starter_shelf
                       (tenant_id, principal, seeded_at, nodes)
                   VALUES (?, ?, ?, '[]')""",
                (tenant, principal, self._clock().isoformat()),
            )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def seeded(self, tenant: str, principal: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT 1 FROM starter_shelf"
                " WHERE tenant_id = ? AND principal = ?",
                (tenant, principal),
            ).fetchone()
        return row is not None

    def record_nodes(
        self, tenant: str, principal: str, node_ids: list[str]
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE starter_shelf SET nodes = ?
                   WHERE tenant_id = ? AND principal = ?""",
                (json.dumps(list(node_ids)), tenant, principal),
            )
