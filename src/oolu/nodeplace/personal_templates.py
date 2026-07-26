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

_STOCK_BODY = r'''
movement = str(values.get("movement") or "").strip()
if movement:
    low = movement.lower()
    floor_found = re.search(
        r"(?:keep\s+)?at\s+least\s+(\d+)\s+(?:of\s+)?(.+)", low
    )
    came = re.search(
        r"(?:received|bought|restocked|added|got)\s+(\d+)\s+(?:of\s+)?(.+)",
        low,
    )
    went = re.search(
        r"(?:sold|used|shipped|sent|lost)\s+(\d+)\s+(?:of\s+)?(.+)", low
    )
    if floor_found:
        item = floor_found.group(2).strip().rstrip(".")
        rows.append({"kind": "floor", "item": item,
                     "floor": int(floor_found.group(1))})
        answer = "Noted — keep at least %s %s." % (
            floor_found.group(1), item)
    elif came:
        item = came.group(2).strip().rstrip(".")
        rows.append({"kind": "move", "item": item,
                     "change": int(came.group(1)), "words": movement})
        answer = "In: %s %s." % (came.group(1), item)
    elif went:
        item = went.group(2).strip().rstrip(".")
        rows.append({"kind": "move", "item": item,
                     "change": -int(went.group(1)), "words": movement})
        answer = "Out: %s %s." % (went.group(1), item)
    else:
        answer = (
            "I couldn't read that movement — say it like \"received 40 "
            "boxes of paper\", \"sold 3 boxes of paper\", or \"keep at "
            "least 10 boxes of paper\". Nothing was recorded."
        )
levels = {}
for row in rows:
    if row.get("kind") == "move":
        item = str(row.get("item"))
        levels[item] = levels.get(item, 0) + int(row.get("change") or 0)
floors = {}
for row in rows:
    if row.get("kind") == "floor":
        floors[str(row.get("item"))] = int(row.get("floor") or 0)
low_stock = [
    "%s — %d left, floor %d" % (item, levels.get(item, 0), floor)
    for item, floor in sorted(floors.items())
    if levels.get(item, 0) < floor
]
offer = {}
if low_stock:
    offer = {
        "text": "restock: " + "; ".join(low_stock),
        "day": (today + timedelta(days=1)).isoformat(),
        "time": "09:00",
    }
if not answer:
    if levels:
        answer = "On the shelf: " + "; ".join(
            "%s: %d" % (item, count) for item, count in sorted(levels.items())
        )
    else:
        answer = "The shelf is empty — nothing recorded yet."
if low_stock:
    answer = answer + " LOW: " + "; ".join(low_stock)
emit_result({
    "answer": answer,
    "records": rows,
    "levels": levels,
    "low_stock": low_stock,
    "reminder_offer": offer,
})
'''

