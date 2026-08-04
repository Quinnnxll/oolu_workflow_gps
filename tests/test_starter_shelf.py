"""P1: the starter shelf — seven personal nodes at account birth.

Exit gate: a fresh account's desk shows the seven starters, each an
ORDINARY node (contribute door, drawer copy landed, plain-word labels
on the listing) whose deterministic function actually executes; the
seeding pass runs exactly once per person (racing sign-ins seed once);
a deleted starter stays gone on the next sign-in; and the whole pass
never breaks a sign-in door.
"""

from __future__ import annotations

import json
import subprocess
import sys

from test_chat_assistant import _FakeModel
from test_growth_trigger import _chat, _rig
from test_http_gateway import _req

from oolu.gateway.app import GatewayApp
from oolu.identity import Hs256Signer, LocalAccountService, LocalUserStore
from oolu.nodeplace.personal_templates import (
    STARTER_SHELF,
    StarterLedger,
    starter_script,
)
from oolu.plainlanguage import mechanism_terms

_RUNTIME_SHIM = (
    "import json\n"
    "def emit_result(value):\n"
    "    with open('result.json', 'w') as handle:\n"
    "        json.dump(value, handle)\n"
)


# --------------------------------------------------------------------------- #
# The catalog: reviewed words, honest scripts.                                 #
# --------------------------------------------------------------------------- #
def test_the_shelf_holds_seven_distinct_gate_clean_specs():
    assert len(STARTER_SHELF) == 7
    assert len({s.key for s in STARTER_SHELF}) == 7
    assert len({s.goal for s in STARTER_SHELF}) == 7
    for spec in STARTER_SHELF:
        io = {
            "inputs": [
                {"name": i.name, "type": i.value_type} for i in spec.inputs
            ],
            "outputs": [{"name": "record", "type": "json"}],
        }
        # Every script passes the standing birth gate…
        assert GatewayApp._birth_problem(starter_script(spec), io) is None
        # …and every ask is in the user's words (the B0 lexicon).
        for item in spec.inputs:
            assert mechanism_terms(item.label) == [], (spec.key, item.label)
            assert item.label.endswith("?")


# One parseable sample per starter — each function's own words.
_SAMPLES = {
    "calendar": "hello world today",
    "tasks": "hello world tomorrow",
    "reminders": "hello world tomorrow at 9",
    "trigger": "every day at 9, run hello world",
    "stock": "received 4 hello world",
    "cashflow": "hello world 120 in today",
    "invoice_scan": "hello-world.txt",
}


def test_every_starter_function_actually_executes(tmp_path):
    # Verify-by-execution, for real: each deterministic script runs in
    # a subprocess against a bindings.json, emits its payload, and the
    # bound value lands in it — the record-keepers in their book, the
    # filing starters in their record.
    for index, spec in enumerate(STARTER_SHELF):
        workdir = tmp_path / f"starter-{index}"
        workdir.mkdir()
        first = spec.inputs[0].name
        sample = _SAMPLES[spec.key]
        (workdir / "main.py").write_text(starter_script(spec))
        (workdir / "bindings.json").write_text(json.dumps({first: sample}))
        (workdir / "_oolu_runtime.py").write_text(_RUNTIME_SHIM)
        subprocess.run(
            [sys.executable, "main.py"],
            cwd=workdir,
            check=True,
            timeout=60,
            capture_output=True,
        )
        payload = json.loads((workdir / "result.json").read_text())
        assert isinstance(payload, dict)
        assert sample in json.dumps(payload), spec.key
        # Every declared output port is covered — the armed run-time
        # contract check will hold real runs to exactly this.
        for port in spec.outputs:
            assert port.name in payload, (spec.key, port.name)


