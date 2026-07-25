"""B0: the interrogation budget — no mechanism reaches a human as a question."""

from __future__ import annotations

from test_growth_trigger import GOAL, _chat, _rig
from test_node_interact import FakeAuthor

from oolu import plainlanguage as pl
from oolu.orchestrator.intake import ModelBackedIntaker
from oolu.orchestrator.state import TaskContract
from oolu.skills.requirements import (
    BriefStatus,
    ParameterSource,
    RequirementConstraintCompiler,
)


# ------------------------------------------------------------------ #
# The lexicon: mechanism asks trip; value-asks pass.                   #
# ------------------------------------------------------------------ #
def test_mechanism_asks_trip_and_value_asks_pass():
    for ask in (
        "What file format should the report be?",
        "Which API endpoint should I call?",
        "What JSON schema does the payload use?",
        "Which authentication token should the request carry?",
        "CSV or XML?",
        "What delimiter does the export use?",
    ):
        assert pl.mechanism_terms(ask), ask
        assert pl.is_mechanism_ask(ask)
    for ask in (
        "Which folder are the invoices in?",
        "What date range should the report cover?",
        "What should the node be called?",
        "What's the web address of the page?",
        "Which account should it use?",
        # "auth" must never fire inside "author".
        "Which author wrote the summary?",
    ):
        assert pl.mechanism_terms(ask) == [], ask
        assert not pl.is_mechanism_ask(ask)


def test_only_question_sentences_are_budgeted():
    # A receipt STATES its mechanism decisions freely; only questions
    # spend the budget — and a plain question after a technical
    # statement is not charged for the statement's vocabulary.
    transcript = (
        "I'll read the folder as CSV files and post the result to the "
        "webhook you granted. Which folder are the invoices in? "
        "What format should the output be?"
    )
    offenders = pl.mechanism_questions(transcript)
    assert offenders == ["What format should the output be?"]


# ------------------------------------------------------------------ #
# The wall RESOLVES: decided, or demoted — never hidden-and-blocking.  #
# ------------------------------------------------------------------ #
class _Intake:
    def __init__(self, reply):
        self._reply = reply

    def propose(self, intent):
        return self._reply


def _briefing(parameters):
    import json

    return json.dumps({"parameters": parameters, "constraints": []})


def test_intake_decides_mechanisms_and_keeps_value_asks(tmp_path):
    intaker = ModelBackedIntaker(
        _Intake(
            _briefing(
                [
                    {
                        "name": "output_format",
                        "description": "the file format of the report",
                        "question": "What file format should the report be?",
                        "suggested_values": ["xlsx"],
                    },
                    {
                        "name": "source_folder",
                        "description": "where the invoices live",
                        "question": "Which folder are the invoices in?",
                    },
                ]
            )
        )
    )
    brief = intaker.intake(TaskContract(intent="tidy my invoices"))
    by_name = {p.name: p for p in brief.parameters}
    # The mechanism was DECIDED — first suggestion, DERIVED provenance,
    # question retired — while the value-ask stands untouched.
    assert by_name["output_format"].value == "xlsx"
    assert by_name["output_format"].source is ParameterSource.DERIVED
    assert by_name["output_format"].question is None
    assert by_name["source_folder"].value is None
    assert by_name["source_folder"].question is not None
    result = RequirementConstraintCompiler().compile(brief)
    assert [q.parameter for q in result.questions] == ["source_folder"]


def test_an_unanswerable_mechanism_never_blocks_the_run(tmp_path):
    # No suggestion, no options: the parameter demotes to optional and
    # the brief advances instead of pausing on a question nobody will
    # ever be asked.
    intaker = ModelBackedIntaker(
        _Intake(
            _briefing(
                [
                    {
                        "name": "api_endpoint",
                        "description": "the API endpoint to call",
                        "question": "Which API endpoint should I call?",
                    }
                ]
            )
        )
    )
    brief = intaker.intake(TaskContract(intent="fetch the weather"))
    (param,) = brief.parameters
    assert param.required is False and param.question is None
    result = RequirementConstraintCompiler().compile(brief)
    assert result.status is not BriefStatus.CLARIFYING
    assert result.questions == []


def test_options_serve_when_the_model_suggested_nothing():
    intaker = ModelBackedIntaker(
        _Intake(
            _briefing(
                [
                    {
                        "name": "export_format",
                        "description": "how the export is encoded",
                        "question": "CSV or XML?",
                        "options": ["csv", "xml"],
                    }
                ]
            )
        )
    )
    brief = intaker.intake(TaskContract(intent="export my ledger"))
    (param,) = brief.parameters
    assert param.value == "csv" and param.source is ParameterSource.DERIVED


