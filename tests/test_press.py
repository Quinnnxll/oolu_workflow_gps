"""The press (A1): publish, attribute, weigh — the contribution spine.

Exit gate (agents-expansion plan, phase A1): publishing without consent
and a known license is refused; the scrub gate refuses loudly with each
leak named instead of silently dropping or rewriting; near-duplicates are
flagged with the original credited; every stored contribution resolves to
its author; unpublishing supersedes — future reads exclude it, history is
never erased, and the audit chain verifies end to end; and the press
package reaches no web-search or external-content seam (the import scan).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.press import (
    GENRES,
    LICENSES,
    MAX_BODY_CHARS,
    TAXONOMY_VERSION,
    ContributionStore,
    PressDesk,
    PressError,
    leak_report,
    taxonomy_items,
)

NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)

LICENSE = next(iter(LICENSES))


def _desk(tmp_path):
    conn = DurableConnection(tmp_path / "press.db")
    tick = {"now": NOW}

    def clock():
        tick["now"] += timedelta(seconds=1)
        return tick["now"]

    return conn, PressDesk(ContributionStore(conn, clock=clock))


def _publish(desk, **overrides):
    args = dict(
        tenant="t1",
        author="alice",
        title="The harbor market reopened",
        body=(
            "After two years of repairs the harbor market reopened this "
            "morning with forty stalls and a queue down the pier."
        ),
        genres=("local",),
        license=LICENSE,
        consent=True,
    )
    args.update(overrides)
    return desk.publish(**args)


# --------------------------------------------------------------------------- #
# The taxonomy: one typed, versioned vocabulary.                               #
# --------------------------------------------------------------------------- #
def test_the_taxonomy_is_typed_and_versioned():
    assert TAXONOMY_VERSION == 1
    items = taxonomy_items()
    assert [i["key"] for i in items] == list(GENRES)
    for item in items:
        # A genre without a label or a contributor's guide cannot ship.
        assert item["label"] and item["description"]
    # The commerce-adjacent genres that seed Explorer's evidence exist.
    assert "products" in GENRES and "results" in GENRES


# --------------------------------------------------------------------------- #
# The publication gate, in order.                                              #
# --------------------------------------------------------------------------- #
def test_consent_and_license_come_first(tmp_path):
    conn, desk = _desk(tmp_path)
    with pytest.raises(PressError, match="consent"):
        _publish(desk, consent=False)
    with pytest.raises(PressError, match="unknown license"):
        _publish(desk, license="cc-by")
    conn.close()


def test_words_bounds_and_genres_are_checked_by_name(tmp_path):
    conn, desk = _desk(tmp_path)
    with pytest.raises(PressError, match="title"):
        _publish(desk, title="   ")
    with pytest.raises(PressError, match="needs words"):
        _publish(desk, body="")
    with pytest.raises(PressError, match="too long"):
        _publish(desk, body="x" * (MAX_BODY_CHARS + 1))
    with pytest.raises(PressError, match="at least one genre"):
        _publish(desk, genres=())
    with pytest.raises(PressError, match="unknown genre: 'crypto'"):
        _publish(desk, genres=("local", "crypto"))
    conn.close()


def test_the_scrub_gate_refuses_loudly_and_names_the_leak(tmp_path):
    conn, desk = _desk(tmp_path)
    with pytest.raises(PressError) as refusal:
        _publish(
            desk,
            body="Write to me at alice@example.com for the full data set.",
        )
    # The refusal is a direction, not a silent rewrite: the leak is named
    # and the author is told the platform never edits their words.
    assert "an email address" in str(refusal.value)
    assert "never rewrites" in str(refusal.value)
    # And nothing was stored on the way out.
    assert desk.store.list(tenant="t1") == []
    conn.close()


def test_leak_report_names_the_shapes():
    assert leak_report("mail me: bob@example.org") == ["an email address"]
    # Assembled at runtime so the repo's own secret scanner never sees a
    # committed key-shaped literal.
    fake_key = "sk-" + "abcdefghijklmnop1234"
    assert "an API key" in leak_report(f"the key is {fake_key}")
    assert leak_report("a perfectly ordinary sentence") == []


def test_a_near_duplicate_is_flagged_and_credits_the_original(tmp_path):
    conn, desk = _desk(tmp_path)
    original = _publish(desk)
    retold = _publish(
        desk,
        author="bob",
        title="Harbor market reopened today",
        body=(
            "After two years of repairs the harbor market reopened this "
            "morning with forty stalls and a queue along the pier."
        ),
    )
    # Flagged, never refused — retelling is human; uncredited retelling
    # is the defect. The original is credited by id, with the score.
    assert retold.similar_to == original.contribution_id
    assert retold.similarity is not None and retold.similarity >= 0.6
    # A genuinely different piece carries no flag.
    fresh = _publish(
        desk,
        author="carol",
        title="Rain finally reached the valley",
        body="Three months dry, and this morning the terraces flooded with runoff.",
    )
    assert fresh.similar_to is None and fresh.similarity is None
    conn.close()


# --------------------------------------------------------------------------- #
# Supersession: a WHERE clause, never an eraser.                               #
# --------------------------------------------------------------------------- #
def test_unpublish_excludes_forward_and_erases_nothing(tmp_path):
    conn, desk = _desk(tmp_path)
    record = _publish(desk)
    resting = desk.unpublish(
        record.contribution_id, tenant="t1", author="alice"
    )
    assert resting.superseded_at is not None
    # Default reads exclude it...
    assert desk.store.list(tenant="t1") == []
    assert desk.store.get(record.contribution_id, tenant="t1") is None
    # ...but history stands, word for word.
    kept = desk.store.get(
        record.contribution_id, tenant="t1", include_superseded=True
    )
    assert kept is not None and kept.body == record.body
    # A second unpublish is a no-op that returns the first truth.
    again = desk.unpublish(record.contribution_id, tenant="t1", author="alice")
    assert again.superseded_at == resting.superseded_at
    conn.close()


def test_only_the_author_may_unpublish(tmp_path):
    conn, desk = _desk(tmp_path)
    record = _publish(desk)
    with pytest.raises(PressError, match="only the author") as refusal:
        desk.unpublish(record.contribution_id, tenant="t1", author="mallory")
    assert refusal.value.status == 403
    with pytest.raises(PressError, match="no such") as missing:
        desk.unpublish("nope", tenant="t1", author="alice")
    assert missing.value.status == 404
    conn.close()


def test_contributions_are_tenant_walled(tmp_path):
    conn, desk = _desk(tmp_path)
    record = _publish(desk)
    assert desk.store.get(record.contribution_id, tenant="t2") is None
    assert desk.store.list(tenant="t2") == []
    conn.close()


def test_the_genre_filter_joins_on_the_taxonomy(tmp_path):
    conn, desk = _desk(tmp_path)
    _publish(desk)
    _publish(
        desk,
        title="A bench test of three kettles",
        body="Watts drawn, minutes to boil, and which handle survived a drop.",
        genres=("products", "results"),
    )
    assert [r.title for r in desk.store.list(tenant="t1", genre="results")] == [
        "A bench test of three kettles"
    ]
    assert len(desk.store.list(tenant="t1")) == 2
    conn.close()


# --------------------------------------------------------------------------- #
# The closed loop, structurally: the import scan.                              #
# --------------------------------------------------------------------------- #
def test_the_press_reaches_no_outside_content_seam():
    """Plan invariant 1 as code: nothing in press/ imports a web-search,
    HTTP, or provider seam — contributions are made IN the app, full
    stop. (The marketplace spine's no-money-path scan, applied here.)"""
    package = Path(__file__).resolve().parents[1] / "src" / "oolu" / "press"
    forbidden = (
        "http",
        "urllib",
        "requests",
        "socket",
        "providers",
        "websearch",
        "web_search",
    )
    for source in sorted(package.glob("*.py")):
        for line_number, line in enumerate(
            source.read_text().splitlines(), start=1
        ):
            stripped = line.strip()
            if not (stripped.startswith("from ") or stripped.startswith("import ")):
                continue
            for word in forbidden:
                assert word not in stripped, (
                    f"{source.name}:{line_number} reaches outside the loop: "
                    f"{stripped}"
                )


# --------------------------------------------------------------------------- #
# The gateway: the doors end to end, with the audit chain verified.            #
# --------------------------------------------------------------------------- #
def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    for name in ("alice", "bob"):
        accounts.create_user(name, f"{name}-password-1", tenant="t1")
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        press=PressDesk(ContributionStore(conn)),
    )
    return gateway, conn, ident


