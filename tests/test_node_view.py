"""F3 — the unified view: every node renders its standing result.

One server-rendered page per node, template owned by the gateway, ZERO
tenant-authored bytes interpreted — safe under the pinned security
headers on every deployment mode. A program node adds its module count
and state names; every other node gets the same free view of its ports.
"""

from __future__ import annotations

import json

from test_http_gateway import _req
from test_program_state import STATE_ENTRY, STATE_LIB, _completed_run
from test_program_substrate import _program_app, _session

from oolu.skills.contract import Slot
from oolu.skills.models import ActionEvent, SkillSignature
from oolu.skills.pack import ReusableSkill
from oolu.skills.program import ModuleSpec, ProgramSpec, StateSpec, UnifiedInterface


def _publish_ledger_program(app, ident):
    spec = ProgramSpec(
        modules=[ModuleSpec(path="lib/ledger.py"), ModuleSpec(path="lib/__init__.py")],
        interface=UnifiedInterface(operation="main", ports=[{"name": "summary"}]),
        state=[
            StateSpec(name="ledger", kind="rows"),
            StateSpec(name="cursor", kind="value"),
        ],
    )
    result = app.publish_program_node(
        _session(app, ident),
        goal="keep the ledger",
        script=STATE_ENTRY,
        files=dict(STATE_LIB),
        program=spec,
        io={"outputs": [{"name": "summary", "type": "str"}]},
    )
    assert result["ok"], result.get("problem")
    return result["node_id"]


def _land_run(app, node_id, run_id, payload):
    run = _completed_run(node_id, run_id, payload)
    app._land_run_io(run)
    app._land_state(run)


def _view(app, ident, node_id, *, token=...):
    tok = ident.token("noder", "t1") if token is ... else token
    return app.handle(_req("GET", f"/v1/nodes/{node_id}/view", token=tok))


# --------------------------------------------------------------------------- #
# The rendered face.                                                           #
# --------------------------------------------------------------------------- #
def test_authed_view_renders_the_standing_result_inline(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    try:
        node_id = _publish_ledger_program(app, ident)
        _land_run(
            app,
            node_id,
            "run-1",
            {
                "summary": "1 rows",
                "state": {
                    "ledger": [{"at": "2026-01-01", "label": "e1", "value": 1}],
                    "cursor": 1,
                },
            },
        )
        response = _view(app, ident, node_id)
        assert response.status == 200, response.body
        assert response.content_type.startswith("text/html")
        page = response.body
        # The standing result renders INLINE: the declared port, its
        # latest verified value, and the run stamp.
        assert "<dt>summary</dt>" in page
        assert "<dd>1 rows</dd>" in page
        assert "run run-1"[:11] in page  # the stamp names the run
        # The program face: module count and the declared state names.
        assert "program node · 2 internal modules" in page
        assert "ledger — rows (1 rows)" in page
        assert "cursor — value" in page
        # The page is inert: no script, no style, no subresource.
        assert "<script" not in page and "<style" not in page
        # The pinned security headers stand UNCHANGED on the HTML route.
        assert response.headers["X-Frame-Options"] == "DENY"
        assert (
            response.headers["Content-Security-Policy"]
            == "default-src 'none'; frame-ancestors 'none'"
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    finally:
        conn.close()


def test_no_verified_run_renders_the_honest_empty_view(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    try:
        node_id = _publish_ledger_program(app, ident)
        response = _view(app, ident, node_id)
        assert response.status == 200
        assert "No verified run yet" in response.body
    finally:
        conn.close()


def test_tenant_bytes_are_escaped_never_interpreted(tmp_path):
    # The one rule that makes the view safe everywhere: a hostile payload
    # value renders as TEXT, never as markup.
    app, conn, ident, runner = _program_app(tmp_path)
    try:
        node_id = _publish_ledger_program(app, ident)
        _land_run(
            app,
            node_id,
            "run-1",
            {"summary": "<script>alert(1)</script><img src=x onerror=y>"},
        )
        page = _view(app, ident, node_id).body
        assert "<script>" not in page
        assert "<img" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The walls.                                                                   #
# --------------------------------------------------------------------------- #
def test_foreign_tenant_sees_a_uniform_404(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    try:
        node_id = _publish_ledger_program(app, ident)
        response = _view(
            app, ident, node_id, token=ident.token("stranger", "t2")
        )
        assert response.status == 404
        # And an unknown node answers identically — no existence oracle.
        unknown = _view(app, ident, "no-such-node")
        assert unknown.status == 404
        assert unknown.body == response.body
    finally:
        conn.close()


def test_unauthenticated_view_is_401(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    try:
        node_id = _publish_ledger_program(app, ident)
        response = _view(app, ident, node_id, token=None)
        assert response.status == 401
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Every node gets the free ports view.                                         #
# --------------------------------------------------------------------------- #
def test_a_plain_function_node_gets_the_same_free_view(tmp_path):
    app, conn, ident, runner = _program_app(tmp_path)
    try:
        skill = ReusableSkill(
            name="rate fetcher",
            description="fetch the rate",
            signature=SkillSignature(application="script", adapter="script"),
            actions=[
                ActionEvent(
                    correlation_id="function",
                    adapter="script",
                    operation="run",
                    parameters={
                        "goal": "fetch the rate",
                        "script": "# fn",
                        "node_key": "node:rate",
                    },
                )
            ],
        )
        contributed = app._nodeplace.contribute(
            noder_principal="noder",
            tenant_id="t1",
            skill=skill,
            semver="1.0.0",
            title="rate fetcher",
            summary="fetch the rate",
            produces=[Slot(name="rate", value_type="number", role="result")],
        )
        node_id = contributed.node.node_id
        _land_run(app, node_id, "run-9", {"rate": 4.25})
        response = _view(app, ident, node_id)
        assert response.status == 200
        page = response.body
        assert "function node" in page and "program node" not in page
        assert "<dt>rate</dt>" in page and "<dd>4.25</dd>" in page
        assert "Program state" not in page  # no program, no state section
    finally:
        conn.close()


def test_a_non_dict_outputs_file_renders_the_empty_view_not_a_500(tmp_path):
    # F3.1: a hand-written runs/*/outputs.json holding a bare list parses
    # cleanly but is NOT a standing result — the view renders the honest
    # empty page instead of crashing on .get().
    from oolu.durable.files import UserFile

    app, conn, ident, runner = _program_app(tmp_path)
    try:
        node_id = _publish_ledger_program(app, ident)
        app._files.save(
            UserFile(
                tenant_id="t1",
                node_id=node_id,
                folder="runs/hand-written",
                name="outputs.json",
                media_type="application/json",
                content="[1, 2, 3]",
            )
        )
        response = _view(app, ident, node_id)
        assert response.status == 200, response.body
        assert "No verified run yet" in response.body
    finally:
        conn.close()
