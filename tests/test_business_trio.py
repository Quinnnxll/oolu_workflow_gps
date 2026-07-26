"""P3: the business trio — stock, cashflow, invoice scan.

Exit gate: stock levels equal the movement ledger's sum at every read
and a level under its floor stands as a reminder offer until answered;
the cashflow chart file regenerates from the ledger alone at all four
time scales; a scanned invoice's rows land on the sheet only after the
strict value check (an unreadable total refuses in words, never a
guess), and the parsed entry OFFERS itself to Cashflow through the
standing B4 hand-off — bound on a yes, cited with the run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta

from test_chat_assistant import _FakeModel
from test_growth_trigger import _chat, _rig
from test_http_gateway import _req
from test_records_trio import _SHIM, SPECS

from oolu.cache import LocalScriptCache
from oolu.nodeplace.personal_templates import starter_script
from oolu.reminders import ReminderStore
from oolu.runtime import NodeScriptRunner, StubBackend
from oolu.runtime.backend import make_success
from oolu.values import ValueStore


def _execute(tmp_path, key, bindings=None, records=None, attachments=None):
    workdir = tmp_path / f"{key}-{len(list(tmp_path.iterdir()))}"
    workdir.mkdir()
    (workdir / "main.py").write_text(starter_script(SPECS[key]))
    (workdir / "_oolu_runtime.py").write_text(_SHIM)
    if bindings is not None:
        (workdir / "bindings.json").write_text(json.dumps(bindings))
    if records is not None:
        (workdir / "records.json").write_text(json.dumps(records))
    for name, content in (attachments or {}).items():
        folder = workdir / "attachments"
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(content)
    subprocess.run(
        [sys.executable, "main.py"],
        cwd=workdir,
        check=True,
        timeout=60,
        capture_output=True,
    )
    return json.loads((workdir / "result.json").read_text())


# --------------------------------------------------------------------------- #
# Stock: levels ARE the ledger's sum; a floor breach stands as an offer.       #
# --------------------------------------------------------------------------- #
def test_stock_levels_equal_the_ledger_sum_at_every_read(tmp_path):
    came = _execute(
        tmp_path, "stock", {"movement": "received 40 boxes of paper"}
    )
    assert came["levels"] == {"boxes of paper": 40}
    went = _execute(
        tmp_path, "stock",
        {"movement": "sold 3 boxes of paper"},
        records=came["records"],
    )
    assert went["levels"] == {"boxes of paper": 37}
    # A plain read recomputes the projection from the ledger alone.
    read = _execute(tmp_path, "stock", {}, records=went["records"])
    assert read["levels"] == {"boxes of paper": 37}
    assert read["low_stock"] == []
    assert read["reminder_offer"] == {}
    # An unreadable movement records NOTHING and says how to say it.
    garbled = _execute(
        tmp_path, "stock",
        {"movement": "paper situation is confusing"},
        records=went["records"],
    )
    assert garbled["levels"] == {"boxes of paper": 37}
    assert "Nothing was recorded" in garbled["answer"]


def test_a_level_under_its_floor_offers_a_restock_reminder(tmp_path):
    came = _execute(
        tmp_path, "stock", {"movement": "received 8 boxes of paper"}
    )
    floored = _execute(
        tmp_path, "stock",
        {"movement": "keep at least 10 boxes of paper"},
        records=came["records"],
    )
    (warning,) = floored["low_stock"]
    assert "boxes of paper — 8 left, floor 10" == warning
    offer = floored["reminder_offer"]
    assert "restock" in offer["text"]
    assert offer["day"] == (date.today() + timedelta(days=1)).isoformat()


# --------------------------------------------------------------------------- #
# Cashflow: the ledger and its chart, redrawn from the book alone.             #
# --------------------------------------------------------------------------- #
def test_cashflow_reads_amount_direction_and_day(tmp_path):
    paid = _execute(
        tmp_path, "cashflow",
        {"entry": "invoice paid, 1200 in, 2026-07-01"},
    )
    (row,) = paid["records"]
    assert row["amount"] == 1200.0 and row["on"] == "2026-07-01"
    spent = _execute(
        tmp_path, "cashflow",
        {"entry": "bought a printer 300.50 out today"},
        records=paid["records"],
    )
    assert spent["records"][-1]["amount"] == -300.5
    summary = spent["cashflow_summary"]
    assert summary == {"in": 1200.0, "out": 300.5, "net": 899.5}


def test_the_chart_regenerates_from_the_ledger_at_all_four_scales(tmp_path):
    paid = _execute(
        tmp_path, "cashflow",
        {"entry": "invoice paid, 1200 in, 2026-07-01"},
    )
    # A plain read, book staged, NO new entry: the chart is a
    # projection — it redraws from the ledger alone.
    read = _execute(tmp_path, "cashflow", {}, records=paid["records"])
    chart = read["files"]["cashflow.html"]
    assert "<svg" in chart
    for scale in ("day", "week", "month", "year"):
        assert f"Per {scale}" in chart
    assert "2563eb" in chart  # money in, the calm hue


# --------------------------------------------------------------------------- #
# Invoice scan: strict values, honest refusals, the sheet.                     #
# --------------------------------------------------------------------------- #
INVOICE_TEXT = (
    "ACME Supplies Ltd\n"
    "Invoice 042 — 2026-07-10\n"
    "3 boxes of paper ... 34.50\n"
    "Total: 1,234.50\n"
)


def test_a_readable_invoice_lands_on_the_sheet_and_offers_its_entry(tmp_path):
    read = _execute(
        tmp_path, "invoice_scan",
        {"invoice_file": "inv-042.txt"},
        attachments={"inv-042.txt": INVOICE_TEXT},
    )
    (row,) = read["records"]
    assert row["amount"] == 1234.5
    assert row["vendor"] == "ACME Supplies Ltd"
    assert row["date"] == "2026-07-10"
    sheet = read["files"]["invoices.csv"]
    assert "file,vendor,date,amount" in sheet
    assert "inv-042.txt,ACME Supplies Ltd,2026-07-10,1234.50" in sheet
    assert "1234.50 out" in read["entry"]


def test_an_unreadable_total_refuses_in_words_and_lands_nothing(tmp_path):
    read = _execute(
        tmp_path, "invoice_scan",
        {"invoice_file": "blur.txt"},
        attachments={"blur.txt": "a smudged photograph of a receipt"},
    )
    assert read["records"] == []
    assert "nothing was guessed" in read["answer"].lower()
    assert "entry" not in read
    missing = _execute(
        tmp_path, "invoice_scan", {"invoice_file": "gone.txt"}
    )
    assert "can't find" in missing["answer"]


# --------------------------------------------------------------------------- #
# The circle: attachment staged, sheet landed, entry handed to cashflow.       #
# --------------------------------------------------------------------------- #
def _business_rig(tmp_path, payloads):
    holder: dict = {}
    backend = StubBackend([make_success(p) for p in payloads])
    runner = NodeScriptRunner(
        backend,
        LocalScriptCache(":memory:"),
        value_resolver=lambda bindings, tenant: holder["store"].resolve_bindings(
            bindings, tenant=tenant
        ),
    )
    app, conn, ident, desk, _exec = _rig(tmp_path, script_exec=runner)
    store = ValueStore(conn)
    holder["store"] = store
    app._values = store
    app._reminders = ReminderStore(conn)
    app._seed_starter_shelf("t1", "user-1")
    return app, conn, ident, backend


def _node_for(app, goal):
    for node in app._nodeplace.all_nodes():
        version = app._nodeplace.latest_version(node.node_id)
        from oolu.skills.models import ReusableSkill

        skill = ReusableSkill.model_validate_json(
            version.sanitized_skill_json
        )
        if skill.description == goal:
            return node
    raise AssertionError(f"no node for goal {goal!r}")


ENTRY = "invoice inv-042.txt from ACME Supplies Ltd: 1234.50 out, 2026-07-10"
INVOICE_PAYLOAD = {
    "answer": "Read inv-042.txt. Added to the sheet.",
    "records": [
        {"file": "inv-042.txt", "vendor": "ACME Supplies Ltd",
         "date": "2026-07-10", "amount": 1234.5}
    ],
    "files": {"invoices.csv": "file,vendor,date,amount\n"},
    "entry": ENTRY,
}
CASH_PAYLOAD = {
    "answer": "Recorded: " + ENTRY,
    "records": [{"entry": ENTRY, "amount": -1234.5, "on": "2026-07-10"}],
    "cashflow_summary": {"in": 0.0, "out": 1234.5, "net": -1234.5},
    "files": {"cashflow.html": "<div><svg></svg></div>"},
}


def test_the_invoice_becomes_a_cashflow_entry_on_a_yes(tmp_path):
    app, conn, ident, backend = _business_rig(
        tmp_path, [INVOICE_PAYLOAD, CASH_PAYLOAD]
    )
    try:
        invoice = _node_for(app, SPECS["invoice_scan"].goal)
        cashflow = _node_for(app, SPECS["cashflow"].goal)
        # The forwarded invoice sits in the node's message drawer.
        from oolu.durable.files import UserFile

        app._files.save(
            UserFile(
                tenant_id="t1",
                node_id=invoice.node_id,
                folder="messages",
                name="inv-042.txt",
                media_type="text/plain",
                content=INVOICE_TEXT,
            )
        )
        ran = app.handle(
            _req(
                "POST",
                "/v1/runs",
                token=ident.token("user-1", "t1"),
                body={
                    "intent": SPECS["invoice_scan"].goal,
                    "values": {"invoice_file": "inv-042.txt"},
                },
            )
        )
        assert ran.status == 202, ran.body
        # The named document was staged into the sandbox…
        staged = backend.requests[-1].files
        assert staged["attachments/inv-042.txt"] == INVOICE_TEXT
        # …the sheet landed under records…
        names = {
            (f.folder, f.name)
            for f in app._files.list(tenant="t1", node_id=invoice.node_id)
        }
        assert ("records", "invoices.csv") in names
        # …and the parsed entry stands on the port index.
        assert app._values.producers_of("t1", "entry")

        # Running Cashflow alone now OFFERS the invoice's entry (B4).
        goal = SPECS["cashflow"].goal
        model = _FakeModel(['{"say": "On it!", "task": "' + goal + '"}'])
        app._tenant_model = lambda tenant: model
        asked = _chat(app, ident, "please " + goal)
        assert asked.status == 200, asked.body
        assert "Use that for this run?" in asked.body["reply"]
        assert asked.body["run_id"] is None

        agreed = _chat(app, ident, "yes")
        run_id = agreed.body["run_id"]
        assert run_id, agreed.body
        # The exact entry words reached the sandbox through the binder…
        bound = json.loads(backend.requests[-1].files["bindings.json"])
        assert bound == {"entry": ENTRY}
        # …and the movement is run-cited on the graph.
        graph = app._temporal_graph()
        (edge,) = graph.neighbors(
            cashflow.node_id, edge_types=("handoff",)
        )
        assert edge["source_id"] == invoice.node_id
        assert edge["attributes"]["run_id"] == run_id
        assert edge["attributes"]["port"] == "entry"
    finally:
        conn.close()


def test_low_stock_offers_a_restock_reminder_through_chat(tmp_path):
    soon = (date.today() + timedelta(days=1)).isoformat()
    payload = {
        "answer": "In: 8 boxes of paper. LOW: boxes of paper.",
        "records": [{"kind": "move", "item": "boxes of paper", "change": 8}],
        "levels": {"boxes of paper": 8},
        "low_stock": ["boxes of paper — 8 left, floor 10"],
        "reminder_offer": {
            "text": "restock: boxes of paper — 8 left, floor 10",
            "day": soon,
            "time": "09:00",
        },
    }
    app, conn, ident, backend = _business_rig(tmp_path, [payload])
    try:
        goal = SPECS["stock"].goal
        model = _FakeModel(['{"say": "On it!", "task": "' + goal + '"}'])
        app._tenant_model = lambda tenant: model
        ran = _chat(app, ident, "please " + goal)
        assert "want a reminder" in ran.body["reply"]
        agreed = _chat(app, ident, "yes")
        assert "Reminder set" in agreed.body["reply"]
        (row,) = app._reminders.upcoming(tenant="t1", principal="user-1")
        assert "restock" in row.text
    finally:
        conn.close()
