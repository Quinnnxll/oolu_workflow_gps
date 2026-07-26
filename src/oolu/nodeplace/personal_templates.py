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


class StarterOutput(BaseModel):
    """One declared output port — what every run's payload must cover."""

    model_config = ConfigDict(frozen=True)

    name: str
    value_type: str = "json"


_RECORD_OUTPUT = (StarterOutput(name="record", value_type="json"),)


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
    outputs: tuple[StarterOutput, ...] = _RECORD_OUTPUT


def _filing_script(spec: StarterSpec) -> str:
    """The P1 essential function: file what the run was given as a
    structured record — the honest first step the later phases grow."""
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


# The record discipline's shared prelude (P2): read the bound values
# and the node's OWN book (./records.json — the drawer's standing rows,
# staged by the runner), plus the deterministic ears for the day and
# clock time plain words name. No model, no network, no guessing.
_BOOK_PRELUDE = r'''import json
import re
from datetime import date, timedelta
from _oolu_runtime import emit_result


def _load(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return None


WEEKDAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]


def day_of(text, today):
    """The day plain words name, deterministically — or '' when the
    words name none (an undated row is honest, never guessed)."""
    low = " " + str(text).lower() + " "
    if " today " in low or " tonight " in low:
        return today.isoformat()
    if " tomorrow " in low:
        return (today + timedelta(days=1)).isoformat()
    found = re.search(r"(20\d\d-\d\d-\d\d)", str(text))
    if found:
        return found.group(1)
    for index, name in enumerate(WEEKDAY_NAMES):
        if name in low:
            ahead = (index - today.weekday()) % 7
            return (today + timedelta(days=ahead)).isoformat()
    return ""


def time_of(text):
    """The clock time plain words name ("at 3pm", "at 9:30"), or ''."""
    found = re.search(
        r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", str(text).lower()
    )
    if not found:
        return ""
    hour = int(found.group(1))
    minute = int(found.group(2) or 0)
    meridiem = found.group(3) or ""
    if meridiem:
        if not 1 <= hour <= 12:
            return ""
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        return ""
    return "%02d:%02d" % (hour, minute)


values = _load("bindings.json") or {}
rows = _load("records.json") or []
today = date.today()
answer = ""
'''

_CALENDAR_BODY = '''
entry = str(values.get("entry") or "").strip()
if entry:
    rows.append({"event": entry, "on": day_of(entry, today)})
    answer = "Noted: " + entry
week_end = (today + timedelta(days=7)).isoformat()
events_today = [
    str(r.get("event")) for r in rows if r.get("on") == today.isoformat()
]
events_week = [
    str(r.get("event"))
    for r in rows
    if r.get("on") and today.isoformat() <= str(r.get("on")) <= week_end
]
if not answer:
    if events_today:
        answer = "Today: " + "; ".join(events_today)
    else:
        answer = "Nothing on the calendar today."
emit_result({
    "answer": answer,
    "records": rows,
    "events_today": events_today,
    "events_week": events_week,
})
'''

_TASKS_BODY = '''
task = str(values.get("task") or "").strip()
finished = str(values.get("done") or "").strip()
offer = {}
if task:
    due = day_of(task, today)
    rows.append({"task": task, "due": due, "done": False})
    answer = "Added: " + task
    if due:
        offer = {"text": task, "day": due, "time": time_of(task) or "09:00"}
elif finished:
    answer = "No open task matches: " + finished
    for row in rows:
        name = str(row.get("task") or "")
        if not row.get("done") and finished.lower() in name.lower():
            row["done"] = True
            answer = "Done: " + name
            break
open_tasks = [str(r.get("task")) for r in rows if not r.get("done")]
if not answer:
    if open_tasks:
        answer = "Open: " + "; ".join(open_tasks)
    else:
        answer = "Nothing open."
emit_result({
    "answer": answer,
    "records": rows,
    "open_tasks": open_tasks,
    "reminder_offer": offer,
})
'''

_REMINDERS_BODY = '''
asked = str(values.get("reminder") or "").strip()
reminder = {}
if asked:
    day = day_of(asked, today) or (today + timedelta(days=1)).isoformat()
    clock = time_of(asked) or "09:00"
    rows.append({"reminder": asked, "day": day, "time": clock})
    reminder = {"text": asked, "day": day, "time": clock}
    answer = "I'll nudge you: " + asked + " (" + day + " " + clock + ")."
standing = [
    str(r.get("reminder")) + " (" + str(r.get("day")) + " "
    + str(r.get("time")) + ")"
    for r in rows
]
if not answer:
    if standing:
        answer = "Standing: " + "; ".join(standing)
    else:
        answer = "No standing reminders."
emit_result({"answer": answer, "records": rows, "reminder": reminder})
'''

_RECORD_BODIES = {
    "calendar": _CALENDAR_BODY,
    "tasks": _TASKS_BODY,
    "reminders": _REMINDERS_BODY,
}


def starter_script(spec: StarterSpec) -> str:
    """The node's function, deterministic either way: the records trio
    (P2) keep their own book — read it, change it, emit it with the
    projections other nodes consume; every other starter files its
    structured record (P1) until its phase grows it. No model writes
    any of these; the shelf IS the plan."""
    body = _RECORD_BODIES.get(spec.key)
    if body is None:
        return _filing_script(spec)
    return (
        f'"""{spec.name} — {spec.responsibility}"""\n'
        + _BOOK_PRELUDE
        + body
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
        outputs=(
            StarterOutput(name="answer", value_type="str"),
            StarterOutput(name="records"),
            StarterOutput(name="events_today"),
            StarterOutput(name="events_week"),
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
            StarterInput(
                name="done",
                label="Which task is finished?",
                example="the quote",
            ),
        ),
        outputs=(
            StarterOutput(name="answer", value_type="str"),
            StarterOutput(name="records"),
            StarterOutput(name="open_tasks"),
            StarterOutput(name="reminder_offer"),
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
        outputs=(
            StarterOutput(name="answer", value_type="str"),
            StarterOutput(name="records"),
            StarterOutput(name="reminder"),
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