def test_the_press_doors_end_to_end(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice, bob = ident.token("alice"), ident.token("bob")

    genres = gateway.handle(_req("GET", "/v1/press/genres", token=alice))
    assert genres.status == 200
    assert genres.body["taxonomy_version"] == TAXONOMY_VERSION
    assert any(i["key"] == "local" for i in genres.body["items"])
    # The stated license rides along, terms and all — consent renders it.
    [license_view] = genres.body["licenses"]
    assert license_view["key"] == LICENSE and license_view["terms"]

    published = gateway.handle(
        _req(
            "POST",
            "/v1/press/contributions",
            token=alice,
            body={
                "title": "The harbor market reopened",
                "body": "Forty stalls and a queue down the pier this morning.",
                "genres": ["local"],
                "license": LICENSE,
                "consent": True,
            },
        )
    )
    assert published.status == 201, published.body
    cid = published.body["contribution_id"]
    assert published.body["author"] == "alice"  # the byline's principal

    # Bob — same tenant — reads the shelf and the detail.
    shelf = gateway.handle(_req("GET", "/v1/press/contributions", token=bob))
    assert [i["contribution_id"] for i in shelf.body["items"]] == [cid]
    detail = gateway.handle(
        _req("GET", f"/v1/press/contributions/{cid}", token=bob)
    )
    assert detail.status == 200 and detail.body["author"] == "alice"

    # A refusal at the door carries the desk's direction.
    refused = gateway.handle(
        _req(
            "POST",
            "/v1/press/contributions",
            token=alice,
            body={
                "title": "x",
                "body": "reach me at alice@example.com",
                "genres": ["local"],
                "license": LICENSE,
                "consent": True,
            },
        )
    )
    assert refused.status == 400
    assert "an email address" in refused.body["error"]["message"]

    # Only the author may unpublish; the author's unpublish supersedes.
    blocked = gateway.handle(
        _req(
            "POST", f"/v1/press/contributions/{cid}/unpublish", token=bob
        )
    )
    assert blocked.status == 403
    resting = gateway.handle(
        _req(
            "POST", f"/v1/press/contributions/{cid}/unpublish", token=alice
        )
    )
    assert resting.status == 200 and resting.body["superseded_at"]

    # The shelf forgets forward; the author still sees their own record;
    # a peer's detail read now finds nothing.
    assert (
        gateway.handle(
            _req("GET", "/v1/press/contributions", token=bob)
        ).body["items"]
        == []
    )
    mine = gateway.handle(
        _req("GET", "/v1/press/contributions", token=alice, query={"mine": "1"})
    )
    assert [i["contribution_id"] for i in mine.body["items"]] == [cid]
    assert (
        gateway.handle(
            _req("GET", f"/v1/press/contributions/{cid}", token=bob)
        ).status
        == 404
    )

    # Both acts stand on the tamper-evident chain, and the chain verifies.
    audit = gateway._durable.audit
    events = [
        e.event_type
        for e in audit.records()
        if e.event_type.startswith("press.")
    ]
    assert events == [
        "press.contribution_published",
        "press.contribution_unpublished",
    ]
    assert audit.verify()
    conn.close()


def test_publishing_a_missing_or_foreign_file_ref_is_refused(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    alice = ident.token("alice")
    refused = gateway.handle(
        _req(
            "POST",
            "/v1/press/contributions",
            token=alice,
            body={
                "title": "With a picture",
                "body": "The picture tells it better than I can.",
                "genres": ["local"],
                "license": LICENSE,
                "consent": True,
                "file_ids": ["not-a-real-file"],
            },
        )
    )
    assert refused.status == 404
    assert "no such file" in refused.body["error"]["message"]
    conn.close()