# ------------------------------------------------------------------ #
# Acceptance: the build transcript spends zero mechanism questions,    #
# and the receipt names its decisions as revisable.                    #
# ------------------------------------------------------------------ #
def test_a_standard_build_asks_no_mechanism_questions(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        app._node_function_author = lambda tenant: FakeAuthor()
        response = _chat(app, ident, "build me a node that " + GOAL)
        assert response.status == 200, response.body
        reply = response.body["reply"]
        assert pl.mechanism_questions(reply) == []
        # The receipt: decisions named, revisable in plain words.
        assert "decided for you" in reply
        assert "revise" in reply
    finally:
        conn.close()


def test_the_prompt_law_is_stated_on_every_conversational_surface():
    from oolu.chat import SYSTEM_PROMPT
    from oolu.orchestrator.intake import INTAKE_SYSTEM_PROMPT

    assert "Never phrase a question about MECHANISMS" in INTAKE_SYSTEM_PROMPT
    assert "Never ask the user TECHNICAL questions" in SYSTEM_PROMPT


# ------------------------------------------------------------------ #
# B1: plain-word labels at birth; forms as strict checks.              #
# ------------------------------------------------------------------ #
def test_io_labels_ride_default_or_refuse():
    from oolu.chat import parse_node_io_checked

    # A declared plain label and example ride through untouched.
    io, problem = parse_node_io_checked(
        'IO: {"inputs": [{"name": "source_folder", "type": "path", '
        '"label": "Which folder are the invoices in?", '
        '"example": "~/Invoices"}], '
        '"outputs": [{"name": "result", "type": "str"}]}'
    )
    assert problem == ""
    (source,) = io["inputs"]
    assert source["label"] == "Which folder are the invoices in?"
    assert source["example"] == "~/Invoices"

    # A missing label is DECIDED from the humanized name, shaped by type.
    io, problem = parse_node_io_checked(
        'IO: {"inputs": [{"name": "day_count", "type": "number"}], '
        '"outputs": [{"name": "result", "type": "str"}]}'
    )
    assert problem == ""
    assert io["inputs"][0]["label"] == "What number should “day count” be?"

    # A mechanism-flavored label refuses, naming the words that tripped.
    _io, problem = parse_node_io_checked(
        'IO: {"inputs": [{"name": "target", "type": "str", '
        '"label": "What JSON schema should the payload use?"}], '
        '"outputs": [{"name": "result", "type": "str"}]}'
    )
    assert "technical question" in problem
    assert "json" in problem


def test_plain_label_shapes_by_type():
    assert pl.plain_label("source_folder", "path") == (
        "Which file or folder is “source folder”?"
    )
    assert pl.plain_label("day_count", "number") == (
        "What number should “day count” be?"
    )
    assert pl.plain_label("title", "str") == "What should “title” be?"


def test_the_strict_check_refuses_in_words():
    from oolu.skills.contract import ActionsBody, NodeContract, ValueInput
    from oolu.skills.inputs import inputs_manifest, validate_user_inputs
    from oolu.skills.models import ActionEvent

    contract = NodeContract(
        id="n1",
        name="report maker",
        inputs=[
            ValueInput(
                name="day_count", value_type="number",
                label="How many days should the report cover?",
            ),
            ValueInput(
                name="tone", value_type="choice",
                choices=["formal", "friendly"],
            ),
        ],
        body=ActionsBody(
            actions=[ActionEvent(correlation_id="c", adapter="cli", operation="run")]
        ),
    )
    manifest = inputs_manifest(contract)
    # Valid values pass and coerce.
    assert validate_user_inputs(manifest, {"day_count": "30"}) == {
        "day_count": 30.0
    }
    # A type-invalid value refuses IN WORDS, using the plain label.
    import pytest as _pytest

    with _pytest.raises(ValueError, match="How many days .* needs a number"):
        validate_user_inputs(manifest, {"day_count": "a fortnight"})
    # A choice outside its set names the set.
    with _pytest.raises(ValueError, match="formal, friendly"):
        validate_user_inputs(manifest, {"tone": "sarcastic"})
    # An undeclared key can smuggle nothing in.
    with _pytest.raises(ValueError, match="no input called 'volume'"):
        validate_user_inputs(manifest, {"volume": "11"})


def test_a_built_node_carries_plain_labels_to_the_library(tmp_path):
    app, conn, ident, desk, script_exec = _rig(tmp_path)
    try:
        labeled = FakeAuthor(
            "1. Read the folder.\n"
            'IO: {"inputs": [{"name": "source_folder", "type": "path", '
            '"label": "Which folder are the invoices in?", '
            '"example": "~/Invoices"}, {"name": "report_name", '
            '"type": "str"}], '
            '"outputs": [{"name": "result", "type": "str"}]}\n'
            "```python\nfrom _oolu_runtime import emit_result\n"
            "emit_result(''.join(['o', 'k']))\n```"
        )
        app._node_function_author = lambda tenant: labeled
        response = _chat(app, ident, "build me a node that " + GOAL)
        assert response.status == 200, response.body
        assert "Built a NEW node" in response.body["reply"]
        (node,) = app._nodeplace.all_nodes()
        version = app._nodeplace.latest_version(node.node_id)
        listing = desk._registry.listing_for_version(version.version_id)
        by_name = {s.name: s for s in listing.consumes}
        # The declared label rides; the missing one was decided plain.
        assert by_name["source_folder"].label == (
            "Which folder are the invoices in?"
        )
        assert by_name["source_folder"].example == "~/Invoices"
        assert by_name["report_name"].label == "What should “report name” be?"
        # Acceptance: no label in the build matches the mechanism lexicon.
        for slot in listing.consumes:
            assert pl.mechanism_terms(slot.label) == [], slot.label
    finally:
        conn.close()
