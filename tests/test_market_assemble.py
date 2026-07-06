"""POST /v1/market/assemble: goal slots in, marketplace workflow preview out."""

from __future__ import annotations

from test_gateway_market import _build, _contribute_and_publish, _req

from oolu.skills.models import (
    ActionEvent,
    ConstraintSpec,
    ReusableSkill,
    SkillParameter,
    SkillSignature,
)

RAW = {"name": "raw", "value_type": "path", "role": "path"}
TIDY = {"name": "tidy", "value_type": "path", "role": "path"}


def _seed_market(app, ident, registry):
    """Two published nodes forming a chain via their slot vocabularies."""
    exporter = _contribute_and_publish(
        app,
        ident,
        registry,
        name="raw exporter",
        noder="noder-export",
        price=0.10,
        produces=[RAW],
        consumes=[],
    )
    cleaner = _contribute_and_publish(
        app,
        ident,
        registry,
        name="invoice cleaner",
        noder="noder-clean",
        price=0.20,
        consumes=[RAW],
        produces=[TIDY],
    )
    return exporter, cleaner


def _assemble(app, ident, body):
    return app.handle(
        _req(
            "POST",
            "/v1/market/assemble",
            token=ident.token("consumer", "t2"),
            body=body,
        )
    )


def test_goal_assembles_a_marketplace_chain_with_payout_previews(tmp_path):
    app, conn, ident, registry, *_rest = _build(tmp_path)
    exporter, cleaner = _seed_market(app, ident, registry)

    resp = _assemble(
        app,
        ident,
        {"goal": {"name": "clean-the-books", "want": [TIDY]}, "q": "invoice"},
    )
    assert resp.status == 200, resp.body
    body = resp.body
    assert body["complete"] is True
    assert set(body["selected"]) == {"invoice cleaner", "raw exporter"}
    assert body["missing"] == []

    by_version = {n["version_id"]: n for n in body["nodes"] if not n["gap"]}
    assert set(by_version) == {exporter, cleaner}
    for node in by_version.values():
        assert node["payout_previews"], "every marketplace node previews payouts"
        assert node["cleared"]["cleared"] > 0
    payees = {
        p["noder_principal"]
        for node in by_version.values()
        for p in node["payout_previews"]
    }
    assert payees == {"noder-export", "noder-clean"}
    assert body["estimated_gross_total"] > 0
    assert body["platform_margin_preview"] > 0

    # The returned contract is the real thing: subgraph with both nodes.
    child_ids = {c["id"] for c in body["contract"]["body"]["nodes"]}
    assert child_ids == {exporter, cleaner}
    # Planning is read-only: the price book never moved.
    assert app._price_book.reference("workflow:invoice_cleaning") is None
    conn.close()


def test_slots_on_hand_skip_producers_and_lineage_rides_previews(tmp_path):
    app, conn, ident, registry, *_rest = _build(tmp_path)
    exporter, cleaner = _seed_market(app, ident, registry)
    # A derived cleaner: its previews must include the ancestor automatically.
    derived = _contribute_and_publish(
        app,
        ident,
        registry,
        name="derived cleaner",
        noder="noder-deriver",
        price=0.05,
        derived_from=cleaner,
        consumes=[RAW],
        produces=[TIDY],
    )

    resp = _assemble(
        app,
        ident,
        {
            "goal": {"name": "finish-up", "want": [TIDY], "have": [RAW]},
            "q": "invoice",
        },
    )
    assert resp.status == 200
    body = resp.body
    assert body["complete"] is True
    assert "raw exporter" not in body["selected"]  # raw is on hand

    (node,) = [n for n in body["nodes"] if not n["gap"]]
    payees = {p["noder_principal"] for p in node["payout_previews"]}
    if node["version_id"] == derived:
        assert payees == {"noder-deriver", "noder-clean"}  # lineage, automatic
    else:
        assert node["version_id"] == cleaner and payees == {"noder-clean"}
    assert exporter not in {n.get("version_id") for n in body["nodes"]}
    conn.close()


def test_missing_slots_report_honestly_and_gaps_fill_on_request(tmp_path):
    app, conn, ident, registry, *_rest = _build(tmp_path)
    _seed_market(app, ident, registry)
    unicorn = {"name": "unicorn", "value_type": "path"}

    honest = _assemble(app, ident, {"goal": {"name": "impossible", "want": [unicorn]}})
    assert honest.status == 200
    assert honest.body["complete"] is False
    assert honest.body["contract"] is None
    assert [s["name"] for s in honest.body["missing"]] == ["unicorn"]

    filled = _assemble(
        app,
        ident,
        {"goal": {"name": "stretch", "want": [unicorn]}, "fill_gaps": True},
    )
    assert filled.status == 200
    assert filled.body["complete"] is True
    assert filled.body["gap_filled"] == ["unicorn"]
    (gap,) = filled.body["nodes"]
    assert gap["gap"] is True and gap["kind"] == "script"
    conn.close()


def test_listing_slot_vocabulary_defaults_from_the_skill(tmp_path):
    """A contribution without declared slots derives them from the skill."""
    app, conn, ident, registry, *_rest = _build(tmp_path)
    skill = ReusableSkill(
        name="templated convert",
        description="converts with an induced parameter",
        signature=SkillSignature(application="cli", adapter="cli"),
        parameters=[
            SkillParameter(name="source", value_type="path", domain={"role": "path"})
        ],
        actions=[ActionEvent(correlation_id="c", adapter="cli", operation="run")],
        validators=[
            ConstraintSpec(
                id="artifacts",
                description="",
                validator="workspace.expected_artifacts",
                evidence={"expected_files": ["out/summary.csv"]},
            )
        ],
    )
    created = app.handle(
        _req(
            "POST",
            "/v1/nodeplace",
            token=ident.token("noder-x", "t1"),
            body={
                "skill": skill.model_dump(mode="json"),
                "semver": "1.0.0",
                "title": "templated convert",
                "summary": "derives its vocabulary",
                "tags": ["class:workflow"],
                "visibility": "public",
            },
        )
    )
    assert created.status == 201
    listing = registry.listing_for_version(created.body["version_id"])
    assert [s.name for s in listing.consumes] == ["source"]
    assert [s.name for s in listing.produces] == ["out/summary.csv"]
    conn.close()


def test_assemble_validates_its_input(tmp_path):
    app, conn, ident, *_rest = _build(tmp_path)
    token = ident.token("consumer", "t2")
    assert (
        app.handle(_req("POST", "/v1/market/assemble", token=token, body={})).status
        == 400
    )
    assert (
        app.handle(
            _req(
                "POST",
                "/v1/market/assemble",
                token=token,
                body={"goal": {"name": "empty", "want": []}},
            )
        ).status
        == 400
    )
    conn.close()
