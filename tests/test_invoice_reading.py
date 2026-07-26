"""The reading seat: invoices the deterministic parse cannot read.

Exit gate (P3's model door, closed): a run whose deterministic parse
found no total EMITS the ask; the gateway offers the text to the
seated author model — metered, once per run — STRICTLY CHECKS the
extraction (a total that does not parse as a positive number is
refused: never a guessed number), and the checked values come back as
bindings on ONE ordinary re-run of the same function, so the row still
lands through a run. No model configured: the worded refusal stands,
audited. The seat saying UNREADABLE lands nothing.
"""

from __future__ import annotations

import json

from test_business_trio import _business_rig, _node_for
from test_business_trio import _execute as _run_script
from test_http_gateway import _req
from test_node_interact import FakeAuthor
from test_records_trio import SPECS

from oolu.durable.files import UserFile

BLUR = "a smudged photograph of a receipt from somewhere"
READING = '{"vendor": "ACME Supplies Ltd", "date": "2026-07-10", "total": "1,234.50"}'

REFUSAL_PAYLOAD = {
    "answer": "I couldn't read a total from \"blur.txt\" myself - if a "
    "reading seat is configured it will take a look.",
    "records": [],
    "files": {"invoices.csv": "file,vendor,date,amount\n"},
    "needs_reading": {"file": "blur.txt"},
}
LANDED_PAYLOAD = {
    "answer": "Read blur.txt: ACME Supplies Ltd - 1234.50 on 2026-07-10. "
    "Added to the sheet. (Read by the reading seat - say the word if a "
    "number is wrong.)",
    "records": [
        {"file": "blur.txt", "vendor": "ACME Supplies Ltd",
         "date": "2026-07-10", "amount": 1234.5}
    ],
    "files": {"invoices.csv": "file,vendor,date,amount\n"},
    "entry": "invoice blur.txt from ACME Supplies Ltd: 1234.50 out, "
    "2026-07-10",
}


# --------------------------------------------------------------------------- #
# The function: the seat's values are checked like any other binding.          #
# --------------------------------------------------------------------------- #
def test_the_script_asks_lands_and_refuses_honestly(tmp_path):
    # Unreadable, no extraction offered: the run EMITS the ask.
    asked = _run_script(
        tmp_path, "invoice_scan",
        {"invoice_file": "blur.txt"},
        attachments={"blur.txt": BLUR},
    )
    assert asked["needs_reading"] == {"file": "blur.txt"}
    assert asked["records"] == []
    assert "nothing is guessed" in asked["answer"].lower()

    # The seat's extraction arrives as bindings: the row lands, named.
    landed = _run_script(
        tmp_path, "invoice_scan",
        {
            "invoice_file": "blur.txt",
            "extracted_total": "1,234.50",
            "extracted_vendor": "ACME Supplies Ltd",
            "extracted_date": "2026-07-10",
        },
        attachments={"blur.txt": BLUR},
    )
    (row,) = landed["records"]
    assert row["amount"] == 1234.5
    assert row["vendor"] == "ACME Supplies Ltd"
    assert row["date"] == "2026-07-10"
    assert "reading seat" in landed["answer"].lower()
    assert "1234.50 out" in landed["entry"]
    assert "needs_reading" not in landed

    # A seat total that does not check as a number lands NOTHING — and
    # never re-asks (the extraction was already offered).
    refused = _run_script(
        tmp_path, "invoice_scan",
        {"invoice_file": "blur.txt", "extracted_total": "about forty"},
        attachments={"blur.txt": BLUR},
    )
    assert refused["records"] == []
    assert "nothing was guessed" in refused["answer"].lower()
    assert "needs_reading" not in refused


# --------------------------------------------------------------------------- #
# The circle: ask → seat → checked bindings → one ordinary re-run.             #
# --------------------------------------------------------------------------- #
def _blur_in_drawer(app, node_id):
    app._files.save(
        UserFile(
            tenant_id="t1",
            node_id=node_id,
            folder="messages",
            name="blur.txt",
            media_type="text/plain",
            content=BLUR,
        )
    )


def _scan(app, ident):
    return app.handle(
        _req(
            "POST",
            "/v1/runs",
            token=ident.token("user-1", "t1"),
            body={
                "intent": SPECS["invoice_scan"].goal,
                "values": {"invoice_file": "blur.txt"},
            },
        )
    )


def test_the_seat_reads_and_the_row_lands_through_a_rerun(tmp_path):
    app, conn, ident, backend = _business_rig(
        tmp_path, [REFUSAL_PAYLOAD, LANDED_PAYLOAD]
    )
    try:
        reader = FakeAuthor(READING)
        app._node_function_author = lambda tenant: reader
        invoice = _node_for(app, SPECS["invoice_scan"].goal)
        _blur_in_drawer(app, invoice.node_id)

        ran = _scan(app, ident)
        assert ran.status == 202, ran.body
        # The seat was consulted with the DOCUMENT, once.
        assert len(reader.calls) == 1
        assert BLUR in reader.calls[0][-1]["content"]
        # The checked extraction rode the re-run as bindings…
        bound = json.loads(backend.requests[-1].files["bindings.json"])
        assert bound["extracted_total"] == 1234.5
        assert bound["extracted_vendor"] == "ACME Supplies Ltd"
        assert bound["extracted_date"] == "2026-07-10"
        assert bound["invoice_file"] == "blur.txt"
        # …the document was staged again for the deterministic path…
        assert backend.requests[-1].files["attachments/blur.txt"] == BLUR
        # …and the act is cited on the chain, original and re-run named.
        (act,) = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "invoice.read_by_seat"
        ]
        assert act.payload["run_id"] == ran.body["run_id"]
        assert act.payload["reread_run_id"]
        assert act.payload["total"] == 1234.5
        states = {
            s.run_id
            for s in app._durable.runs.list()
            if s.contract.intent == SPECS["invoice_scan"].goal
        }
        assert act.payload["reread_run_id"] in states
    finally:
        conn.close()


def test_no_model_means_the_worded_refusal_stands(tmp_path):
    app, conn, ident, backend = _business_rig(tmp_path, [REFUSAL_PAYLOAD])
    try:
        invoice = _node_for(app, SPECS["invoice_scan"].goal)
        _blur_in_drawer(app, invoice.node_id)
        ran = _scan(app, ident)
        assert ran.status == 202, ran.body
        (miss,) = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "invoice.reading_failed"
        ]
        assert "no model" in miss.payload["reason"]
        # Exactly one run — nothing re-fired, nothing landed.
        assert len(backend.requests) == 1
    finally:
        conn.close()


def test_an_unreadable_verdict_from_the_seat_lands_nothing(tmp_path):
    app, conn, ident, backend = _business_rig(tmp_path, [REFUSAL_PAYLOAD])
    try:
        app._node_function_author = lambda tenant: FakeAuthor("UNREADABLE")
        invoice = _node_for(app, SPECS["invoice_scan"].goal)
        _blur_in_drawer(app, invoice.node_id)
        ran = _scan(app, ident)
        assert ran.status == 202, ran.body
        (miss,) = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "invoice.reading_failed"
        ]
        assert "no definite total" in miss.payload["reason"]
        assert len(backend.requests) == 1
    finally:
        conn.close()
