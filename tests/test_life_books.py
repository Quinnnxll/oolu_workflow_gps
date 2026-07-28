"""Life books: one shared function, one private book per member.

Exit gate: every prebuilt book (invoice_scan, cashflow, stock,
automation, reminder, task, calendar) lives behind ONE central registry
owned by the OoLu official account; each member's data is ONE file in
their OWN Life drawer at a stable pointer — the drawer's memories gate
is the whole access model (another member's doors see an empty book);
what the nodes documented imports into the books idempotently; OoLu
reads and writes through the pointer reliably; and "show cashflow" in
OoLu's own conversation answers with a CHART BLOCK drawn from the
member's book, never an invention.
"""

from __future__ import annotations

import pytest
from test_http_gateway import NOW, _app, _req

from oolu import lifebooks
from oolu.durable.files import UserFileStore
from oolu.social import AssistantHistoryStore


def test_the_pointer_is_the_command_and_the_owner_is_the_house():
    assert lifebooks.OFFICIAL_OWNER == "oolu-official"
    assert set(lifebooks.BOOKS) == {
        "invoice_scan",
        "cashflow",
        "stock",
        "automation",
        "reminder",
        "task",
        "calendar",
    }
    # The same kind, the same file, forever.
    assert lifebooks.pointer("cashflow") == {
        "folder": "books",
        "name": "cashflow.json",
    }
    with pytest.raises(ValueError, match="unknown book"):
        lifebooks.pointer("diary")


def test_books_write_through_the_pointer_and_stay_private(tmp_path):
    app, conn, ident = _app(tmp_path)
    files = UserFileStore(conn)
    added = lifebooks.append_rows(
        files,
        tenant="t1",
        owner="alice",
        kind="cashflow",
        rows=[
            {"at": "2026-07-01", "label": "rent", "value": -900},
            {"at": "2026-07-03", "label": "invoice 12", "value": 1400.5},
        ],
    )
    assert added == 2
    # Idempotent through the pointer: the same rows never double.
    assert (
        lifebooks.append_rows(
            files,
            tenant="t1",
            owner="alice",
            kind="cashflow",
            rows=[{"at": "2026-07-01", "label": "rent", "value": -900}],
        )
        == 0
    )
    rows = lifebooks.read_rows(files, tenant="t1", owner="alice", kind="cashflow")
    assert [r["label"] for r in rows] == ["rent", "invoice 12"]
    # The memories gate IS the access model: bob's book is empty.
    assert (
        lifebooks.read_rows(files, tenant="t1", owner="bob", kind="cashflow")
        == []
    )
    # Numeric rows chart as they stand; label-only rows chart as counts.
    points = lifebooks.chart_points("cashflow", rows)
    assert points == [
        {"label": "rent", "value": -900.0},
        {"label": "invoice 12", "value": 1400.5},
    ]
    assert lifebooks.chart_points(
        "task", [{"at": "", "label": "open", "value": None}] * 3
    ) == [{"label": "open", "value": 3}]
    conn.close()


def test_the_doors_import_review_chart_and_the_chat_draws(tmp_path):
    from oolu.reminders import ReminderStore

    app, conn, ident = _app(tmp_path)
    app._files = UserFileStore(conn)
    app._assistant_history = AssistantHistoryStore(conn)
    app._reminders = ReminderStore(conn)
    alice = ident.token("alice")

    # What the prebuilt nodes documented: a reminder, an event, a
    # standing automation trigger.
    app.handle(
        _req(
            "POST",
            "/v1/reminders",
            token=alice,
            body={"text": "pay the rent", "due_at": "2027-01-05T09:00:00+00:00"},
        )
    )
    app.handle(
        _req(
            "POST",
            "/v1/records/calendar",
            token=alice,
            body={
                "title": "Harbor market",
                "starts_at": "2027-01-06T08:00:00+00:00",
                "ends_at": "2027-01-06T09:00:00+00:00",
            },
        )
    )
    app._pulse.add(
        "t1",
        "alice",
        cadence="daily",
        at_minute=8 * 60,
        goal="press.morning_edition",
        tz_offset_minutes=0,
        label="Morning edition",
        now=NOW,
    )

    imported = app.handle(
        _req("POST", "/v1/life/books/import", token=alice)
    )
    assert imported.status == 200
    counts = imported.body["imported"]
    assert counts["reminder"] == 1
    assert counts["calendar"] == 1
    assert counts["automation"] >= 1
    # Idempotent: a re-run doubles nothing.
    again = app.handle(_req("POST", "/v1/life/books/import", token=alice))
    assert all(v == 0 for v in again.body["imported"].values())

    # One glance: every book named with its pointer and count.
    glance = app.handle(_req("GET", "/v1/life/books", token=alice))
    assert glance.body["official_owner"] == "oolu-official"
    by_kind = {i["kind"]: i for i in glance.body["items"]}
    assert by_kind["reminder"]["rows"] == 1
    assert by_kind["calendar"]["pointer"] == {
        "folder": "books",
        "name": "calendar.json",
    }
    # Another member's doors see empty books — the wall is the drawer's.
    bob_glance = app.handle(
        _req("GET", "/v1/life/books", token=ident.token("bob"))
    )
    assert all(i["rows"] == 0 for i in bob_glance.body["items"])

    rows = app.handle(_req("GET", "/v1/life/books/reminder", token=alice))
    assert rows.body["rows"][0]["label"] == "pay the rent"
    assert (
        app.handle(_req("GET", "/v1/life/books/diary", token=alice)).status
        == 404
    )

    # OoLu draws the book on demand — a chart block, from the member's
    # own file, before any model spend.
    lifebooks.append_rows(
        app._files,
        tenant="t1",
        owner="alice",
        kind="cashflow",
        rows=[
            {"at": "2026-07-01", "label": "rent", "value": -900},
            {"at": "2026-07-03", "label": "invoice 12", "value": 1400.5},
        ],
    )
    drawn = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=alice,
            body={"message": "show cashflow", "history": []},
        )
    )
    assert drawn.status == 200 and drawn.body["source"] == "desk"
    assert drawn.body["block"]["kind"] == "chart"
    assert drawn.body["block"]["title"] == "Cashflow"
    assert drawn.body["block"]["points"][0]["label"] == "rent"
    chart = app.handle(
        _req("GET", "/v1/life/books/cashflow/chart", token=alice)
    )
    assert chart.body["points"] == drawn.body["block"]["points"]
    # An empty book answers honestly, block-less.
    empty = app.handle(
        _req(
            "POST",
            "/v1/chat",
            token=alice,
            body={"message": "show my stock", "history": []},
        )
    )
    assert "empty" in empty.body["reply"] and empty.body["block"] is None
    conn.close()
