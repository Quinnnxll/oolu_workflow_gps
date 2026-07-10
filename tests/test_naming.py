"""Concise, keyword-oriented naming for auto-created nodes and skills.

A name is a label, not a transcript: when the system names something
itself, the name is the task's keywords; the full sentence stays in the
description. Explicit names are always honored verbatim.
"""

from __future__ import annotations

from oolu.naming import concise_name, keyword_slug, keywords


def test_keywords_keep_order_drop_stopwords_and_duplicates():
    assert keywords(
        "convert the quarterly report to pdf and email it to john"
    ) == ["convert", "quarterly", "report", "pdf"]
    assert keywords("pdf report report pdf convert") == [
        "pdf",
        "report",
        "convert",
    ]


def test_concise_name_is_keywords_never_the_sentence():
    assert (
        concise_name("convert the quarterly report to pdf and email it")
        == "Convert Quarterly Report Pdf"
    )
    assert keyword_slug("convert the quarterly report to pdf") == (
        "convert.quarterly.report.pdf"
    )


def test_all_stopwords_falls_back_to_the_trimmed_text():
    assert concise_name("do it") == "do it"
    assert concise_name("") == ""
    assert keyword_slug("do it for me") == ""


def test_learned_skill_without_a_name_gets_a_keyword_identity(tmp_path):
    from oolu.naming import concise_name as cname
    from oolu.skills.learner import SkillLearner, _slug

    # The cli names an unnamed recording with the intent's keywords; the
    # learner then derives the id from that concise name.
    intent = "book me a flight from lilongwe to nairobi next tuesday please"
    name = cname(intent)
    assert name == "Book Flight Lilongwe Nairobi"
    assert f"learned.{_slug(name)}" == "learned.book.flight.lilongwe.nairobi"


def test_desk_title_condenses_a_learned_skill_id(tmp_path):
    from oolu.durable import DurableConnection
    from oolu.nodeplace import NodeAccountStore, RegistryStore, WorkDesk
    from oolu.nodeplace.models import Node

    conn = DurableConnection(tmp_path / "d.db")
    try:
        registry = RegistryStore(conn)
        registry.add_node(
            Node(
                node_id="n1",
                tenant_id="t1",
                noder_principal="alice",
                skill_id="learned.convert.quarterly.report.pdf",
            )
        )
        desk = WorkDesk(registry=registry, accounts=NodeAccountStore(conn))
        (entry,) = desk.overview(principal="alice", tenant="t1")
        # No listing yet: the title is the condensed keyword name, never
        # the raw dotted id.
        assert entry.title == "Convert Quarterly Report Pdf"
    finally:
        conn.close()