_CASHFLOW_BODY = r'''
entry = str(values.get("entry") or "").strip()
if entry:
    low = " " + entry.lower() + " "
    found = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", entry)
    if not found:
        answer = (
            "I couldn't read an amount in that — say it like "
            "\"invoice paid, 1200 in, tomorrow\". Nothing was recorded."
        )
    else:
        amount = float(found.group(1).replace(",", ""))
        outward = (
            " out " in low
            or any(w in low for w in (" spent ", " bought ", " paid for "))
        )
        if outward:
            amount = -amount
        day = day_of(entry, today) or today.isoformat()
        rows.append({"entry": entry, "amount": amount, "on": day})
        answer = "Recorded: %s (%s%.2f on %s)." % (
            entry, "-" if amount < 0 else "+", abs(amount), day
        )
        if not outward and " in " not in low:
            answer += " I read that as money IN - say \"out\" to record spending."

def _bucket(day, scale):
    if scale == "day":
        return day
    if scale == "month":
        return day[:7]
    if scale == "year":
        return day[:4]
    import datetime as _dt
    parsed = _dt.date.fromisoformat(day)
    year, week, _ = parsed.isocalendar()
    return "%04d-W%02d" % (year, week)

def _chart(rows):
    took = {"in": 0.0, "out": 0.0}
    for row in rows:
        amount = float(row.get("amount") or 0)
        took["in" if amount > 0 else "out"] += abs(amount)
    net = took["in"] - took["out"]
    keep = {"day": 14, "week": 12, "month": 12, "year": 5}
    parts = [
        "<h1>Cashflow</h1>",
        "<p>In %.2f - Out %.2f - Net %+.2f</p>" % (
            took["in"], took["out"], net),
    ]
    for scale in ("day", "week", "month", "year"):
        buckets = {}
        for row in rows:
            day = str(row.get("on") or "")
            if len(day) == 10:
                key = _bucket(day, scale)
                buckets[key] = buckets.get(key, 0.0) + float(
                    row.get("amount") or 0)
        named = sorted(buckets.items())[-keep[scale]:]
        parts.append("<h2>Per %s</h2>" % scale)
        if not named:
            parts.append("<p>No entries yet.</p>")
            continue
        top = max(abs(v) for _, v in named) or 1.0
        width = 44
        svg = ["<svg width='%d' height='150' role='img'>"
               % (len(named) * width + 10)]
        svg.append(
            "<line x1='0' y1='75' x2='%d' y2='75' stroke='#9ca3af'/>"
            % (len(named) * width + 10)
        )
        for index, (key, value) in enumerate(named):
            bar = int(abs(value) / top * 60)
            x = index * width + 8
            if value >= 0:
                y, color = 75 - bar, "#2563eb"
            else:
                y, color = 75, "#d97706"
            svg.append(
                "<rect x='%d' y='%d' width='28' height='%d' rx='3' "
                "fill='%s'/>" % (x, y, max(bar, 2), color)
            )
            svg.append(
                "<text x='%d' y='148' font-size='9' fill='#6b7280'>%s"
                "</text>" % (x, key[-5:])
            )
        svg.append("</svg>")
        parts.append("".join(svg))
    return (
        "<div style='font-family: sans-serif; color: #1f2937'>"
        + "".join(parts) + "</div>"
    )

took_in = sum(float(r.get("amount") or 0) for r in rows
              if float(r.get("amount") or 0) > 0)
took_out = sum(-float(r.get("amount") or 0) for r in rows
               if float(r.get("amount") or 0) < 0)
summary = {"in": took_in, "out": took_out, "net": took_in - took_out}
if not answer:
    answer = "In %.2f - out %.2f - net %+.2f over %d entries." % (
        took_in, took_out, summary["net"], len(rows))
emit_result({
    "answer": answer,
    "records": rows,
    "cashflow_summary": summary,
    "files": {"cashflow.html": _chart(rows)},
})
'''

_INVOICE_BODY = r'''
name = str(values.get("invoice_file") or "").strip()
text = ""
if name:
    try:
        with open("attachments/" + name.replace("/", "_")) as handle:
            text = handle.read()
    except OSError:
        text = ""
entry = ""
if not name:
    answer = (
        "Which file is the invoice? Forward it to this node (it lands "
        "in the drawer's messages) and give me its name."
    )
elif not text:
    answer = (
        "I can't find \"" + name + "\" in this node's drawer - forward "
        "the invoice here first, then ask again."
    )
else:
    labeled = re.search(
        r"total[^0-9-]{0,24}(\d[\d,]*(?:\.\d{1,2})?)", text, re.I
    )
    numbers = re.findall(r"(\d[\d,]*\.\d{2})", text)
    raw = labeled.group(1) if labeled else (numbers[-1] if numbers else "")
    day_found = re.search(r"(20\d\d-\d\d-\d\d)", text)
    day = day_found.group(1) if day_found else today.isoformat()
    vendor = ""
    for line in text.splitlines():
        if line.strip():
            vendor = line.strip().replace(",", " ")[:60]
            break
    try:
        amount = float(raw.replace(",", ""))
    except ValueError:
        amount = None
    if amount is None:
        answer = (
            "I couldn't read a total from \"" + name + "\" - no amount "
            "was recorded, nothing was guessed. A clearer copy will "
            "read; nothing lands without a checked number."
        )
    else:
        rows.append({"file": name, "vendor": vendor, "date": day,
                     "amount": amount})
        entry = "invoice %s from %s: %.2f out, %s" % (
            name, vendor or "(no vendor line)", amount, day)
        answer = "Read %s: %s - %.2f on %s. Added to the sheet." % (
            name, vendor or "(no vendor line)", amount, day)
sheet = ["file,vendor,date,amount"]
for row in rows:
    sheet.append("%s,%s,%s,%.2f" % (
        str(row.get("file", "")).replace(",", " "),
        str(row.get("vendor", "")).replace(",", " "),
        row.get("date", ""),
        float(row.get("amount") or 0),
    ))
payload = {
    "answer": answer,
    "records": rows,
    "files": {"invoices.csv": "\n".join(sheet) + "\n"},
}
if entry:
    payload["entry"] = entry
emit_result(payload)
'''

# The morning pulse (P4): a daily schedule seeded DISABLED with the
# shelf. Its goal is this exact sentence — the pulse tick recognizes it
# and fires the owner's Calendar and Tasks as ordinary runs, landing
# the combined answer through the reminder channel.
MORNING_PULSE_GOAL = "the morning pulse — today's calendar and open tasks"

