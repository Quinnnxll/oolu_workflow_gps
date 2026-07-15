"""The Supernode's template button: a lean working structure, imported.

Deterministic plan first, exactly like node execution: a recorded choice
returns instantly and is never re-reasoned; a keyword match over the
Supernode's description is pure arithmetic; the model is consulted only
when the evidence is thin — and then only to PICK a key from the curated
catalog, never to invent an org chart. Every template stays lean (the
roles cap), because communication, coordination, trust, and clear
responsibility are what limit mass-produced intelligence — not headcount.
"""

from __future__ import annotations

from test_http_gateway import _req
from test_supernode_kyc import _kyc_rig, _seed_supernode

from oolu.nodeplace.org_templates import (
    FALLBACK_KEY,
    MAX_TEMPLATE_ROLES,
    TEMPLATES,
    match_template,
    model_chooser,
    resolve_org_template,
    role_script,
    template_by_key,
)
from oolu.nodeplace.screening import screen_script


# --------------------------------------------------------------------------- #
# The catalog: lean by construction, deterministic scripts that really run.    #
# --------------------------------------------------------------------------- #
def test_every_template_is_lean_and_every_role_answers_for_one_thing():
    assert TEMPLATES, "the catalog is the plan"
    for template in TEMPLATES:
        assert len(template.roles) <= MAX_TEMPLATE_ROLES, template.key
        for role in template.roles:
            assert role.responsibility.strip(), role.name
            assert role.goal.strip(), role.name
            assert 1 <= role.authority <= 5
    assert template_by_key(FALLBACK_KEY) is not None


def test_role_scripts_are_deterministic_run_and_pass_screening():
    import sys
    import types

    shim = types.ModuleType("_oolu_runtime")
    out: dict = {}
    shim.emit_result = lambda value: out.setdefault("result", value)
    sys.modules["_oolu_runtime"] = shim
    try:
        for template in TEMPLATES:
            for role in template.roles:
                script = role_script(role)
                # No model wrote this: same role, same script, forever.
                assert script == role_script(role)
                assert screen_script(script) == []
                exec(compile(script, "<template>", "exec"), {})  # noqa: S102
                product = out.pop("result")
                assert product["role"] == role.name
                assert set(product["record"]) == set(role.fields)
                assert [c["step"] for c in product["checklist"]] == list(
                    role.checklist
                )
    finally:
        del sys.modules["_oolu_runtime"]


# --------------------------------------------------------------------------- #
# Resolution: recorded > matched > model-picked > lean fallback.               #
# --------------------------------------------------------------------------- #
def test_matching_is_deterministic_and_demands_evidence():
    template, evidence = match_template(
        "An online shop selling handmade products to customers"
    )
    assert template is not None and template.key == "commerce"
    assert "shop" in evidence
    # One keyword is a hint, not evidence.
    thin, _ = match_template("we have a shop")
    assert thin is None
    # No words at all: nothing matched, nothing guessed.
    none, evidence = match_template("")
    assert none is None and evidence == ()


def test_resolution_order_and_that_recorded_never_rethinks():
    calls: list[str] = []

    def chooser(description, catalog):
        calls.append(description)
        return "research"

    # A recorded choice wins outright — the chooser is never consulted.
    recorded = resolve_org_template(
        "anything at all", recorded="software", chooser=chooser
    )
    assert recorded.template.key == "software"
    assert recorded.source == "recorded" and calls == []
    # Strong evidence: deterministic, still no model.
    matched = resolve_org_template(
        "a ministry serving citizens with permits", chooser=chooser
    )
    assert matched.template.key == "government"
    assert matched.source == "matched" and calls == []
    # Thin evidence: the model picks FROM the catalog.
    picked = resolve_org_template("we do mysterious things", chooser=chooser)
    assert picked.template.key == "research" and picked.source == "model"
    assert calls == ["we do mysterious things"]
    # No chooser (or a useless answer): the lean generic shape.
    fallback = resolve_org_template("we do mysterious things")
    assert fallback.template.key == FALLBACK_KEY
    assert fallback.source == "fallback"


