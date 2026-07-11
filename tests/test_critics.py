"""Critics — step 4 of the industrial vertical.

Critics submit evidence-backed findings; they do not rewrite accepted
work. Exit gates: a finding lands under ``issues/{target path}``
through the kernel with write scope on the issues subtree ONLY (the
design stays closed to the critic); the door refuses findings without
evidence or a recommended action; an OPEN blocking finding stops the
target's status from advancing to approved/released — while fixing
parameters stays allowed, because that is how findings get resolved —
and a resolved finding unblocks the climb.
"""

from __future__ import annotations

from test_http_gateway import _app, _req

PROJECT = "/v1/graph/veh-4"


def _propose(app, ident, body, *, principal="alice", tenant="t1"):
    return app.handle(
        _req(
            "POST",
            f"{PROJECT}/proposals",
            token=ident.token(principal, tenant),
            body=body,
        )
    )


def _seed(app, ident):
    """Alice opens the project with one component; Carol the critic is
    granted the ISSUES subtree only — she can read the design, never
    write it."""
    created = _propose(
        app,
        ident,
        {
            "reason": "initial design drop",
            "patch": [
                {
                    "op": "create",
                    "object": {
                        "object_id": "m1",
                        "path": "subsystems/suspension/front",
                        "type": "component",
                        "parameters": {"y_mm": 412},
                    },
                }
            ],
        },
    )
    assert created.status == 200, created.body
    granted = app.handle(
        _req(
            "POST",
            f"{PROJECT}/scopes",
            token=ident.token("alice", "t1"),
            body={
                "principal": "carol",
                "read_paths": ["subsystems", "issues"],
                "write_paths": ["issues"],
            },
        )
    )
    assert granted.status == 200


def _file_finding(app, ident, *, severity="blocking", principal="carol"):
    return app.handle(
        _req(
            "POST",
            f"{PROJECT}/findings",
            token=ident.token(principal, "t1"),
            body={
                "target": "m1",
                "severity": severity,
                "finding": "potential assembly-tool interference",
                "recommended_action": "reconsider the mount position",
                "evidence": {
                    "tool_clearance_mm": 8.1,
                    "required_clearance_mm": 18.0,
                    "simulation_id": "CLR-9921",
                },
            },
        )
    )


def test_a_critic_files_findings_but_can_never_rewrite_the_design(tmp_path):
    app, conn, ident = _app(tmp_path)
    try:
        _seed(app, ident)
        filed = _file_finding(app, ident)
        assert filed.status == 200, filed.body
        finding_id = filed.body["finding_id"]

        # The finding is real truth: revisioned, evidence-backed, placed
        # under issues/{target path}.
        obj = app.handle(
            _req(
                "GET",
                f"{PROJECT}/objects/{finding_id}",
                token=ident.token("carol", "t1"),
            )
        )
        assert obj.status == 200
        assert obj.body["path"] == "issues/subsystems/suspension/front"
        assert obj.body["evidence"][0]["simulation_id"] == "CLR-9921"

        # But the DESIGN is closed territory: the same critic's attempt
        # to touch the component itself dies at the kernel's wall.
        rewrite = _propose(
            app,
            ident,
            {
                "reason": "just fixing it myself",
                "patch": [
                    {
                        "op": "set",
                        "object_id": "m1",
                        "base_revision": 1,
                        "pointer": "parameters/y_mm",
                        "old_value": 412,
                        "new_value": 398,
                    }
                ],
            },
            principal="carol",
        )
        assert rewrite.status == 409
        assert any("write paths" in r for r in rewrite.body["reasons"])
    finally:
        conn.close()


def test_the_door_refuses_opinions_and_junk(tmp_path):
    app, conn, ident = _app(tmp_path)
    try:
        _seed(app, ident)
        base = {
            "target": "m1",
            "severity": "blocking",
            "finding": "something is off",
            "recommended_action": "look again",
            "evidence": {"measured": 1},
        }
        for broken, message in (
            ({**base, "evidence": {}}, "opinion"),
            ({**base, "severity": "catastrophic"}, "severity"),
            ({**base, "recommended_action": " "}, "what to do next"),
            ({**base, "target": "ghost"}, "no such object"),
        ):
            response = app.handle(
                _req(
                    "POST",
                    f"{PROJECT}/findings",
                    token=ident.token("carol", "t1"),
                    body=broken,
                )
            )
            assert response.status in (400, 404), broken
            assert message in response.body["error"]["message"]
    finally:
        conn.close()


def test_an_open_blocking_finding_stops_the_climb(tmp_path):
    app, conn, ident = _app(tmp_path)
    try:
        _seed(app, ident)
        assert _file_finding(app, ident).status == 200
        finding_id = _file_finding(app, ident, severity="minor").body[
            "finding_id"
        ]
        assert finding_id  # a second, informational finding rides along

        # Fixing the design stays allowed — that is HOW findings resolve.
        fix = _propose(
            app,
            ident,
            {
                "reason": "increase tool clearance per CLR-9921",
                "patch": [
                    {
                        "op": "set",
                        "object_id": "m1",
                        "base_revision": 1,
                        "pointer": "parameters/y_mm",
                        "old_value": 412,
                        "new_value": 398,
                    }
                ],
            },
        )
        assert fix.status == 200, fix.body

        # But advancement is gated while the blocking finding is open.
        climb = _propose(
            app,
            ident,
            {
                "reason": "ready for release",
                "patch": [
                    {
                        "op": "set",
                        "object_id": "m1",
                        "base_revision": 2,
                        "pointer": "status",
                        "old_value": "draft",
                        "new_value": "approved",
                    }
                ],
            },
        )
        assert climb.status == 409
        assert any("blocking finding" in r for r in climb.body["reasons"])
        assert any("reconsider the mount" in r for r in climb.body["reasons"])
    finally:
        conn.close()


def test_a_resolved_finding_unblocks_and_minors_never_blocked(tmp_path):
    app, conn, ident = _app(tmp_path)
    try:
        _seed(app, ident)
        blocking_id = _file_finding(app, ident).body["finding_id"]

        # The critic resolves their own finding (issues is their
        # territory), on the record, with words.
        resolved = _propose(
            app,
            ident,
            {
                "reason": "clearance re-simulated at 21mm — resolved",
                "patch": [
                    {
                        "op": "set",
                        "object_id": blocking_id,
                        "base_revision": 1,
                        "pointer": "parameters/state",
                        "old_value": "open",
                        "new_value": "resolved",
                    }
                ],
            },
            principal="carol",
        )
        assert resolved.status == 200, resolved.body

        climb = _propose(
            app,
            ident,
            {
                "reason": "ready for release",
                "patch": [
                    {
                        "op": "set",
                        "object_id": "m1",
                        "base_revision": 1,
                        "pointer": "status",
                        "old_value": "draft",
                        "new_value": "approved",
                    }
                ],
            },
        )
        assert climb.status == 200, climb.body

        # The findings ledger reads open-first, honest either way.
        ledger = app.handle(
            _req(
                "GET",
                f"{PROJECT}/findings",
                token=ident.token("carol", "t1"),
                query={"target": "m1"},
            )
        )
        assert ledger.status == 200
        states = [
            item["parameters"]["state"] for item in ledger.body["items"]
        ]
        assert states == ["resolved"]
    finally:
        conn.close()