_TRIGGER_BODY = r'''
rhythm = str(values.get("rhythm") or "").strip().rstrip(".!")
MONTHS_OF = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
TIME_PART = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
GOAL_PART = r"\s*[,:]?\s+(?:run\s+|do\s+)?(.+?)$"


def _minute(hour_text, minute_text, meridiem):
    hour = int(hour_text)
    minute = int(minute_text or 0)
    meridiem = (meridiem or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


parsed = None
if rhythm:
    daily = re.match(
        r"^every\s+day\s+at\s+" + TIME_PART + GOAL_PART, rhythm, re.I)
    weekly = re.match(
        r"^every\s+(monday|tuesday|wednesday|thursday|friday|saturday"
        r"|sunday)\s+at\s+" + TIME_PART + GOAL_PART, rhythm, re.I)
    monthly = re.match(
        r"^every\s+month\s+on\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?"
        r"\s+at\s+" + TIME_PART + GOAL_PART, rhythm, re.I)
    yearly = re.match(
        r"^every\s+year\s+on\s+(january|february|march|april|may|june"
        r"|july|august|september|october|november|december)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?\s+at\s+" + TIME_PART + GOAL_PART,
        rhythm, re.I)
    if daily:
        minute = _minute(daily.group(1), daily.group(2), daily.group(3))
        if minute is not None:
            parsed = {"cadence": "daily", "at_minute": minute,
                      "goal": daily.group(4).strip()}
    elif weekly:
        minute = _minute(weekly.group(2), weekly.group(3), weekly.group(4))
        if minute is not None:
            parsed = {
                "cadence": "weekly",
                "weekday": WEEKDAY_NAMES.index(weekly.group(1).lower()),
                "at_minute": minute, "goal": weekly.group(5).strip(),
            }
    elif monthly:
        minute = _minute(monthly.group(2), monthly.group(3),
                         monthly.group(4))
        if minute is not None:
            parsed = {
                "cadence": "monthly",
                "day_of_month": int(monthly.group(1)),
                "at_minute": minute, "goal": monthly.group(5).strip(),
            }
    elif yearly:
        minute = _minute(yearly.group(3), yearly.group(4), yearly.group(5))
        if minute is not None:
            parsed = {
                "cadence": "yearly",
                "month": MONTHS_OF[yearly.group(1).lower()],
                "day": int(yearly.group(2)),
                "at_minute": minute, "goal": yearly.group(6).strip(),
            }
if rhythm and parsed:
    rows.append({"rhythm": rhythm})
    schedule = dict(parsed)
    schedule["words"] = rhythm
    answer = (
        "Standing rhythm set: " + rhythm + ". Each firing is an "
        "ordinary run of that goal - say \"schedules\" for the "
        "standing list, \"cancel schedule <id>\" to stop one."
    )
elif rhythm:
    answer = (
        "I couldn't read that rhythm - say it like \"every day at 9, "
        "run my invoice node\" or \"every monday at 7:30pm, send the "
        "weekly report\". Nothing was set."
    )
else:
    asked = [str(r.get("rhythm")) for r in rows if r.get("rhythm")]
    if asked:
        answer = (
            "Rhythms asked through this node: " + "; ".join(asked)
            + ". The standing list answers to \"schedules\"."
        )
    else:
        answer = (
            "No rhythms yet - say \"every day at 9, run ...\" and "
            "I'll keep the beat."
        )
import datetime as _dt
now_stamp = _dt.datetime.now()
payload = {
    "answer": answer,
    "records": rows,
    "fired_at": now_stamp.isoformat(),
    "occasion": WEEKDAY_NAMES[now_stamp.weekday()],
}
if rhythm and parsed:
    payload["schedule"] = schedule
emit_result(payload)
'''

_RECORD_BODIES = {
    "calendar": _CALENDAR_BODY,
    "tasks": _TASKS_BODY,
    "reminders": _REMINDERS_BODY,
    "stock": _STOCK_BODY,
    "cashflow": _CASHFLOW_BODY,
    "invoice_scan": _INVOICE_BODY,
    "trigger": _TRIGGER_BODY,
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
        outputs=(
            StarterOutput(name="answer", value_type="str"),
            StarterOutput(name="records"),
            StarterOutput(name="fired_at", value_type="str"),
            StarterOutput(name="occasion", value_type="str"),
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
        outputs=(
            StarterOutput(name="answer", value_type="str"),
            StarterOutput(name="records"),
            StarterOutput(name="levels"),
            StarterOutput(name="low_stock"),
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
        outputs=(
            StarterOutput(name="answer", value_type="str"),
            StarterOutput(name="records"),
            StarterOutput(name="cashflow_summary"),
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
                example="invoice-042.txt",
            ),
        ),
        outputs=(
            StarterOutput(name="answer", value_type="str"),
            StarterOutput(name="records"),
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