def test_the_model_chooser_selects_a_key_and_never_invents():
    class _Model:
        def __init__(self, answer):
            self.answer = answer

        def reply(self, messages):
            return self.answer

    catalog = [(t.key, t.name, t.purpose) for t in TEMPLATES]
    assert model_chooser(_Model("commerce"))("d", catalog) == "commerce"
    # A chatty answer: the FIRST catalog key named wins, deterministically.
    chatty = model_chooser(_Model("either logistics or commerce fits"))
    assert chatty("d", catalog) == "logistics"
    # An invented structure is no answer at all.
    assert model_chooser(_Model("a bespoke 12-team matrix org"))(
        "d", catalog
    ) is None


# --------------------------------------------------------------------------- #
# The routes: preview resolves and records; apply imports the missing seats.   #
# --------------------------------------------------------------------------- #
def _template_rig(tmp_path):
    app, conn, ident, registry, desk, kyc, plans = _kyc_rig(
        tmp_path, plans={"t1": "pro"}
    )
    super_id, member_id = _seed_supernode(app, ident, registry, desk)
    owner = ident.token("noder-export", "t1")
    return app, conn, ident, desk, super_id, member_id, owner


def test_the_template_routes_preview_record_and_import(tmp_path):
    app, conn, ident, desk, super_id, member_id, owner = _template_rig(tmp_path)
    try:
        # Preview: the description ("raw exporter"...) matches nothing —
        # no model is configured, so the lean fallback answers, and the
        # verdict is RECORDED on the account.
        preview = app.handle(
            _req("GET", f"/v1/work/nodes/{super_id}/template", token=owner)
        )
        assert preview.status == 200, preview.body
        assert preview.body["key"] == FALLBACK_KEY
        assert preview.body["source"] == "fallback"
        assert all(not r["exists"] for r in preview.body["roles"])
        assert desk.account_for(super_id).org_template == FALLBACK_KEY

        # Recorded: the second press resolves instantly — and even a model
        # that would now answer is never asked.
        def _never_called(tenant):  # pragma: no cover - the point is silence
            raise AssertionError("a recorded choice must never re-reason")

        app._node_function_author = _never_called
        again = app.handle(
            _req("GET", f"/v1/work/nodes/{super_id}/template", token=owner)
        )
        assert again.status == 200
        assert again.body["source"] == "recorded"

        # Apply: every missing seat is minted under the Supernode as an
        # unclaimed member with its role's authority and a real function.
        applied = app.handle(
            _req("POST", f"/v1/work/nodes/{super_id}/template", token=owner)
        )
        assert applied.status == 200, applied.body
        template = template_by_key(FALLBACK_KEY)
        assert len(applied.body["created"]) == len(template.roles)
        for created in applied.body["created"]:
            account = desk.account_for(created["node_id"])
            assert account.supernode_id == super_id
            assert account.authority_level == created["authority"]
            assert account.responsible == ""  # a claim ticket, kept private

        # Idempotent by role name: pressing import again mints nothing.
        second = app.handle(
            _req("POST", f"/v1/work/nodes/{super_id}/template", token=owner)
        )
        assert second.status == 200
        assert second.body["created"] == []
        assert {s["reason"] for s in second.body["skipped"]} == {
            "already seated"
        }
    finally:
        conn.close()


def test_the_template_door_is_the_supernodes_humans_only(tmp_path):
    app, conn, ident, desk, super_id, member_id, owner = _template_rig(tmp_path)
    try:
        # A member node is not a Supernode: 404 in words.
        not_super = app.handle(
            _req("GET", f"/v1/work/nodes/{member_id}/template", token=owner)
        )
        assert not_super.status == 404
        # A stranger cannot resolve (or import) someone else's org.
        stranger = app.handle(
            _req(
                "GET",
                f"/v1/work/nodes/{super_id}/template",
                token=ident.token("stranger", "t1"),
            )
        )
        assert stranger.status == 403
        assert desk.account_for(super_id).org_template == ""
    finally:
        conn.close()