# --------------------------------------------------------------------------- #
# The seeding pass: exactly once, ordinary nodes, deletions respected.         #
# --------------------------------------------------------------------------- #
def test_seeding_mints_seven_ordinary_nodes_exactly_once(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        created = app._seed_starter_shelf("t1", "user-1")
        assert [c["key"] for c in created] == [s.key for s in STARTER_SHELF]

        # The desk shows all seven, owned by the person.
        mine = desk.overview(principal="user-1", tenant="t1")
        assert sorted(e.title for e in mine) == sorted(
            s.name for s in STARTER_SHELF
        )
        # Each is an ORDINARY node: drawer copy landed (B2) and the
        # listing carries the plain-word ask (B1).
        for entry in created:
            assert any(
                f.folder == "src" and f.name == "main.py"
                for f in app._files.list(tenant="t1", node_id=entry["node_id"])
            ), entry["key"]
            version = app._nodeplace.latest_version(entry["node_id"])
            listing = app._nodeplace.listing_for_version(version.version_id)
            assert listing is not None
            assert all(s.label for s in listing.consumes), entry["key"]
        seeded = [
            e
            for e in app._durable.audit.records()
            if e.event_type == "node.starter_seeded"
        ]
        assert len(seeded) == 7

        # Exactly once: the second pass finds the claim spent.
        assert app._seed_starter_shelf("t1", "user-1") == []
        assert len(desk.overview(principal="user-1", tenant="t1")) == 7

        # A deleted starter stays gone — the claim never leaves, so no
        # later sign-in resurrects it.
        victim = created[0]["node_id"]
        gone = app.handle(
            _req(
                "DELETE",
                f"/v1/work/nodes/{victim}",
                token=ident.token("user-1", "t1"),
            )
        )
        assert gone.status == 200, gone.body
        app._maybe_seed_starters("t1", "user-1")
        assert app._node_deleted(victim)
        assert len(desk.overview(principal="user-1", tenant="t1")) == 6
    finally:
        conn.close()


def test_a_starter_runs_through_the_standing_run_door(tmp_path):
    # Live-runnable: a seeded node's goal fires ITS OWN function
    # through the ordinary chat run door — no build, no model spend.
    # V1: the ask queues; the worker's drive fires the function.
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        app._seed_starter_shelf("t1", "user-1")
        goal = STARTER_SHELF[1].goal  # Tasks
        model = _FakeModel(['{"say": "On it!", "task": "' + goal + '"}'])
        app._tenant_model = lambda tenant: model
        ran = _chat(app, ident, "please " + goal)
        assert ran.status == 200, ran.body
        assert ran.body["run_id"], ran.body
        assert ran.body["run"]["phase"] == "intake"  # queued, not driven
        assert app.drive_queue() >= 1
        (action,) = script_exec.actions
        assert "Tasks —" in action.parameters["script"]
        assert action.parameters["goal"] == goal
    finally:
        conn.close()


def test_the_signin_door_seeds_and_never_breaks(tmp_path):
    # The full circle: a real account, a real login, the shelf on the
    # desk afterwards — and a second login seeds nothing more.
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        signer = Hs256Signer(
            secret="s3cret", issuer="local", audience="oolu"
        )
        accounts = LocalAccountService(
            LocalUserStore(tmp_path / "users.db"), ident.store, signer
        )
        app._accounts = accounts
        accounts.create_user("quinn", "pass-word-1", tenant="t1")

        first = app.handle(
            _req(
                "POST",
                "/v1/auth/login",
                body={"username": "quinn", "password": "pass-word-1"},
            )
        )
        assert first.status == 200, first.body
        mine = desk.overview(principal="quinn", tenant="t1")
        assert sorted(e.title for e in mine) == sorted(
            s.name for s in STARTER_SHELF
        )

        again = app.handle(
            _req(
                "POST",
                "/v1/auth/login",
                body={"username": "quinn", "password": "pass-word-1"},
            )
        )
        assert again.status == 200, again.body
        assert len(desk.overview(principal="quinn", tenant="t1")) == 7
    finally:
        conn.close()


def test_the_ledger_claim_is_exactly_once_across_processes(tmp_path):
    from oolu.durable import DurableConnection

    conn = DurableConnection(tmp_path / "shelf.db")
    try:
        ledger = StarterLedger(conn)
        assert ledger.claim("t1", "alice") is True
        assert ledger.claim("t1", "alice") is False
        # A second process shares the row, not the luck.
        assert StarterLedger(conn).claim("t1", "alice") is False
        assert ledger.claim("t1", "bob") is True
        assert ledger.seeded("t1", "alice") is True
        assert ledger.seeded("t2", "alice") is False  # tenant-walled
    finally:
        conn.close()
